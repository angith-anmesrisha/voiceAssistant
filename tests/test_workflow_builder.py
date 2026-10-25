"""Workflows that start by themselves (time of day, JARVIS starting), the workflow tools,
the controller running a workflow once it's free, and the builder window."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from PySide6.QtWidgets import QApplication, QWidget

from jarvis.agent.assistant import Reply
from jarvis.agent.workflows import (
    Step,
    Trigger,
    Workflow,
    WorkflowStore,
    WorkflowTriggers,
    parse_clock,
    register_workflow_tools,
)
from jarvis.app.events import RunWorkflow
from jarvis.app.state import AppState
from jarvis.storage.keep import Keep
from jarvis.tools.registry import ToolRegistry
from jarvis.ui.workflows import WorkflowBuilder, problems
from tests.test_controller_and_ui import make_controller

NOW = datetime(2026, 10, 26, 7, 40).astimezone()  # a Monday


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    app = QApplication.instance() or QApplication([])
    assert isinstance(app, QApplication)
    return app


def timed(at: str, days: list[str] | None = None) -> Workflow:
    return Workflow(
        "Morning", Trigger("time", at=at, days=days or []), [Step("say", {"text": "Hi"})]
    )


@pytest.mark.parametrize(
    ("text", "clock"),
    [
        ("07:30", (7, 30)),
        ("7.05", (7, 5)),
        ("7 pm", (19, 0)),
        ("12:15 am", (0, 15)),
        ("25:00", None),
        ("soon", None),
    ],
)
def test_clock_times(text: str, clock: tuple[int, int] | None) -> None:
    assert parse_clock(text) == clock


async def test_a_timed_workflow_runs_once_a_day_up_to_30_minutes_late(tmp_path: Path) -> None:
    store = WorkflowStore(tmp_path / "w.json")
    keep = Keep(tmp_path / "keep.sqlite3")
    store.put(timed("07:30"))
    store.put(timed("07:00"))  # 40 minutes ago: too late, skipped today
    store.put(timed("07:30", ["tue"]))  # not on a Monday
    ran: list[tuple[str, str]] = []
    job = WorkflowTriggers(store, keep, lambda w, why: ran.append((w.trigger.at, why)))
    await job.tick(NOW)
    await job.tick(NOW + timedelta(minutes=1))
    assert ran == [("07:30", "it's 07:30")]
    again = WorkflowTriggers(store, keep, lambda w, why: ran.append((w.trigger.at, why)))
    await again.tick(NOW + timedelta(minutes=2))  # after a restart: still once
    assert len(ran) == 1
    await job.tick(NOW + timedelta(days=1))  # Tuesday: the 07:30 ones run again
    assert len(ran) == 3
    keep.close()


async def test_startup_workflows_run_once_shortly_after_start(tmp_path: Path) -> None:
    store = WorkflowStore(tmp_path / "w.json")
    keep = Keep(tmp_path / "keep.sqlite3")
    store.put(Workflow("Hello", Trigger("startup"), [Step("say", {"text": "Hi"})]))
    off = Workflow("Off", Trigger("startup"), [Step("say", {"text": "No"})], enabled=False)
    store.put(off)
    ran: list[str] = []

    async def run(workflow: Workflow, why: str) -> None:
        ran.append(workflow.name)

    job = WorkflowTriggers(store, keep, run)
    await job.tick(NOW)
    assert ran == []
    await job.tick(NOW + timedelta(seconds=10))
    await job.tick(NOW + timedelta(seconds=20))
    assert ran == ["Hello"]
    keep.close()


async def call(registry: ToolRegistry, name: str, args: dict[str, Any]) -> Any:
    spec = registry.get(name)
    assert spec is not None
    return await registry.run(spec, args)


async def test_workflow_tools_list_and_queue(tmp_path: Path) -> None:
    store = WorkflowStore(tmp_path / "w.json")
    store.put(
        Workflow("Study Mode", Trigger("phrase", ["study mode"]), [Step("say", {"text": "Go"})])
    )
    registry = ToolRegistry()
    queued: list[str] = []
    register_workflow_tools(registry, store, lambda w, why: queued.append(w.name))
    listed = await call(registry, "list_workflows", {})
    assert "Study Mode: When you say “study mode”; Say “Go”" in listed.data["workflows"][0]
    run = await call(registry, "run_workflow", {"name": "study mode"})
    assert run.ok and queued == ["Study Mode"]
    missing = await call(registry, "run_workflow", {"name": "gym"})
    assert not missing.ok


async def test_the_controller_runs_a_triggered_workflow_once_free(tmp_path: Path) -> None:
    _controller, rig = make_controller()
    store = WorkflowStore(tmp_path / "w.json")
    workflow = Workflow("Hello", Trigger("startup"), [Step("say", {"text": "Hi"})])
    store.put(workflow)
    ran: list[tuple[str, bool, str]] = []

    async def run_workflow(
        w: Workflow, *, spoken: bool = False, reason: str = "", variables: Any = None
    ) -> Reply:
        ran.append((w.name, spoken, reason))
        return Reply("Hello is done. Hi", ok=True)

    rig.assistant.workflows = store
    rig.assistant.run_workflow = run_workflow
    await rig.state.transition(AppState.THINKING)  # busy: it waits
    await rig.bus.publish(RunWorkflow(workflow.id, "JARVIS started"))
    await asyncio.sleep(0.05)
    assert ran == []
    await rig.state.transition(AppState.IDLE)
    for _ in range(40):
        if rig.speaker.spoken:
            break
        await asyncio.sleep(0.05)
    assert ran == [("Hello", True, "JARVIS started")]
    assert rig.speaker.spoken == ["Hello is done. Hi"]
    assert rig.state.state is AppState.IDLE


def test_problems_name_what_is_missing() -> None:
    empty = Workflow("", Trigger("time", at="later"), [Step("open_app", {})])
    found = " ".join(problems(empty))
    assert "name" in found and "HH:MM" in found and "Application" in found
    assert problems(timed("07:30")) == []


def edited(settings: dict[str, Any]) -> Any:
    def edit(step: Step, _parent: QWidget) -> Step:
        return Step(step.action, {**step.settings, **settings})

    return edit


def test_the_builder_makes_reorders_tests_and_saves(qapp: QApplication, tmp_path: Path) -> None:
    store = WorkflowStore(tmp_path / "w.json")
    tested: list[Workflow] = []
    builder = WorkflowBuilder(store, tested.append, edit_step=edited({"app": "Notion"}))
    builder.new_workflow()
    assert not builder.save_button.isEnabled()  # no phrase, no steps
    builder.name.setText("Study Mode")
    builder.phrases.setPlainText("activate study mode\nstudy time")
    builder._changed()
    builder.add_step("open_app")
    builder._edit_step = edited({"minutes": 25})
    builder.add_step("start_timer")
    builder._edit_step = edited({"title": "Checklist", "items": ["Water"]})
    builder.add_step("show_checklist")
    current = builder._current
    assert current is not None
    assert [s.action for s in current.steps] == ["open_app", "start_timer", "show_checklist"]
    builder.steps.setCurrentRow(2)
    builder.move_step(-1)
    builder.move_step(-1)
    assert current.steps[0].action == "show_checklist"
    builder.steps.setCurrentRow(1)
    builder.remove_step()
    builder.test()
    assert tested and tested[0].name == "Study Mode"
    assert store.all == []  # testing doesn't save
    assert builder.save()
    saved = WorkflowStore(tmp_path / "w.json").all
    assert len(saved) == 1 and saved[0].trigger.phrases == ["activate study mode", "study time"]
    assert [s.action for s in saved[0].steps] == ["show_checklist", "start_timer"]
    assert builder.list.count() == 1


def test_the_builder_shows_only_the_trigger_fields_it_needs(
    qapp: QApplication, tmp_path: Path
) -> None:
    store = WorkflowStore(tmp_path / "w.json")
    builder = WorkflowBuilder(store, lambda w: None)
    builder.add_template("Study Mode")
    builder.show()
    assert builder.phrases.isVisible() and not builder.at.isVisible()
    builder.trigger_kind.setCurrentIndex(1)  # time
    assert builder.at.isVisible() and not builder.phrases.isVisible()
    builder.at.setText("07:30")
    builder._changed()
    assert builder._current is not None and builder._current.trigger.kind == "time"
    assert builder.save()
    builder.duplicate()
    assert builder._current.name == "Study Mode copy"
    builder.close()
