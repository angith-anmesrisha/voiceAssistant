from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

import pytest
from PySide6.QtWidgets import QApplication

from jarvis.agent.assistant import Reply
from jarvis.app.config import VoiceSettings
from jarvis.app.events import ChatEntry, EntryKind, EventBus, PushToTalkPressed, PushToTalkReleased
from jarvis.app.state import AppState, StateMachine
from jarvis.controller import AppController
from jarvis.providers.base import Capability, Transcription
from jarvis.providers.router import AllProvidersFailedError, Attempt
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
        self.calls: list[tuple[str, bool]] = []
        self.state = state

    async def handle(self, text: str, *, spoken: bool = False) -> Reply:
        self.calls.append((text, spoken))
        await self.state.transition(AppState.THINKING)
        await asyncio.sleep(0)
        await self.state.transition(AppState.IDLE)
        return Reply(f"answer to {text}", ok=True, provider="Groq", model="m")


class FakeRouter:
    def __init__(self, text: str = "open notepad", fail: bool = False) -> None:
        self.text = text
        self.fail = fail

    async def transcribe(self, wav: bytes) -> Transcription:
        if self.fail:
            raise AllProvidersFailedError(Capability.STT, [Attempt("Groq", "no API key")])
        return Transcription(self.text, "Groq", "whisper")


class FakeLocalStt:
    def __init__(self, text: str | None) -> None:
        self.text = text
        self.available = True

    async def transcribe(self, wav: bytes) -> str:
        if self.text is None:
            raise RuntimeError("no recognizer installed")
        return self.text


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


def make_controller(
    router: FakeRouter | None = None, local_stt: FakeLocalStt | None = None
) -> tuple[AppController, Any]:
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
        router or FakeRouter(),  # type: ignore[arg-type]
        Recorder(mic, 16_000),
        rig.speaker,
        VoiceSettings(),
        local_stt,
    )
    rig.entries = []

    async def record(entry: ChatEntry) -> None:
        rig.entries.append(entry)

    bus.subscribe(ChatEntry, record)
    return controller, rig


async def settle() -> None:
    for _ in range(20):
        await asyncio.sleep(0)


async def test_typed_request_is_answered_in_text_only() -> None:
    controller, rig = make_controller()
    assert controller.submit("open notepad")
    await settle()
    assert rig.assistant.calls == [("open notepad", False)]
    assert rig.speaker.spoken == []
    assert not controller.busy


async def test_push_to_talk_transcribes_answers_and_speaks() -> None:
    _controller, rig = make_controller()
    await rig.bus.publish(PushToTalkPressed())
    assert rig.state.state is AppState.LISTENING
    rig.mic.on_chunk(b"\x10\x00" * 16_000)  # one second of audio
    await rig.bus.publish(PushToTalkReleased())
    await settle()
    assert rig.assistant.calls == [("open notepad", True)]
    assert rig.speaker.spoken == ["answer to open notepad"]
    final: AppState = rig.state.state
    assert final is AppState.IDLE


async def speak_one_second(controller: AppController, rig: Any) -> None:
    await controller.start_listening()
    rig.mic.on_chunk(b"\x10\x00" * 16_000)
    await controller.stop_listening()
    await settle()


async def test_failed_transcription_reports_once_and_frees_jarvis() -> None:
    controller, rig = make_controller(FakeRouter(fail=True))
    await speak_one_second(controller, rig)
    errors = [e for e in rig.entries if e.kind is EntryKind.ERROR]
    assert len(errors) == 1 and "Groq: no API key" in errors[0].text
    assert not controller.busy
    assert rig.state.state is AppState.IDLE
    assert controller.submit("analyse")  # typing works straight after the failure
    await settle()
    assert rig.assistant.calls == [("analyse", False)]


async def test_offline_speech_recognition_is_the_fallback() -> None:
    controller, rig = make_controller(FakeRouter(fail=True), FakeLocalStt("what time is it"))
    await speak_one_second(controller, rig)
    assert rig.assistant.calls == [("what time is it", True)]
    assert not [e for e in rig.entries if e.kind is EntryKind.ERROR]


async def test_offline_recognizer_failure_is_reported() -> None:
    controller, rig = make_controller(FakeRouter(fail=True), FakeLocalStt(None))
    await speak_one_second(controller, rig)
    errors = [e for e in rig.entries if e.kind is EntryKind.ERROR]
    assert len(errors) == 1 and "no recognizer installed" in errors[0].text
    assert not controller.busy


async def test_typing_while_listening_discards_the_recording() -> None:
    controller, rig = make_controller()
    await controller.start_listening()
    assert controller.submit("hello")
    await settle()
    assert not controller.listening
    assert rig.assistant.calls == [("hello", False)]
    assert rig.state.state is AppState.IDLE


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


