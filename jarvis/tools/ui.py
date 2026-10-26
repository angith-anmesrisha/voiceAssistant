"""Seeing and operating app windows through Windows UI Automation.

``read_ui`` lists a window's controls (buttons, fields, list items, text) with numbers,
the model picks one, and ``click_element`` / ``type_into`` act on it and return the
window as it looks afterwards, so the model can check each step (look, act, check).

Limits: shells, terminals, system editors, Windows Settings, Control Panel, Task Manager
and JARVIS itself are never read or driven; password fields are never read, and only
``fill_login`` (a workflow's "Fill in a login" node, with a login saved in Credential
Manager) types into one; programs are never started by double-clicking them in
Explorer. Clicks that send, pay, delete and the like, and pressing Enter after typing,
ask first (see ``click_risk``). Everything read here is outside text: the assistant
screens it with the prompt-injection guard and the prompt treats it as information,
never instructions.
"""

from __future__ import annotations

import asyncio
import os
import re
import sys
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import PurePath
from typing import Any, Protocol, TypeVar

from loguru import logger

from jarvis.tools.files import RUNNABLE
from jarvis.tools.registry import Risk

T = TypeVar("T")

# Never read or driven: with clicks and typing they would be a back door to running
# commands or changing Windows settings.
BLOCKED_PROCESSES = frozenset(
    {
        "cmd.exe",
        "powershell.exe",
        "powershell_ise.exe",
        "pwsh.exe",
        "windowsterminal.exe",
        "openconsole.exe",
        "conhost.exe",
        "wsl.exe",
        "wslhost.exe",
        "bash.exe",
        "mintty.exe",
        "regedit.exe",
        "mmc.exe",
        "systemsettings.exe",
        "control.exe",
        "taskmgr.exe",
        "consent.exe",
        "credentialuibroker.exe",
        "lockapp.exe",
        "msconfig.exe",
        "gpedit.exe",
        "services.exe",
        "eventvwr.exe",
        "perfmon.exe",
        "resmon.exe",
        "devenv.exe",
    }
)
_BLOCKED_TITLES = re.compile(
    r"^(run|user account control)$|\b(command prompt|powershell|terminal|registry editor|"
    r"control panel|group policy|services|task scheduler|device manager|disk management|"
    r"windows security)\b",
    re.IGNORECASE,
)

# Clicking these sends, pays, deletes or otherwise can't be taken back: ask first.
_COMMITS = re.compile(
    r"\b(send|post|publish|submit|reply|forward|share|tweet|pay|buy|purchase|order|"
    r"checkout|check out|place order|donate|transfer|delete|remove|erase|discard|trash|"
    r"uninstall|install|update now|restart|shut ?down|sign out|log ?out|format|reset|"
    r"empty|clear all|block|report|unsubscribe|unfriend|leave|accept|decline|confirm|"
    r"agree|allow|grant|approve|call|video call|voice call|dial|save|overwrite|replace)\b"
    r"(?!\.\w)",  # not file names such as "report.docx"
    re.IGNORECASE,
)

# Roles worth showing; the rest (panes, groups, ...) are walked through but not listed.
_ROLES = frozenset(
    {
        "Button",
        "SplitButton",
        "Hyperlink",
        "MenuItem",
        "MenuBar",
        "TabItem",
        "ListItem",
        "TreeItem",
        "DataItem",
        "CheckBox",
        "RadioButton",
        "ComboBox",
        "Edit",
        "Document",
        "Slider",
        "Spinner",
        "Text",
        "Header",
        "HeaderItem",
    }
)
_TYPABLE = frozenset({"Edit", "Document", "ComboBox"})

SNAPSHOT_CHARS = 3200
_MAX_NODES = 2500
_MAX_DEPTH = 40
_WALK_BUDGET_S = 4.0
_SETTLE_S = 0.7  # let the app redraw before reading it again


class UiError(RuntimeError):
    """An app window couldn't be read or operated (the message says why, for the user)."""


@dataclass(frozen=True, slots=True)
class UiElement:
    id: int
    role: str
    name: str
    value: str = ""
    enabled: bool = True
    focused: bool = False
    password: bool = False


