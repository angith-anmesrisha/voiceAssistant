"""App state machine: IDLE, LISTENING, THINKING, SPEAKING, ACTING."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from jarvis.app.events import Event, EventBus


class AppState(StrEnum):
    IDLE = "idle"
    LISTENING = "listening"
    THINKING = "thinking"
    SPEAKING = "speaking"
    ACTING = "acting"


_S = AppState

# Any state may return to IDLE (cancel/error). LISTENING is reachable from SPEAKING
# for barge-in and from THINKING/ACTING when the user interrupts. IDLE -> SPEAKING
# covers a reply that arrives after a timeout (and proactive speech later on).
ALLOWED_TRANSITIONS: dict[AppState, frozenset[AppState]] = {
    _S.IDLE: frozenset({_S.LISTENING, _S.THINKING, _S.SPEAKING, _S.ACTING}),
    _S.LISTENING: frozenset({_S.IDLE, _S.THINKING, _S.SPEAKING}),
    _S.THINKING: frozenset({_S.IDLE, _S.LISTENING, _S.SPEAKING, _S.ACTING}),
    _S.SPEAKING: frozenset({_S.IDLE, _S.LISTENING, _S.THINKING, _S.ACTING}),
    _S.ACTING: frozenset({_S.IDLE, _S.LISTENING, _S.THINKING, _S.SPEAKING}),
}


class InvalidTransitionError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class StateChanged(Event):
    previous: AppState
    current: AppState


class StateMachine:
    def __init__(self, bus: EventBus) -> None:
        self._bus = bus
        self._state = AppState.IDLE

    @property
    def state(self) -> AppState:
        return self._state

    def can_transition(self, target: AppState) -> bool:
        return target in ALLOWED_TRANSITIONS[self._state]

    async def transition(self, target: AppState) -> None:
        """Move to ``target`` and publish :class:`StateChanged`. Same-state is a no-op."""
        if target == self._state:
            return
        if not self.can_transition(target):
            raise InvalidTransitionError(f"{self._state} -> {target}")
        previous, self._state = self._state, target
        await self._bus.publish(StateChanged(previous=previous, current=target))
