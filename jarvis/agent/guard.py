"""Prompt-injection screening with Meta's Llama Prompt Guard 2 (on Groq).

Prompt Guard is a small classifier, not a chat model: it can't answer or call tools. It
scores text for injection and jailbreak attempts. JARVIS runs it over text that comes
from outside the conversation (clipboard, folder listings, the screen description,
window titles) before that text reaches the chat model. Flagged text is withheld and the
UI turns red. If the guard can't be reached, text passes (the system prompt still treats
it as information only) and the log says so.
"""

from __future__ import annotations

import asyncio
import re
from typing import Protocol

from loguru import logger

# Prompt Guard 2 reads at most 512 tokens; ~1800 characters stays under that.
_CHUNK_CHARS = 1800
_MAX_CHUNKS = 6
_FLAG_WORDS = re.compile(r"\b(malicious|injection|jailbreak|unsafe)\b", re.IGNORECASE)
_SAFE_WORDS = re.compile(r"\b(benign|safe)\b", re.IGNORECASE)


class Classifier(Protocol):
    def guard_ready(self) -> bool: ...

    async def classify(self, text: str) -> str: ...


def attack_score(content: str) -> float:
    """The guard's reply as a 0..1 attack probability (a number, or a label)."""
    text = content.strip()
    try:
        return max(0.0, min(1.0, float(text)))
    except ValueError:
        pass
    if _FLAG_WORDS.search(text):
        return 1.0
    if _SAFE_WORDS.search(text):
        return 0.0
    return 0.0


def chunks(text: str) -> list[str]:
    parts = [text[i : i + _CHUNK_CHARS] for i in range(0, len(text), _CHUNK_CHARS)]
    if len(parts) > _MAX_CHUNKS:  # long text: check the start and the end
        parts = parts[: _MAX_CHUNKS // 2] + parts[-(_MAX_CHUNKS // 2) :]
    return [p for p in parts if p.strip()]


class PromptGuard:
    def __init__(self, classifier: Classifier | None, threshold: float = 0.8) -> None:
        self._classifier = classifier
        self.threshold = threshold

    @property
    def ready(self) -> bool:
        return self._classifier is not None and self._classifier.guard_ready()

    async def suspicious(self, text: str) -> bool:
        """True if any part of ``text`` looks like an attempt to steer JARVIS."""
        classifier = self._classifier
        if classifier is None or not text.strip() or not classifier.guard_ready():
            return False
        try:
            replies = await asyncio.gather(*(classifier.classify(c) for c in chunks(text)))
        except Exception as exc:
            logger.warning("Prompt guard unavailable, text passed unchecked: {}", exc)
            return False
        flagged = any(attack_score(r) >= self.threshold for r in replies)
        if flagged:
            logger.warning("Prompt guard flagged untrusted text; it was withheld")
        return flagged


__all__ = ["PromptGuard", "attack_score", "chunks"]