@dataclass(frozen=True, slots=True)
class UiSnapshot:
    app: str  # process name, e.g. "WhatsApp.exe"
    title: str
    elements: tuple[UiElement, ...]
    partial: bool = False  # the window had more than could be read in time

    def element(self, element_id: int) -> UiElement | None:
        for element in self.elements:
            if element.id == element_id:
                return element
        return None


def app_label(app: str) -> str:
    """ "WhatsApp.exe" -> "WhatsApp"."""
    return PurePath(app).stem or app


def blocked_reason(process: str, title: str, *, own: bool = False) -> str | None:
    """Why JARVIS won't read or drive this window, or None if it may."""
    if own:
        return "that's JARVIS's own window"
    if process.lower() in BLOCKED_PROCESSES or _BLOCKED_TITLES.search(title.strip()):
        return (
            f"{app_label(process) or title} is a shell, system tool or settings window, which "
            "JARVIS doesn't operate"
        )
    return None


def runnable_name(name: str) -> bool:
    return PurePath(name.strip().lower()).suffix in RUNNABLE


def click_risk(element: UiElement | None, app: str, *, double: bool = False) -> Risk:
    """LOW for moving around an app; MEDIUM for clicks that send, pay, delete and so on."""
    if element is None:
        return Risk.LOW  # unknown id: the click fails without doing anything
    if _COMMITS.search(element.name):
        return Risk.MEDIUM
    if double and app.lower() == "explorer.exe":
        return Risk.MEDIUM  # double-click opens things
    return Risk.LOW


def _line(element: UiElement) -> str:
    text = f'[{element.id}] {element.role} "{element.name}"'
    if element.password:
        text += " (password field: never read or typed)"
    elif element.value and element.value != element.name:
        text += f' = "{element.value}"'
    if not element.enabled:
        text += " (disabled)"
    if element.focused:
        text += " (focused)"
    return text


def format_snapshot(snapshot: UiSnapshot, limit: int = SNAPSHOT_CHARS) -> str:
    """The window as numbered lines, cut to about ``limit`` characters.

    When everything doesn't fit, plain text is dropped first so the controls stay.
    """
    head = f'{app_label(snapshot.app)} window "{snapshot.title}"'
    lines = [_line(e) for e in snapshot.elements]
    if sum(len(x) + 1 for x in lines) > limit:
        lines = [_line(e) for e in snapshot.elements if e.role != "Text"]
    kept: list[str] = []
    size = 0
    for line in lines:
        if size + len(line) + 1 > limit:
            break
        kept.append(line)
        size += len(line) + 1
    more = len(kept) < len(snapshot.elements) or snapshot.partial
    if not kept:
        return head + ": no readable controls (try look_at_screen)."
    tail = "\n(more not shown: scroll, or read again after narrowing down)" if more else ""
    return head + ":\n" + "\n".join(kept) + tail


class UiDriver(Protocol):
    @property
    def app(self) -> str: ...
    def element(self, element_id: int) -> UiElement | None: ...
    async def snapshot(self, window: str = "") -> UiSnapshot: ...
    async def refresh(self) -> UiSnapshot: ...
    async def click(
        self, element_id: int, *, double: bool = False, right: bool = False
    ) -> None: ...
    async def type_into(
        self, element_id: int, text: str, *, clear: bool = False, submit: bool = False
    ) -> None: ...
    async def scroll(self, element_id: int | None, down: bool, amount: int) -> None: ...


# Text boxes that are never a login's username: the browser's address bar, search boxes.
_NOT_USERNAME = re.compile(r"\b(address|search|url|omnibox|find)\b", re.IGNORECASE)
_USERNAME = re.compile(
    r"\b(user\s?name|user|e-?mail|login|log in|sign in|account|phone|mobile|id|"
    r"roll|register\w*)\b",
    re.IGNORECASE,
)


