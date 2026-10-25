"""Watchtower: JARVIS keeps an eye on what you chose, on this PC only.

- **Folders you name** ("watch my Downloads for new PDFs", or the Watchtower tab):
  each is checked every ``interval_s`` seconds by listing it (no file is opened or
  read), and a new or changed file that matches the pattern is mentioned ("New PDF in
  Downloads: report.pdf") and starts any workflow whose trigger is that folder. Only
  folders inside your own folders can be watched, never system ones. A changed file is
  reported once it has stopped changing, so a download in progress isn't.
- **Deadlines**: a task due tomorrow is mentioned the day before and again an hour
  before, and once when it becomes overdue. Each is said once.
- **Calendar**: a heads-up ten minutes before an event, when a calendar is connected.

Watches are kept in keep.sqlite3 (state ``watchtower``); nothing leaves the PC.
"""

from __future__ import annotations

import fnmatch
import json
import os
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from loguru import logger

from jarvis.agent.routines import Said, quiet
from jarvis.storage.keep import Keep
from jarvis.tools.calendar_ics import Calendar, CalendarError
from jarvis.tools.files import FileAccessError, FileArea

EVENTS = ("new", "changed")
STATE_KEY = "watchtower"
_TEMPORARY = ("*.crdownload", "*.part", "*.partial", "*.tmp", "~$*", ".~lock*", "*.download")
_KINDS = {
    ".pdf": "PDF",
    ".docx": "Word document",
    ".doc": "Word document",
    ".xlsx": "spreadsheet",
    ".csv": "spreadsheet",
    ".pptx": "presentation",
    ".png": "picture",
    ".jpg": "picture",
    ".jpeg": "picture",
    ".zip": "zip file",
    ".mp4": "video",
    ".mp3": "audio file",
}


@dataclass
class Watch:
    folder: str  # resolved, absolute
    pattern: str = "*"
    events: list[str] = field(default_factory=lambda: ["new"])
    recursive: bool = False
    announce: bool = True
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])

    def describe(self) -> str:
        what = " or ".join("new" if e == "new" else "changed" for e in self.events)
        files = "files" if self.pattern in ("", "*") else f"{self.pattern} files"
        deep = " and its subfolders" if self.recursive else ""
        return f"{what} {files} in {self.folder}{deep}"


@dataclass(frozen=True, slots=True)
class Change:
    watch: Watch
    path: Path
    event: str  # new | changed


def file_kind(path: Path) -> str:
    return _KINDS.get(path.suffix.lower(), "file")


def describe_change(change: Change) -> str:
    kind = file_kind(change.path)
    where = Path(change.watch.folder).name or change.watch.folder
    if change.event == "new":
        return f"New {kind} in {where}: {change.path.name}."
    return f"{change.path.name} in {where} was changed."


async def load_watches(keep: Keep) -> list[Watch]:
    try:
        data = json.loads(await keep.get_state(STATE_KEY) or "[]")
    except ValueError:
        return []
    known = Watch.__dataclass_fields__
    watches = []
    for item in data if isinstance(data, list) else []:
        if isinstance(item, dict) and item.get("folder"):
            watches.append(Watch(**{k: v for k, v in item.items() if k in known}))
    return watches


async def save_watches(keep: Keep, watches: list[Watch]) -> None:
    await keep.set_state(STATE_KEY, json.dumps([asdict(w) for w in watches]))


def snapshot(watch: Watch, limit: int = 5000) -> dict[str, tuple[float, int]]:
    """{path: (modified, size)} of the files a watch covers. Only lists folders."""
    found: dict[str, tuple[float, int]] = {}
    pending = [Path(watch.folder)]
    while pending and len(found) < limit:
        folder = pending.pop()
        try:
            entries = list(os.scandir(folder))
        except OSError:
            continue
        for entry in entries:
            name = entry.name
            if name.startswith(".") or any(fnmatch.fnmatch(name.lower(), t) for t in _TEMPORARY):
                continue
            try:
                if entry.is_dir(follow_symlinks=False):
                    if watch.recursive:
                        pending.append(Path(entry.path))
                    continue
                if not fnmatch.fnmatch(name.lower(), (watch.pattern or "*").lower()):
                    continue
                stat = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            found[entry.path] = (stat.st_mtime, stat.st_size)
            if len(found) >= limit:
                break
    return found


