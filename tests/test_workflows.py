"""Workflows: saving, finding by phrase, running steps with the usual confirmations,
passing results along a command chain, and proposals from plain English that are saved
only after a yes."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from jarvis.agent.assistant import Assistant, ConfirmRequest
from jarvis.agent.workflows import (
    ACTIONS,
    TRIGGER_ID,
    Link,
    ProposalError,
    Step,
    Trigger,
    Workflow,
    WorkflowStore,
    condition_met,
    fill,
    parse_proposal,
    run_order,
    templates,
    tool_call,
    workflow_request,
    would_loop,
)
from jarvis.app.config import ToolSettings
from jarvis.app.events import EntryKind, PlanUpdated, StepStatus
from jarvis.storage.keep import Keep
from jarvis.tools.apps import AppIndex
from jarvis.tools.builtin import build_registry
from jarvis.tools.files import FileArea
from jarvis.tools.tasks import parse_due
from tests.test_assistant import Rig, ScriptedRouter, text_reply


def study_mode() -> Workflow:
    return Workflow(
        "Study Mode",
        Trigger("phrase", ["activate study mode", "study mode"]),
        [
            Step("open_app", {"app": "Notion"}),
            Step("show_tasks", {"project": "Study"}),
            Step("start_timer", {"minutes": 25}),
            Step("show_checklist", {"title": "Study checklist", "items": "Water\nPhone away"}),
        ],
    )


def test_workflows_are_saved_and_found_by_phrase(tmp_path: Path) -> None:
    store = WorkflowStore(tmp_path / "workflows.json")
    store.put(study_mode())
    again = WorkflowStore(tmp_path / "workflows.json")
    assert [w.name for w in again.all] == ["Study Mode"]
    for said in ("Activate Study Mode.", "Jarvis, study mode", "run the study mode workflow"):
        assert again.match(said) is not None, said
    assert again.match("what is study mode in physics") is None
    workflow = again.all[0]
    workflow.enabled = False
    again.put(workflow)
    assert again.match("study mode") is None


def test_steps_become_tool_calls_and_text_is_passed_on() -> None:
    assert tool_call(Step("open_folder", {"folder": "documents/Study"})) == (
        "open_path",
        {"path": "documents/Study"},
    )
    assert tool_call(Step("start_timer", {"minutes": "25"})) == (
        "set_reminder",
        {"text": "Time's up", "in_minutes": 25.0},
    )
    assert tool_call(Step("tool", {"name": "set_volume", "arguments": '{"level": 30}'})) == (
        "set_volume",
        {"level": 30},
    )
    assert tool_call(Step("show_checklist", {"items": ["a"]})) is None
    assert fill("Outline from {previous} and {step 1}", ["one", "two"]) == (
        "Outline from two and one"
    )
    assert all(step.action in ACTIONS for t in templates() for step in t.steps)


@pytest.mark.parametrize(
    "text",
    [
        "Whenever I start work, open my project folder and show me my pending tasks.",
        "Create a workflow that opens Spotify and starts a 25 minute timer",
        "set up a routine for mornings",
        "When I say goodnight, lock the PC",
    ],
)
def test_workflow_requests_are_recognised(text: str) -> None:
    assert workflow_request(text)


@pytest.mark.parametrize(
    "text", ["When is my car service?", "what's a workflow", "open my project folder"]
)
def test_other_requests_are_not_workflows(text: str) -> None:
    assert not workflow_request(text)


def test_a_proposal_keeps_only_known_actions() -> None:
    reply = json.dumps(
        {
            "name": "Start work",
            "trigger": {"kind": "phrase", "phrases": ["start work"], "days": ["Monday", "xx"]},
            "steps": [
                {"action": "open_folder", "settings": {"folder": "documents/Projects"}},
                {"action": "run_shell", "settings": {"command": "del *"}},
                {"action": "show_tasks", "settings": {}},
            ],
        }
    )
    workflow = parse_proposal("<think>x</think>" + reply)
    assert [s.action for s in workflow.steps] == ["open_folder", "show_tasks"]
    assert workflow.trigger.days == ["mon"]
    with pytest.raises(ProposalError):
        parse_proposal('{"name": "x", "steps": [{"action": "run_shell"}]}')


class WorkflowRig(Rig):
    def __init__(self, tmp_path: Path, router: ScriptedRouter, approve: bool = True) -> None:
        super().__init__(tmp_path, router, approve)
        self.keep = Keep(tmp_path / "keep.sqlite3")
        self.store = self.store
        self.workflows = WorkflowStore(tmp_path / "workflows.json")
        self.plans: list[PlanUpdated] = []

        async def on_plan(event: PlanUpdated) -> None:
            self.plans.append(event)

        self.bus.subscribe(PlanUpdated, on_plan)
        (tmp_path / "Study").mkdir()

        async def confirm(request: ConfirmRequest) -> bool:
            self.confirms.append(request)
            return self.approve

        async def capture() -> bytes | None:
            return None

        async def briefing() -> str:
            return "Good morning. 24 degrees and clear."

        self.assistant = Assistant(
            router,  # type: ignore[arg-type]
            self.store,
            build_registry(
                ToolSettings(),
                self.desktop,
                AppIndex([], apps_folder=False),
                files=FileArea([tmp_path]),
                keep=self.keep,
            ),
            self.bus,
            self.state,
            confirm,
            capture,
            workflows=self.workflows,
            briefing=briefing,
        )


async def test_saying_the_phrase_runs_the_steps(tmp_path: Path) -> None:
    rig = WorkflowRig(tmp_path, ScriptedRouter())
    await rig.keep.add_task("Revise chapter 4", project="Study")
    workflow = study_mode()
    workflow.steps.pop(0)  # no installed apps in the sandbox
    workflow.steps.insert(0, Step("open_folder", {"folder": str(tmp_path / "Study")}))
    workflow.steps.append(Step("briefing", {}))
    rig.workflows.put(workflow)
    reply = await rig.assistant.handle("activate study mode", spoken=True)
    assert reply.text.startswith("Study Mode is done.")
    assert "24 degrees" in reply.text  # the briefing is said
    shown = [e for e in rig.entries if e.kind is EntryKind.ASSISTANT][-1].text
    assert "Revise chapter 4" in shown and "☐ Water" in shown
    final = rig.plans[-1]
    assert final.finished and all(s.status is StepStatus.DONE for s in final.steps)
    assert rig.router.requests == []  # no AI needed


async def test_a_declined_confirm_stops_the_chain(tmp_path: Path) -> None:
    rig = WorkflowRig(tmp_path, ScriptedRouter(), approve=False)
    rig.workflows.put(
        Workflow(
            "Chain",
            Trigger("phrase", ["chain"]),
            [
                Step("say", {"text": "First."}),
                Step("confirm", {"question": "Carry on?"}),
                Step("say", {"text": "Never said."}),
            ],
        )
    )
    reply = await rig.assistant.handle("chain")
    statuses = [s.status for s in rig.plans[-1].steps]
    assert statuses == [StepStatus.DONE, StepStatus.FAILED, StepStatus.SKIPPED]
    assert reply.text.split("\n\n")[-1] == "First."


async def test_command_chain_passes_results_to_the_next_step(tmp_path: Path) -> None:
    router = ScriptedRouter(text_reply("1. Intro\n2. Results"))
    rig = WorkflowRig(tmp_path, router)
    rig.workflows.put(
        Workflow(
            "Prepare my presentation",
            Trigger("phrase", ["prepare my presentation"]),
            [
                Step("say", {"text": "Key points: growth doubled"}),
                Step("ask_jarvis", {"request": "Outline these: {previous}"}),
                Step("save_text", {"path": str(tmp_path / "Outline.md"), "text": "{previous}"}),
            ],
        )
    )
    await rig.assistant.handle("prepare my presentation")
    asked = router.requests[0]["messages"][-1]["content"]
    assert asked == "Outline these: Key points: growth doubled"
    assert (tmp_path / "Outline.md").read_text(encoding="utf-8").startswith("1. Intro")


async def test_medium_risk_steps_still_ask(tmp_path: Path) -> None:
    rig = WorkflowRig(tmp_path, ScriptedRouter(), approve=False)
    rig.workflows.put(
        Workflow("Bedtime", Trigger("phrase", ["bedtime"]), [Step("tool", {"name": "lock_pc"})])
    )
    await rig.assistant.handle("bedtime")
    assert [c.tool for c in rig.confirms] == ["lock_pc"]
    assert rig.desktop.calls == []


async def test_a_proposed_workflow_is_saved_only_after_yes(tmp_path: Path) -> None:
    proposal = json.dumps(
        {
            "name": "Start work",
            "trigger": {"kind": "phrase", "phrases": ["start work"]},
            "steps": [
                {"action": "open_folder", "settings": {"folder": "documents/Projects"}},
                {"action": "show_tasks", "settings": {}},
            ],
        }
    )
    text = "Whenever I start work, open my project folder and show me my pending tasks."
    rig = WorkflowRig(tmp_path, ScriptedRouter(text_reply(proposal)), approve=False)
    reply = await rig.assistant.handle(text)
    assert "haven't saved" in reply.text and rig.workflows.all == []
    assert "Start work: When you say “start work”" in rig.confirms[0].description

    (tmp_path / "b").mkdir()
    yes = WorkflowRig(tmp_path / "b", ScriptedRouter(text_reply(proposal)))
    reply = await yes.assistant.handle(text)
    assert "Say “start work” to run it" in reply.text
    assert [w.name for w in yes.workflows.all] == ["Start work"]
    assert yes.workflows.match("start work") is not None


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("tomorrow", "2026-10-26 23:59"),
        ("friday 5 pm", "2026-10-30 17:00"),
        ("in 3 days", "2026-10-28 23:59"),
        ("2026-11-02", "2026-11-02 23:59"),
        ("2 nov", "2026-11-02 23:59"),
        ("today 18:30", "2026-10-25 18:30"),
    ],
)
def test_due_dates_are_understood(text: str, expected: str) -> None:
    from datetime import datetime

    now = datetime(2026, 10, 25, 9, 0).astimezone()  # a Sunday
    due = parse_due(text, now)
    assert due is not None and due.strftime("%Y-%m-%d %H:%M") == expected


async def test_tasks_and_ideas(tmp_path: Path) -> None:
    rig = WorkflowRig(tmp_path, ScriptedRouter())
    registry: Any = rig.assistant._registry
    add = registry.get("add_task")
    done = registry.get("complete_task")
    listed = registry.get("list_tasks")
    assert (
        await registry.run(add, {"text": "Essay draft", "project": "Study", "due": "tomorrow"})
    ).ok
    assert (await registry.run(add, {"text": "App for plant care", "kind": "idea"})).ok
    pending = await registry.run(listed, {})
    assert pending.summary == "1 tasks." and "Essay draft [Study] (due" in pending.data["tasks"][0]
    ideas = await registry.run(listed, {"kind": "idea"})
    assert "plant care" in ideas.data["ideas"][0]
    assert (await registry.run(done, {"text": "essay"})).ok
    assert (await registry.run(listed, {})).summary == "No pending tasks."


# --- the canvas: links, branches and node kinds ---


def branching() -> Workflow:
    steps = [
        Step("say", {"text": "Rain is likely after 3 pm."}),
        Step("condition", {"value": "{previous}", "test": "contains", "compare": "rain"}),
        Step("say", {"text": "Take an umbrella."}),
        Step("say", {"text": "No umbrella needed."}),
    ]
    ids = [s.id for s in steps]
    return Workflow(
        "Rain",
        Trigger("phrase", ["rain check"]),
        steps,
        links=[
            Link(TRIGGER_ID, "out", ids[0]),
            Link(ids[0], "out", ids[1]),
            Link(ids[1], "true", ids[2]),
            Link(ids[1], "false", ids[3]),
        ],
    )


def test_old_workflows_run_in_order_and_canvas_ones_keep_their_links() -> None:
    old = Workflow.from_json(study_mode().to_json())
    assert old.links is None and "links" not in old.to_json()
    assert run_order(old) == [0, 1, 2, 3]
    assert [link.dst for link in old.graph()] == [s.id for s in old.steps]
    flow = branching()
    flow.steps[2].x, flow.steps[2].y = 360.0, -80.0
    again = Workflow.from_json(json.loads(json.dumps(flow.to_json())))
    assert again.links == flow.links and again.steps[2].x == 360.0
    assert [s.id for s in again.steps] == [s.id for s in flow.steps]
    # A step nothing leads to doesn't run, and links can't make loops.
    flow.steps.append(Step("say", {"text": "Alone"}))
    assert sorted(run_order(flow)) == [0, 1, 2, 3]
    assert "not connected" in flow.describe()
    assert would_loop(flow, flow.steps[3].id, flow.steps[0].id)
    assert not would_loop(flow, flow.steps[0].id, flow.steps[4].id)
    # Links to steps that don't exist are dropped when loading.
    data = flow.to_json()
    data["links"].append({"src": "nope", "port": "out", "dst": flow.steps[0].id})
    assert len(Workflow.from_json(data).links or []) == 4


@pytest.mark.parametrize(
    ("settings", "met"),
    [
        ({"value": "Light rain at 4 pm", "test": "contains", "compare": "rain"}, True),
        ({"value": "Sunny", "test": "contains", "compare": "rain"}, False),
        ({"value": "Sunny", "test": "doesn't contain", "compare": "rain"}, True),
        ({"value": "", "test": "is empty"}, True),
        ({"value": "3 unread", "test": "isn't empty"}, True),
        ({"value": " Done ", "test": "equals", "compare": "done"}, True),
        ({"value": "Nifty is at 24,310.5", "test": "number above", "compare": "24000"}, True),
        ({"value": "Gold ₹7,100 per gram", "test": "number below", "compare": "7000"}, False),
        ({"value": "no number", "test": "number above", "compare": "1"}, False),
        ({"value": "The check failed: disk full", "test": "has errors"}, True),
        ({"value": "All good", "test": "has errors"}, False),
    ],
)
def test_if_nodes(settings: dict[str, Any], met: bool) -> None:
    assert condition_met(settings) is met


def test_every_node_runs_a_tool_that_exists() -> None:
    source = "\n".join(p.read_text(encoding="utf-8") for p in Path("jarvis").rglob("*.py"))
    for action in ACTIONS.values():
        if action.tool:
            assert f'"{action.tool}",' in source, action.name
    groups = {action.group for action in ACTIONS.values()}
    assert {"Gmail", "Calendar", "Notes & memory", "Tasks & reminders"} <= groups


async def test_an_if_node_takes_one_branch(tmp_path: Path) -> None:
    rig = WorkflowRig(tmp_path, ScriptedRouter())
    flow = branching()
    rig.workflows.put(flow)
    reply = await rig.assistant.handle("rain check")
    assert reply.text.startswith("Rain is done.") and "No umbrella" not in reply.text
    assert reply.text.split("\n\n")[-1] == "Take an umbrella."
    final = rig.plans[-1]
    assert final.workflow_id == flow.id and final.ids == tuple(s.id for s in flow.steps)
    assert [s.status for s in final.steps] == [
        StepStatus.DONE,
        StepStatus.DONE,
        StepStatus.DONE,
        StepStatus.SKIPPED,
    ]
    assert final.steps[1].note == "True"


async def test_a_no_branch_runs_when_you_say_no(tmp_path: Path) -> None:
    rig = WorkflowRig(tmp_path, ScriptedRouter(), approve=False)
    steps = [
        Step("confirm", {"question": "Draft replies?"}),
        Step("say", {"text": "Drafting."}),
        Step("say", {"text": "Fine, no drafts."}),
    ]
    ids = [s.id for s in steps]
    links = [
        Link(TRIGGER_ID, "out", ids[0]),
        Link(ids[0], "yes", ids[1]),
        Link(ids[0], "no", ids[2]),
    ]
    rig.workflows.put(Workflow("Ask", Trigger("phrase", ["ask me"]), steps, links=links))
    reply = await rig.assistant.handle("ask me")
    statuses = [s.status for s in rig.plans[-1].steps]
    assert statuses == [StepStatus.DONE, StepStatus.SKIPPED, StepStatus.DONE]
    assert reply.text.split("\n\n")[-1] == "Fine, no drafts."


async def test_ai_nodes_ask_jarvis_with_the_previous_result(tmp_path: Path) -> None:
    router = ScriptedRouter(text_reply("- Growth doubled"))
    rig = WorkflowRig(tmp_path, router)
    rig.workflows.put(
        Workflow(
            "Sum up",
            Trigger("phrase", ["sum it up"]),
            [
                Step("say", {"text": "Sales grew from 10 to 20 this year."}),
                Step("summarise", {"text": "{previous}", "style": "bullets"}),
            ],
        )
    )
    reply = await rig.assistant.handle("sum it up")
    asked = router.requests[0]["messages"][-1]["content"]
    assert asked.startswith("Summarise this in up to five bullet points")
    assert asked.endswith("Sales grew from 10 to 20 this year.")
    assert "Growth doubled" in reply.text


def test_every_template_is_ready_to_save() -> None:
    from jarvis.ui.workflows import problems

    for template in templates():
        assert problems(template) == [], template.name
        assert len(run_order(template)) == len(template.steps), template.name
