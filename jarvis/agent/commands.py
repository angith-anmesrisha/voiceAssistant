"""Requests JARVIS handles itself, without asking a model.

Choosing the AI ("use Gemini from now on", "switch to the local model", "go back to
automatic") and testing the AIs ("test the AI providers", "run diagnostics"). Matching is
strict (the whole request must be the command), so "use Google to search for flights" is
still an ordinary request.
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


_DIAGNOSE_COMMAND = re.compile(
    r"""^\s*(?:(?:hey\s+)?jarvis[\s,]+)?(?:please\s+|can\s+you\s+|could\s+you\s+)?
    (?:run\s+(?:a\s+)?diagnostics?|diagnose(?:\s+yourself)?|self[\s-]?test
       |(?:test|check)\s+(?:the\s+|all\s+(?:the\s+)?|your\s+|my\s+)?
        (?:ai|ais|providers?|models?|connections?|apis?)(?:\s+(?:providers?|models?))?)
    \s*[.!?]*\s*$""",
    re.IGNORECASE | re.VERBOSE,
)


def diagnose_command(text: str) -> bool:
    """True for "test the AI providers", "run diagnostics" and the like."""
    return _DIAGNOSE_COMMAND.match(text) is not None


def provider_command(text: str) -> str | None:
    """The provider a request asks to switch to ("auto" for automatic), else None."""
    match = _PROVIDER_COMMAND.match(text)
    if match is None:
        return None
    return _NAMES.get(match.group("name").lower().strip())