class FolderWatcher:
    """Compares each watched folder with its last listing. A new or changed file is
    reported on the first scan where it hasn't changed since the scan before, so a file
    still being downloaded or saved isn't."""

    def __init__(self) -> None:
        self._seen: dict[str, dict[str, tuple[float, int]]] = {}
        self._pending: dict[str, dict[str, str]] = {}  # watch id -> {path: event}

    def scan(self, watches: list[Watch]) -> list[Change]:
        changes: list[Change] = []
        live = {w.id for w in watches}
        for gone in set(self._seen) - live:
            self._seen.pop(gone, None)
            self._pending.pop(gone, None)
        for watch in watches:
            now = snapshot(watch)
            before = self._seen.get(watch.id)
            self._seen[watch.id] = now
            if before is None:
                continue  # the first look only learns what's there
            pending = self._pending.setdefault(watch.id, {})
            for path in list(pending):
                if path not in now:
                    pending.pop(path)
                elif before.get(path) == now[path]:
                    event = pending.pop(path)
                    if event in watch.events:
                        changes.append(Change(watch, Path(path), event))
            for path, info in now.items():
                if path not in before:
                    pending.setdefault(path, "new")
                elif info != before[path]:
                    pending.setdefault(path, "changed")
        return changes

    def learn(self, watch: Watch) -> None:
        """A watch that was just added: what's there now is the starting point."""
        self._seen[watch.id] = snapshot(watch)


RunFolderWorkflows = Callable[[Change], Awaitable[None]]


class Watchtower:
    """The background job (ticked every 10 s by :class:`Background`)."""

    def __init__(
        self,
        keep: Keep,
        *,
        interval_s: float = 30.0,
        deadlines: bool = True,
        calendar: Calendar | None = None,
        calendar_alerts: bool = True,
        calendar_minutes: int = 10,
        on_change: RunFolderWorkflows | None = None,
        quiet_hours: tuple[int, int] = (22, 8),
        extra: Callable[[], list[Watch]] | None = None,
    ) -> None:
        self._keep = keep
        self._interval = timedelta(seconds=interval_s)
        self.deadlines = deadlines  # both can be switched in the Watchtower tab
        self.calendar_alerts = calendar_alerts
        self._calendar = calendar
        self._calendar_minutes = calendar_minutes
        self._on_change = on_change
        self._quiet = quiet_hours
        self._extra = extra or (lambda: [])  # folder workflows' own watches
        self._watcher = FolderWatcher()
        self._last_scan: datetime | None = None

    async def tick(self, now: datetime) -> list[Said]:
        said: list[Said] = []
        if self._last_scan is None or now - self._last_scan >= self._interval:
            self._last_scan = now
            said += await self._folders()
        if self.deadlines:
            said += await self._due(now)
        if self.calendar_alerts and self._calendar is not None and self._calendar.available:
            said += await self._events(now)
        return said

    async def _folders(self) -> list[Said]:
        import asyncio

        watches = await load_watches(self._keep) + self._extra()
        if not watches:
            return []
        changes = await asyncio.to_thread(self._watcher.scan, watches)
        said = []
        for change in changes[:10]:
            if change.watch.announce:
                said.append(Said(describe_change(change), "watch"))
            if self._on_change is not None:
                try:
                    await self._on_change(change)
                except Exception as exc:
                    logger.warning("Folder workflow failed to start: {}", type(exc).__name__)
        if len(changes) > 10:
            said.append(Said(f"And {len(changes) - 10} more files changed.", "watch"))
        return said

    async def _alerted(self) -> dict[str, list[str]]:
        try:
            data = json.loads(await self._keep.get_state("deadline_alerts") or "{}")
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}

    async def _due(self, now: datetime) -> list[Said]:
        tasks = [t for t in await self._keep.tasks(kind="task", done=False) if t.due]
        if not tasks:
            return []
        alerted = await self._alerted()
        said = []
        stamp = now.timestamp()
        for task in tasks:
            due = task.due or 0
            sent = alerted.setdefault(str(task.id), [])
            left = due - stamp
            when = datetime.fromtimestamp(due, now.tzinfo)
            clock = "" if (when.hour, when.minute) == (23, 59) else f" at {when:%H:%M}"
            stage, text = "", ""
            if left < 0 and "overdue" not in sent and -left < 7 * 86400:
                stage, text = "overdue", f"“{task.text}” is now overdue."
            elif 0 <= left <= 3600 and "hour" not in sent:
                stage, text = "hour", f"“{task.text}” is due within the hour{clock}."
            elif 3600 < left <= 86400 and "day" not in sent:
                if quiet(now, *self._quiet):
                    continue
                day = "today" if when.date() == now.date() else "tomorrow"
                stage, text = "day", f"Heads up: “{task.text}” is due {day}{clock}."
            if stage:
                sent.append(stage)
                # Earlier stages that were skipped don't come back later.
                if stage == "overdue":
                    sent += ["hour", "day"]
                elif stage == "hour":
                    sent.append("day")
                said.append(Said(text, "deadline"))
        live = {str(t.id) for t in tasks}
        alerted = {k: v for k, v in alerted.items() if k in live}
        await self._keep.set_state("deadline_alerts", json.dumps(alerted))
        return said

    async def _events(self, now: datetime) -> list[Said]:
        assert self._calendar is not None
        window = timedelta(minutes=self._calendar_minutes)
        try:
            events = await self._calendar.events(now, now + window + timedelta(minutes=1))
        except CalendarError:
            return []
        try:
            told = set(json.loads(await self._keep.get_state("event_alerts") or "[]"))
        except ValueError:
            told = set()
        said = []
        for event in events:
            if event.all_day or event.start <= now:
                continue
            key = f"{event.title}|{event.start.isoformat()}"
            if key in told:
                continue
            told.add(key)
            minutes = max(1, round((event.start - now).total_seconds() / 60))
            said.append(Said(f"{event.title} starts in {minutes} minutes.", "calendar"))
        if said:
            await self._keep.set_state("event_alerts", json.dumps(sorted(told)[-200:]))
        return said

    # --- changing what's watched (tools and the Watchtower tab) ---

    async def watches(self) -> list[Watch]:
        return await load_watches(self._keep)

    async def add(self, area: FileArea, folder: str, pattern: str = "*",
                  events: list[str] | None = None, recursive: bool = False) -> Watch:  # fmt: skip
        path = area.resolve(folder)
        if not path.is_dir():
            raise FileAccessError(f"{path} isn't a folder")
        events = [e for e in (events or ["new"]) if e in EVENTS] or ["new"]
        watches = await load_watches(self._keep)
        pattern = (pattern or "*").strip() or "*"
        for watch in watches:
            if Path(watch.folder) == path and watch.pattern == pattern:
                watch.events, watch.recursive = events, recursive
                await save_watches(self._keep, watches)
                return watch
        if len(watches) >= 20:
            raise FileAccessError("20 folders are watched already; stop watching one first")
        watch = Watch(str(path), pattern, events, recursive)
        watches.append(watch)
        await save_watches(self._keep, watches)
        self._watcher.learn(watch)
        return watch

    async def remove(self, area: FileArea | None, folder: str) -> list[Watch]:
        watches = await load_watches(self._keep)
        wanted = folder.strip().lower()
        try:
            resolved = str(area.resolve(folder)).lower() if area is not None else ""
        except FileAccessError:
            resolved = ""
        gone = [
            w
            for w in watches
            if w.id == folder.strip()
            or w.folder.lower() == resolved
            or Path(w.folder).name.lower() == wanted
        ]
        await save_watches(self._keep, [w for w in watches if w not in gone])
        return gone


