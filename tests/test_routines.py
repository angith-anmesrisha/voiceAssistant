"""Morning briefing, watched topics, hardware alerts, check-ins and session summaries."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from jarvis.agent.background import Background
from jarvis.agent.routines import (
    Briefing,
    CheckIns,
    HardwareWatch,
    Reading,
    Said,
    SessionSummaries,
    TopicWatch,
    quiet,
)
from jarvis.app.events import Announce, EventBus
from jarvis.storage.db import HistoryStore
from jarvis.storage.keep import Keep
from jarvis.tools.web import DayForecast, Forecast, Headline, WebError

TZ = datetime(2026, 10, 24, 9, 5).astimezone().tzinfo
MORNING = datetime(2026, 10, 24, 9, 5, tzinfo=TZ)


@pytest.fixture
def keep(tmp_path: Path) -> Keep:
    return Keep(tmp_path / "keep.sqlite3")


def headline(title: str, source: str = "BBC") -> Headline:
    return Headline(title, source, "", "")


KOCHI = Forecast(
    "Kochi, India", "light rain", 27, 31, 80, 10, (DayForecast("Today", "rain", 24, 30, 70),)
)


@pytest.mark.parametrize(
    ("hour", "expected"), [(23, True), (2, True), (8, False), (12, False), (22, True)]
)
def test_quiet_hours_wrap_past_midnight(hour: int, expected: bool) -> None:
    assert quiet(MORNING.replace(hour=hour), 22, 8) is expected
    assert not quiet(MORNING.replace(hour=hour), 0, 0)


async def test_briefing_once_a_morning_when_someone_is_there(keep: Keep) -> None:
    async def weather() -> Forecast:
        return KOCHI

    async def news() -> list[Headline]:
        return [headline("Rain due in Kerala."), headline("Markets rise"), headline("New phone")]

    idle = [600.0]
    await keep.add_reminder((MORNING + timedelta(hours=1)).timestamp(), "call mom")
    await keep.add_reminder((MORNING + timedelta(hours=3)).timestamp(), "gym")
    await keep.add_summary(7, "2026-10-23", "You had me tidy your desktop.")
    briefing = Briefing(keep, weather, news, idle=lambda: idle[0])
    assert await briefing.tick(MORNING) == []  # away from the PC: wait
    idle[0] = 3.0
    assert await briefing.tick(MORNING.replace(hour=13)) == []  # afternoon: no briefing
    [said] = await briefing.tick(MORNING)
    assert said.kind == "briefing"
    assert said.text.splitlines() == [
        "Good morning. It's Saturday 24 October, 9:05.",
        "In Kochi it's 27°C and light rain; today 24 to 30°C with a 70% chance of rain.",
        "Two reminders today; the first at 10:05: call mom.",
        "From last time: You had me tidy your desktop.",
        "In the news: Rain due in Kerala; Markets rise; and New phone.",
    ]
    assert await briefing.tick(MORNING + timedelta(minutes=5)) == []
    again = Briefing(keep, weather, news, idle=lambda: 0.0)  # after a restart
    assert await again.tick(MORNING + timedelta(minutes=10)) == []


async def test_briefing_copes_without_internet(keep: Keep) -> None:
    async def offline() -> Forecast:
        raise WebError("no connection to the site")

    async def no_news() -> list[Headline]:
        raise WebError("no connection to the site")

    [said] = await Briefing(keep, offline, no_news, idle=lambda: 0.0).tick(MORNING)
    assert said.text == "Good morning. It's Saturday 24 October, 9:05."


async def test_topics_report_only_new_headlines(keep: Keep) -> None:
    feed = [[headline("SpaceX tests Starship")]]

    async def fetch(topic: str) -> list[Headline]:
        return feed[0]

    await keep.watch("SpaceX")
    watch = TopicWatch(keep, fetch)
    assert await watch.tick(MORNING) == []  # first look only notes what's there
    feed[0] = [headline("Starship reaches orbit", "Reuters")]
    assert await watch.tick(MORNING + timedelta(hours=1)) == []  # checked daily
    later = MORNING + timedelta(days=1)
    assert await watch.tick(later) == [
        Said("News on SpaceX: Starship reaches orbit (Reuters).", "news")
    ]
    night = MORNING + timedelta(days=3)
    assert await watch.tick(night.replace(hour=23)) == []  # quiet hours


async def test_hardware_alerts_are_not_repeated() -> None:
    readings = [Reading(19, False, 10, 40)]
    watch = HardwareWatch(read=lambda: readings[0], sustained=3)
    assert [s.text for s in await watch.tick(MORNING)] == [
        "Battery's down to 19%; you may want to plug in."
    ]
    assert await watch.tick(MORNING) == []
    readings[0] = Reading(9, False, 10, 40)
    assert "plug in now" in (await watch.tick(MORNING))[0].text
    readings[0] = Reading(8, True, 10, 40)
    assert await watch.tick(MORNING) == []  # plugged in: resets
    readings[0] = Reading(None, True, 99, 40)
    said = [await watch.tick(MORNING + timedelta(seconds=10 * i)) for i in range(4)]
    assert [len(s) for s in said] == [0, 0, 1, 0]
    assert "processor" in said[2][0].text


async def test_check_ins_after_long_stretches_only() -> None:
    idle = [5.0]
    check = CheckIns(2.0, idle=lambda: idle[0])
    start = MORNING.replace(hour=10)
    assert await check.tick(start) == []
    assert await check.tick(start + timedelta(hours=1)) == []
    [nudge] = await check.tick(start + timedelta(hours=2))
    assert nudge.text.startswith("You've been at it for two hours") and nudge.kind == "check-in"
    idle[0] = 900  # a real break
    assert await check.tick(start + timedelta(hours=3)) == []
    idle[0] = 5
    assert await check.tick(start + timedelta(hours=4, minutes=59)) == []
    assert await check.tick(start.replace(hour=23) + timedelta(hours=3)) == []
    assert await CheckIns(0).tick(start) == []


async def test_sessions_are_summarised_after_a_pause(tmp_path: Path, keep: Keep) -> None:
    history = HistoryStore(tmp_path / "history.sqlite3")
    conversation = await history.new_conversation()
    await history.add_message(conversation, {"role": "user", "content": "old chat"})
    asked: list[str] = []

    async def summarize(transcript: str) -> str:
        asked.append(transcript)
        return "You had me open Spotify and set a reminder."

    job = SessionSummaries(history, keep, summarize, gap=timedelta(0))
    now = datetime.now(UTC)
    assert await job.tick(now) == [] and asked == []  # old history isn't summarised
    for role, content in [
        ("user", "open spotify"),
        ("assistant", "Opening Spotify now."),
        ("user", "remind me at 6 to call mom"),
        ("assistant", "Done."),
    ]:
        await history.add_message(conversation, {"role": role, "content": content})
    later = now + timedelta(minutes=2)
    assert await job.tick(later) == []
    assert asked[0].splitlines()[0] == "User: open spotify"
    tomorrow = (datetime.now().astimezone() + timedelta(days=1)).date().isoformat()
    assert await keep.unmentioned_summaries(tomorrow) == [
        "You had me open Spotify and set a reminder."
    ]
    assert await job.tick(later + timedelta(minutes=2)) == [] and len(asked) == 1


async def test_background_runs_every_job_even_if_one_breaks(keep: Keep) -> None:
    class Broken:
        async def tick(self, now: datetime) -> list[Said]:
            raise RuntimeError("boom")

    class Talks:
        async def tick(self, now: datetime) -> list[Said]:
            return [Said("Battery's down to 19%; you may want to plug in.", "hardware")]

    bus = EventBus()
    heard: list[Announce] = []

    async def on_announce(event: Announce) -> None:
        heard.append(event)

    bus.subscribe(Announce, on_announce)
    await Background(bus, keep, jobs=[Broken(), Talks()]).tick()
    assert [(a.text, a.kind) for a in heard] == [
        ("Battery's down to 19%; you may want to plug in.", "hardware")
    ]
