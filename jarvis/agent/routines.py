"""Things JARVIS brings up by itself: the morning briefing, news on watched topics,
hardware warnings, a nudge after long stretches at the PC, and a short summary of each
session (mentioned once, in the next morning's briefing).

:class:`~jarvis.agent.background.Background` calls each job's ``tick`` every 10 s with
the local time; a job returns what to say, usually nothing. Jobs pace themselves.
"""

from __future__ import annotations

import asyncio
import ctypes
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Protocol

from loguru import logger

from jarvis.storage.db import HistoryStore
from jarvis.storage.keep import Keep
from jarvis.tools.web import Forecast, Headline, WebError


@dataclass(frozen=True, slots=True)
class Said:
    text: str
    kind: str  # briefing, news, hardware, check-in


class Job(Protocol):
    async def tick(self, now: datetime) -> list[Said]: ...


def quiet(now: datetime, start: int, end: int) -> bool:
    """Whether ``now`` falls in the quiet hours (which may wrap past midnight)."""
    if start == end:
        return False
    return start <= now.hour or now.hour < end if start > end else start <= now.hour < end


def idle_seconds() -> float:
    """Seconds since the last key press or mouse move (0 where Windows can't say)."""
    if sys.platform != "win32":
        return 0.0

    class LastInput(ctypes.Structure):
        _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]

    info = LastInput()
    info.cbSize = ctypes.sizeof(LastInput)
    windll = ctypes.windll  # type: ignore[attr-defined,unused-ignore]
    if not windll.user32.GetLastInputInfo(ctypes.byref(info)):
        return 0.0
    return max(0.0, (windll.kernel32.GetTickCount() - info.dwTime) / 1000.0)


