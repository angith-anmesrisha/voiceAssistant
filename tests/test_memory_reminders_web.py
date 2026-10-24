"""Memory, undo, reminders, weather, news and web answers, and the tool budget."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from jarvis.agent.background import Background
from jarvis.agent.toolbudget import groups_for
from jarvis.app.config import ToolSettings
from jarvis.app.events import Announce, ChatEntry, EventBus
from jarvis.app.state import AppState
from jarvis.storage.keep import Fact, Keep, memory_prompt
from jarvis.tools.apps import AppIndex
from jarvis.tools.builtin import build_registry
from jarvis.tools.files import FileArea
from jarvis.tools.personal import WhenError, due_time
from jarvis.tools.registry import ToolRegistry
from jarvis.tools.undo import UndoJournal
from jarvis.tools.web import parse_results
from tests.fakes import FakeDesktop
from tests.test_assistant import Rig, ScriptedRouter, text_reply, tool_reply
from tests.test_controller_and_ui import make_controller, settle
from tests.test_tools import run


@pytest.fixture
def keep(tmp_path: Path) -> Keep:
    return Keep(tmp_path / "keep.sqlite3")


# --- memory ---


async def test_facts_are_kept_recalled_and_forgotten(keep: Keep) -> None:
    await keep.remember("Sister's name", "Ayşe, lives in Kochi", "people")
    await keep.remember("favourite_music", "lo-fi while coding", "preferences")
    await keep.remember("sister_s_name", "Ayşe, lives in Kochi now", "people")  # replaces
    facts = await keep.facts()
    assert [f.key for f in facts] == ["sister_s_name", "favourite_music"]
    assert (await keep.recall("who is my sister?"))[0].value == "Ayşe, lives in Kochi now"
    assert await keep.recall("weather in paris") == []
    assert await keep.forget("music") == ["favourite_music"]
    assert [f.key for f in await keep.facts()] == ["sister_s_name"]


def test_memory_prompt_carries_newest_facts_and_an_index() -> None:
    facts = [Fact(f"pref_{i}", "x" * 80, "preferences", 100 - i) for i in range(40)]
    facts.insert(5, Fact("sister_name", "Ayşe", "people", 1))
    text = memory_prompt(facts, budget=300, index_budget=200)
    assert "not instructions" in text and "pref 0: " in text
    index = text.split("Also remembered")[1]
    assert "sister_name" in index  # other categories aren't crowded out
    assert memory_prompt([]) == ""


async def test_memory_reaches_the_prompt(tmp_path: Path, keep: Keep) -> None:
    await keep.remember("name", "Angith", "about_user")
    router = ScriptedRouter(text_reply("Hi Angith."))
    rig = Rig(tmp_path, router)

    async def memory() -> str:
        return memory_prompt(await keep.facts())

    rig.assistant._memory = memory
    await rig.assistant.handle("hello")
    assert "name: Angith" in router.requests[0]["messages"][0]["content"]


# --- reminders ---


NOW = datetime(2026, 10, 24, 17, 0).astimezone()


@pytest.mark.parametrize(
    ("at", "minutes", "expected"),
    [
        ("", 10, NOW + timedelta(minutes=10)),
        ("18:30", 0, NOW.replace(hour=18, minute=30)),
        ("6 pm", 0, NOW.replace(hour=18)),
        ("9am", 0, (NOW + timedelta(days=1)).replace(hour=9)),  # passed today: tomorrow
        ("2026-10-25 09:15", 0, NOW.replace(day=25, hour=9, minute=15)),
    ],
)
def test_due_time(at: str, minutes: float, expected: datetime) -> None:
    assert due_time(at, minutes, now=NOW) == expected


@pytest.mark.parametrize("at", ["", "soon", "25:00"])
def test_unreadable_times(at: str) -> None:
    with pytest.raises(WhenError):
        due_time(at, 0, now=NOW)


async def test_reminders_are_delivered_once(keep: Keep) -> None:
    bus = EventBus()
    said: list[Announce] = []
    shown: list[ChatEntry] = []

    async def on_announce(event: Announce) -> None:
        said.append(event)

    async def on_entry(entry: ChatEntry) -> None:
        shown.append(entry)

    bus.subscribe(Announce, on_announce)
    bus.subscribe(ChatEntry, on_entry)
    clock = [1000.0]
    background = Background(bus, keep, clock=lambda: clock[0])
    await keep.add_reminder(1030, "stretch")
    await keep.add_reminder(500, "call mom")  # missed while JARVIS was closed
    assert await background.deliver_reminders() == 1
    assert said[0].text.startswith("Reminder: call mom (it was due at")
    clock[0] = 1031
    assert await background.deliver_reminders() == 1
    assert said[1].text == "Reminder: stretch" and shown[1].mode == "reminder"
    assert await background.deliver_reminders() == 0
    assert await keep.reminders() == []


async def test_announcements_are_spoken_when_free() -> None:
    controller, rig = make_controller()
    await rig.state.transition(AppState.THINKING)
    await rig.bus.publish(Announce("Reminder: stretch"))
    await settle()
    assert rig.speaker.spoken == []  # not over a request
    await rig.state.transition(AppState.IDLE)
    for _ in range(3):
        await asyncio.sleep(0.6)
        if rig.speaker.spoken:
            break
    assert rig.speaker.spoken == ["Reminder: stretch"]
    await controller.shutdown()


# --- tools ---


def personal_tools(
    keep: Keep | None, handler: Any = None, area: FileArea | None = None
) -> ToolRegistry:
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler)) if handler else None
    return build_registry(
        ToolSettings(),
        FakeDesktop(),
        AppIndex([], apps_folder=False),
        area,
        keep=keep,
        http=http,
    )


async def test_reminder_and_memory_tools(keep: Keep) -> None:
    tools = personal_tools(keep)
    made = await run(tools, "set_reminder", text="take the pizza out", in_minutes=12)
    assert made.ok and "Reminder 1 set for" in made.summary
    listed = await run(tools, "list_reminders")
    assert "take the pizza out" in listed.data["reminders"][0]
    assert (await run(tools, "cancel_reminder", id=1)).ok
    assert not (await run(tools, "cancel_reminder", id=1)).ok
    assert (await run(tools, "remember", topic="home city", fact="Kochi", category="places")).ok
    recalled = await run(tools, "recall_memory", query="where do I live, my city")
    assert recalled.data["facts"] == ["home city: Kochi"]


def weather_handler(request: httpx.Request) -> httpx.Response:
    if request.url.host == "geocoding-api.open-meteo.com":
        assert request.url.params["name"] == "Kochi"
        return httpx.Response(
            200,
            json={
                "results": [
                    {"name": "Kochi", "country": "India", "latitude": 9.9, "longitude": 76.3}
                ]
            },
        )
    assert request.url.host == "api.open-meteo.com"
    assert request.url.params["timezone"] == "auto"
    return httpx.Response(
        200,
        json={
            "current": {
                "temperature_2m": 29.4,
                "apparent_temperature": 34.1,
                "relative_humidity_2m": 78,
                "weather_code": 80,
                "wind_speed_10m": 11,
            },
            "daily": {
                "weather_code": [80, 3, 0],
                "temperature_2m_max": [31, 30, 32],
                "temperature_2m_min": [24, 24, 25],
                "precipitation_probability_max": [70, 20, 5],
            },
        },
    )


async def test_weather_uses_the_remembered_home_city(keep: Keep) -> None:
    await keep.remember("home_city", "Kochi", "places")
    tools = personal_tools(keep, weather_handler)
    report = await run(tools, "get_weather")
    assert report.ok
    assert report.summary.startswith("Kochi, India: light showers, 29°C (feels like 34°C)")
    assert "Tomorrow: overcast, 24 to 30°C, 20% chance of rain." in report.summary


async def test_weather_without_a_place_asks() -> None:
    tools = personal_tools(None, weather_handler)
    assert "Which city" in (await run(tools, "get_weather")).summary


RSS = b"""<?xml version="1.0"?><rss><channel>
<item><title>Rain alert for Kerala - The Hindu</title><source>The Hindu</source>
<pubDate>Sat, 24 Oct 2026 05:00:00 GMT</pubDate><link>https://news.google.com/a</link></item>
<item><title>Second story - BBC</title><source>BBC</source><pubDate>x</pubDate>
<link>https://news.google.com/b</link></item></channel></rss>"""

