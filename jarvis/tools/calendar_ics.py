"""Your calendar, read-only, from its private iCal (.ics) address.

Google Calendar ("Settings > your calendar > Secret address in iCal format"), Outlook
("Publish calendar > ICS") and Apple iCloud all give one. The address is a secret, so,
like API keys, it lives only in an environment variable, ``JARVIS_CALENDAR_ICS`` (several
separated by spaces), and is never logged. JARVIS only reads it; nothing is ever written
to your calendar.

The parser covers what calendars actually send: timed and all-day events, ``TZID``
times, UTC times, and repeating events (``RRULE`` daily/weekly/monthly/yearly with
``INTERVAL``, ``COUNT``, ``UNTIL``, ``BYDAY``, plus ``EXDATE``). Event titles are
outside text (anyone can send you an invite), so tools that hand them to the AI are
screened by Prompt Guard like web text.
"""

from __future__ import annotations

import contextlib
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from loguru import logger

from jarvis.app.keys import get_key

ENV = "JARVIS_CALENDAR_ICS"
_DAY_CODES = ("MO", "TU", "WE", "TH", "FR", "SA", "SU")
_MAX_OCCURRENCES = 2000  # per repeating event, while looking for the wanted window

# Windows time zone names some Outlook feeds use, for the common ones.
_WINDOWS_ZONES = {
    "India Standard Time": "Asia/Kolkata",
    "GMT Standard Time": "Europe/London",
    "W. Europe Standard Time": "Europe/Berlin",
    "Romance Standard Time": "Europe/Paris",
    "Eastern Standard Time": "America/New_York",
    "Central Standard Time": "America/Chicago",
    "Pacific Standard Time": "America/Los_Angeles",
    "Arabian Standard Time": "Asia/Dubai",
    "Singapore Standard Time": "Asia/Singapore",
    "Tokyo Standard Time": "Asia/Tokyo",
    "AUS Eastern Standard Time": "Australia/Sydney",
    "UTC": "UTC",
}


class CalendarError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class Event:
    title: str
    start: datetime  # local time, aware
    end: datetime
    all_day: bool = False
    location: str = ""

    def describe(self, now: datetime | None = None) -> str:
        when = "all day" if self.all_day else f"{_clock(self.start)} to {_clock(self.end)}"
        where = f" at {self.location}" if self.location else ""
        return f"{when}: {self.title}{where}"


def _clock(moment: datetime) -> str:
    text = moment.strftime("%I:%M %p").lstrip("0")
    return text.replace(":00 ", " ")


def configured() -> list[str]:
    raw = get_key(ENV) or ""
    return [u for u in raw.split() if u.startswith(("https://", "http://", "webcal://"))]


# --- parsing ---


def _unfold(text: str) -> list[str]:
    lines: list[str] = []
    for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if line[:1] in (" ", "\t") and lines:
            lines[-1] += line[1:]
        elif line:
            lines.append(line)
    return lines


def _unescape(value: str) -> str:
    return (
        value.replace("\\n", " ")
        .replace("\\N", " ")
        .replace("\\,", ",")
        .replace("\\;", ";")
        .replace("\\\\", "\\")
        .strip()
    )


def _zone(name: str | None, local: tzinfo) -> tzinfo:
    if not name:
        return local
    name = name.strip('"')
    try:
        return ZoneInfo(_WINDOWS_ZONES.get(name, name))
    except (ZoneInfoNotFoundError, ValueError):
        return local


def _moment(value: str, params: dict[str, str], local: tzinfo) -> tuple[datetime, bool]:
    """(aware datetime in the event's own zone, all-day) from a DTSTART/DTEND/EXDATE
    value. Kept in its own zone so repeats stay at the same wall-clock time across
    daylight-saving changes."""
    value = value.strip()
    if params.get("VALUE") == "DATE" or re.fullmatch(r"\d{8}", value):
        day = datetime.strptime(value[:8], "%Y%m%d").date()
        return datetime.combine(day, time(), local), True
    utc = value.endswith("Z")
    parsed = datetime.strptime(value.rstrip("Z")[:15], "%Y%m%dT%H%M%S")
    if utc:
        return parsed.replace(tzinfo=UTC), False
    return parsed.replace(tzinfo=_zone(params.get("TZID"), local)), False