_WORDS = ("no", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine")


def _number(n: int) -> str:
    return _WORDS[n] if 0 <= n < len(_WORDS) else str(n)


def _clock(moment: datetime) -> str:
    return f"{moment.hour}:{moment.minute:02d}"


# --- morning briefing ---


@dataclass
class Briefing:
    keep: Keep
    weather: Callable[[], Awaitable[Forecast | None]]
    headlines: Callable[[], Awaitable[list[Headline]]]
    idle: Callable[[], float] = idle_seconds
    until_hour: int = 12
    _done: str = ""

    async def tick(self, now: datetime) -> list[Said]:
        today = now.date().isoformat()
        if self._done == today or not 4 <= now.hour < self.until_hour:
            return []
        if await self.keep.get_state("briefing_day") == today:
            self._done = today
            return []
        if self.idle() > 120:
            return []  # nobody at the PC: wait for them
        self._done = today
        await self.keep.set_state("briefing_day", today)
        return [Said(await self.compose(now), "briefing")]

    async def compose(self, now: datetime) -> str:
        lines = [f"Good morning. It's {now:%A} {now.day} {now:%B}, {_clock(now)}."]
        try:
            forecast = await self.weather()
        except WebError as exc:
            logger.info("Briefing without weather: {}", exc)
            forecast = None
        if forecast is not None:
            lines.append(_weather_line(forecast))
        lines.extend(await self._reminders(now))
        recap = await self.keep.unmentioned_summaries(now.date().isoformat())
        if recap:
            lines.append("From last time: " + " ".join(recap[-3:]))
        try:
            news = await self.headlines()
        except WebError as exc:
            logger.info("Briefing without news: {}", exc)
            news = []
        if news:
            titles = [h.title.rstrip(".") for h in news]
            joined = (
                titles[0] if len(titles) == 1 else "; ".join(titles[:-1]) + "; and " + titles[-1]
            )
            lines.append(f"In the news: {joined}.")
        return "\n".join(lines)

    async def _reminders(self, now: datetime) -> list[str]:
        end = datetime.combine(now.date() + timedelta(days=1), datetime.min.time(), now.tzinfo)
        today = [
            r for r in await self.keep.reminders() if now.timestamp() <= r.due < end.timestamp()
        ]
        if not today:
            return []
        first = today[0]
        at = _clock(datetime.fromtimestamp(first.due, now.tzinfo))
        if len(today) == 1:
            return [f"One reminder today, at {at}: {first.text}."]
        return [
            f"{_number(len(today)).capitalize()} reminders today; the first at {at}: {first.text}."
        ]


def _weather_line(forecast: Forecast) -> str:
    place = forecast.place.split(",")[0]
    line = f"In {place} it's {forecast.temperature}°C and {forecast.sky}"
    if forecast.days:
        day = forecast.days[0]
        line += f"; today {day.low} to {day.high}°C"
        if day.rain is not None and day.rain >= 30:
            line += f" with a {day.rain}% chance of rain"
    return line + "."


# --- watched news topics ---


@dataclass
class TopicWatch:
    keep: Keep
    fetch: Callable[[str], Awaitable[list[Headline]]]
    quiet_hours: tuple[int, int] = (22, 8)
    every: timedelta = timedelta(hours=20)
    _next: datetime | None = None

    async def tick(self, now: datetime) -> list[Said]:
        if self._next is not None and now < self._next:
            return []
        self._next = now + timedelta(minutes=10)
        if quiet(now, *self.quiet_hours):
            return []
        said = []
        for topic, seen, checked in await self.keep.topics():
            if now.timestamp() - checked < self.every.total_seconds():
                continue
            try:
                found = await self.fetch(topic)
            except WebError as exc:
                logger.info("Topic check failed: {}", exc)
                await self.keep.mark_topic(topic, seen, now.timestamp())  # again tomorrow
                continue
            newest = found[0] if found else None
            if newest is None:
                await self.keep.mark_topic(topic, seen, now.timestamp())
                continue
            if seen and newest.title != seen:
                source = f" ({newest.source})" if newest.source else ""
                said.append(Said(f"News on {topic}: {newest.title}{source}.", "news"))
            await self.keep.mark_topic(topic, newest.title, now.timestamp())
        return said


# --- hardware ---


@dataclass(frozen=True, slots=True)
class Reading:
    battery: int | None  # percent, None without a battery
    plugged: bool
    cpu: float
    memory: float


def read_hardware() -> Reading:
    import psutil

    battery = psutil.sensors_battery()
    return Reading(
        round(battery.percent) if battery is not None else None,
        bool(battery.power_plugged) if battery is not None else True,
        float(psutil.cpu_percent(None)),
        float(psutil.virtual_memory().percent),
    )


@dataclass
class HardwareWatch:
    read: Callable[[], Reading] = read_hardware
    battery_low: int = 20
    busy: float = 90.0
    sustained: int = 12  # readings in a row (10 s apart): two minutes
    repeat: timedelta = timedelta(minutes=30)
    _battery_said: int | None = None
    _high: dict[str, int] = field(default_factory=dict)
    _last: dict[str, datetime] = field(default_factory=dict)

    async def tick(self, now: datetime) -> list[Said]:
        reading = await asyncio.to_thread(self.read)
        said = self._battery(reading)
        for name, value, line in (
            (
                "cpu",
                reading.cpu,
                "The processor has been flat out for a couple of minutes. Something may be "
                "working hard in the background; Task Manager will show what.",
            ),
            (
                "memory",
                reading.memory,
                f"Memory is {round(reading.memory)}% full. Closing a few apps or browser tabs "
                "would help.",
            ),
        ):
            self._high[name] = self._high.get(name, 0) + 1 if value >= self.busy else 0
            last = self._last.get(name)
            if self._high[name] >= self.sustained and (last is None or now - last >= self.repeat):
                self._last[name] = now
                said.append(Said(line, "hardware"))
        return said

    def _battery(self, reading: Reading) -> list[Said]:
        level = reading.battery
        if level is None or reading.plugged:
            self._battery_said = None
            return []
        critical = max(5, self.battery_low // 2)
        if level <= critical and (self._battery_said is None or self._battery_said > critical):
            self._battery_said = level
            return [Said(f"Battery's at {level}%. I'd plug in now, or save your work.", "hardware")]
        if level <= self.battery_low and self._battery_said is None:
            self._battery_said = level
            return [Said(f"Battery's down to {level}%; you may want to plug in.", "hardware")]
        return []


# --- check-ins ---

_NUDGES = (
    "You've been at it for {hours} without a break. A short pause might be in order.",
    "{Hours} at the screen. Perhaps a glass of water and a stretch?",
    "A gentle word: it's been {hours} without a break. Your eyes would appreciate one.",
)


@dataclass
class CheckIns:
    hours: float = 2.0
    idle: Callable[[], float] = idle_seconds
    quiet_hours: tuple[int, int] = (22, 8)
    break_s: float = 300.0  # this long away from the PC counts as a break
    _since: datetime | None = None
    _count: int = 0

    async def tick(self, now: datetime) -> list[Said]:
        if self.hours <= 0:
            return []
        if self.idle() >= self.break_s or self._since is None:
            self._since = now
            return []
        if now - self._since < timedelta(hours=self.hours) or quiet(now, *self.quiet_hours):
            return []
        self._since = now
        hours = round(self.hours * 2) / 2
        spoken = f"{_number(int(hours))} hours" if hours == int(hours) else f"{hours} hours"
        line = _NUDGES[self._count % len(_NUDGES)]
        self._count += 1
        return [Said(line.format(hours=spoken, Hours=spoken.capitalize()), "check-in")]


# --- session summaries ---


@dataclass
class SessionSummaries:
    history: HistoryStore
    keep: Keep
    summarize: Callable[[str], Awaitable[str]]
    gap: timedelta = timedelta(minutes=15)  # this quiet = the session is over
    _next: datetime | None = None

    async def tick(self, now: datetime) -> list[Said]:
        if self._next is not None and now < self._next:
            return []
        self._next = now + timedelta(minutes=1)
        mark = await self.keep.get_state("summarised_upto")
        if mark is None:  # first run: earlier history isn't summarised after the fact
            await self.keep.set_state("summarised_upto", str(await self.history.last_message_id()))
            return []
        messages = await self.history.messages_after(int(mark), 200)
        if not messages:
            return []
        ended = datetime.fromisoformat(messages[-1].created_at)
        if now.astimezone(UTC) - ended.astimezone(UTC) < self.gap:
            return []
        upto = str(messages[-1].id)
        said = [m for m in messages if m.role in ("user", "assistant") and m.content.strip()]
        if sum(m.role == "user" for m in said) < 2:
            await self.keep.set_state("summarised_upto", upto)
            return []
        transcript = "\n".join(
            f"{'User' if m.role == 'user' else 'JARVIS'}: {m.content.strip()[:300]}" for m in said
        )[-6000:]
        try:
            summary = (await self.summarize(transcript)).strip()
        except Exception as exc:
            logger.info("Session summary postponed: {}", type(exc).__name__)
            self._next = now + timedelta(minutes=30)
            return []
        if summary:
            day = datetime.fromisoformat(said[0].created_at).astimezone().date().isoformat()
            await self.keep.add_summary(messages[-1].id, day, summary[:400])
        await self.keep.set_state("summarised_upto", upto)
        return []
