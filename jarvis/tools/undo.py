"""Taking back what JARVIS did: its own file moves, renames, copies and new folders, and
the settings it changed (volume, brightness, wallpaper).

Each change records how to reverse it at the moment it is made, from what was really
there before (never a guess). Reversing a copy sends the copy to the Recycle Bin;
reversing a new folder removes it only while it is still empty. This session only.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

MAX_STEPS = 30


@dataclass(frozen=True, slots=True)
class UndoStep:
    what: str  # "Moved notes.txt to Downloads"
    revert: Callable[[], str]  # does it, returns what was done ("Moved notes.txt back.")


class UndoJournal:
    def __init__(self) -> None:
        self._steps: list[UndoStep] = []

    def record(self, what: str, revert: Callable[[], str]) -> None:
        self._steps.append(UndoStep(what, revert))
        del self._steps[:-MAX_STEPS]

    @property
    def last(self) -> UndoStep | None:
        return self._steps[-1] if self._steps else None

    def undo(self) -> str:
        """Reverse the most recent change. Raises LookupError if there is none."""
        if not self._steps:
            raise LookupError("there's nothing of mine to undo")
        step = self._steps.pop()
        return step.revert()