@dataclass
class _Raw:
    title: str = ""
    location: str = ""
    start: datetime | None = None
    end: datetime | None = None
    all_day: bool = False
    rule: dict[str, str] | None = None
    excluded: frozenset[datetime] = frozenset()
    recurrence_id: datetime | None = None
    uid: str = ""
    cancelled: bool = False


def parse(text: str, local: tzinfo) -> list[_Raw]:
    events: list[_Raw] = []
    current: _Raw | None = None
    excluded: set[datetime] = set()
    for line in _unfold(text):
        name_part, _, value = line.partition(":")
        name, *raw_params = name_part.split(";")
        params = dict(p.split("=", 1) for p in raw_params if "=" in p)
        name = name.upper()
        if name == "BEGIN" and value.upper() == "VEVENT":
            current, excluded = _Raw(), set()
        elif name == "END" and value.upper() == "VEVENT" and current is not None:
            current.excluded = frozenset(excluded)
            if current.start is not None:
                events.append(current)
            current = None
        elif current is None:
            continue
        elif name == "SUMMARY":
            current.title = _unescape(value)[:200]
        elif name == "LOCATION":
            current.location = _unescape(value)[:120]
        elif name == "UID":
            current.uid = value.strip()
        elif name == "STATUS":
            current.cancelled = value.strip().upper() == "CANCELLED"
        elif name in ("DTSTART", "DTEND", "RECURRENCE-ID"):
            try:
                moment, all_day = _moment(value, params, local)
            except ValueError:
                continue
            if name == "DTSTART":
                current.start, current.all_day = moment, all_day
            elif name == "DTEND":
                current.end = moment
            else:
                current.recurrence_id = moment
        elif name == "RRULE":
            current.rule = dict(
                part.split("=", 1) for part in value.upper().split(";") if "=" in part
            )
        elif name == "EXDATE":
            for item in value.split(","):
                try:
                    excluded.add(_moment(item, params, local)[0])
                except ValueError:
                    continue
    return events


def _occurrences(raw: _Raw, until: datetime) -> list[datetime]:
    """Start times of ``raw`` up to ``until`` (one for a single event)."""
    assert raw.start is not None
    rule = raw.rule
    if not rule:
        return [raw.start]
    freq = rule.get("FREQ", "")
    interval = max(1, int(rule.get("INTERVAL", "1") or 1))
    count = int(rule["COUNT"]) if rule.get("COUNT", "").isdigit() else None
    end = until
    if rule.get("UNTIL"):
        with contextlib.suppress(ValueError):
            end = min(end, _moment(rule["UNTIL"], {}, raw.start.tzinfo or UTC)[0])
    by_day = [d for d in rule.get("BYDAY", "").split(",") if d[-2:] in _DAY_CODES]
    days = [_DAY_CODES.index(d[-2:]) for d in by_day]
    ordinals = [(int(d[:-2]), _DAY_CODES.index(d[-2:])) for d in by_day if d[:-2].lstrip("+-")]
    found: list[datetime] = []
    start = raw.start
    step = 0
    while len(found) < _MAX_OCCURRENCES:
        if freq == "DAILY":
            candidates = [start + timedelta(days=step * interval)]
        elif freq == "WEEKLY":
            week = start - timedelta(days=start.weekday()) + timedelta(weeks=step * interval)
            wanted = days or [start.weekday()]
            candidates = sorted(week + timedelta(days=d) for d in wanted)
        elif freq == "MONTHLY":
            month_index = start.month - 1 + step * interval
            year, month = start.year + month_index // 12, month_index % 12 + 1
            if ordinals:  # "the second Tuesday", "the last Friday"
                candidates = sorted(
                    m
                    for n, weekday in ordinals
                    if (m := _nth_weekday(start, year, month, n, weekday)) is not None
                )
            else:
                try:
                    candidates = [start.replace(year=year, month=month)]
                except ValueError:  # the 31st in a short month
                    candidates = []
        elif freq == "YEARLY":
            try:
                candidates = [start.replace(year=start.year + step * interval)]
            except ValueError:  # 29 February
                candidates = []
        else:
            return [start]
        step += 1
        if candidates and candidates[0] > end:
            break
        for moment in candidates:
            if moment < start or moment > end:
                continue
            found.append(moment)
            if count is not None and len(found) >= count:
                return found
    return found


