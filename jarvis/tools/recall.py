"""Recall: "continue the project I worked on yesterday", "where did I leave off?".

``recall_work`` gathers what this PC knows about a stretch of time (and a project, if
named): the day notes and conversation summaries in the memory folder, what you asked
JARVIS, tasks added, finished and still open, and the files you changed in your own
folders (names only; files are listed, never read). The AI then sums up where things
stand and suggests the next step. Nothing leaves the PC except what goes to the AI
with that request, and the result is screened like other outside text.
"""

from __future__ import annotations

import asyncio
import os
import re
from collections.abc import Callable
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

from jarvis.storage.db import HistoryStore
from jarvis.storage.keep import Keep
from jarvis.storage.memory import Memory
from jarvis.tools.registry import Risk, ToolRegistry, ToolResult, ToolSpec
from jarvis.tools.tasks import task_line

_WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
_SKIP_DIRS = {
    "node_modules", ".git", "__pycache__", ".venv", "venv", "appdata", "$recycle.bin",
    "site-packages", ".cache", "build", "dist", ".idea", ".vscode",
}  # fmt: skip
_MAX_VISITS = 6000


def period(when: str, now: datetime) -> tuple[datetime, datetime, str]:
    """(start, end, label) for "yesterday", "today", "last week", "monday", "last 3 days",
    "2026-10-20"; "" = the last seven days."""
    text = " ".join(when.lower().split())
    today = now.date()
    tz = now.tzinfo

    def day_span(first: date, days: int = 1) -> tuple[datetime, datetime]:
        start = datetime.combine(first, time(), tz)
        return start, start + timedelta(days=days)

    if text in ("", "recently", "lately", "this week so far"):
        start, end = day_span(today - timedelta(days=6), 7)
        return start, end, "the last seven days"
    if text == "today":
        return (*day_span(today), "today")
    if text in ("yesterday", "last time", "last session"):
        if text == "yesterday":
            return (*day_span(today - timedelta(days=1)), "yesterday")
        start, end = day_span(today - timedelta(days=6), 7)
        return start, end, "the last seven days"
    if text in ("this week",):
        monday = today - timedelta(days=today.weekday())
        return (*day_span(monday, (today - monday).days + 1), "this week")
    if text in ("last week", "past week"):
        monday = today - timedelta(days=today.weekday() + 7)
        return (*day_span(monday, 7), "last week")
    if match := re.fullmatch(r"(?:the )?(?:last|past) (\d{1,2}) days?", text):
        days = max(1, min(31, int(match.group(1))))
        return (*day_span(today - timedelta(days=days - 1), days), f"the last {days} days")
    if match := re.fullmatch(r"(?:on |last )?(" + "|".join(_WEEKDAYS) + ")", text):
        back = (today.weekday() - _WEEKDAYS.index(match.group(1))) % 7 or 7
        first = today - timedelta(days=back)
        return (*day_span(first), f"{first:%A %d %B}")
    try:
        first = date.fromisoformat(text)
    except ValueError:
        start, end = day_span(today - timedelta(days=6), 7)
        return start, end, "the last seven days"
    return (*day_span(first), f"{first:%A %d %B}")


def recent_files(
    folders: list[Path], start: datetime, end: datetime, words: set[str], limit: int = 15
) -> list[str]:
    """Files changed between ``start`` and ``end`` under ``folders`` (names and times
    only), those matching ``words`` first."""
    low, high = start.timestamp(), end.timestamp()
    found: list[tuple[bool, float, Path]] = []
    visits = 0
    for folder in folders:
        for root, dirs, files in os.walk(folder):
            visits += 1
            if visits > _MAX_VISITS:
                break
            here = Path(root)
            if len(here.relative_to(folder).parts) >= 4:
                dirs[:] = []
            dirs[:] = [
                d for d in dirs if not d.startswith((".", "~")) and d.lower() not in _SKIP_DIRS
            ]
            for name in files:
                if name.startswith(("~$", ".")) or name.lower().endswith((".tmp", ".lnk", ".ini")):
                    continue
                path = here / name
                try:
                    modified = path.stat().st_mtime
                except OSError:
                    continue
                if low <= modified < high:
                    text = str(path).lower()
                    found.append((bool(words) and any(w in text for w in words), modified, path))
    found.sort(key=lambda item: (not item[0], -item[1]))
    tz = start.tzinfo
    return [
        f"{datetime.fromtimestamp(modified, tz):%a %d %b %H:%M} {path}"
        for _match, modified, path in found[:limit]
    ]


