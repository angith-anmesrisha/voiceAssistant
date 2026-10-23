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
