from __future__ import annotations

from dataclasses import dataclass

from hitodama.app.events import Event, EventBus


@dataclass(frozen=True, slots=True)
class Ping(Event):
    n: int


@dataclass(frozen=True, slots=True)
class LoudPing(Ping):
    pass


async def test_publish_delivers_to_subscribers() -> None:
    bus = EventBus()
    seen: list[int] = []

    async def handler(event: Ping) -> None:
        seen.append(event.n)

    bus.subscribe(Ping, handler)
    await bus.publish(Ping(1))
    assert seen == [1]


async def test_base_class_subscribers_receive_subclass_events() -> None:
    bus = EventBus()
    seen: list[str] = []

    async def on_ping(event: Ping) -> None:
        seen.append("ping")

    async def on_any(event: Event) -> None:
        seen.append("any")

    bus.subscribe(Ping, on_ping)
    bus.subscribe(Event, on_any)
    await bus.publish(LoudPing(2))
    assert sorted(seen) == ["any", "ping"]


async def test_unsubscribe_stops_delivery() -> None:
    bus = EventBus()
    seen: list[int] = []

    async def handler(event: Ping) -> None:
        seen.append(event.n)

    unsubscribe = bus.subscribe(Ping, handler)
    unsubscribe()
    unsubscribe()  # idempotent
    await bus.publish(Ping(3))
    assert seen == []


async def test_failing_handler_does_not_break_others() -> None:
    bus = EventBus()
    seen: list[int] = []

    async def bad(event: Ping) -> None:
        raise RuntimeError("boom")

    async def good(event: Ping) -> None:
        seen.append(event.n)

    bus.subscribe(Ping, bad)
    bus.subscribe(Ping, good)
    await bus.publish(Ping(4))
    assert seen == [4]


async def test_publish_without_subscribers_is_noop() -> None:
    await EventBus().publish(Ping(5))