def register_watch_tools(registry: Any, tower: Watchtower, area: FileArea) -> None:
    from jarvis.tools.registry import Risk, ToolResult, ToolSpec

    async def watch_folder(
        folder: str, pattern: str = "*", events: str = "new", include_subfolders: bool = False
    ) -> ToolResult:
        wanted = ["new", "changed"] if events == "both" else [events]
        try:
            watch = await tower.add(area, folder, pattern, wanted, include_subfolders)
        except FileAccessError as exc:
            return ToolResult(False, f"I can't watch that: {exc}.")
        return ToolResult(True, f"Watching for {watch.describe()}. I'll mention each one.")

    async def stop_watching_folder(folder: str) -> ToolResult:
        gone = await tower.remove(area, folder)
        if not gone:
            return ToolResult(False, "I wasn't watching that folder.")
        return ToolResult(True, "Stopped watching " + "; ".join(w.describe() for w in gone) + ".")

    async def list_watches() -> ToolResult:
        watches = await tower.watches()
        if not watches:
            return ToolResult(True, "No folders are being watched.")
        return ToolResult(
            True, f"{len(watches)} watches.", {"watching": [w.describe() for w in watches]}
        )

    registry.register(
        ToolSpec(
            "watch_folder",
            "Watch one of the user's folders (on this PC) and mention new or changed files, "
            "e.g. new PDFs in Downloads. pattern like '*.pdf'; events new, changed or both.",
            {
                "type": "object",
                "properties": {
                    "folder": {"type": "string", "maxLength": 300},
                    "pattern": {"type": "string", "maxLength": 40},
                    "events": {"type": "string", "enum": ["new", "changed", "both"]},
                    "include_subfolders": {"type": "boolean"},
                },
                "required": ["folder"],
            },
            watch_folder,
            Risk.LOW,
            lambda a: f"Watch {a.get('folder', '')}",
            group="watch",
        )
    )
    registry.register(
        ToolSpec(
            "stop_watching_folder",
            "Stop watching a folder (its name, path or watch id).",
            {
                "type": "object",
                "properties": {"folder": {"type": "string", "maxLength": 300}},
                "required": ["folder"],
            },
            stop_watching_folder,
            Risk.LOW,
            lambda a: f"Stop watching {a.get('folder', '')}",
            group="watch",
        )
    )
    registry.register(
        ToolSpec(
            "list_watches",
            "List the folders JARVIS is watching.",
            {"type": "object", "properties": {}, "required": []},
            list_watches,
            Risk.LOW,
            lambda a: "List watched folders",
            group="watch",
        )
    )