def login_fields(boxes: list[tuple[str, bool]]) -> tuple[int | None, int | None]:
    """Which of a window's text boxes, given in reading order as (name, is_password),
    take a login's username and password: (username index, password index).

    The password box is the first password box. The username box is the box before it
    that is named like one (user, email, login...), else the nearest plain box before
    it; address and search bars never count. With no password box (a two-step sign-in),
    the username box is the one named like one, or the only plain box."""
    password = next((i for i, (_name, secret) in enumerate(boxes) if secret), None)
    plain = [
        i
        for i, (name, secret) in enumerate(boxes)
        if not secret and not _NOT_USERNAME.search(name) and (password is None or i < password)
    ]
    named = [i for i in plain if _USERNAME.search(boxes[i][0])]
    if password is not None:
        if named:
            return named[-1], password
        return (plain[-1] if plain else None), password
    if named:
        return named[0], None
    return (plain[0] if len(plain) == 1 else None), None


def _clip(text: str, limit: int = 100) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


class WindowsUi:
    """UI Automation (via the ``uiautomation`` package) on one dedicated COM thread.

    UIA objects may only be used on the thread that created them, so every call runs on
    the same single worker thread.
    """

    def __init__(
        self,
        previous_window: Callable[[], int | None],
        press: Callable[[list[str]], None],
        type_text: Callable[[str], None],
    ) -> None:
        self._previous_window = previous_window
        self._press = press
        self._type_text = type_text
        self._pool = ThreadPoolExecutor(1, "uia", initializer=self._init_thread)
        self._local = threading.local()
        self._controls: dict[int, Any] = {}
        self._snapshot: UiSnapshot | None = None
        self._hwnd = 0

    @property
    def app(self) -> str:
        return self._snapshot.app if self._snapshot is not None else ""

    def element(self, element_id: int) -> UiElement | None:
        return self._snapshot.element(element_id) if self._snapshot is not None else None

    # --- thread plumbing ---

    def _init_thread(self) -> None:
        if sys.platform != "win32":
            return
        # Imported here, after Qt has set the DPI mode (the module tries to set it too).
        import uiautomation as uia

        self._local.init = uia.UIAutomationInitializerInThread()

    async def _run(self, fn: Callable[[], T]) -> T:
        if sys.platform != "win32":
            raise UiError("reading app windows only works on Windows")
        return await asyncio.get_running_loop().run_in_executor(self._pool, fn)

    # --- reading ---

    async def snapshot(self, window: str = "") -> UiSnapshot:
        return await self._run(lambda: self._read(self._find(window)))

    async def refresh(self) -> UiSnapshot:
        await asyncio.sleep(_SETTLE_S)
        return await self._run(lambda: self._read(self._hwnd))

    def _find(self, window: str, *, title_only: bool = False) -> int:
        import uiautomation as uia

        if not window.strip():
            hwnd = self._previous_window() or 0
            if not hwnd:
                raise UiError("there's no app window to read; say which app")
            return hwnd
        wanted = window.strip().lower()
        deadline = time.monotonic() + 8.0  # an app that was just opened needs a moment
        while True:
            for top in uia.GetRootControl().GetChildren():
                try:
                    title = top.Name or ""
                    process = _process_name(top.ProcessId)
                    if not top.NativeWindowHandle or top.IsOffscreen:
                        continue
                except Exception:
                    continue
                if wanted in title.lower() or (
                    not title_only and wanted == app_label(process).lower()
                ):
                    reason = blocked_reason(process, title, own=top.ProcessId == os.getpid())
                    if reason:
                        raise UiError(reason)
                    hwnd = int(top.NativeWindowHandle)
                    self._bring_to_front(hwnd)
                    return hwnd
            if time.monotonic() > deadline:
                raise UiError(f"no open window matches “{window}”")
            time.sleep(0.5)

    def _read(self, hwnd: int) -> UiSnapshot:
        import uiautomation as uia

        root = uia.ControlFromHandle(hwnd) if hwnd else None
        if root is None:
            raise UiError("that window has closed")
        process = _process_name(root.ProcessId)
        title = root.Name or ""
        reason = blocked_reason(process, title, own=root.ProcessId == os.getpid())
        if reason:
            raise UiError(reason)
        frame = root.BoundingRectangle
        elements: list[UiElement] = []
        controls: dict[int, Any] = {}
        started = time.monotonic()
        partial = False
        for visited, (control, _depth) in enumerate(uia.WalkControl(root, maxDepth=_MAX_DEPTH)):
            if visited >= _MAX_NODES or time.monotonic() - started > _WALK_BUDGET_S:
                partial = True
                break
            try:
                role = control.ControlTypeName.removesuffix("Control")
                if role not in _ROLES:
                    continue
                name = _clip(control.Name)
                if role == "Text" and (not name or (elements and elements[-1].name == name)):
                    continue  # a label repeating its button's name
                rect = control.BoundingRectangle
                if control.IsOffscreen or rect.isempty():
                    continue
                if not (frame.left <= rect.xcenter() < frame.right) or not (
                    frame.top <= rect.ycenter() < frame.bottom
                ):
                    continue
                password = bool(control.IsPassword)
                value = ""
                if role in _TYPABLE and not password:
                    pattern = control.GetPattern(uia.PatternId.ValuePattern)
                    value = _clip(pattern.Value) if pattern is not None else ""
                if not name and not value and role not in _TYPABLE:
                    continue
                element = UiElement(
                    len(elements) + 1,
                    role,
                    name,
                    value,
                    enabled=bool(control.IsEnabled),
                    focused=bool(control.HasKeyboardFocus),
                    password=password,
                )
            except Exception:
                continue  # elements vanish while being read; skip them
            elements.append(element)
            controls[element.id] = control
        snapshot = UiSnapshot(process, _clip(title, 120), tuple(elements), partial)
        self._snapshot, self._controls, self._hwnd = snapshot, controls, hwnd
        logger.info("Read {} controls of {}", len(elements), process)  # no titles or text
        return snapshot

    # --- acting ---

    def _control(self, element_id: int) -> tuple[Any, UiElement]:
        control = self._controls.get(element_id)
        element = self.element(element_id)
        if control is None or element is None:
            raise UiError(f"there is no element {element_id}; read the window again")
        return control, element

    def _bring_to_front(self, hwnd: int) -> None:
        import uiautomation as uia

        if hwnd and not uia.SetForegroundWindow(hwnd):
            uia.SwitchToThisWindow(hwnd)
        time.sleep(0.15)

    async def click(self, element_id: int, *, double: bool = False, right: bool = False) -> None:
        def go() -> None:
            import uiautomation as uia

            control, element = self._control(element_id)
            if double and self.app.lower() == "explorer.exe" and runnable_name(element.name):
                raise UiError("JARVIS doesn't start programs or scripts")
            self._bring_to_front(self._hwnd)
            if not double and not right and _press_by_pattern(control, element.role):
                return  # buttons, menus, links, checkboxes and tabs: no mouse needed
            if control.BoundingRectangle.isempty():
                invoke = control.GetPattern(uia.PatternId.InvokePattern)
                if invoke is None:
                    raise UiError(f"“{element.name}” can't be clicked right now")
                invoke.Invoke()
            elif double:
                control.DoubleClick(simulateMove=False)
            elif right:
                control.RightClick(simulateMove=False)
            else:
                control.Click(simulateMove=False)

        await self._run(go)

    async def type_into(
        self, element_id: int, text: str, *, clear: bool = False, submit: bool = False
    ) -> None:
        def go() -> None:
            control, element = self._control(element_id)
            if element.password or control.IsPassword:
                raise UiError("JARVIS doesn't type into password fields")
            self._bring_to_front(self._hwnd)
            control.SetFocus()
            if not control.HasKeyboardFocus:
                control.Click(simulateMove=False)
            if clear:
                self._press(["ctrl", "a"])
                self._press(["backspace"])
            if text:
                self._type_text(text)
            if submit:
                time.sleep(0.1)
                self._press(["enter"])

        await self._run(go)

    async def scroll(self, element_id: int | None, down: bool, amount: int) -> None:
        def go() -> None:
            import uiautomation as uia

            self._bring_to_front(self._hwnd)
            if element_id is not None:
                control = self._control(element_id)[0]
            else:
                control = uia.ControlFromHandle(self._hwnd)
            if control is None:
                raise UiError("that window has closed")
            wheel = control.WheelDown if down else control.WheelUp
            wheel(wheelTimes=max(1, min(amount, 20)) * 3)

        await self._run(go)

    async def fill_login(self, site: str, username: str, password: str, submit: bool) -> str:
        """Type a saved login into the window whose title contains ``site``: the username
        box, then the password box (see :func:`login_fields`), then Enter if ``submit``.
        Returns what was filled ("username and password", "username" or "password").
        Nothing typed is logged or read back."""
        if not site.strip():
            raise UiError("the login has no window title words to match")

        def go() -> str:
            import uiautomation as uia

            hwnd = self._find(site, title_only=True)
            root = uia.ControlFromHandle(hwnd)
            if root is None:
                raise UiError("that window has closed")
            boxes: list[tuple[str, bool]] = []
            controls: list[Any] = []
            started = time.monotonic()
            for visited, (control, _depth) in enumerate(uia.WalkControl(root, maxDepth=_MAX_DEPTH)):
                if visited >= _MAX_NODES or time.monotonic() - started > _WALK_BUDGET_S:
                    break
                try:
                    if control.ControlTypeName != "EditControl" or control.IsOffscreen:
                        continue
                    if not control.IsEnabled or control.BoundingRectangle.isempty():
                        continue
                    boxes.append((str(control.Name or ""), bool(control.IsPassword)))
                except Exception:
                    continue
                controls.append(control)
            user_at, pass_at = login_fields(boxes)
            if user_at is None and pass_at is None:
                raise UiError("I couldn't find a username or password box in that window")

            def put(control: Any, text: str) -> None:
                if uia.GetForegroundWindow() != hwnd:
                    self._bring_to_front(hwnd)
                    if uia.GetForegroundWindow() != hwnd:
                        raise UiError("the login window wasn't in front, so I stopped")
                control.SetFocus()
                if not control.HasKeyboardFocus:
                    control.Click(simulateMove=False)
                if not control.HasKeyboardFocus:
                    raise UiError("I couldn't put the cursor in the login box")
                self._press(["ctrl", "a"])
                self._press(["backspace"])
                self._type_text(text)
                time.sleep(0.1)

            filled = []
            if user_at is not None and username:
                put(controls[user_at], username)
                filled.append("username")
            if pass_at is not None and password:
                put(controls[pass_at], password)
                filled.append("password")
            if not filled:
                raise UiError("there was nothing to fill in")
            if submit:
                self._press(["enter"])
            logger.info("Filled a login ({})", " and ".join(filled))  # never what
            return " and ".join(filled)

        return await self._run(go)

    def close(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)


