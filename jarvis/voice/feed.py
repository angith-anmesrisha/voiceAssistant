"""Text on its way to the voice: a whole reply, or one still being written.

The speaker takes pieces from a :class:`SpeechFeed` as it goes. A piece is whole
clauses (up to a comma, full stop and so on) up to a size the speaker asks for, so it can
size each piece to be made while the one before plays (no gaps), and so speech can start
on the first sentence of a reply that's still being written.
"""

from __future__ import annotations

import re
import threading
import time

_BOUNDARY = re.compile(r"(?<=[,.;:!?…])\s+|\n+")
_SENTENCE_END = re.compile(r"[.!?…]\s*$|\n\s*$")


class SpeechFeed:
    def __init__(self, text: str = "", *, closed: bool = False) -> None:
        self._buffer = text
        self._closed = closed
        self._changed = threading.Condition()

    def put(self, text: str) -> None:
        with self._changed:
            self._buffer += text
            self._changed.notify_all()

    def close(self) -> None:
        with self._changed:
            self._closed = True
            self._changed.notify_all()

    @property
    def finished(self) -> bool:
        with self._changed:
            return self._closed and not self._buffer.strip()

    def _ready(self) -> str:
        """The part of the buffer that ends at a clause boundary (all of it once closed)."""
        if self._closed:
            return self._buffer
        ends = [m.end() for m in _BOUNDARY.finditer(self._buffer)]
        return self._buffer[: ends[-1]] if ends else ""

    def take(
        self,
        max_chars: int,
        *,
        wait_s: float = 0.0,
        stop: threading.Event | None = None,
        single: bool = False,
    ) -> str | None:
        """The next piece: whole clauses up to about ``max_chars`` (just one with
        ``single``). "" if nothing is ready within ``wait_s``; None once everything has
        been taken (or ``stop`` is set)."""
        deadline = time.monotonic() + wait_s
        with self._changed:
            while True:
                if stop is not None and stop.is_set():
                    return None
                ready = self._ready()
                if ready.strip():
                    break
                if self._closed:
                    self._buffer = ""
                    return None
                left = deadline - time.monotonic()
                if left <= 0:
                    return ""
                self._changed.wait(min(left, 0.05))
            piece = _clauses_upto(ready, max_chars, single=single)
            self._buffer = self._buffer[len(piece) :]
            return piece.strip()


def _clauses_upto(text: str, max_chars: int, *, single: bool = False) -> str:
    """The longest run of whole clauses from the start of ``text`` within ``max_chars``
    (at least one clause; a single very long clause is cut at a space). A run that would
    stop inside a sentence stops at the last full stop in it instead: a piece is spoken
    on its own, and pieces cut mid-sentence ("Good morning," / "sir. It's ...") are what
    made the voice sound choppy (owner, 2026-10-29)."""
    ends = [m.end() for m in _BOUNDARY.finditer(text)]
    if not ends or ends[-1] < len(text):
        ends.append(len(text))
    chosen = ends[0]
    if single:
        # The first piece: the whole first sentence when it's short ("Good morning, sir."),
        # else its first clause.
        first = next((end for end in ends if _SENTENCE_END.search(text[:end])), None)
        if first is not None and len(text[:first].strip()) <= max_chars:
            chosen = first
    for end in [] if single else ends[1:]:
        if len(text[:end].strip()) > max_chars:
            break
        chosen = end
    if not _SENTENCE_END.search(text[:chosen]):
        whole = [end for end in ends if end < chosen and _SENTENCE_END.search(text[:end])]
        if whole:
            chosen = whole[-1]
    piece = text[:chosen]
    if len(piece.strip()) > max_chars * 1.5:
        cut = text.rfind(" ", 0, max_chars)
        if cut > max_chars // 3:
            return text[: cut + 1]
    return piece
