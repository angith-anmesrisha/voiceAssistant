"""Your day at a glance, for the Daily Planner ("plan my day"), the End-of-Day Review
("review my day") and calendar questions ("what's on my calendar tomorrow?").

``my_day`` gathers what's known on this PC (tasks due, overdue and pending; reminders;
calendar events if a calendar is connected; for a review, what got done and what the
day's conversations were about) and the AI turns it into a plan or a review. Nothing is
changed. Calendar titles come from outside (invites), so the result is screened.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Any

from jarvis.storage.keep import Keep
from jarvis.tools.calendar_ics import ENV as CALENDAR_ENV
from jarvis.tools.calendar_ics import Calendar, CalendarError
from jarvis.tools.registry import Risk, ToolRegistry, ToolResult, ToolSpec
from jarvis.tools.tasks import DueError, parse_due, task_line


def _day(when: str, now: datetime) -> date:
    when = (when or "today").strip().lower()
    if when in ("today", "now", ""):
        return now.date()
    if when == "yesterday":
        return now.date() - timedelta(days=1)
    try:
        parsed = parse_due(when, now)
    except DueError:
        return now.date()
    return parsed.date() if parsed else now.date()


async def day_material(
    keep: Keep,
    calendar: Calendar | None,
    day: date,
    *,
    review: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    """What JARVIS knows about ``day``, as lists of short lines."""
    now = now or datetime.now().astimezone()
    tz = now.tzinfo
    start = datetime.combine(day, time(), tz)
    end = start + timedelta(days=1)
    pending = await keep.tasks(kind="task", done=False)
    overdue = [t for t in pending if t.due is not None and t.due < start.timestamp()]
    due = [t for t in pending if t.due is not None and start.timestamp() <= t.due < end.timestamp()]
    soon = [
        t
        for t in pending
        if t.due is not None and end.timestamp() <= t.due < (end + timedelta(days=6)).timestamp()
    ]
    undated = [t for t in pending if t.due is None]
    reminders = [
        f"{datetime.fromtimestamp(r.due, tz):%H:%M}: {r.text}"
        for r in await keep.reminders()
        if start.timestamp() <= r.due < end.timestamp()
    ]
    material: dict[str, Any] = {"day": f"{day:%A %d %B %Y}"}
    if calendar is not None and calendar.available:
        try:
            events = await calendar.events(start, end)
            material["calendar"] = [e.describe() for e in events] or ["Nothing scheduled."]
        except CalendarError as exc:
            material["calendar"] = [f"Calendar unavailable: {exc}."]
    else:
        material["calendar"] = [f"No calendar connected (set {CALENDAR_ENV})."]
    material["overdue"] = [task_line(t) for t in overdue[:15]]
    material["due_that_day"] = [task_line(t) for t in due[:15]]
    material["due_this_week"] = [task_line(t) for t in soon[:10]]
    material["other_pending"] = [task_line(t) for t in undated[:15]]
    material["reminders"] = reminders
    if review:
        changed = await keep.tasks_between(start.timestamp(), end.timestamp())
        material["done_that_day"] = [
            task_line(t) for t in changed if t.done and start.timestamp() <= t.done
        ][:20]
        material["added_that_day"] = [
            task_line(t) for t in changed if not t.done and t.created >= start.timestamp()
        ][:20]
        material["conversations"] = [
            text for _day_, text in await keep.summaries_between(day.isoformat(), day.isoformat())
        ][:10]
    return {k: v for k, v in material.items() if v != []}


def register_day_tools(registry: ToolRegistry, keep: Keep, calendar: Calendar | None) -> None:
    async def my_day(when: str = "today", review: bool = False) -> ToolResult:
        now = datetime.now().astimezone()
        day = _day(when, now)
        material = await day_material(keep, calendar, day, review=review, now=now)
        what = "review" if review else "plan"
        return ToolResult(True, f"Material to {what} {material['day']}.", material)

    async def calendar_events(start: str = "today", days: int = 1) -> ToolResult:
        if calendar is None or not calendar.available:
            return ToolResult(
                False,
                f"No calendar is connected. Set {CALENDAR_ENV} to the calendar's secret "
                "iCal address (read-only).",
            )
        now = datetime.now().astimezone()
        first = _day(start, now)
        days = max(1, min(31, days))
        begin = datetime.combine(first, time(), now.tzinfo)
        try:
            events = await calendar.events(begin, begin + timedelta(days=days))
        except CalendarError as exc:
            return ToolResult(False, f"Couldn't read the calendar: {exc}.")
        lines = [f"{e.start:%a %d %b}: {e.describe()}" for e in events[:60]]
        span = f"{first:%A %d %B}" + (f" and the {days - 1} days after" if days > 1 else "")
        if not lines:
            return ToolResult(True, f"Nothing on the calendar for {span}.")
        return ToolResult(True, f"{len(lines)} events for {span}.", {"events": lines})

    registry.register(
        ToolSpec(
            "my_day",
            "Everything known about a day (calendar, tasks due and overdue, reminders; with "
            "review=true also what got done and what was discussed). Use it to plan the "
            "user's day or review it. when: 'today', 'tomorrow', 'yesterday', 'friday'.",
            {
                "type": "object",
                "properties": {
                    "when": {"type": "string", "maxLength": 30},
                    "review": {"type": "boolean"},
                },
                "required": [],
            },
            my_day,
            Risk.LOW,
            lambda a: "Look over the day",
            group="tasks",
        )
    )
    registry.register(
        ToolSpec(
            "calendar_events",
            "Read the user's calendar (read-only): events from start ('today', 'tomorrow', "
            "'monday', '2026-11-02') for a number of days.",
            {
                "type": "object",
                "properties": {
                    "start": {"type": "string", "maxLength": 30},
                    "days": {"type": "integer", "minimum": 1, "maximum": 31},
                },
                "required": [],
            },
            calendar_events,
            Risk.LOW,
            lambda a: "Read the calendar",
            group="tasks",
        )
    )
