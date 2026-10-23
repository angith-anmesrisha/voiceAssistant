"""Test doubles for the desktop (nothing here touches the real PC)."""

from __future__ import annotations


class FakeDesktop:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []
        self.focus_ok = True

    def launch(self, target: str) -> None:
        self.calls.append(("launch", target))

    def open_installed(self, app: object) -> None:
        self.calls.append(("open_installed", app))

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