DDG = """
<div class="result"><a rel="nofollow" class="result__a"
 href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fpage&amp;rut=1">Example <b>Page</b></a>
<a class="result__snippet" href="x">The <b>answer</b> is 42.</a></div>
<div class="result"><a class="result__a" href="https://duckduckgo.com/y.js?ad=1">Ad</a></div>
"""


def web_handler(request: httpx.Request) -> httpx.Response:
    if request.url.host == "news.google.com":
        assert request.url.params["q"] == "kerala rain"
        assert request.url.params["ceid"] == "US:en"
        return httpx.Response(200, content=RSS)
    assert request.url.host == "html.duckduckgo.com"
    return httpx.Response(200, text=DDG)


async def test_news_and_search_results() -> None:
    tools = personal_tools(None, web_handler)
    headlines = await run(tools, "look_up", query="kerala rain", kind="news")
    assert headlines.data["headlines"][0].startswith("Rain alert for Kerala (The Hindu,")
    results = await run(tools, "look_up", query="meaning of life")
    assert results.data["results"] == [
        "Example Page | The answer is 42. | https://example.com/page"
    ]
    assert parse_results("<html>nothing</html>") == []


async def test_web_errors_are_reported() -> None:
    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline", request=request)

    failed = await run(personal_tools(None, down), "look_up", query="x")
    assert not failed.ok and "no connection" in failed.summary


