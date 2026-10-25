"""The calendar (read-only iCal), repeating reminders, and the material for planning
and reviewing a day, including the briefing's calendar and task lines."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
import pytest

from jarvis.agent.background import Background
from jarvis.agent.routines import Briefing
from jarvis.agent.workflows import WorkflowStore, starters
from jarvis.app.events import Announce, EventBus
from jarvis.storage.keep import Keep, next_occurrence
from jarvis.tools.calendar_ics import Calendar, events_between, parse
from jarvis.tools.day import day_material
from jarvis.tools.web import Forecast, Headline

IST = timezone(timedelta(hours=5, minutes=30))

FEED = """BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:one
SUMMARY:Dentist
LOCATION:MG Road
DTSTART;TZID=Asia/Kolkata:20261026T100000
DTEND;TZID=Asia/Kolkata:20261026T103000
END:VEVENT
BEGIN:VEVENT
UID:standup
SUMMARY:Stand-up
DTSTART:20261001T033000Z
DTEND:20261001T034500Z
RRULE:FREQ=WEEKLY;BYDAY=MO,WE,FR;UNTIL=20261231T000000Z
EXDATE:20261028T033000Z
END:VEVENT
BEGIN:VEVENT
UID:standup
RECURRENCE-ID:20261030T033000Z
SUMMARY:Stand-up (moved)
DTSTART:20261030T060000Z
DTEND:20261030T061500Z
END:VEVENT
BEGIN:VEVENT
UID:holiday
SUMMARY:Diwali
DTSTART;VALUE=DATE:20261108
DTEND;VALUE=DATE:20261109
END:VEVENT
BEGIN:VEVENT
UID:club
SUMMARY:Book club\\, monthly
DTSTART;TZID=Asia/Kolkata:20260908T190000
DTEND;TZID=Asia/Kolkata:20260908T200000
RRULE:FREQ=MONTHLY;BYDAY=2TU;COUNT=6
END:VEVENT
BEGIN:VEVENT
UID:gone
SUMMARY:Cancelled thing
STATUS:CANCELLED
DTSTART:20261026T080000Z
END:VEVENT
END:VCALENDAR
"""


def day(d: date) -> tuple[datetime, datetime]:
    start = datetime(d.year, d.month, d.day, tzinfo=IST)
    return start, start + timedelta(days=1)


def titles(d: date) -> list[str]:
    return [e.describe() for e in events_between(parse(FEED, IST), *day(d))]


def test_single_and_weekly_events() -> None:
    assert titles(date(2026, 10, 26)) == [
        "9 AM to 9:15 AM: Stand-up",
        "10 AM to 10:30 AM: Dentist at MG Road",
    ]
    assert titles(date(2026, 10, 27)) == []  # Tuesday: no stand-up
    assert titles(date(2026, 10, 28)) == []  # skipped (EXDATE)
    assert titles(date(2026, 10, 30)) == ["11:30 AM to 11:45 AM: Stand-up (moved)"]


def test_all_day_and_monthly_by_weekday() -> None:
    assert titles(date(2026, 11, 8)) == ["all day: Diwali"]
    assert titles(date(2026, 11, 10)) == ["7 PM to 8 PM: Book club, monthly"]  # 2nd Tuesday
    assert titles(date(2026, 11, 3)) == []
    assert titles(date(2027, 3, 9)) == []  # COUNT=6 ran out in February


def test_until_ends_a_repeat() -> None:
    assert titles(date(2027, 1, 4)) == []


async def test_calendar_reads_the_feed_once_and_caches() -> None:
    hits = []

    def answer(request: httpx.Request) -> httpx.Response:
        hits.append(str(request.url))
        return httpx.Response(200, text=FEED)

    client = httpx.AsyncClient(transport=httpx.MockTransport(answer))
    calendar = Calendar(client, ["webcal://example.com/private.ics"])
    first = await calendar.events(*day(date(2026, 10, 26)))
    await calendar.events(*day(date(2026, 10, 27)))
    assert [e.title for e in first] == ["Stand-up", "Dentist"]
    assert hits == ["https://example.com/private.ics"]
    await client.aclose()


@pytest.mark.parametrize(
    ("repeat", "expected"),
    [
        ("daily", datetime(2026, 10, 27, 9, 0)),
        ("weekdays", datetime(2026, 11, 2, 9, 0)),  # Friday → Monday
        ("weekly", datetime(2026, 11, 6, 9, 0)),
        ("monthly", datetime(2026, 11, 30, 9, 0)),
        ("", None),
    ],
)
def test_next_occurrence(repeat: str, expected: datetime | None) -> None:
    due = datetime(2026, 10, 30, 9, 0).astimezone().timestamp()  # a Friday
    if repeat == "daily":
        due = datetime(2026, 10, 26, 9, 0).astimezone().timestamp()
    now = due + 60
    found = next_occurrence(due, repeat, now)
    want = expected.astimezone().timestamp() if expected else None
    assert found == want


async def test_a_repeating_reminder_comes_back(tmp_path: Path) -> None:
    keep = Keep(tmp_path / "keep.sqlite3")
    clock = [datetime(2026, 10, 26, 9, 0).astimezone().timestamp()]
    await keep.add_reminder(clock[0] - 5, "Take vitamins", "daily")
    said: list[str] = []
    bus = EventBus()

    async def heard(event: Announce) -> None:
        said.append(event.text)

    bus.subscribe(Announce, heard)
    background = Background(bus, keep, clock=lambda: clock[0])
    assert await background.deliver_reminders() == 1
    [following] = await keep.reminders()
    assert following.repeat == "daily" and following.due == clock[0] - 5 + 86400
    assert said == ["Reminder: Take vitamins"]
    keep.close()


async def test_day_material_for_planning_and_review(tmp_path: Path) -> None:
    keep = Keep(tmp_path / "keep.sqlite3")
    now = datetime(2026, 10, 26, 8, 0, tzinfo=IST)
    today = now.replace(hour=17).timestamp()
    late = await keep.add_task("Submit form", due=(now - timedelta(days=2)).timestamp())
    await keep.add_task("Revise chapter 4", project="Study", due=today)
    await keep.add_task("Buy cables")
    done = await keep.add_task("Email Priya")
    await keep.finish_task(done.id)
    await keep.add_summary(3, "2026-10-26", "We planned the trip to Munnar.")
    material = await day_material(keep, None, now.date(), review=True, now=now)
    assert material["day"] == "Monday 26 October 2026"
    assert material["overdue"][0].startswith(f"{late.id}. Submit form")
    assert "Revise chapter 4 [Study]" in material["due_that_day"][0]
    assert material["other_pending"] == ["3. Buy cables"]
    assert material["calendar"][0].startswith("No calendar connected")
    assert material["conversations"] == ["We planned the trip to Munnar."]
    keep.close()


async def test_briefing_has_calendar_and_tasks(tmp_path: Path) -> None:
    keep = Keep(tmp_path / "keep.sqlite3")
    morning = datetime(2026, 10, 26, 8, 0).astimezone()
    await keep.add_task("Revise chapter 4", due=morning.replace(hour=17).timestamp())
    await keep.add_task("Submit form", due=(morning - timedelta(days=1)).timestamp())

    class OneEvent:
        available = True

        async def day(self, d: date) -> list[Any]:
            from jarvis.tools.calendar_ics import Event

            start = morning.replace(hour=10)
            return [Event("Dentist", start, start + timedelta(minutes=30))]

    async def weather() -> Forecast | None:
        return None

    async def news() -> list[Headline]:
        return []

    briefing = Briefing(keep, weather, news, idle=lambda: 0.0, calendar=OneEvent())  # type: ignore[arg-type]
    text = await briefing.compose(morning)
    assert "On your calendar, one event: 10 AM to 10:30 AM: Dentist." in text
    assert "Due today: Revise chapter 4." in text
    assert "One task is overdue." in text
    keep.close()


def test_starter_workflows_answer_everyday_phrases(tmp_path: Path) -> None:
    store = WorkflowStore(tmp_path / "w.json")
    for workflow in starters():
        store.put(workflow)
    assert store.match("plan my day").name == "Daily Planner"  # type: ignore[union-attr]
    assert store.match("Jarvis, review my day").name == "End-of-Day Review"  # type: ignore[union-attr]
    assert store.match("brief me").name == "Morning Briefing"  # type: ignore[union-attr]
