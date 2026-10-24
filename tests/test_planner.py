"""Long requests are split into tasks and carried out one after another."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication

from jarvis.agent.assistant import plan_summary
from jarvis.agent.planner import MAX_TASKS, Task, needs_plan, parse_plan, step_request
from jarvis.app.events import EntryKind, PlanStep, PlanUpdated, StepStatus
from jarvis.app.state import AppState
from jarvis.providers.base import Capability
from jarvis.providers.router import AllProvidersFailedError
from tests.test_assistant import Rig, ScriptedRouter, text_reply, tool_reply
from tests.test_controller_and_ui import qapp

__all__ = ["qapp"]

LONG = (
    "open spotify and play something relaxing, then create a folder called Trip on my "
    "desktop, and after that tell me a fun fact about Paris"
)


def plan(*tasks: tuple[str, str]) -> str:
    return json.dumps({"tasks": [{"title": t, "do": d} for t, d in tasks]})


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("what time is it", False),
        ("open notepad then type hello", False),  # short: the agent loop handles it
        (LONG, True),
        ("open spotify, create a folder called Work on the desktop, and lock the PC", True),
        ("1. open notepad 2. type a shopping list for this week 3. save it", True),
        ("can you explain how a transformer neural network works in simple words", False),
        (" ".join(["word"] * 30), True),
    ],
)
def test_needs_plan(text: str, expected: bool) -> None:
    assert needs_plan(text) is expected


def test_parse_plan_is_forgiving() -> None:
    fenced = (
        "```json\n" + plan(("Open Spotify", "Open Spotify."), ("Fact", "Tell a fact.")) + "\n```"
    )
    assert parse_plan(fenced) == [
        Task("Open Spotify", "Open Spotify."),
        Task("Fact", "Tell a fact."),
    ]
    assert parse_plan("Sure! I'll do that.") == []
    assert parse_plan('{"tasks": "nope"}') == []
    assert parse_plan('{"tasks": [{"title": "x"}, 3]}') == []  # no "do": nothing to run
    many = plan(*[(f"T{i}", f"do {i}") for i in range(12)])
    assert len(parse_plan(many)) == MAX_TASKS
    long_title = parse_plan(plan(("one two three four five six seven", "x")))
    assert long_title[0].title == "one two three four five six"


def test_step_request_names_only_this_task() -> None:
    tasks = [Task("A", "Open Spotify."), Task("B", "Make a folder.")]
    text = step_request("the whole thing", tasks, 1, ["1. done: A: Opened."])
    assert "task 2 of 2" in text and "This task: Make a folder." in text
    assert "the whole thing" in text and "1. done: A: Opened." in text


async def test_long_request_runs_each_task(tmp_path: Path) -> None:
    router = ScriptedRouter(
        text_reply(
            plan(
                ("Open Spotify", "Open Spotify."),
                ("Make Trip folder", "Create a folder called Trip on the desktop."),
                ("Paris fact", "Tell the user a fun fact about Paris."),
            )
        ),
        tool_reply("open_app", name="spotify"),
        text_reply("Spotify is open."),
        text_reply("Made the Trip folder."),
        text_reply("The Eiffel Tower grows in summer."),
    )
    rig = Rig(tmp_path, router)
    plans: list[PlanUpdated] = []

    async def on_plan(event: PlanUpdated) -> None:
        plans.append(event)

    rig.bus.subscribe(PlanUpdated, on_plan)
    reply = await rig.assistant.handle(LONG)

    # The planner gets no tools; every task runs in the normal tool loop.
    assert router.requests[0]["tools"] is None
    assert all(r["tools"] for r in router.requests[1:])
    step2 = router.requests[3]["messages"][-1]["content"]
    assert "task 2 of 3" in step2 and "Trip" in step2 and "Spotify is open." in step2
    # Each step sees only its own instruction, not the whole request as a user turn.
    assert [m["role"] for m in router.requests[3]["messages"]] == ["system", "user"]

    assert [p.current for p in plans] == [0, 1, 2, -1] and plans[-1].finished
    assert [s.status for s in plans[-1].steps] == [StepStatus.DONE] * 3
    final = rig.entries[-1]
    assert final.kind is EntryKind.ASSISTANT and not final.failed
    assert final.text.startswith("All three tasks are complete.")
    assert "✓ 3. Paris fact" in final.text
    assert final.mode is not None and "plan" in final.mode
    assert reply.ok and rig.state.state is AppState.IDLE
    assert [m.content for m in await rig.store.messages(await rig.assistant.conversation_id())][
        -1
    ] == final.text


async def test_a_failed_task_turns_the_plan_red(tmp_path: Path) -> None:
    router = ScriptedRouter(
        text_reply(plan(("Open Spotify", "Open Spotify."), ("Lock the PC", "Lock the PC."))),
        text_reply("Opened Spotify."),
        tool_reply("lock_pc"),
        text_reply("Okay, I won't lock it."),
    )
    rig = Rig(tmp_path, router, approve=False)
    reply = await rig.assistant.handle(
        "open spotify for me please, and then lock the computer when you are done"
    )
    final = rig.entries[-1]
    assert final.failed
    assert final.text.startswith("One of two tasks complete; one didn't go through.")
    assert "✕ 2. Lock the PC" in final.text
    assert any(e.kind is EntryKind.ACTION and e.text.startswith("Declined") for e in rig.entries)
    assert reply.ok


async def test_no_provider_stops_the_remaining_tasks(tmp_path: Path) -> None:
    router = ScriptedRouter(
        text_reply(plan(("One", "Do one."), ("Two", "Do two."), ("Three", "Do three."))),
        text_reply("One done."),
        AllProvidersFailedError(Capability.CHAT, []),
    )
    rig = Rig(tmp_path, router)
    await rig.assistant.handle(" ".join(["please do these things"] * 8))
    final = rig.entries[-1]
    assert final.failed and "One of three tasks complete" in final.text
    assert "✕ 2. Two" in final.text and "- 3. Three: not started" in final.text


async def test_one_task_plans_take_the_normal_path(tmp_path: Path) -> None:
    router = ScriptedRouter(
        text_reply(plan(("Explain", "Explain transformers simply."))),
        text_reply("Transformers pay attention."),
    )
    rig = Rig(tmp_path, router)
    await rig.assistant.handle(" ".join(["explain transformers to me"] * 8))
    final = rig.entries[-1]
    assert final.text == "Transformers pay attention." and not final.failed
    assert final.mode is None


def test_spoken_summary_is_short() -> None:
    steps = [
        PlanStep("Open Spotify", StepStatus.DONE, "Opened."),
        PlanStep("Lock", StepStatus.FAILED, "You declined."),
    ]
    shown, said = plan_summary(steps)
    assert "\n" in shown and "\n" not in said
    assert said == "One of two tasks complete; one didn't go through. Lock: You declined."


async def test_hud_shows_the_task_list(qapp: QApplication) -> None:
    from jarvis.ui.main_window import MainWindow

    window = MainWindow()
    window.set_app_state(AppState.THINKING)
    steps = (
        PlanStep("Open Spotify", StepStatus.DONE, "Opened."),
        PlanStep("Make Trip folder", StepStatus.RUNNING),
        PlanStep("Paris fact"),
    )
    window.show_plan(PlanUpdated(steps))
    assert window.card.meta.text() == "PLAN · 3 TASKS · STEP 2/3"
    assert window.card.body.text().splitlines() == [
        "✓ 1. Open Spotify: Opened.",
        "✱ 2. Make Trip folder",
        "○ 3. Paris fact",
    ]
    assert window.caption._title == "STEP 2/3" and window.caption._hint == "Make Trip folder"
    window.show_plan(PlanUpdated(steps, finished=True))
    assert window._plan_step is None
    window.quitting = True
    window.close()
    window.overlay.hide()