async def test_identical_errors_are_not_stacked(qapp: QApplication) -> None:
    from jarvis.ui.main_window import MainWindow

    window = MainWindow()
    window.add_entry(ChatEntry(EntryKind.ERROR, "Voice failed"))
    window.add_entry(ChatEntry(EntryKind.ERROR, "Voice failed"))
    assert window.chat.entry_count() == 1
    assert "Same problem" in window.notice.text()
    window.add_entry(ChatEntry(EntryKind.USER, "again", mode="assist"))
    window.add_entry(ChatEntry(EntryKind.ERROR, "Voice failed"))  # after a new request: shown
    assert window.chat.entry_count() == 3
    window.quitting = True
    window.close()


async def test_keys_overlay_saves_only_filled_fields(qapp: QApplication) -> None:
    from jarvis.ui.main_window import MainWindow
    from jarvis.ui.widgets.keys import KeyField

    saved: list[dict[str, str]] = []
    window = MainWindow()
    window.resize(1280, 800)
    window.keys_overlay._on_save = saved.append
    window._key_fields = lambda: [
        KeyField("Groq", "GROQ_API_KEY", "https://console.groq.com/keys", False),
        KeyField("Gemini", "GEMINI_API_KEY", "https://aistudio.google.com/apikey", True),
    ]
    window.open_keys()
    assert window.keys_overlay.isVisible()
    window.keys_overlay._inputs["GROQ_API_KEY"].setText(" gsk_abc ")
    window.keys_overlay.save.click()
    assert saved == [{"GROQ_API_KEY": "gsk_abc"}]
    assert not window.keys_overlay.isVisible()
    window.quitting = True
    window.close()


class SlowAssistant(FakeAssistant):
    """Works until cancelled (or released), like a long task."""

    def __init__(self, state: StateMachine) -> None:
        super().__init__(state)
        self.release = asyncio.Event()
        self.cancelled = 0

    async def handle(self, text: str, *, spoken: bool = False) -> Reply:
        self.calls.append((text, spoken))
        await self.state.transition(AppState.THINKING)
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled += 1
            await self.state.transition(AppState.IDLE)
            raise
        await self.state.transition(AppState.IDLE)
        return Reply(f"answer to {text}", ok=True)


def slow_controller() -> tuple[AppController, Any]:
    controller, rig = make_controller()
    rig.assistant = SlowAssistant(rig.state)
    controller._assistant = rig.assistant
    return controller, rig


async def test_interrupt_stops_the_request_in_hand() -> None:
    controller, rig = slow_controller()
    controller.submit("do a long task")
    await settle()
    assert controller.busy
    assert await controller.interrupt()
    assert not controller.busy and rig.assistant.cancelled == 1
    assert rig.entries[-1].text == "Stopped."
    assert rig.state.state is AppState.IDLE
    assert not await controller.interrupt()  # nothing left to stop


async def test_a_new_typed_request_replaces_the_running_one() -> None:
    controller, rig = slow_controller()
    controller.submit("first")
    await settle()
    assert controller.submit("second")
    await settle()
    assert rig.assistant.cancelled == 1
    assert [c[0] for c in rig.assistant.calls] == ["first", "second"]
    rig.assistant.release.set()
    await settle()
    assert not controller.busy


async def test_typing_stop_only_stops() -> None:
    controller, rig = slow_controller()
    controller.submit("first")
    await settle()
    controller.submit("stop")
    await settle()
    assert [c[0] for c in rig.assistant.calls] == ["first"]
    assert rig.entries[-1].text == "Stopped."


async def test_push_to_talk_while_working_interrupts_and_listens() -> None:
    controller, rig = slow_controller()
    controller.submit("first")
    await settle()
    await rig.bus.publish(PushToTalkPressed())
    assert rig.assistant.cancelled == 1
    assert rig.state.state is AppState.LISTENING


async def test_saying_stop_ends_it_without_asking_the_ai() -> None:
    _controller, rig = make_controller(FakeRouter(text="Stop."))
    await rig.bus.publish(PushToTalkPressed())
    rig.mic.on_chunk(b"\x10\x00" * 16_000)
    await rig.bus.publish(PushToTalkReleased())
    await settle()
    assert rig.assistant.calls == []
    assert rig.entries[-1].text == "Stopped."


async def test_interrupting_off_keeps_the_old_behaviour() -> None:
    controller, _rig = slow_controller()
    controller._voice = VoiceSettings(interrupt=False)
    controller.submit("first")
    await settle()
    assert not controller.submit("second")
