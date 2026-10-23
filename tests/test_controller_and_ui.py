from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

import pytest
from PySide6.QtWidgets import QApplication

from jarvis.agent.assistant import Reply
from jarvis.agent.prompts import Mode
from jarvis.app.config import VoiceSettings
from jarvis.app.events import ChatEntry, EntryKind, EventBus, PushToTalkPressed, PushToTalkReleased
from jarvis.app.state import AppState, StateMachine
from jarvis.controller import AppController
from jarvis.providers.base import Transcription
from jarvis.tools.registry import Risk
from jarvis.voice.audio_io import Recorder


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    app = QApplication.instance() or QApplication([])
    assert isinstance(app, QApplication)
    from jarvis.ui import theme

    theme.load_fonts()
    return app


class FakeMic:
    def __init__(self) -> None:
        self.on_chunk: Callable[[bytes], None] | None = None

    def start(self, on_chunk: Callable[[bytes], None]) -> None:
        self.on_chunk = on_chunk

    def stop(self) -> None:
        self.on_chunk = None


class FakeAssistant:
    def __init__(self, state: StateMachine) -> None:
        self.calls: list[tuple[str, Mode, bool]] = []
        self.state = state

    async def handle(self, text: str, mode: Mode, *, spoken: bool = False) -> Reply:
        self.calls.append((text, mode, spoken))
        await self.state.transition(AppState.THINKING)
        await asyncio.sleep(0)
        await self.state.transition(AppState.IDLE)
        return Reply(f"answer to {text}", ok=True, provider="Groq", model="m")


class FakeRouter:
    def __init__(self, text: str = "open notepad") -> None:
        self.text = text

    async def transcribe(self, wav: bytes) -> Transcription:
        return Transcription(self.text, "Groq", "whisper")


class FakeSpeaker:
    def __init__(self) -> None:
        self.spoken: list[str] = []
        self.available = True
        self.speaking = False
        self.stopped = 0

    async def speak(self, text: str) -> None:
        self.spoken.append(text)

    def stop(self) -> None:
        self.stopped += 1

    def shutdown(self) -> None:
        pass


def make_controller() -> tuple[AppController, Any]:
    bus = EventBus()
    state = StateMachine(bus)
    mic = FakeMic()
    rig: Any = type("Rig", (), {})()
    rig.bus, rig.state, rig.mic = bus, state, mic
    rig.assistant = FakeAssistant(state)
    rig.speaker = FakeSpeaker()
    controller = AppController(
        bus,
        state,
        rig.assistant,
        FakeRouter(),  # type: ignore[arg-type]
        Recorder(mic, 16_000),
        rig.speaker,
        VoiceSettings(),
    )
    return controller, rig


async def settle() -> None:
    for _ in range(20):
        await asyncio.sleep(0)


async def test_typed_request_is_answered_in_text_only() -> None:
    controller, rig = make_controller()
    controller.mode = Mode.EXECUTE
    assert controller.submit("open notepad")
    assert not controller.submit("second")  # busy
    await settle()
    assert rig.assistant.calls == [("open notepad", Mode.EXECUTE, False)]
    assert rig.speaker.spoken == []
    assert not controller.busy


async def test_push_to_talk_transcribes_answers_and_speaks() -> None:
    _controller, rig = make_controller()
    await rig.bus.publish(PushToTalkPressed())
    assert rig.state.state is AppState.LISTENING
    rig.mic.on_chunk(b"\x10\x00" * 16_000)  # one second of audio
    await rig.bus.publish(PushToTalkReleased())
    await settle()
    assert rig.assistant.calls == [("open notepad", Mode.ASSIST, True)]
    assert rig.speaker.spoken == ["answer to open notepad"]
    final: AppState = rig.state.state
    assert final is AppState.IDLE


async def test_tap_too_short_is_ignored() -> None:
    controller, rig = make_controller()
    await controller.start_listening()
    rig.mic.on_chunk(b"\x00\x00" * 800)
    await controller.stop_listening()
    assert rig.assistant.calls == []
    assert rig.state.state is AppState.IDLE


async def test_barge_in_stops_speech() -> None:
    controller, rig = make_controller()
    await rig.state.transition(AppState.SPEAKING)
    await controller.start_listening()
    assert rig.speaker.stopped == 1
    assert rig.state.state is AppState.LISTENING


async def test_window_renders_every_state(qapp: QApplication) -> None:
    from jarvis.ui.main_window import MainWindow
    from jarvis.ui.widgets.orb import VisualState

    window = MainWindow()
    window.resize(1280, 800)
    window.add_entry(ChatEntry(EntryKind.USER, "hi", mode="assist"))
    window.add_entry(ChatEntry(EntryKind.ASSISTANT, "hello", "Groq", "m"))
    window.add_entry(ChatEntry(EntryKind.ACTION, "✓ Opened notepad."))
    window.add_entry(ChatEntry(EntryKind.ERROR, "oops"))
    for state, visual in (
        (AppState.IDLE, VisualState.IDLE),
        (AppState.LISTENING, VisualState.LISTENING),
        (AppState.THINKING, VisualState.THINKING),
        (AppState.SPEAKING, VisualState.SPEAKING),
        (AppState.ACTING, VisualState.ACTING),
    ):
        window.set_app_state(state)
        assert window.orb.state is visual
        window.orb.freeze(1.0, 0.5)
        assert not window.grab().isNull()
    window.quitting = True
    window.close()


async def test_confirm_overlay_requires_typing_for_high_risk(qapp: QApplication) -> None:
    from jarvis.ui.main_window import MainWindow

    window = MainWindow()
    overlay = window.confirm_overlay
    future = overlay.ask("Shut down", Risk.HIGH)
    assert not overlay.allow.isEnabled()
    overlay.typed.setText("CONFIRM")
    assert overlay.allow.isEnabled()
    overlay.allow.click()
    assert await asyncio.wait_for(future, 1) is True

    future = overlay.ask("Type hello", Risk.MEDIUM)
    assert overlay.allow.isEnabled()
    overlay.cancel.click()
    assert await asyncio.wait_for(future, 1) is False
    window.quitting = True
    window.close()