async def work_material(
    keep: Keep,
    history: HistoryStore | None,
    memory: Memory | None,
    folders: list[Path],
    when: str = "",
    project: str = "",
    now: datetime | None = None,
) -> dict[str, Any]:
    now = now or datetime.now().astimezone()
    start, end, label = period(when, now)
    words = {w for w in re.findall(r"\w+", project.lower()) if len(w) > 2}
    material: dict[str, Any] = {"period": label}
    if project:
        material["project"] = project

    first_day, last_day = start.date(), (end - timedelta(seconds=1)).date()
    summaries = await keep.summaries_between(first_day.isoformat(), last_day.isoformat())
    material["conversation_summaries"] = [f"{day}: {text}" for day, text in summaries][-12:]

    if history is not None:
        asked = await history.messages_between(start, end)
        requests = [" ".join(m.content.split())[:200] for m in asked if m.content.strip()]
        if words:
            matching = [r for r in requests if any(w in r.lower() for w in words)]
            requests = matching or requests
        material["what_you_asked"] = requests[-30:]

    changed = await keep.tasks_between(start.timestamp(), end.timestamp())
    material["tasks_finished"] = [task_line(t) for t in changed if t.done][:15]
    material["tasks_added"] = [task_line(t) for t in changed if not t.done][:15]
    open_tasks = await keep.tasks(kind=None, done=False)
    if words:
        open_tasks = [
            t
            for t in open_tasks
            if words & set(re.findall(r"\w+", f"{t.text} {t.project}".lower()))
        ]
    material["still_open"] = [f"{t.kind}: {task_line(t)}" for t in open_tasks[:15]]

    if memory is not None:
        notes: list[str] = []
        day = last_day
        while day >= first_day and len(notes) < 3:
            text = await asyncio.to_thread(memory.vault.daily_text, day)
            if text.strip():
                notes.append(f"{day:%A %d %B}:\n{text[-2500:]}")
            day -= timedelta(days=1)
        material["day_notes"] = notes
        if project:
            hits = await memory.search(project, limit=5)
            material["notes_about_project"] = [
                f"[{h.note}] {' '.join(h.text.split())[:400]}" for h in hits
            ]

    material["files_changed"] = await asyncio.to_thread(recent_files, folders, start, end, words)
    return {k: v for k, v in material.items() if v not in ([], "")}


def register_recall_tool(
    registry: ToolRegistry,
    keep: Keep,
    history: HistoryStore | None,
    memory: Memory | None,
    folders: Callable[[], list[Path]],
) -> None:
    async def recall_work(when: str = "", project: str = "") -> ToolResult:
        material = await work_material(keep, history, memory, folders(), when, project)
        found = sum(len(v) for v in material.values() if isinstance(v, list))
        if not found:
            return ToolResult(True, f"I have nothing recorded for {material['period']}.")
        return ToolResult(True, f"What I have for {material['period']} ({found} items).", material)

    registry.register(
        ToolSpec(
            "recall_work",
            "Pick up past work: gathers day notes, conversation summaries, the user's "
            "requests, tasks and changed files for a period ('yesterday', 'last week', "
            "'monday', 'last 3 days'; empty = last 7 days), optionally for one project. "
            "Use it for 'continue where I left off' or 'what was I working on'.",
            {
                "type": "object",
                "properties": {
                    "when": {"type": "string", "maxLength": 30},
                    "project": {"type": "string", "maxLength": 80},
                },
                "required": [],
            },
            recall_work,
            Risk.LOW,
            lambda a: "Look back over recent work",
            group="recall",
        )
    )


def work_folders(home: Path | None = None) -> list[Path]:
    """Where your work usually is: Desktop, Documents and Downloads."""
    from jarvis.tools.files import known_folder

    home = home or Path.home()
    folders = []
    for name in ("desktop", "documents", "downloads"):
        try:
            folder = known_folder(name, home)
        except Exception:
            continue
        if folder.is_dir() and folder not in folders:
            folders.append(folder)
    return folders
