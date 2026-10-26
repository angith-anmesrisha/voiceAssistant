"""Workflows that start by themselves (time of day, JARVIS starting), the workflow tools,
the controller running a workflow once it's free, and the builder window."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from PySide6.QtCore import QPointF
from PySide6.QtWidgets import QApplication

from jarvis.agent.assistant import Reply
from jarvis.agent.workflows import (
    TRIGGER_ID,
    Step,
    Trigger,
    Workflow,
    WorkflowStore,
    WorkflowTriggers,
    parse_clock,
    register_workflow_tools,
)
from jarvis.app.events import PlanStep, PlanUpdated, RunWorkflow, StepStatus
from jarvis.app.state import AppState
from jarvis.storage.keep import Keep
from jarvis.tools.registry import ToolRegistry
from jarvis.ui.workflows import WorkflowBuilder, problems, warnings
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


def now(builder: WorkflowBuilder) -> Workflow:
    assert builder.current is not None
    return builder.current


def add_from_panel(builder: WorkflowBuilder, action: str) -> Step:
    """Press + and pick ``action`` in the node panel."""
    builder.open_palette()
    assert not builder.pane.node_panel.isHidden()
    builder.pane.node_panel.chosen.emit(action)
    current = builder.current
    assert current is not None
    return current.steps[-1]


def test_the_canvas_builder_links_tests_and_saves(qapp: QApplication, tmp_path: Path) -> None:
    store = WorkflowStore(tmp_path / "w.json")
    tested: list[Workflow] = []
    builder = WorkflowBuilder(store, tested.append)
    builder.show()
    builder.new_workflow()
    assert builder.save_button.isEnabled()  # always; a press says what is missing
    builder.name.setText("Study Mode")
    builder.select_node(TRIGGER_ID)
    assert "phrases" in builder.inspector.fields
    builder.set_trigger(phrases=["activate study mode", "study time"])
    app = add_from_panel(builder, "open_app")
    builder.set_setting(app.id, "app", "Notion")
    timer = add_from_panel(builder, "start_timer")  # + goes after the last node
    check = add_from_panel(builder, "show_checklist")
    builder.set_setting(check.id, "items", ["Water"])
    current = builder.current
    assert current is not None and current.links is not None
    assert [(link.src, link.dst) for link in current.links] == [
        (TRIGGER_ID, app.id),
        (app.id, timer.id),
        (timer.id, check.id),
    ]
    assert len(builder.scene.nodes) == 4 and len(builder.scene.links) == 3
    # No loops, and nothing links into the trigger.
    assert not builder.connect_nodes(check.id, "out", app.id)
    assert not builder.connect_nodes(app.id, "out", TRIGGER_ID)
    # Removing the middle node keeps the chain whole; undo brings it back.
    builder.remove_node(timer.id)
    after = builder.current
    assert after is not None and [s.action for s in after.steps] == ["open_app", "show_checklist"]
    assert (app.id, check.id) in {(link.src, link.dst) for link in after.links or []}
    builder.undo()
    assert [s.action for s in now(builder).steps] == [
        "open_app",
        "start_timer",
        "show_checklist",
    ]
    builder.redo()
    assert len(now(builder).steps) == 2
    builder.test()
    assert tested and tested[0].name == "Study Mode"
    assert builder.log.toPlainText().count("Testing “Study Mode”") == 1
    assert store.all == []  # testing doesn't save
    assert builder.dirty and builder.save()
    assert not builder.dirty
    saved = WorkflowStore(tmp_path / "w.json").all
    assert len(saved) == 1 and saved[0].trigger.phrases == ["activate study mode", "study time"]
    assert [s.action for s in saved[0].steps] == ["open_app", "show_checklist"]
    assert saved[0].steps[0].settings["app"] == "Notion" and saved[0].steps[0].x is not None
    assert builder.list.count() == 1
    builder.close()


def test_branches_unlinking_and_the_node_panel(qapp: QApplication, tmp_path: Path) -> None:
    builder = WorkflowBuilder(WorkflowStore(tmp_path / "w.json"), lambda w: None)
    builder.new_workflow()
    builder.set_trigger(phrases=["rain check"])
    weather = add_from_panel(builder, "weather")
    test = add_from_panel(builder, "condition")
    builder.set_setting(test.id, "compare", "rain")
    builder._plus(test.id, "true")
    builder.pane.node_panel.chosen.emit("say")
    yes = now(builder).steps[-1]
    builder._plus(test.id, "false")
    builder.pane.node_panel.chosen.emit("say")
    no = now(builder).steps[-1]
    assert (yes.y or 0) < (no.y or 0)  # true above false
    links = {(link.src, link.port, link.dst) for link in now(builder).links or []}
    assert {(test.id, "true", yes.id), (test.id, "false", no.id)} <= links
    assert builder.scene.nodes[test.id].linked_ports == {"true", "false"}
    builder.unlink(test.id, "false", no.id)
    assert "isn't linked" in " ".join(warnings(now(builder)))
    assert builder.connect_nodes(test.id, "false", no.id)
    assert weather.id in builder.scene.nodes
    # The panel's search finds nodes by group and words.
    panel = builder.pane.node_panel
    panel.open()
    panel.search.setText("gmail")
    assert {"email_summary", "email_drafts", "check_email"} <= set(panel.visible_names())
    panel.search.setText("calendar")
    assert "calendar_events" in panel.visible_names()
    panel.hide()


def test_the_trigger_settings_follow_its_kind(qapp: QApplication, tmp_path: Path) -> None:
    store = WorkflowStore(tmp_path / "w.json")
    builder = WorkflowBuilder(store, lambda w: None)
    builder.add_template("Study Mode")
    builder.select_node(TRIGGER_ID)
    assert "phrases" in builder.inspector.fields and "at" not in builder.inspector.fields
    builder.set_trigger(kind="time")
    assert "at" in builder.inspector.fields and "phrases" not in builder.inspector.fields
    builder.set_trigger(at="07:45")
    assert builder.current is not None and builder.current.trigger.kind == "time"
    assert builder.save()
    builder.duplicate()
    assert builder.current is not None and builder.current.name == "Study Mode copy"


def test_old_workflows_open_laid_out_and_light_up_when_tested(
    qapp: QApplication, tmp_path: Path
) -> None:
    store = WorkflowStore(tmp_path / "w.json")
    old = Workflow(
        "Old", Trigger("phrase", ["old"]), [Step("say", {"text": "a"}), Step("say", {"text": "b"})]
    )
    store.put(old)
    builder = WorkflowBuilder(store, lambda w: None)
    current = builder.current
    assert current is not None and current.links is None  # untouched until edited
    assert [s.x for s in current.steps] == [180.0, 360.0]
    assert not builder.dirty
    ids = tuple(s.id for s in current.steps)
    running = (PlanStep("a", StepStatus.DONE, "Said"), PlanStep("b", StepStatus.RUNNING))
    builder.show_run(PlanUpdated(running, workflow_id=current.id, ids=ids))
    assert builder.scene.nodes[ids[0]].status == "done"
    assert builder.scene.nodes[ids[1]].status == "running"
    assert builder.scene.links[0].fired
    builder.show_run(PlanUpdated(running, workflow_id="another", ids=ids))  # ignored
    finished = (PlanStep("a", StepStatus.DONE), PlanStep("b", StepStatus.FAILED, "No"))
    builder.show_run(PlanUpdated(finished, finished=True, workflow_id=current.id, ids=ids))
    assert "Finished: 1 done, 1 failed" in builder.log.toPlainText()


def test_dragging_a_link_to_empty_canvas_offers_a_node_there(
    qapp: QApplication, tmp_path: Path
) -> None:
    builder = WorkflowBuilder(WorkflowStore(tmp_path / "w.json"), lambda w: None)
    builder.add_template("Rain check")
    current = builder.current
    assert current is not None
    last = builder.scene.nodes[current.steps[-1].id]
    builder.scene.begin_wire(last, "out")
    builder.scene.end_wire(last, "out", QPointF(2000, 900))
    assert not builder.pane.node_panel.isHidden()
    builder.pane.node_panel.chosen.emit("note")
    added = current.steps[-1]
    assert added.action == "note" and (added.x, added.y) == (2000.0, 860.0)
    # Dropping on another node links them instead.
    first = builder.scene.nodes[current.steps[0].id]
    builder.scene.begin_wire(builder.scene.nodes[added.id], "out")
    builder.scene.end_wire(builder.scene.nodes[added.id], "out", first.sceneBoundingRect().center())
    assert builder.current is not None
    assert not [link for link in now(builder).links or [] if link.src == added.id]  # a loop
    builder.tidy()
    assert now(builder).steps[0].x == 180.0
