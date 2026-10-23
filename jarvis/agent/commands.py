"""Requests JARVIS handles itself, without asking a model.

Right now: choosing the AI ("use Gemini from now on", "switch to the local model",
"go back to automatic"). Matching is strict (the whole request must be the command), so
"use Google to search for flights" is still an ordinary request.
"""

from __future__ import annotations

import re

AUTO = "auto"

_NAMES = {
    "groq": "groq",
    "grok": "groq",  # how speech-to-text often spells it
    "gemini": "gemini",
    "google": "gemini",
    "google gemini": "gemini",
    "ollama": "ollama",
    "llama": "ollama",
    "local": "ollama",
    "offline": "ollama",
    "qwen": "ollama",
    "auto": AUTO,
    "automatic": AUTO,
    "default": AUTO,
    "the best available": AUTO,
}

_PROVIDER_COMMAND = re.compile(
    r"""^\s*
    (?:(?:hey\s+)?jarvis[\s,]+)?
    (?:please\s+|can\s+you\s+|could\s+you\s+)?
    (?:use|switch(?:\s+over)?\s+to|change\s+to|go\s+(?:back\s+)?to|move\s+to|start\s+using
       |set\s+(?:the\s+)?(?:ai|model|provider|brain)\s+to)
    \s+(?:the\s+)?
    (?P<name>google\s+gemini|the\s+best\s+available|[a-z]+)
    (?:\s+(?:ai|model|models|provider|mode|brain))?
    (?:\s+(?:from\s+now\s+on|for\s+now|instead|now|again|please|only))*
    \s*[.!]*\s*$""",
    re.IGNORECASE | re.VERBOSE,
)


def provider_command(text: str) -> str | None:
    """The provider a request asks to switch to ("auto" for automatic), else None."""
    match = _PROVIDER_COMMAND.match(text)
    if match is None:
        return None
    return _NAMES.get(match.group("name").lower().strip())
