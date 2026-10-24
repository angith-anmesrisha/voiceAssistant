from __future__ import annotations

import asyncio
from typing import Any

from jarvis.tools.messaging import register_messaging_tools, shows_contact
from jarvis.tools.registry import Risk, ToolRegistry, ToolResult
from jarvis.tools.ui import UiElement, UiSnapshot
from tests.fakes import FakeDesktop, FakeUi


async def _no_wait(_seconds: float) -> None:
    return None


def _setup(
    *, ui: Any = None, opens: bool = True, front: str = "WhatsApp"
) -> tuple[ToolRegistry, FakeDesktop, list[str]]:
    desktop = FakeDesktop()
    desktop.clipboard = "earlier"
    opened: list[str] = []

    async def open_app(name: str) -> ToolResult:
        opened.append(name)
        if opens:
            desktop.active_title = front
            return ToolResult(True, "WhatsApp was already open; brought it to the front.")
        return ToolResult(False, "WhatsApp isn't installed on this PC.")

    registry = ToolRegistry()
    register_messaging_tools(registry, desktop, open_app, ui, sleep=_no_wait)
    return registry, desktop, opened


def _send(registry: ToolRegistry, contact: str = "Mom", message: str = "On my way") -> ToolResult:
    spec = registry.get("send_whatsapp")
    assert spec is not None
    return asyncio.run(registry.run(spec, {"contact": contact, "message": message}))


def _chat(*names: str) -> UiSnapshot:
    elements = tuple(UiElement(i + 1, "Text", name) for i, name in enumerate(names))
    return UiSnapshot("WhatsApp.exe", "WhatsApp", (UiElement(0, "Edit", "Mom"), *elements))


def test_keystrokes_follow_the_search_paste_enter_method() -> None:
    registry, desktop, opened = _setup()
    result = _send(registry)
    assert result.ok, result.summary
    assert opened == ["WhatsApp"]
    assert [c for c in desktop.calls if c[0] in ("press", "type", "clipboard")] == [
        ("press", ("ctrl", "f")),
        ("type", "Mom"),
        ("press", ("enter",)),
        ("clipboard", "On my way"),
        ("press", ("ctrl", "v")),
        ("press", ("enter",)),
        ("clipboard", "earlier"),
    ]
    assert desktop.clipboard == "earlier"  # the user's clipboard is put back


def test_one_medium_confirmation_names_the_message_and_contact() -> None:
    registry, _desktop, _opened = _setup()
    spec = registry.get("send_whatsapp")
    assert spec is not None
    assert spec.risk_for({"contact": "Mom", "message": "hi"}) is Risk.MEDIUM
    assert spec.describe({"contact": "Mom", "message": "hi"}) == "Send “hi” to Mom on WhatsApp"


def test_nothing_is_typed_when_whatsapp_is_not_in_front() -> None:
    registry, desktop, _opened = _setup(front="Untitled - Notepad")
    result = _send(registry)
    assert not result.ok
    assert not [c for c in desktop.calls if c[0] in ("press", "type")]


def test_open_failure_is_reported() -> None:
    registry, desktop, _opened = _setup(opens=False)
    result = _send(registry)
    assert not result.ok and "isn't installed" in result.summary
    assert not desktop.calls


def test_chat_check_matches_contact_but_ignores_the_search_box() -> None:
    registry, _desktop, _opened = _setup(ui=FakeUi(_chat("Mom", "online")))
    result = _send(registry)
    assert result.ok and result.data["chat_check"] == "matched"

    registry, _desktop, _opened = _setup(ui=FakeUi(_chat("Dad", "online")))
    result = _send(registry)
    assert result.ok  # never blocks the send
    assert result.data["chat_check"] == "not matched"
    assert "couldn't see Mom" in result.summary


def test_chat_check_failure_does_not_block() -> None:
    ui = FakeUi(_chat("Mom"))
    ui.error = RuntimeError("no UIA")
    registry, _desktop, _opened = _setup(ui=ui)
    result = _send(registry)
    assert result.ok and result.data["chat_check"] == "not checked"


def test_shows_contact() -> None:
    assert shows_contact(["Mom ❤️", "last seen today"], "mom")
    assert not shows_contact(["Dad"], "Mom")
    assert not shows_contact(["Mom"], "  ")


def test_empty_contact_or_message_refused() -> None:
    registry, desktop, _opened = _setup()
    assert not _send(registry, contact=" ").ok
    assert not _send(registry, message="").ok
    assert not desktop.calls
