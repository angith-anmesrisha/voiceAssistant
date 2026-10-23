"""What JARVIS knows about the moment: the window in front, other open windows, the AI.

Sent with each request as information (never instructions), so "close this", "what's in
here" or "paste it there" can be resolved without asking. Window titles stay on the PC
except for this one request, and are never logged.
"""

from __future__ import annotations

from collections.abc import Callable

from loguru import logger

from jarvis.providers.router import ProviderRouter
from jarvis.tools.desktop import Desktop

_MAX_WINDOWS = 12
_HIDDEN = {
    "",
    "jarvis",
    "program manager",
    "windows input experience",
    "microsoft text input application",
    "nvidia geforce overlay",
    "settings",
}


def _clip(text: str, limit: int = 80) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def open_windows(titles: list[str], active: str = "") -> list[str]:
    """Distinct, readable window titles, the active one left out."""
    seen = {active.strip().lower()}
    kept: list[str] = []
    for title in titles:
        key = title.strip().lower()
        if key in _HIDDEN or key in seen:
            continue
        seen.add(key)
        kept.append(_clip(title))
        if len(kept) >= _MAX_WINDOWS:
            break
    return kept


def situation(desktop: Desktop, router: ProviderRouter | None = None) -> Callable[[], str]:
    """A function that describes the current situation in a few lines."""

    def describe() -> str:
        lines: list[str] = []
        try:
            active = desktop.active_window_title()
            if active:
                lines.append(f"Window the user is working in: {_clip(active)}")
            others = open_windows(desktop.window_titles(), active)
            if others:
                lines.append("Other open windows: " + "; ".join(others))
        except Exception as exc:  # off Windows, or a window closed mid-read
            logger.debug("No window context: {}", type(exc).__name__)
        if router is not None:
            chosen = router.provider(router.preferred) if router.preferred else None
            lines.append(
                f"AI choice: {chosen.display_name} first" if chosen else "AI choice: automatic"
            )
        return "\n".join(lines)

    return describe


__all__ = ["open_windows", "situation"]