def _nth_weekday(start: datetime, year: int, month: int, n: int, weekday: int) -> datetime | None:
    first = date(year, month, 1)
    if n > 0:
        day = first + timedelta(days=(weekday - first.weekday()) % 7 + 7 * (n - 1))
    else:
        last = date(year + month // 12, month % 12 + 1, 1) - timedelta(days=1)
        day = last - timedelta(days=(last.weekday() - weekday) % 7 + 7 * (-n - 1))
    if day.month != month:
        return None
    return start.replace(year=day.year, month=day.month, day=day.day)


def events_between(raws: list[_Raw], start: datetime, end: datetime) -> list[Event]:
    """Events that overlap [start, end), soonest first."""
    moved = {(r.uid, r.recurrence_id) for r in raws if r.recurrence_id is not None}
    result: list[Event] = []
    for raw in raws:
        if raw.start is None or raw.cancelled:
            continue
        length = (raw.end - raw.start) if raw.end else timedelta(days=1 if raw.all_day else 0)
        for begin in _occurrences(raw, end):
            if begin in raw.excluded or (raw.recurrence_id is None and (raw.uid, begin) in moved):
                continue
            finish = begin + length
            if begin < end and (finish > start or begin >= start):
                local = start.tzinfo
                result.append(
                    Event(
                        raw.title or "(no title)",
                        begin.astimezone(local),
                        finish.astimezone(local),
                        raw.all_day,
                        raw.location,
                    )
                )
    return sorted(result, key=lambda e: (not e.all_day, e.start))


class Calendar:
    """Reads the configured calendars (cached for 10 minutes)."""

    def __init__(self, client: httpx.AsyncClient, urls: list[str] | None = None) -> None:
        self._client = client
        self._urls = urls
        self._cache: tuple[float, list[_Raw]] | None = None

    @property
    def available(self) -> bool:
        return bool(self._urls if self._urls is not None else configured())

    async def _raws(self) -> list[_Raw]:
        import time as clock

        if self._cache is not None and clock.monotonic() - self._cache[0] < 600:
            return self._cache[1]
        urls = self._urls if self._urls is not None else configured()
        if not urls:
            raise CalendarError(f"no calendar is connected (set {ENV} to its iCal address)")
        local = datetime.now().astimezone().tzinfo
        assert local is not None
        raws: list[_Raw] = []
        failures = 0
        for url in urls:
            try:
                response = await self._client.get(
                    url.replace("webcal://", "https://", 1), timeout=15, follow_redirects=True
                )
                response.raise_for_status()
            except httpx.HTTPError as exc:
                failures += 1
                logger.info("A calendar couldn't be read: {}", type(exc).__name__)
                continue
            raws.extend(parse(response.text, local))
        if failures == len(urls):
            raise CalendarError("the calendar couldn't be reached")
        self._cache = (clock.monotonic(), raws)
        return raws

    async def events(self, start: datetime, end: datetime) -> list[Event]:
        return events_between(await self._raws(), start, end)

    async def day(self, day: date) -> list[Event]:
        local = datetime.now().astimezone().tzinfo
        begin = datetime.combine(day, time(), local)
        return await self.events(begin, begin + timedelta(days=1))
