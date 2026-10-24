"""Test doubles for the desktop (nothing here touches the real PC)."""

from __future__ import annotations


class FakeDesktop:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []
        self.focus_ok = True
        self.clipboard = ""

    def launch(self, target: str) -> None:
        self.calls.append(("launch", target))

    def open_installed(self, app: object) -> None:
        self.calls.append(("open_installed", app))

    def open_path(self, path: object) -> None:
        self.calls.append(("open_path", path))

    def get_clipboard_text(self) -> str:
        return self.clipboard

    def set_clipboard_text(self, text: str) -> None:
        self.clipboard = text
        self.calls.append(("clipboard", text))

    def active_window_title(self) -> str:
        return "Untitled - Notepad"

    def open_url(self, url: str) -> None:
        self.calls.append(("open_url", url))

    def press(self, keys: list[str]) -> None:
        self.calls.append(("press", tuple(keys)))

    def type_text(self, text: str) -> None:
        self.calls.append(("type", text))

    def focus_previous_window(self) -> bool:
        self.calls.append(("focus", None))
        return self.focus_ok

    def window_titles(self) -> list[str]:
        return ["Untitled - Notepad"]

    def lock(self) -> None:
        self.calls.append(("lock", None))

    def sleep(self) -> None:
        self.calls.append(("sleep", None))

    def schedule_power(self, action: str, delay_s: int) -> None:
        self.calls.append(("power", (action, delay_s)))

    def cancel_power(self) -> None:
        self.calls.append(("cancel_power", None))


class FakeUi:
    """A scripted app window for the UI tools: each read returns the next screen."""

    def __init__(self, *screens: object, app: str = "WhatsApp.exe") -> None:
        from jarvis.tools.ui import UiSnapshot

        self.screens: list[UiSnapshot] = list(screens)  # type: ignore[arg-type]
        self.current: UiSnapshot | None = None
        self.calls: list[tuple[str, object]] = []
        self._app = app
        self.error: Exception | None = None

    @property
    def app(self) -> str:
        return self.current.app if self.current is not None else ""

    def element(self, element_id: int) -> object:
        return self.current.element(element_id) if self.current is not None else None

    def _next(self) -> object:
        if self.error is not None:
            raise self.error
        if len(self.screens) > 1:
            self.current = self.screens.pop(0)
        else:
            self.current = self.screens[0]
        return self.current

    async def snapshot(self, window: str = "") -> object:
        self.calls.append(("read", window))
        return self._next()

    async def refresh(self) -> object:
        return self._next()

    async def click(self, element_id: int, *, double: bool = False, right: bool = False) -> None:
        if self.element(element_id) is None:
            from jarvis.tools.ui import UiError

            raise UiError(f"there is no element {element_id}; read the window again")
        self.calls.append(("click", element_id))

    async def type_into(
        self, element_id: int, text: str, *, clear: bool = False, submit: bool = False
    ) -> None:
        from jarvis.tools.ui import UiError

        element = self.element(element_id)
        if element is None:
            raise UiError(f"there is no element {element_id}; read the window again")
        if getattr(element, "password", False):
            raise UiError("JARVIS doesn't type into password fields")
        self.calls.append(("type", (element_id, text, clear, submit)))

    async def scroll(self, element_id: int | None, down: bool, amount: int) -> None:
        self.calls.append(("scroll", (element_id, down, amount)))
