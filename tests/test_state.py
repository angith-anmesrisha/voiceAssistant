from __future__ import annotations

import pytest

from hitodama.app.events import EventBus
from hitodama.app.state import AppState, InvalidTransitionError, StateChanged, StateMachine


async def test_starts_idle_and_publishes_changes() -> None:
    bus = EventBus()
    changes: list[tuple[AppState, AppState]] = []

    async def on_change(event: StateChanged) -> None:
        changes.append((event.previous, event.current))

    bus.subscribe(StateChanged, on_change)
    sm = StateMachine(bus)
    assert sm.state is AppState.IDLE

    await sm.transition(AppState.LISTENING)
    await sm.transition(AppState.THINKING)
    await sm.transition(AppState.SPEAKING)
    await sm.transition(AppState.LISTENING)  # barge-in
    assert changes == [
        (AppState.IDLE, AppState.LISTENING),
        (AppState.LISTENING, AppState.THINKING),
        (AppState.THINKING, AppState.SPEAKING),
        (AppState.SPEAKING, AppState.LISTENING),
    ]


async def test_same_state_is_noop() -> None:
    bus = EventBus()
    calls: list[StateChanged] = []

    async def on_change(event: StateChanged) -> None:
        calls.append(event)

    bus.subscribe(StateChanged, on_change)
    await StateMachine(bus).transition(AppState.IDLE)
    assert calls == []


async def test_invalid_transition_raises() -> None:
    sm = StateMachine(EventBus())
    await sm.transition(AppState.LISTENING)
    with pytest.raises(InvalidTransitionError):
        await sm.transition(AppState.ACTING)
    assert sm.state is AppState.LISTENING


@pytest.mark.parametrize("state", list(AppState))
async def test_every_state_can_return_to_idle(state: AppState) -> None:
    from hitodama.app.state import ALLOWED_TRANSITIONS

    assert state is AppState.IDLE or AppState.IDLE in ALLOWED_TRANSITIONS[state]
