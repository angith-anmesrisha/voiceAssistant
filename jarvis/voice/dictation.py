"""Dictation, the idea behind Handy (cjpais/handy, MIT; reimplemented): hold a hotkey,
speak, let go, and the words appear where the cursor is, in any app. No AI answers and
nothing is saved: the speech-to-text result is pasted (clipboard, Ctrl+V, clipboard put
back) and that's all.

It never types into shells, terminals, system tools or JARVIS itself (the same list the
app-operating tools refuse), and it never presses Enter.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from jarvis.tools.desktop import Desktop
from jarvis.tools.ui import blocked_reason

SleepFn = Callable[[float], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class Foreground:
    process: str  # "notepad.exe"
    title: str
    own: bool = False  # JARVIS's own window


def tidy(text: str) -> str:
    """One line, single spaces, ending with a space so the next dictation follows on."""
    text = re.sub(r"\s+", " ", text).strip()
    return f"{text} " if text else ""


class Dictation:
    def __init__(
        self,
        desktop: Desktop,
        foreground: Callable[[], Foreground | None],
        *,
        sleep: SleepFn = asyncio.sleep,
    ) -> None:
        self._desktop = desktop
        self._foreground = foreground
        self._sleep = sleep

    def refusal(self) -> str | None:
        """Why the window in front can't take dictation, or None if it can."""
        window = self._foreground()
        if window is None:
            return "I couldn't tell which window is in front, so I didn't type anything."
        reason = blocked_reason(window.process, window.title, own=window.own)
        if reason:
            return f"Not typed: {reason}."
        return None

    async def paste(self, text: str) -> str | None:
        """Put ``text`` where the cursor is. Returns a problem to show, or None."""
        words = tidy(text)
        if not words:
            return None
        problem = self.refusal()
        if problem:
            return problem
        previous = ""
        with contextlib.suppress(Exception):
            previous = self._desktop.get_clipboard_text()
        try:
            self._desktop.set_clipboard_text(words)
            await asyncio.to_thread(self._desktop.press, ["ctrl", "v"])
            await self._sleep(0.3)  # let the app read the clipboard before it changes back
        except Exception as exc:
            return f"Couldn't type the words ({type(exc).__name__})."
        finally:
            with contextlib.suppress(Exception):
                self._desktop.set_clipboard_text(previous)
        return None
