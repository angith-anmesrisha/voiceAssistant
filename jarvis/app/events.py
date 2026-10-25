"""Async in-process event bus.

Modules communicate by publishing event objects; subscribers register for an event
class and receive instances of it (including subclasses). A failing handler is logged
and never breaks the publisher or other handlers.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, TypeVar

from loguru import logger


@dataclass(frozen=True, slots=True)
class Event:
    """Base class for all events."""


@dataclass(frozen=True, slots=True)
class QuitRequested(Event):
    reason: str = "user"


E = TypeVar("E", bound=Event)
Handler = Callable[[E], Awaitable[None]]


class EventBus:
    def __init__(self) -> None:
        self._handlers: defaultdict[type[Event], list[Handler[Any]]] = defaultdict(list)

    def subscribe(self, event_type: type[E], handler: Handler[E]) -> Callable[[], None]:
        """Register ``handler`` for ``event_type``. Returns an unsubscribe function."""
        self._handlers[event_type].append(handler)

        def unsubscribe() -> None:
            handlers = self._handlers.get(event_type)
            if handlers and handler in handlers:
                handlers.remove(handler)

        return unsubscribe

    async def publish(self, event: Event) -> None:
        """Deliver ``event`` to all matching handlers concurrently and wait for them."""
        handlers = [
            handler
            for event_type in type(event).__mro__
            if isinstance(event_type, type) and issubclass(event_type, Event)
            for handler in list(self._handlers.get(event_type, ()))
        ]
        if not handlers:
            return
        results = await asyncio.gather(*(h(event) for h in handlers), return_exceptions=True)
        for handler, result in zip(handlers, results, strict=True):
            if isinstance(result, BaseException):
                logger.opt(exception=result).error(
                    "Handler {} failed on {}",
                    getattr(handler, "__qualname__", repr(handler)),
                    type(event).__name__,
                )


class NoticeLevel(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class UserNotice(Event):
    """Something the user should see (status line in the window, tray balloon if hidden)."""

    title: str
    message: str
    level: NoticeLevel = NoticeLevel.INFO


@dataclass(frozen=True, slots=True)
class PushToTalkPressed(Event):
    pass


@dataclass(frozen=True, slots=True)
class PushToTalkReleased(Event):
    pass


@dataclass(frozen=True, slots=True)
class DictationPressed(Event):
    """The dictation hotkey went down: record, then type the words where the cursor is."""


@dataclass(frozen=True, slots=True)
class DictationReleased(Event):
    pass


@dataclass(frozen=True, slots=True)
class WakeWordHeard(Event):
    """Someone said the wake word: listen hands-free until they go quiet."""


@dataclass(frozen=True, slots=True)
class ConversationChanged(Event):
    """A wake-word conversation started or ended (follow-ups need no wake word)."""

    active: bool
    listening_again: bool = False  # True while waiting for a follow-up after a reply


class EntryKind(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"
    ACTION = "action"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class ChatEntry(Event):
    """A line for the conversation panel. Never log these: they are the user's words."""

    kind: EntryKind
    text: str
    provider: str | None = None
    model: str | None = None
    mode: str | None = None
    failed: bool = False  # the request couldn't be carried out (the UI turns red)


@dataclass(frozen=True, slots=True)
class ProvidersChanged(Event):
    """Provider or network status changed; the UI re-reads the router."""


class StepStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass(frozen=True, slots=True)
class PlanStep:
    title: str
    status: StepStatus = StepStatus.PENDING
    note: str = ""  # the step's reply or error, once it has run


@dataclass(frozen=True, slots=True)
class PlanUpdated(Event):
    """A long request was split into tasks; sent as each one starts and ends."""

    steps: tuple[PlanStep, ...]
    finished: bool = False

    @property
    def current(self) -> int:
        """Index of the running step, or -1."""
        for i, step in enumerate(self.steps):
            if step.status is StepStatus.RUNNING:
                return i
        return -1


@dataclass(frozen=True, slots=True)
class Announce(Event):
    """Something JARVIS brings up by itself (a reminder, an alert, a briefing). It is shown
    in the conversation and said out loud once JARVIS isn't busy."""

    text: str
    kind: str = "reminder"  # reminder | briefing | news | hardware | check-in


@dataclass(frozen=True, slots=True)
class RunWorkflow(Event):
    """Run a saved workflow once JARVIS is free (its time came, JARVIS started, a watched
    folder changed, or a tool asked for it)."""

    workflow_id: str
    reason: str = ""
    file: str = ""  # the file that started a folder workflow ({file} in its steps)
