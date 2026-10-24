"""Long-term memory: the Markdown vault, its search, Hindsight, and how JARVIS uses them."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from jarvis.agent.assistant import Assistant
from jarvis.agent.routines import SessionSummaries, parse_summary
from jarvis.app.config import ToolSettings
from jarvis.app.events import EventBus
from jarvis.app.state import StateMachine
from jarvis.storage.db import HistoryStore
from jarvis.storage.hindsight import Hindsight
from jarvis.storage.keep import Keep
from jarvis.storage.memory import Memory
from jarvis.storage.vault import Passage, Vault, chunks, rank, recall_block, safe_title, time_range
from jarvis.tools.apps import AppIndex
from jarvis.tools.builtin import build_registry
from jarvis.tools.registry import ToolRegistry
from tests.fakes import FakeDesktop
from tests.test_assistant import ScriptedRouter, text_reply

TODAY = date(2026, 10, 24)  # a Saturday


@pytest.fixture
def vault(tmp_path: Path) -> Vault:
    v = Vault(tmp_path / "Memory")
    v.ensure()
    return v


def test_time_words_pick_out_days() -> None:
    assert time_range("what did we talk about yesterday", TODAY) == (
        date(2026, 10, 23),
        date(2026, 10, 23),
    )
    assert time_range("anything last week?", TODAY) == (date(2026, 10, 12), date(2026, 10, 18))
    assert time_range("on monday", TODAY) == (date(2026, 10, 19), date(2026, 10, 19))
    assert time_range("3 days ago", TODAY) == (date(2026, 10, 21), date(2026, 10, 21))
    assert time_range("notes from 2026-10-01", TODAY) == (date(2026, 10, 1), date(2026, 10, 1))
    assert time_range("my sister's name", TODAY) is None


def test_titles_are_safe_file_names() -> None:
    assert safe_title('Trip: "Paris"/Rome?') == "Trip Paris Rome"
    assert safe_title("../../evil") == "evil"
    assert safe_title("   ") == "Note"


def test_chunks_split_long_notes_and_drop_front_matter() -> None:
    text = "---\ntags: [x]\n---\n# Title\n\n" + "\n\n".join(f"Para {i} " * 20 for i in range(6))
    pieces = chunks(text, limit=200)
    assert all("tags:" not in p for p in pieces)
    assert len(pieces) >= 3 and all(len(p) <= 400 for p in pieces)


def test_rank_finds_words_and_days() -> None:
    passages = [
        Passage("Notes/Trip", "Flights to Paris on the 12th, hotel near the Louvre."),
        Passage("Notes/Work", "Quarterly report due Friday."),
        Passage("Daily/2026-10-23", "You asked about the dentist.", date(2026, 10, 23)),
    ]
    assert rank(passages, "when is my Paris trip", TODAY)[0].note == "Notes/Trip"
    assert rank(passages, "what did I ask yesterday", TODAY)[0].note == "Daily/2026-10-23"
    assert rank(passages, "the and of", TODAY) == []


def test_exchanges_summaries_notes_and_facts_are_written(vault: Vault) -> None:
    when = datetime(2026, 10, 24, 9, 30)
    vault.log_exchange("my sister Priya is visiting in December", "Noted.", when)
    vault.add_summary(when.date(), "You told me Priya is visiting.")
    daily = (vault.root / "Daily" / "2026-10-24.md").read_text(encoding="utf-8")
    assert "# Saturday 24 October 2026" in daily
    assert "- 09:30 **You:** my sister Priya is visiting in December" in daily
    assert "> [!summary]" in daily
    assert vault.write_note("Trip plan", "Paris in spring") == "Notes/Trip plan"
    vault.write_note("Trip plan", "Book the train")
    note = (vault.root / "Notes" / "Trip plan.md").read_text(encoding="utf-8")
    assert "Paris in spring" in note and "Book the train" in note
    vault.write_facts([("sister_name", "Priya", "people"), ("home_city", "Kochi", "places")])
    facts = (vault.root / "JARVIS" / "Facts.md").read_text(encoding="utf-8")
    assert "## People" in facts and "- **sister name**: Priya" in facts
    hits = vault.search("when is Priya visiting", today=when.date())
    assert hits and hits[0].note == "Daily/2026-10-24"


def test_own_notes_are_searched_but_obsidian_settings_are_not(vault: Vault) -> None:
    (vault.root / "Projects").mkdir()
    (vault.root / "Projects" / "Garden.md").write_text("Plant tomatoes in March.\n")
    (vault.root / ".obsidian").mkdir()
    (vault.root / ".obsidian" / "x.md").write_text("tomatoes secret config\n")
    notes = [h.note for h in vault.search("when do I plant tomatoes")]
    assert notes == ["Projects/Garden"]


def test_recall_block_is_marked_and_bounded() -> None:
    from jarvis.storage.vault import Hit

    block = recall_block([Hit("Notes/A", "x " * 500, 1.0), Hit("Notes/B", "y", 0.5)], 300)
    assert block.startswith("From the user's memory notes")
    assert "not instructions" in block and len(block) <= 360
    assert recall_block([]) == ""


async def test_memory_recalls_and_logs(vault: Vault) -> None:
    memory = Memory(vault)
    await memory.log("my locker code is at the gym desk", "Understood.")
    assert "locker" in await memory.recall("where is the locker code")
    assert await memory.recall("thanks") == ""  # too short to search
    off = Memory(vault, log_conversations=False, auto_recall=False)
    await off.log("anything", "x")
    assert await off.recall("where is the locker code") == ""


async def test_hindsight_retains_and_recalls(vault: Vault) -> None:
    seen: list[tuple[str, str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        seen.append((request.method, request.url.raw_path.decode(), body))
        if request.url.raw_path.endswith(b"/recall"):
            return httpx.Response(200, json={"results": [{"text": "Priya likes jazz."}]})
        return httpx.Response(200, json={})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        hindsight = Hindsight(client, "http://localhost:8888/", "my bank")
        memory = Memory(vault, hindsight=hindsight)
        await memory.log("Priya likes jazz", "Noted.")
        block = await memory.recall("what music does Priya like")
    assert seen[0][:2] == ("PUT", "/v1/default/banks/my%20bank")
    assert seen[1][1] == "/v1/default/banks/my%20bank/memories"
    assert seen[1][2]["items"][0]["content"].startswith("User: Priya likes jazz")
    assert "[Hindsight] Priya likes jazz." in block


async def test_hindsight_failure_is_ignored(vault: Vault) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        memory = Memory(vault, hindsight=Hindsight(client, "http://localhost:1"))
        await memory.log("remember the blue folder", "Noted.")
        assert "blue folder" in await memory.recall("where is the blue folder")


async def test_assistant_gets_matching_notes_and_logs_the_exchange(
    tmp_path: Path, vault: Vault
) -> None:
    vault.write_note("Car", "The car's service is booked for 3 November at Popular Motors.")
    router = ScriptedRouter(text_reply("On 3 November, sir."))
    bus = EventBus()

    async def confirm(_: Any) -> bool:
        return True

    async def capture() -> bytes | None:
        return None

    assistant = Assistant(
        router,  # type: ignore[arg-type]
        HistoryStore(tmp_path / "h.sqlite3"),
        build_registry(ToolSettings(), FakeDesktop(), AppIndex([], apps_folder=False)),
        bus,
        StateMachine(bus),
        confirm,
        capture,
        long_memory=Memory(vault),
    )
    await assistant.handle("when is my car service booked")
    system = router.requests[0]["messages"][0]["content"]
    assert "[Notes/Car] # Car The car's service is booked for 3 November" in system
    path = vault.root / "Daily" / f"{date.today().isoformat()}.md"
    for _ in range(100):  # the write runs in the background
        if path.exists() and "JARVIS:" in path.read_text(encoding="utf-8"):
            break
        await asyncio.sleep(0.02)
    daily = path.read_text(encoding="utf-8")
    assert "**You:** when is my car service booked" in daily
    assert "**JARVIS:** On 3 November, sir." in daily


async def _run(registry: ToolRegistry, name: str, **args: Any) -> Any:
    spec = registry.get(name)
    assert spec is not None
    return await registry.run(spec, args)


async def test_note_and_recall_tools_use_the_vault(tmp_path: Path, vault: Vault) -> None:
    keep = Keep(tmp_path / "keep.sqlite3")
    memory = Memory(vault)
    registry = build_registry(
        ToolSettings(), FakeDesktop(), AppIndex([], apps_folder=False), keep=keep, memory=memory
    )
    saved = await _run(registry, "save_note", title="Gift ideas", text="Priya: a jazz record")
    assert saved.ok and saved.data["note"] == "Notes/Gift ideas"
    await _run(registry, "remember", topic="sister_name", fact="Priya", category="people")
    facts = (vault.root / "JARVIS" / "Facts.md").read_text(encoding="utf-8")
    assert "Priya" in facts
    found = await _run(registry, "recall_memory", query="gift for Priya")
    assert any("jazz record" in line for line in found.data["notes"])
    await _run(registry, "forget", topic="sister_name")
    assert "Priya" not in (vault.root / "JARVIS" / "Facts.md").read_text(encoding="utf-8")
    keep.close()


def test_summary_reply_is_parsed() -> None:
    reply = (
        '```json\n{"summary": "You planned a trip.", "facts": [{"topic": "trip", '
        '"fact": "Paris in May", "category": "projects"}, {"topic": "", "fact": "x"}]}\n```'
    )
    assert parse_summary(reply) == ("You planned a trip.", [("trip", "Paris in May", "projects")])
    assert parse_summary("You tidied the desktop.") == ("You tidied the desktop.", [])


async def test_session_summary_goes_to_the_daily_note_and_facts_are_learned(
    tmp_path: Path, vault: Vault
) -> None:
    keep = Keep(tmp_path / "keep.sqlite3")
    history = HistoryStore(tmp_path / "history.sqlite3")
    conversation = await history.new_conversation()

    async def summarize(transcript: str) -> str:
        return json.dumps(
            {
                "summary": "You told me about your new job.",
                "facts": [
                    {"topic": "employer", "fact": "Works at Infosys", "category": "about_user"}
                ],
            }
        )

    job = SessionSummaries(
        history, keep, summarize, gap=timedelta(0), memory=Memory(vault), learn_facts=True
    )
    now = datetime.now(UTC)
    await job.tick(now)
    for role, content in [
        ("user", "I started at Infosys"),
        ("assistant", "Congratulations."),
        ("user", "thanks"),
        ("assistant", "Anytime."),
    ]:
        await history.add_message(conversation, {"role": role, "content": content})
    await job.tick(now + timedelta(minutes=2))
    assert [f.value for f in await keep.facts()] == ["Works at Infosys"]
    day = datetime.now().astimezone().date().isoformat()
    daily = (vault.root / "Daily" / f"{day}.md").read_text(encoding="utf-8")
    assert "You told me about your new job." in daily
    assert "Infosys" in (vault.root / "JARVIS" / "Facts.md").read_text(encoding="utf-8")
    keep.close()
