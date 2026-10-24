"""Words speech-to-text should expect: your ``voice.custom_words`` plus the names in what
JARVIS remembers about people and places (Handy's custom-words idea, reimplemented).
They go to Whisper as its prompt and to Gemini in its transcription instruction, so
"Priya" isn't heard as "Korea". Refreshed every few minutes as memory changes.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from loguru import logger

from jarvis.agent.routines import Said
from jarvis.storage.keep import Fact, Keep

MAX_WORDS = 60
_NAME = re.compile(r"\b[A-Z][\w'\u2019.-]{1,30}\b")
_COMMON_CAPITALS = (
    "I I'm My The A An He She They We You It His Her Their Our And But Or In On At To Of "
    "Mr Mrs Ms Dr Mom Mum Dad Monday Tuesday Wednesday Thursday Friday Saturday Sunday "
    "January February March April May June July August September October November "
    "December"
)
_NOT_NAMES = frozenset(_COMMON_CAPITALS.split())


def vocabulary(custom: list[str], facts: list[Fact]) -> list[str]:
    words: list[str] = []
    seen: set[str] = set()

    def add(word: str) -> None:
        word = " ".join(word.split()).strip(" ,.;:")
        if word and word.casefold() not in seen and len(word) <= 40:
            seen.add(word.casefold())
            words.append(word)

    for word in custom:
        add(word)
    for fact in facts:
        if fact.category not in ("people", "places", "about_user", "projects"):
            continue
        for name in _NAME.findall(fact.value):
            if name not in _NOT_NAMES:
                add(name)
    return words[:MAX_WORDS]


@dataclass
class VocabularyRefresh:
    """A background job: rebuild the word list now and then, and hand it on."""

    keep: Keep
    custom: list[str]
    apply: Callable[[list[str]], None]
    every: timedelta = timedelta(minutes=5)
    _next: datetime | None = None
    _last: list[str] = field(default_factory=list)

    async def tick(self, now: datetime) -> list[Said]:
        if self._next is not None and now < self._next:
            return []
        self._next = now + self.every
        words = vocabulary(self.custom, await self.keep.facts())
        if words != self._last:
            self._last = words
            self.apply(words)
            logger.info("Speech-to-text expects {} names and words", len(words))
        return []
