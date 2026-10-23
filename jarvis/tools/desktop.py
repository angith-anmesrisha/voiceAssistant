"""The only code that touches the Windows desktop.

Everything is narrowly scoped: fixed programs from an allowlist, http(s) URLs, named
key combinations, typing into the window the user was last using, and Windows' own
lock/sleep/shutdown commands. There is no shell and nothing here takes a command line
from the model.
"""

from __future__ import annotations

import contextlib
import ctypes
import os
import shutil
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path
from typing import Any, Protocol

from loguru import logger

from jarvis.tools.apps import InstalledApp, start_menu_folders


class DesktopUnsupportedError(RuntimeError):
    pass


class Desktop(Protocol):
    def launch(self, target: str) -> None: ...
    def open_installed(self, app: InstalledApp) -> None: ...
    def open_path(self, path: Path) -> None: ...
    def get_clipboard_text(self) -> str: ...
    def set_clipboard_text(self, text: str) -> None: ...
    def active_window_title(self) -> str: ...
    def open_url(self, url: str) -> None: ...
    def press(self, keys: list[str]) -> None: ...
    def type_text(self, text: str) -> None: ...
    def focus_previous_window(self) -> bool: ...
    def window_titles(self) -> list[str]: ...
    def lock(self) -> None: ...
    def sleep(self) -> None: ...
    def schedule_power(self, action: str, delay_s: int) -> None: ...
    def cancel_power(self) -> None: ...


_NO_WINDOW = 0x08000000  # CREATE_NO_WINDOW: never flash a console


def _system32(exe: str) -> str:
    root = os.environ.get("SYSTEMROOT", r"C:\Windows")  # env names are case-insensitive on Windows
    return os.path.join(root, "System32", exe)


