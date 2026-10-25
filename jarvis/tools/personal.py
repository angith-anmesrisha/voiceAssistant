"""Tools for memory, reminders and timers, weather, news and web answers."""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any

import httpx

from jarvis.storage.keep import CATEGORIES, REPEATS, Keep
from jarvis.storage.memory import Memory
from jarvis.tools.registry import Risk, ToolRegistry, ToolResult, ToolSpec
from jarvis.tools.web import WebError, news, search, weather


def _schema(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": required or []}


class WhenError(ValueError):
    pass


def due_time(at: str = "", in_minutes: float = 0, now: datetime | None = None) -> datetime:
    """When a reminder is due: ``in_minutes`` from now, or ``at`` ("18:30", "6 pm",
    "2026-10-25 09:00"). A bare time that has passed today means tomorrow."""
    now = now or datetime.now().astimezone()
    if in_minutes and in_minutes > 0:
        return now + timedelta(minutes=in_minutes)
    text = at.strip().lower().replace(".", "")
    if not text:
        raise WhenError("say when: a time, or how many minutes from now")
    if "-" in text:  # a date: "2026-10-25 09:00"
        try:
            full = datetime.fromisoformat(text.replace(" ", "T", 1))
        except ValueError as exc:
            raise WhenError(f"I couldn't read the date “{at}”") from exc
        return full if full.tzinfo else full.replace(tzinfo=now.tzinfo)
    match = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", text)
    if not match:
        raise WhenError(f"I couldn't read the time “{at}”")
    hour, minute = int(match.group(1)), int(match.group(2) or 0)
    if match.group(3) == "pm" and hour < 12:
        hour += 12
    if match.group(3) == "am" and hour == 12:
        hour = 0
    if hour > 23 or minute > 59:
        raise WhenError(f"“{at}” isn't a time of day")
    when = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return when if when > now else when + timedelta(days=1)


def spoken_time(when: datetime, now: datetime | None = None) -> str:
    now = now or datetime.now().astimezone()
    clock = when.strftime("%H:%M")
    if when.date() == now.date():
        return f"today at {clock}"
    if when.date() == (now + timedelta(days=1)).date():
        return f"tomorrow at {clock}"
    return when.strftime("%A %d %B at %H:%M")


def register_personal_tools(
    registry: ToolRegistry,
    keep: Keep | None,
    http: httpx.AsyncClient | None,
    *,
    home_place: str = "",
    news_locale: str = "en-US",
    memory: Memory | None = None,
) -> None:
    if keep is not None:
        _memory_tools(registry, keep, memory)
        _reminder_tools(registry, keep)
    if memory is not None:
        _note_tool(registry, memory)
    if http is not None:
        _web_tools(registry, http, keep, home_place, news_locale)


async def home_city(keep: Keep | None, default: str = "") -> str:
    """The user's city: from memory if they've mentioned it, else ``tools.home_place``."""
    if keep is not None:
        for fact in await keep.recall("home city town live location", limit=3):
            if any(w in fact.key for w in ("city", "town", "home", "live", "location")):
                return fact.value
    return default


async def mirror_facts(keep: Keep, memory: Memory | None) -> None:
    """Rewrite JARVIS/Facts.md in the memory folder from the remembered facts."""
    if memory is not None:
        await memory.facts_changed((f.key, f.value, f.category) for f in await keep.facts())


def _memory_tools(registry: ToolRegistry, keep: Keep, memory: Memory | None) -> None:
    async def remember(topic: str, fact: str, category: str = "other") -> ToolResult:
        try:
            saved = await keep.remember(topic, fact, category)
        except ValueError as exc:
            return ToolResult(False, f"Couldn't remember that: {exc}.")
        await mirror_facts(keep, memory)
        return ToolResult(True, f"Remembered ({saved.key.replace('_', ' ')}).")

    async def recall_memory(query: str = "") -> ToolResult:
        facts = await keep.recall(query)
        lines = [f"{f.key.replace('_', ' ')}: {f.value}" for f in facts]
        data: dict[str, Any] = {"facts": lines}
        notes: list[str] = []
        if memory is not None and query.strip():
            hits = await memory.search(query)
            notes = [f"[{h.note}] {' '.join(h.text.split())[:500]}" for h in hits]
            data["notes"] = notes
        if not lines and not notes:
            return ToolResult(True, "Nothing in memory matches that.")
        summary = f"{len(lines)} remembered facts"
        if notes:
            summary += f", {len(notes)} passages from past conversations and notes"
        return ToolResult(True, summary + ".", data)

    async def forget(topic: str) -> ToolResult:
        gone = await keep.forget(topic)
        if not gone:
            return ToolResult(False, "I had nothing remembered about that.")
        await mirror_facts(keep, memory)
        return ToolResult(True, f"Forgot {gone[0].replace('_', ' ')}.")

    registry.register(
        ToolSpec(
            "remember",
            "Save a lasting fact about the user (name, people, preferences, projects, places) "
            "when they tell you something worth keeping or ask you to remember it. Same "
            "topic replaces the old fact.",
            _schema(
                {
                    "topic": {"type": "string", "maxLength": 60, "description": "e.g. sister_name"},
                    "fact": {"type": "string", "maxLength": 500},
                    "category": {"type": "string", "enum": list(CATEGORIES)},
                },
                ["topic", "fact"],
            ),
            remember,
            Risk.LOW,
            lambda a: f"Remember {a.get('topic', 'that')}",
        )
    )
    registry.register(
        ToolSpec(
            "recall_memory",
            "Look up what the user told you before: remembered facts, plus past "
            "conversations and notes in their memory folder (words, or times like "
            "'yesterday', 'last week'). Empty = the most recent facts. Name the note you "
            "found something in.",
            _schema({"query": {"type": "string", "maxLength": 200}}),
            recall_memory,
            Risk.LOW,
            lambda a: "Look in memory",
        )
    )
    registry.register(
        ToolSpec(
            "forget",
            "Forget a remembered fact when the user asks.",
            _schema({"topic": {"type": "string", "maxLength": 200}}, ["topic"]),
            forget,
            Risk.LOW,
            lambda a: f"Forget {a.get('topic', 'that')}",
            group="memory",
        )
    )


def _note_tool(registry: ToolRegistry, memory: Memory) -> None:
    async def save_note(title: str, text: str) -> ToolResult:
        if not text.strip():
            return ToolResult(False, "There's nothing to write in the note.")
        try:
            where = await memory.note(title, text)
        except OSError as exc:
            return ToolResult(False, f"Couldn't write the note ({type(exc).__name__}).")
        return ToolResult(True, f"Noted in {where}.", {"note": where})

    registry.register(
        ToolSpec(
            "save_note",
            "Write a note into the user's memory folder (Markdown, Obsidian-friendly) when "
            "they ask you to note, jot down or keep something longer than a fact: plans, "
            "lists, ideas, meeting notes. The same title adds to that note.",
            _schema(
                {
                    "title": {"type": "string", "maxLength": 80},
                    "text": {"type": "string", "maxLength": 8000},
                },
                ["title", "text"],
            ),
            save_note,
            Risk.LOW,
            lambda a: f"Write the note “{a.get('title', '')}”",
            group="memory",
        )
    )


_REPEAT_WORDS = {
    "daily": "every day",
    "weekdays": "every weekday",
    "weekly": "every week",
    "monthly": "every month",
}


def _reminder_tools(registry: ToolRegistry, keep: Keep) -> None:
    async def set_reminder(
        text: str, at: str = "", in_minutes: float = 0, repeat: str = ""
    ) -> ToolResult:
        try:
            when = due_time(at, in_minutes)
        except WhenError as exc:
            return ToolResult(False, f"Couldn't set the reminder: {exc}.")
        reminder = await keep.add_reminder(when.timestamp(), text, repeat)
        again = f", then {_REPEAT_WORDS[reminder.repeat]}" if reminder.repeat else ""
        return ToolResult(
            True,
            f"Reminder {reminder.id} set for {spoken_time(when)}{again}.",
            {"id": reminder.id},
        )

    async def list_reminders() -> ToolResult:
        pending = await keep.reminders()
        if not pending:
            return ToolResult(True, "No reminders are set.")
        lines = [
            f"{r.id}: {spoken_time(datetime.fromtimestamp(r.due).astimezone())}: {r.text}"
            + (f" ({_REPEAT_WORDS[r.repeat]})" if r.repeat else "")
            for r in pending
        ]
        return ToolResult(True, f"{len(lines)} reminders.", {"reminders": lines})

    async def cancel_reminder(id: int) -> ToolResult:
        if await keep.finish_reminder(id):
            return ToolResult(True, f"Cancelled reminder {id}.")
        return ToolResult(False, f"There's no pending reminder {id}.")

    registry.register(
        ToolSpec(
            "set_reminder",
            "Set a reminder or timer. Give in_minutes for 'in 10 minutes', or at for a time "
            "('18:30', '6 pm', '2026-10-25 09:00'). repeat for 'every day/weekday/week/"
            "month'. JARVIS says it out loud when due.",
            _schema(
                {
                    "text": {"type": "string", "maxLength": 300},
                    "at": {"type": "string", "maxLength": 40},
                    "in_minutes": {"type": "number"},
                    "repeat": {"type": "string", "enum": ["", *REPEATS]},
                },
                ["text"],
            ),
            set_reminder,
            Risk.LOW,
            lambda a: f"Set a reminder: {a.get('text', '')}",
            group="reminders",
        )
    )
    registry.register(
        ToolSpec(
            "list_reminders",
            "List the reminders and timers that are set.",
            _schema({}),
            list_reminders,
            Risk.LOW,
            lambda a: "List reminders",
            group="reminders",
        )
    )
    registry.register(
        ToolSpec(
            "cancel_reminder",
            "Cancel a reminder by its number (see list_reminders).",
            _schema({"id": {"type": "integer"}}, ["id"]),
            cancel_reminder,
            Risk.LOW,
            lambda a: f"Cancel reminder {a.get('id')}",
            group="reminders",
        )
    )


def _web_tools(
    registry: ToolRegistry,
    http: httpx.AsyncClient,
    keep: Keep | None,
    home_place: str,
    news_locale: str,
) -> None:
    async def place_or_home(place: str) -> str:
        return place.strip() or await home_city(keep, home_place)

    async def get_weather(place: str = "") -> ToolResult:
        where = await place_or_home(place)
        if not where:
            return ToolResult(False, "Which city? (Tell me once and I'll remember it.)")
        try:
            report = await weather(http, where)
        except WebError as exc:
            return ToolResult(False, f"Couldn't get the weather: {exc}.")
        return ToolResult(True, report)

    async def look_up(query: str, kind: str = "search") -> ToolResult:
        try:
            if kind == "news":
                found = await news(http, query, news_locale)
                if not found:
                    return ToolResult(True, "No news found on that.")
                lines = [f"{h.title} ({h.source}, {h.published})" for h in found]
                return ToolResult(True, f"{len(lines)} headlines.", {"headlines": lines})
            hits = await search(http, query)
        except WebError as exc:
            return ToolResult(False, f"Couldn't search the web: {exc}.")
        lines = [f"{h.title} | {h.snippet} | {h.url}" for h in hits]
        return ToolResult(True, f"{len(lines)} results.", {"results": lines})

    registry.register(
        ToolSpec(
            "get_weather",
            "Current weather and the next days' forecast (empty place = the user's home city).",
            _schema({"place": {"type": "string", "maxLength": 100}}),
            get_weather,
            Risk.LOW,
            lambda a: f"Check the weather in {a.get('place') or 'your city'}",
        )
    )
    registry.register(
        ToolSpec(
            "look_up",
            "Read web search results or the latest news headlines to answer questions about "
            "current events, prices, scores, people or facts you're unsure of. Say where the "
            "answer came from.",
            _schema(
                {
                    "query": {"type": "string", "maxLength": 300},
                    "kind": {"type": "string", "enum": ["search", "news"]},
                },
                ["query"],
            ),
            look_up,
            Risk.LOW,
            lambda a: f"Look up {a.get('query', '')}",
        )
    )
    if keep is None:
        return

    async def watch_topic(topic: str, stop: bool = False) -> ToolResult:
        if stop:
            if await keep.unwatch(topic):
                return ToolResult(True, f"Stopped watching news about {topic}.")
            return ToolResult(False, f"I wasn't watching {topic}.")
        await keep.watch(topic)
        return ToolResult(True, f"I'll check news about {topic} once a day and tell you.")

    registry.register(
        ToolSpec(
            "watch_topic",
            "Keep an eye on news about a topic (checked daily; the user is told about new "
            "headlines), or stop watching it.",
            _schema(
                {"topic": {"type": "string", "maxLength": 100}, "stop": {"type": "boolean"}},
                ["topic"],
            ),
            watch_topic,
            Risk.LOW,
            lambda a: f"Watch news about {a.get('topic', '')}",
            group="reminders",
        )
    )