# --- undo ---


async def test_undo_reverses_file_changes(tmp_path: Path) -> None:
    home = tmp_path / "home"
    (home / "Documents").mkdir(parents=True)
    (home / "Downloads").mkdir()
    (home / "Documents" / "notes.txt").write_text("hi")
    trashed: list[Path] = []

    def trash(path: Path) -> None:
        trashed.append(path)
        path.unlink()

    area = FileArea([home], home=home, trash=trash)
    journal = UndoJournal()
    tools = build_registry(
        ToolSettings(), FakeDesktop(), AppIndex([], apps_folder=False), area, undo=journal
    )
    assert not (await run(tools, "undo_last")).ok
    await run(tools, "move_item", source="documents/notes.txt", destination="downloads")
    await run(tools, "rename_item", path="downloads/notes.txt", new_name="old.txt")
    await run(tools, "create_folder", path="documents/Trip")
    await run(tools, "copy_item", source="downloads/old.txt", destination="documents")

    assert (await run(tools, "undo_last")).summary == "Sent the copy old.txt to the Recycle Bin."
    assert trashed == [(home / "Documents" / "old.txt").resolve()]
    assert (await run(tools, "undo_last")).ok and not (home / "Documents" / "Trip").exists()
    assert (await run(tools, "undo_last")).ok and (home / "Downloads" / "notes.txt").exists()
    assert (await run(tools, "undo_last")).ok
    assert (home / "Documents" / "notes.txt").read_text() == "hi"


async def test_a_folder_with_things_in_it_is_not_removed(tmp_path: Path) -> None:
    home = tmp_path / "home"
    (home / "Desktop").mkdir(parents=True)
    area = FileArea([home], home=home)
    tools = build_registry(ToolSettings(), FakeDesktop(), AppIndex([], apps_folder=False), area)
    await run(tools, "create_folder", path="desktop/Work")
    (home / "Desktop" / "Work" / "plan.txt").write_text("keep me")
    undone = await run(tools, "undo_last")
    assert not undone.ok and "isn't empty" in undone.summary
    assert (home / "Desktop" / "Work" / "plan.txt").exists()


# --- tool budget ---


@pytest.mark.parametrize(
    ("text", "wanted", "unwanted"),
    [
        ("what's the time", set(), {"files", "ui", "power"}),
        ("move my notes to downloads", {"files"}, {"ui"}),
        ("send mom a whatsapp message", {"ui"}, {"files"}),
        ("remind me in 10 minutes to stretch", {"reminders"}, {"ui"}),
        ("lock the pc", {"power"}, set()),
        ("turn the volume down", {"system"}, set()),
    ],
)
def test_groups_for(text: str, wanted: set[str], unwanted: set[str]) -> None:
    found = groups_for(text)
    assert wanted <= found and not (unwanted & found)


async def test_requests_carry_only_the_tools_they_need(tmp_path: Path, keep: Keep) -> None:
    router = ScriptedRouter(
        tool_reply("set_reminder", text="stretch", in_minutes=10),
        text_reply("Done."),
        text_reply("It's set."),
    )
    rig = Rig(tmp_path, router)
    rig.assistant._registry = personal_tools(keep)
    await rig.assistant.handle("remind me in 10 minutes to stretch")
    names = {t["function"]["name"] for t in router.requests[0]["tools"]}
    assert "set_reminder" in names and "move_item" not in names and "lock_pc" not in names
    full = len(json.dumps(rig.assistant._registry.declarations()))
    assert len(json.dumps(router.requests[0]["tools"])) < full * 0.6
    await rig.assistant.handle("and what did you set?")  # follow-up keeps the group
    names = {t["function"]["name"] for t in router.requests[2]["tools"]}
    assert "list_reminders" in names
