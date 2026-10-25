"""Quick capture (tasks, ideas and notes saved as said, no model), the screen assistant's
wording, and memory search finding tasks and ideas."""

from __future__ import annotations

from pathlib import Path

import pytest

from jarvis.agent.commands import Capture, capture_command
from jarvis.agent.prompts import wants_screen
from jarvis.storage.keep import Keep
from jarvis.tools.personal import register_personal_tools
from jarvis.tools.registry import ToolRegistry
from tests.test_assistant import ScriptedRouter
from tests.test_workflows import WorkflowRig


@pytest.mark.parametrize(
    ("text", "capture"),
    [
        ("idea: a plant-watering bot", Capture("idea", "a plant-watering bot")),
        ("Save this as a project idea: smart mirror", Capture("idea", "smart mirror")),
        ("add an idea for JARVIS: wake on clap", Capture("idea", "wake on clap", "JARVIS")),
        ("add a task to call the bank tomorrow", Capture("task", "call the bank", due="tomorrow")),
        (
            "Jarvis, add a study task: revise chapter 4 due friday",
            Capture("task", "revise chapter 4", "Study", "friday"),
        ),
        ("todo: renew passport by 2 nov", Capture("task", "renew passport", due="2 nov")),
        ("add milk to my shopping tasks", Capture("task", "milk", "Shopping")),
        ("add revise optics to my to-do list", Capture("task", "revise optics")),
        ("quick note: parked on level 3", Capture("note", "parked on level 3")),
        ("add milk to my shopping list in Notepad", None),
        ("create a task in Notion to review the design", None),
        ("save this as an idea", None),
        ("what is a good idea for a gift", None),
        ("make a folder called ideas", None),
    ],
)
def test_capture_forms(text: str, capture: Capture | None) -> None:
    assert capture_command(text) == capture


async def test_a_capture_is_saved_without_a_model(tmp_path: Path) -> None:
    router = ScriptedRouter()
    rig = WorkflowRig(tmp_path, router)
    reply = await rig.assistant.handle("add a study task: revise chapter 4 due tomorrow")
    assert reply.ok and reply.text.startswith("Task 1 saved under Study, due tomorrow")
    idea = await rig.assistant.handle("idea: a plant-watering bot")
    assert idea.text == "Idea 2 saved: a plant-watering bot"
    [task] = await rig.keep.tasks(project="Study")
    assert task.text == "revise chapter 4" and task.due is not None
    assert router.requests == []


@pytest.mark.parametrize(
    "text",
    ["explain this error", "what should I click next", "is this a scam?", "check this email"],
)
def test_screen_assistant_wording(text: str) -> None:
    assert wants_screen(text)


async def test_memory_search_finds_tasks_and_ideas(tmp_path: Path) -> None:
    keep = Keep(tmp_path / "keep.sqlite3")
    await keep.add_task("Plant-watering bot", kind="idea", project="Robotics")
    await keep.add_task("Buy cables")
    registry = ToolRegistry()
    register_personal_tools(registry, keep, None)
    spec = registry.get("recall_memory")
    assert spec is not None
    result = await registry.run(spec, {"query": "robotics ideas"})
    assert result.data["tasks_and_ideas"] == ["idea 1 [Robotics] (open): Plant-watering bot"]
    keep.close()