class WindowsDesktop:
    """Real implementation. Methods raise DesktopUnsupportedError off Windows."""

    def __init__(self) -> None:
        self._previous_hwnd: int | None = None
        self._stop = threading.Event()
        self._tracker: threading.Thread | None = None

    @staticmethod
    def _require_windows() -> None:
        if sys.platform != "win32":
            raise DesktopUnsupportedError("desktop actions only work on Windows")

    # --- remember which window the user was in, so typing goes there, not to JARVIS ---

    def start_tracking(self) -> None:
        if sys.platform != "win32" or self._tracker is not None:
            return
        self._tracker = threading.Thread(target=self._track, name="foreground", daemon=True)
        self._tracker.start()

    def stop_tracking(self) -> None:
        self._stop.set()

    def _track(self) -> None:
        user32 = ctypes.windll.user32  # type: ignore[attr-defined]
        own_pid = os.getpid()
        pid = ctypes.c_ulong()
        while not self._stop.wait(0.5):
            hwnd = user32.GetForegroundWindow()
            if not hwnd:
                continue
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value != own_pid:
                self._previous_hwnd = int(hwnd)

    def focus_previous_window(self) -> bool:
        self._require_windows()
        if not self._previous_hwnd:
            return False
        user32 = ctypes.windll.user32  # type: ignore[attr-defined]
        if not user32.IsWindow(self._previous_hwnd):
            return False
        user32.ShowWindow(self._previous_hwnd, 9)  # SW_RESTORE
        ok = bool(user32.SetForegroundWindow(self._previous_hwnd))
        time.sleep(0.15)
        return ok

    def active_window_title(self) -> str:
        """Title of the window the user was in before talking to JARVIS."""
        if sys.platform != "win32" or not self._previous_hwnd:
            return ""
        user32 = ctypes.windll.user32  # type: ignore[attr-defined,unused-ignore]
        length = user32.GetWindowTextLengthW(self._previous_hwnd)
        buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(self._previous_hwnd, buffer, length + 1)
        return str(buffer.value)

    def window_titles(self) -> list[str]:
        self._require_windows()
        user32 = ctypes.windll.user32  # type: ignore[attr-defined]
        titles: list[str] = []

        @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)  # type: ignore[attr-defined, untyped-decorator]
        def callback(hwnd: Any, _lparam: Any) -> bool:
            if user32.IsWindowVisible(hwnd):
                length = user32.GetWindowTextLengthW(hwnd)
                if length:
                    buffer = ctypes.create_unicode_buffer(length + 1)
                    user32.GetWindowTextW(hwnd, buffer, length + 1)
                    titles.append(buffer.value)
            return True

        user32.EnumWindows(callback, 0)
        return titles

    # --- launching ---

    def launch(self, target: str) -> None:
        self._require_windows()
        if target.endswith(":") or "://" in target:
            os.startfile(target)  # type: ignore[attr-defined]  # registered URI (ms-settings:)
            return
        path = shutil.which(target)
        if path:
            subprocess.Popen([path], shell=False, close_fds=True)
        else:
            os.startfile(target)  # type: ignore[attr-defined]  # App Paths lookup

    def open_installed(self, app: InstalledApp) -> None:
        """Open a Start menu entry the way the Start menu does (no command line)."""
        self._require_windows()
        if app.shortcut is not None:
            path = Path(app.shortcut).resolve()
            roots = [root.resolve() for root in start_menu_folders()]
            if not any(path.is_relative_to(root) for root in roots):
                raise PermissionError("not a Start menu shortcut")
            os.startfile(str(path))  # type: ignore[attr-defined]
        elif app.app_id is not None:
            os.startfile("shell:AppsFolder\\" + app.app_id)  # type: ignore[attr-defined]
        else:
            raise ValueError("nothing to open")

    def open_path(self, path: Path) -> None:
        """A folder in Explorer, or a document in its default app (checked by FileArea)."""
        self._require_windows()
        os.startfile(str(path))  # type: ignore[attr-defined]

    # --- clipboard ---

    def get_clipboard_text(self) -> str:
        self._require_windows()
        import win32clipboard

        win32clipboard.OpenClipboard()
        try:
            if not win32clipboard.IsClipboardFormatAvailable(win32clipboard.CF_UNICODETEXT):
                return ""
            return str(win32clipboard.GetClipboardData(win32clipboard.CF_UNICODETEXT))
        finally:
            win32clipboard.CloseClipboard()

    def set_clipboard_text(self, text: str) -> None:
        self._require_windows()
        import win32clipboard

        win32clipboard.OpenClipboard()
        try:
            win32clipboard.EmptyClipboard()
            win32clipboard.SetClipboardText(text, win32clipboard.CF_UNICODETEXT)
        finally:
            win32clipboard.CloseClipboard()

    def open_url(self, url: str) -> None:
        if not webbrowser.open(url, new=2):
            raise RuntimeError("no web browser available")

    # --- keyboard ---

    def press(self, keys: list[str]) -> None:
        from pynput.keyboard import Controller, Key

        controller = Controller()
        resolved: list[Any] = [getattr(Key, k) if len(k) > 1 else k for k in keys]
        for key in resolved:
            controller.press(key)
        for key in reversed(resolved):
            controller.release(key)

    def type_text(self, text: str) -> None:
        from pynput.keyboard import Controller

        Controller().type(text)

    # --- power ---

    def lock(self) -> None:
        self._require_windows()
        if not ctypes.windll.user32.LockWorkStation():  # type: ignore[attr-defined]
            raise RuntimeError("Windows refused to lock")

    def sleep(self) -> None:
        self._require_windows()
        subprocess.run(
            [_system32("rundll32.exe"), "powrprof.dll,SetSuspendState", "0,1,0"],
            check=True,
            creationflags=_NO_WINDOW,
        )

    def schedule_power(self, action: str, delay_s: int) -> None:
        self._require_windows()
        flag = {"shutdown": "/s", "restart": "/r"}[action]
        subprocess.run(
            [
                _system32("shutdown.exe"),
                flag,
                "/t",
                str(delay_s),
                "/c",
                f"JARVIS: {action} in {delay_s} seconds. Say 'cancel shutdown' to stop it.",
            ],
            check=True,
            creationflags=_NO_WINDOW,
        )
        logger.info("Scheduled {} in {}s", action, delay_s)

    def cancel_power(self) -> None:
        self._require_windows()
        with contextlib.suppress(subprocess.CalledProcessError):
            subprocess.run([_system32("shutdown.exe"), "/a"], check=True, creationflags=_NO_WINDOW)
