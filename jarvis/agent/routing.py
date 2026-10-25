"""Which requests the local models answer (owner, 2026-10-25: "all normal conversations
should be routed here. only the ones that actually require cloud models ... to cloud").

The local models (Qwen 3.5 4B and Gemma 3 4B in Ollama) get conversation: questions,
explanations, advice, writing, chat. The cloud gets what a small local model can't do
well or at all: acting on the PC (tools), the screen, current or online information,
long multi-step requests, slides. A wrong guess is cheap: the local model is told to
answer only ``NEEDS_TOOLS`` when the request needs more, and that goes to the cloud.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any

NEEDS_TOOLS = "NEEDS_TOOLS"

# Requests to do something, or about things only tools or the internet know now.
_ACTION = re.compile(
    r"^\W*(?:(?:hey\s+)?jarvis\W+)?(?:(?:please|kindly|can you|could you|would you|will you|"
    r"i want you to|i need you to|go ahead and|now)\s+)*"
    r"(?:open|launch|start|run|play|pause|resume|stop|skip|close|quit|exit|send|message|"
    r"text|call|email|set|turn|switch|enable|disable|show|display|search|find|look|check|"
    r"google|browse|navigate|go to|visit|download|install|uninstall|update|remind|schedule|"
    r"book|order|buy|make|create|generate|save|delete|remove|move|copy|rename|paste|type|"
    r"press|click|scroll|lock|sleep|shut|restart|mute|unmute|increase|decrease|lower|raise|"
    r"take|capture|record|track|watch|monitor|note|add|list|read|summari[sz]e|translate this|"
    r"organi[sz]e|tidy|clean|undo|pair|connect|log|share|upload|print)\b",
    re.IGNORECASE,
)
_CURRENT = re.compile(
    r"\b(today|tonight|tomorrow|yesterday|this (week|month|morning|evening)|right now|"
    r"currently|current|latest|recent(ly)?|news|headlines?|weather|forecast|temperature|"
    r"score|match|price|stock|rate|traffic|open now|near me|my (files?|folders?|pc|"
    r"computer|laptop|screen|desktop|downloads|documents|calendar|emails?|inbox|tasks?|"
    r"reminders?|notes?)|on (my|the) screen|this (pdf|file|document|page|window|app)|"
    r"https?://|www\.|\.com\b)",
    re.IGNORECASE,
)


def is_conversation(text: str, groups: set[str]) -> bool:
    """True when ``text`` reads as conversation the local models can answer."""
    if groups or not text.strip():
        return False
    return not (_ACTION.search(text) or _CURRENT.search(text))


def wants_cloud(reply: str) -> bool:
    """The local model said the request needs tools (or gave nothing usable)."""
    cleaned = reply.strip().strip("`*\"'. ")
    return not cleaned or cleaned.upper().startswith(NEEDS_TOOLS)


class KeepLocalWarm:
    """Keeps the local chat model loaded so a conversation doesn't wait for it to load
    (a background job: asks Ollama every ``every_s`` while it's running)."""

    def __init__(
        self, ready: Callable[[], bool], warm: Callable[[], Awaitable[bool]], every_s: float = 1200
    ) -> None:
        self._ready = ready
        self._warm = warm
        self._every_s = every_s
        self._last = 0.0

    async def tick(self, now: datetime) -> list[Any]:
        stamp = now.timestamp()
        if stamp - self._last >= self._every_s and self._ready():
            self._last = stamp
            await self._warm()
        return []
