"""Tasks and ideas: a small to-do list kept on this PC (keep.sqlite3).

Used by the workflows ("show my pending study tasks"), the daily planner and day review,
quick capture ("save this as a project idea") and Watchtower's deadline alerts.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta

from jarvis.storage.keep import Keep, Task
from jarvis.tools.registry import Risk, ToolRegistry, ToolResult, ToolSpec

_DAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")


class DueError(ValueError):
    pass


def parse_due(text: str, now: datetime | None = None) -> datetime | None:
    """A due date from "tomorrow", "friday 5 pm", "in 3 days", "2026-10-30 17:00", "26
    oct". None for "". A date without a time is due at the end of that day (23:59)."""
    now = now or datetime.now().astimezone()
    raw = " ".join(text.lower().replace(",", " ").split())
    if not raw:
        return None
    time_match = re.search(
        r"\b(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b|\b(\d{1,2}):(\d{2})\b", raw
    )
    hour, minute = 23, 59
    if time_match:
        if time_match.group(1):
            hour, minute = int(time_match.group(1)), int(time_match.group(2) or 0)
            if time_match.group(3) == "pm" and hour < 12:
                hour += 12
            if time_match.group(3) == "am" and hour == 12:
                hour = 0
        else:
            hour, minute = int(time_match.group(4)), int(time_match.group(5))
        raw = (raw[: time_match.start()] + raw[time_match.end() :]).strip()
    if hour > 23 or minute > 59:
        raise DueError(f"“{text}” has no valid time")
    day = now.date()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}(t.*)?", raw):
        try:
            full = datetime.fromisoformat(raw.upper().replace("T", " ") if "t" in raw else raw)
        except ValueError as exc:
            raise DueError(f"I couldn't read the date “{text}”") from exc
        day = full.date()
        if "t" in raw:
            hour, minute = full.hour, full.minute
    elif raw in ("", "today", "tonight"):
        pass
    elif raw == "tomorrow":
        day = day + timedelta(days=1)
    elif match := re.fullmatch(r"in (\d+) (day|days|week|weeks)", raw):
        count = int(match.group(1)) * (7 if match.group(2).startswith("week") else 1)
        day = day + timedelta(days=count)
    elif match := re.fullmatch(r"(?:next |this |on )?(" + "|".join(_DAYS) + ")", raw):
        ahead = (_DAYS.index(match.group(1)) - day.weekday()) % 7 or 7
        day = day + timedelta(days=ahead)
    elif match := re.fullmatch(r"(\d{1,2}) ([a-z]{3})[a-z]*|([a-z]{3})[a-z]* (\d{1,2})", raw):
        number = int(match.group(1) or match.group(4))
        month_name = match.group(2) or match.group(3)
        try:
            month = datetime.strptime(month_name, "%b").month
            candidate = day.replace(month=month, day=number)
        except ValueError as exc:
            raise DueError(f"I couldn't read the date “{text}”") from exc
        day = candidate if candidate >= day else candidate.replace(year=day.year + 1)
    else:
        raise DueError(f"I couldn't read the date “{text}”")
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=now.tzinfo)


def describe_due(due: float | None, now: datetime | None = None) -> str:
    if due is None:
        return ""
    now = now or datetime.now().astimezone()
    when = datetime.fromtimestamp(due).astimezone()
    clock = "" if (when.hour, when.minute) == (23, 59) else f" at {when:%H:%M}"
    days = (when.date() - now.date()).days
    if days < 0:
        return f"overdue since {when:%a %d %b}{clock}"
    if days == 0:
        return f"due today{clock}"
    if days == 1:
        return f"due tomorrow{clock}"
    return f"due {when:%A %d %B}{clock}"


def task_line(task: Task) -> str:
    parts = [f"{task.id}. {task.text}"]
    if task.project:
        parts.append(f"[{task.project}]")
    if due := describe_due(task.due):
        parts.append(f"({due})")
    if task.done:
        parts.append("✓ done")
    return " ".join(parts)


def register_task_tools(registry: ToolRegistry, keep: Keep) -> None:
    async def add_task(
        text: str, kind: str = "task", project: str = "", due: str = ""
    ) -> ToolResult:
        try:
            when = parse_due(due)
        except DueError as exc:
            return ToolResult(False, f"Couldn't add it: {exc}.")
        task = await keep.add_task(
            text, kind=kind, project=project, due=when.timestamp() if when else None
        )
        noun = "Idea" if task.kind == "idea" else "Task"
        extra = f", {describe_due(task.due)}" if task.due else ""
        where = f" under {task.project}" if task.project else ""
        return ToolResult(
            True, f"{noun} {task.id} saved{where}{extra}: {task.text}", {"id": task.id}
        )

    async def list_tasks(
        project: str = "", kind: str = "task", include_done: bool = False
    ) -> ToolResult:
        found = await keep.tasks(
            kind=kind if kind in ("task", "idea") else None,
            project=project,
            done=None if include_done else False,
        )
        noun = "ideas" if kind == "idea" else "tasks"
        if not found:
            where = f" for {project}" if project else ""
            return ToolResult(True, f"No {'saved' if kind == 'idea' else 'pending'} {noun}{where}.")
        lines = [task_line(t) for t in found[:40]]
        return ToolResult(True, f"{len(found)} {noun}.", {noun: lines})

    async def complete_task(id: int = 0, text: str = "") -> ToolResult:
        task_id = id
        if not task_id and text.strip():
            words = set(re.findall(r"\w+", text.lower()))
            candidates = [
                (len(words & set(re.findall(r"\w+", t.text.lower()))), t)
                for t in await keep.tasks(kind=None)
            ]
            candidates = [c for c in candidates if c[0]]
            if candidates:
                task_id = max(candidates, key=lambda c: c[0])[1].id
        if not task_id:
            return ToolResult(False, "Which task? Give its number or some of its words.")
        task = await keep.finish_task(task_id)
        if task is None:
            return ToolResult(False, f"There's no task {task_id}.")
        return ToolResult(True, f"Marked done: {task.text}", {"id": task.id})

    registry.register(
        ToolSpec(
            "add_task",
            "Save a task (kind 'task', optional project and due date such as 'tomorrow', "
            "'friday 5 pm', '2026-10-30') or an idea (kind 'idea') to the user's list.",
            {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "maxLength": 400},
                    "kind": {"type": "string", "enum": ["task", "idea"]},
                    "project": {"type": "string", "maxLength": 60},
                    "due": {"type": "string", "maxLength": 40},
                },
                "required": ["text"],
            },
            add_task,
            Risk.LOW,
            lambda a: (
                f"Save {'an idea' if a.get('kind') == 'idea' else 'a task'}: {a.get('text', '')}"
            ),
            group="tasks",
        )
    )
    registry.register(
        ToolSpec(
            "list_tasks",
            "List pending tasks (or saved ideas with kind 'idea'), optionally for one project.",
            {
                "type": "object",
                "properties": {
                    "project": {"type": "string", "maxLength": 60},
                    "kind": {"type": "string", "enum": ["task", "idea"]},
                    "include_done": {"type": "boolean"},
                },
                "required": [],
            },
            list_tasks,
            Risk.LOW,
            lambda a: "List tasks",
            group="tasks",
        )
    )
    registry.register(
        ToolSpec(
            "complete_task",
            "Mark a task done, by its number or some of its words.",
            {
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "text": {"type": "string", "maxLength": 200},
                },
                "required": [],
            },
            complete_task,
            Risk.LOW,
            lambda a: f"Mark a task done: {a.get('text') or a.get('id')}",
            group="tasks",
        )
    )
