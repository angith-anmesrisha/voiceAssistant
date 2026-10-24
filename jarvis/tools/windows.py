"""Finding, raising and closing app windows (Windows only; matching is testable anywhere).

Closing is polite: the app gets WM_CLOSE, exactly like clicking its X, so "save your
changes?" prompts still appear. JARVIS's own windows and the desktop shell are never
closed.
"""

from __future__ import annotations

import ctypes
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import PurePath
from typing import Any, Protocol

_NEVER_CLOSE = frozenset({"explorer.exe", "dwm.exe", "csrss.exe", "winlogon.exe", "lockapp.exe"})
_WM_CLOSE = 0x0010


@dataclass(frozen=True, slots=True)
class Window:
    handle: int
    title: str
    process: str  # "Spotify.exe"
    pid: int


def _norm(text: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", text.lower()))


def matches(name: str, window: Window) -> bool:
    """Whether the user's name for an app fits a window: its program name, or its title."""
    wanted = _norm(name)
    if not wanted:
        return False
    program = _norm(PurePath(window.process).stem)
    title = _norm(window.title)
    squashed = wanted.replace(" ", "")
    return (
        wanted == program
        or squashed == program.replace(" ", "")
        or (len(squashed) >= 3 and program.replace(" ", "").startswith(squashed))
        or f" {wanted} " in f" {title} "
        or title.endswith(wanted)
    )


def closable(window: Window, own_pid: int) -> bool:
    return window.pid != own_pid and window.process.lower() not in _NEVER_CLOSE


class WindowControl(Protocol):
    def windows(self) -> list[Window]: ...
    def raise_window(self, window: Window) -> bool: ...
    def close(self, window: Window) -> None: ...
    def exists(self, window: Window) -> bool: ...


class WindowsWindows:
    """Real implementation with user32 (no shell, no taskkill)."""

    def windows(self) -> list[Window]:
        if sys.platform != "win32":
            return []
        import psutil

        user32: Any = ctypes.windll.user32  # type: ignore[attr-defined,unused-ignore]
        found: list[Window] = []
        names: dict[int, str] = {}

        @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)  # type: ignore[attr-defined,untyped-decorator,unused-ignore]
        def callback(hwnd: Any, _lparam: Any) -> bool:
            if not user32.IsWindowVisible(hwnd) or user32.GetWindow(hwnd, 4):  # GW_OWNER
                return True
            length = user32.GetWindowTextLengthW(hwnd)
            if not length:
                return True
            buffer = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buffer, length + 1)
            pid = ctypes.c_ulong()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value not in names:
                try:
                    names[pid.value] = str(psutil.Process(pid.value).name())
                except Exception:
                    names[pid.value] = ""
            found.append(Window(int(hwnd), buffer.value, names[pid.value], int(pid.value)))
            return True

        user32.EnumWindows(callback, 0)
        return found

    def raise_window(self, window: Window) -> bool:
        if sys.platform != "win32":
            return False
        user32: Any = ctypes.windll.user32  # type: ignore[attr-defined,unused-ignore]
        if user32.IsIconic(window.handle):
            user32.ShowWindow(window.handle, 9)  # SW_RESTORE
        return bool(user32.SetForegroundWindow(window.handle))

    def close(self, window: Window) -> None:
        if sys.platform == "win32":
            user32: Any = ctypes.windll.user32  # type: ignore[attr-defined,unused-ignore]
            user32.PostMessageW(window.handle, _WM_CLOSE, 0, 0)

    def exists(self, window: Window) -> bool:
        if sys.platform != "win32":
            return False
        user32: Any = ctypes.windll.user32  # type: ignore[attr-defined,unused-ignore]
        return bool(user32.IsWindow(window.handle)) and bool(user32.IsWindowVisible(window.handle))


def find(control: WindowControl, name: str) -> list[Window]:
    return [w for w in control.windows() if matches(name, w)]


def close_app(control: WindowControl, name: str, wait_s: float = 3.0) -> tuple[int, int]:
    """Ask every matching window to close. Returns (asked, still open after ``wait_s``)."""
    own = os.getpid()
    targets = [w for w in find(control, name) if closable(w, own)]
    for window in targets:
        control.close(window)
    deadline = time.monotonic() + wait_s
    remaining = targets
    while remaining and time.monotonic() < deadline:
        time.sleep(0.2)
        remaining = [w for w in remaining if control.exists(w)]
    return len(targets), len(remaining)