# Controls whose own UI Automation action is the same as a left click. Using it works
# when the control is covered or scrolled half out of view, and doesn't move the mouse
# (trycua/cua does the same). List items stay mouse clicks: selecting one isn't always
# opening it.
_PATTERN_CLICK = {
    "Button": "InvokePattern",
    "MenuItem": "InvokePattern",
    "Hyperlink": "InvokePattern",
    "SplitButton": "InvokePattern",
    "CheckBox": "TogglePattern",
    "RadioButton": "SelectionItemPattern",
    "TabItem": "SelectionItemPattern",
}


def _press_by_pattern(control: Any, role: str) -> bool:
    """Do a control's own click action. False if it has none (then the mouse is used)."""
    import uiautomation as uia

    name = _PATTERN_CLICK.get(role)
    if name is None:
        return False
    try:
        pattern = control.GetPattern(getattr(uia.PatternId, name))
        if pattern is None:
            return False
        if name == "InvokePattern":
            pattern.Invoke()
        elif name == "TogglePattern":
            pattern.Toggle()
        else:
            pattern.Select()
    except Exception as exc:
        logger.debug("Pattern click failed ({}); using the mouse", type(exc).__name__)
        return False
    return True


def _process_name(pid: int) -> str:
    import psutil

    try:
        return str(psutil.Process(pid).name())
    except Exception:
        return ""
