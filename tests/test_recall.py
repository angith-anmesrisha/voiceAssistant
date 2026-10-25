"""Recall: "continue the project I worked on yesterday" gathers notes, requests, tasks
and changed files for the period (and project) so the AI can sum up and suggest the
next step."""

from __future__ import annotations

import os
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from jarvis.agent.prompts import guidance
from jarvis.agent.toolbudget import groups_for
from jarvis.storage.db import HistoryStore
from jarvis.storage.keep import Keep
from jarvis.storage.memory import Memory
from jarvis.storage.vault import Vault
from jarvis.tools.recall import period, recent_files, work_material

NOW = datetime(2026, 10, 26, 10, 0).astimezone()  # a Monday


@pytest.mark.parametrize(
    ("when", "first", "days", "label"),
    [
        ("yesterday", date(2026, 10, 25), 1, "yesterday"),
        ("today", date(2026, 10, 26), 1, "today"),
        ("", date(2026, 10, 20), 7, "the last seven days"),
        ("last week", date(2026, 10, 19), 7, "last week"),
        ("last 3 days", date(2026, 10, 24), 3, "the last 3 days"),
        ("friday", date(2026, 10, 23), 1, "Friday 23 October"),
        ("monday", date(2026, 10, 19), 1, "Monday 19 October"),
        ("2026-10-21", date(2026, 10, 21), 1, "Wednesday 21 October"),
    ],
)
def test_periods(when: str, first: date, days: int, label: str) -> None:
    start, end, said = period(when, NOW)
    assert start.date() == first and (end - start).days == days and said == label


def test_files_changed_in_the_period_matching_first(tmp_path: Path) -> None:
    docs = tmp_path / "Documents"
    (docs / "Robot arm").mkdir(parents=True)
    (docs / "node_modules").mkdir()
    yesterday = (NOW - timedelta(days=1)).timestamp()
    for path in (docs / "Robot arm" / "kinematics.py", docs / "essay.docx",
                 docs / "node_modules" / "x.js"):  # fmt: skip
        path.write_text("x")
        os.utime(path, (yesterday, yesterday))
    old = docs / "old.txt"
    old.write_text("x")
    os.utime(old, (yesterday - 10 * 86400, yesterday - 10 * 86400))
    start, end, _ = period("yesterday", NOW)
    found = recent_files([docs], start, end, {"robot"})
    assert len(found) == 2
    assert found[0].endswith("kinematics.py") and found[1].endswith("essay.docx")


async def test_work_material_for_a_project(tmp_path: Path) -> None:
    keep = Keep(tmp_path / "keep.sqlite3")
    history = HistoryStore(tmp_path / "jarvis.sqlite3")
    vault = Vault(tmp_path / "vault")
    vault.ensure()
    memory = Memory(vault)
    now = datetime.now(UTC).astimezone()  # the history store stamps messages with real time
    yesterday = now - timedelta(days=1)
    vault.log_exchange("How do I fix the robot arm's inverse kinematics?", "Use…", yesterday)
    await keep.add_summary(4, yesterday.date().isoformat(), "Worked on the robot arm IK.")
    await keep.add_task("Tune the PID for the robot arm", project="Robot arm")
    await keep.add_task("Buy milk")
    conversation = await history.new_conversation()
    await history.add_message(conversation, {"role": "user", "content": "robot arm IK help"})
    await history.add_message(conversation, {"role": "user", "content": "weather?"})
    material = await work_material(keep, history, memory, [], "", "robot arm", now=now)
    assert material["project"] == "robot arm"
    assert any("robot arm IK" in s for s in material["conversation_summaries"])
    assert material["what_you_asked"] == ["robot arm IK help"]
    assert material["still_open"] == ["task: 1. Tune the PID for the robot arm [Robot arm]"]
    assert "inverse kinematics" in material["day_notes"][0]
    keep.close()
    history.close()


@pytest.mark.parametrize(
    "text",
    [
        "continue the project I worked on yesterday",
        "where did I leave off?",
        "what was I working on last week",
        "pick up where I left off on the robot arm",
    ],
)
def test_recall_requests_get_the_tool_and_the_guidance(text: str) -> None:
    assert "recall" in groups_for(text)
    assert "recall_work" in guidance(text)
