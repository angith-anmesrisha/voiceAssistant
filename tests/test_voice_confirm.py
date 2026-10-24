"""Medium-risk steps are asked out loud; a spoken yes or no answers them."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from jarvis.agent.assistant import ConfirmRequest
from jarvis.app.state import AppState
from jarvis.tools.registry import Risk
from jarvis.voice.confirm import parse_yes_no, spoken_question
from tests.test_controller_and_ui import FakeRouter, make_controller, settle
from tests.test_hands_free_and_switching import LOUD, QUIET, speak

SEND = ConfirmRequest(
    "type_into",
    Risk.MEDIUM,
    "Type “hi” into “Type a message” in WhatsApp and press Enter (this sends or submits it)",
)


@pytest.mark.parametrize(
    ("heard", "answer"),
    [
        ("Yes.", True),
        ("yeah go ahead", True),
        ("Sure, send it", True),
        ("haan", True),
        ("No.", False),
        ("no don't", False),
        ("yes, no wait", False),  # any no wins
        ("cancel that", False),
        ("what?", None),
        ("", None),
    ],
)
def test_parse_yes_no(heard: str, answer: bool | None) -> None:
    assert parse_yes_no(heard) is answer


def test_its_own_question_is_not_an_answer() -> None:
    question = spoken_question("Send the message to Mom (go ahead and confirm)")
    assert question == "Shall I send the message to Mom?"
    assert parse_yes_no("message to mom", asked=question) is None


class UiButtons:
    def __init__(self) -> None:
        self.future: asyncio.Future[bool] | None = None
        self.settled: list[bool] = []

    async def ask(self, request: ConfirmRequest) -> bool:
        self.future = asyncio.get_running_loop().create_future()
        return await self.future

    def dismiss(self, allowed: bool) -> None:
        self.settled.append(allowed)
        if self.future is not None and not self.future.done():
            self.future.set_result(allowed)


async def answer_aloud(rig: Any, words: str) -> None:
    rig.router.text = words
    for _ in range(50):
        await settle()
        if rig.mic.on_chunk is not None:
            break
    speak(rig, [QUIET] * 4 + [LOUD] * 30 + [QUIET] * 40)


def rigged(text: str = "yes") -> tuple[Any, Any, UiButtons]:
    router = FakeRouter(text)
    controller, rig = make_controller(router)
    rig.router = router
    buttons = UiButtons()
    controller.set_confirm_ui(buttons.ask, buttons.dismiss)
    return controller, rig, buttons


async def test_spoken_yes_allows_and_closes_the_buttons() -> None:
    controller, rig, buttons = rigged()
    await rig.state.transition(AppState.THINKING)
    pending = asyncio.ensure_future(controller.confirm(SEND))
    await answer_aloud(rig, "yes, send it")
    assert await asyncio.wait_for(pending, 2) is True
    assert rig.speaker.spoken[0].startswith("Shall I type “hi” into")
    assert "(" not in rig.speaker.spoken[0]
    assert buttons.settled == [True]
    assert rig.state.state is AppState.THINKING  # back to work


async def test_spoken_no_declines() -> None:
    controller, rig, _buttons = rigged()
    await rig.state.transition(AppState.THINKING)
    pending = asyncio.ensure_future(controller.confirm(SEND))
    await answer_aloud(rig, "no")
    assert await asyncio.wait_for(pending, 2) is False


async def test_unclear_answer_is_asked_again_then_taken_as_no() -> None:
    controller, rig, _buttons = rigged()
    await rig.state.transition(AppState.THINKING)
    pending = asyncio.ensure_future(controller.confirm(SEND))
    await answer_aloud(rig, "what was that")
    await answer_aloud(rig, "hmm")
    assert await asyncio.wait_for(pending, 2) is False
    assert rig.speaker.spoken[1] == "Sorry, was that a yes or a no?"
    assert rig.speaker.spoken[-1] == "I'll take that as a no."


async def test_a_click_still_works_and_stops_listening() -> None:
    controller, rig, buttons = rigged()
    await rig.state.transition(AppState.THINKING)
    pending = asyncio.ensure_future(controller.confirm(SEND))
    for _ in range(20):
        await settle()
    assert controller.listening
    assert buttons.future is not None
    buttons.future.set_result(True)
    assert await asyncio.wait_for(pending, 2) is True
    await settle()
    assert not controller.listening


async def test_high_risk_still_needs_the_typed_confirm() -> None:
    controller, rig, buttons = rigged()
    shutdown = ConfirmRequest("shutdown_or_restart_pc", Risk.HIGH, "Shutdown the PC")
    pending = asyncio.ensure_future(controller.confirm(shutdown))
    await settle()
    assert rig.speaker.spoken == [] and not controller.listening
    assert buttons.future is not None
    buttons.future.set_result(False)
    assert await asyncio.wait_for(pending, 2) is False


async def test_no_speech_to_text_leaves_it_to_the_buttons() -> None:
    controller, rig, buttons = rigged()
    rig.router.fail = True
    await rig.state.transition(AppState.THINKING)
    pending = asyncio.ensure_future(controller.confirm(SEND))
    await answer_aloud(rig, "yes")
    for _ in range(20):
        await settle()
    assert not pending.done()
    assert buttons.future is not None
    buttons.future.set_result(False)
    assert await asyncio.wait_for(pending, 2) is False
