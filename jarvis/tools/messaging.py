"""Sending a WhatsApp message the quick way: search the chat, paste, Enter.

The idea comes from Mark-LVI (reimplemented, no code copied): bring WhatsApp Desktop to
the front, Ctrl+F, type the contact, Enter to open the top match, paste the message and
press Enter. It needs no reading of the window, so it works where UI Automation sees
little. One confirmation (MEDIUM) covers the whole send.

Two guards keep the keystrokes where they belong: before typing, and again before
sending, the window in front must be WhatsApp, otherwise nothing more is pressed. After
the chat opens, a quick look at the window checks that the contact's name is shown; it
never blocks the send, but the result says when it couldn't be confirmed.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
from collections.abc import Awaitable, Callable
from typing import Any

from loguru import logger

from jarvis.tools.desktop import Desktop
from jarvis.tools.registry import Risk, ToolRegistry, ToolResult, ToolSpec
from jarvis.tools.ui import UiDriver

APP = "WhatsApp"
OpenFn = Callable[[str], Awaitable[ToolResult]]
SleepFn = Callable[[float], Awaitable[None]]

# Pauses between steps (seconds); WhatsApp needs a moment to search and open a chat.
_FOCUS_S = 0.6
_SEARCH_S = 0.4
_RESULTS_S = 1.2
_OPEN_S = 1.0
_PASTE_S = 0.4
_CHECK_TIMEOUT_S = 4.0


def _norm(text: str) -> str:
    return " ".join(re.findall(r"\w+", text.casefold()))


def shows_contact(names: list[str], contact: str) -> bool:
    """Whether any of the window's texts names the contact."""
    wanted = _norm(contact)
    return bool(wanted) and any(wanted in _norm(name) for name in names)


def register_messaging_tools(
    registry: ToolRegistry,
    desktop: Desktop,
    open_app: OpenFn,
    ui: UiDriver | None = None,
    *,
    sleep: SleepFn = asyncio.sleep,
) -> None:
    def in_front() -> bool:
        try:
            return APP.casefold() in desktop.active_window_title().casefold()
        except Exception:
            return False

    async def chat_opened(contact: str) -> bool | None:
        """True/False from a look at the window; None when it can't be read."""
        if ui is None:
            return None
        try:
            snapshot = await asyncio.wait_for(ui.snapshot(APP), _CHECK_TIMEOUT_S)
        except Exception as exc:
            logger.debug("WhatsApp chat check skipped: {}", type(exc).__name__)
            return None
        # The search box still holds what was typed, so it doesn't count.
        names = [e.name for e in snapshot.elements if e.role.lower() != "edit"]
        return shows_contact(names, contact)

    async def send_whatsapp(contact: str, message: str) -> ToolResult:
        contact, message = contact.strip(), message.strip()
        if not contact or not message:
            return ToolResult(False, "I need both the contact and the message.")
        opened = await open_app(APP)
        if not opened.ok:
            return opened
        await sleep(_FOCUS_S)
        if not in_front():
            return ToolResult(
                False, "WhatsApp didn't come to the front, so I didn't type anything."
            )
        try:
            await asyncio.to_thread(desktop.press, ["ctrl", "f"])
            await sleep(_SEARCH_S)
            await asyncio.to_thread(desktop.type_text, contact)
            await sleep(_RESULTS_S)
            await asyncio.to_thread(desktop.press, ["enter"])
            await sleep(_OPEN_S)
        except Exception as exc:
            return ToolResult(False, f"Couldn't search WhatsApp for {contact} ({exc}).")
        seen = await chat_opened(contact)
        if not in_front():
            return ToolResult(
                False, f"WhatsApp lost focus after opening {contact}'s chat; nothing was sent."
            )
        previous = ""
        with contextlib.suppress(Exception):
            previous = desktop.get_clipboard_text()
        try:
            desktop.set_clipboard_text(message)
            await asyncio.to_thread(desktop.press, ["ctrl", "v"])
            await sleep(_PASTE_S)
            await asyncio.to_thread(desktop.press, ["enter"])
            await sleep(_PASTE_S)
        except Exception as exc:
            return ToolResult(False, f"Couldn't send the message ({exc}).")
        finally:
            with contextlib.suppress(Exception):
                desktop.set_clipboard_text(previous)
        check = "not checked" if seen is None else "matched" if seen else "not matched"
        data: dict[str, Any] = {"chat_check": check}
        if seen is False:
            return ToolResult(
                True,
                f"Sent it, but I couldn't see {contact}'s name in the open chat; "
                "worth a glance to check it went to the right person.",
                data,
            )
        return ToolResult(True, f"Sent your message to {contact} on WhatsApp.", data)

    def describe(args: dict[str, Any]) -> str:
        message = str(args.get("message", "")).strip()
        shown = message if len(message) <= 120 else message[:119] + "…"
        return f"Send “{shown}” to {str(args.get('contact', '')).strip()} on WhatsApp"

    registry.register(
        ToolSpec(
            "send_whatsapp",
            "Send a WhatsApp message to a contact or group by name, in one step (opens "
            "WhatsApp, finds the chat, sends). Use this for any WhatsApp message.",
            {
                "type": "object",
                "properties": {
                    "contact": {"type": "string", "maxLength": 100},
                    "message": {"type": "string", "maxLength": 4000},
                },
                "required": ["contact", "message"],
            },
            send_whatsapp,
            Risk.MEDIUM,
            describe,
            group="ui",
        )
    )
