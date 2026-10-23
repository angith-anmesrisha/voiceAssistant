"""Glue between the UI, voice input, the assistant and spoken replies.

Handles one request at a time. Typed requests are answered in text; spoken requests
are transcribed, answered, and (optionally) read aloud. Starting to talk while JARVIS
is speaking cuts the speech off (barge-in).
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Coroutine
from typing import Any

from loguru import logger

from jarvis.agent.assistant import Assistant
from jarvis.agent.prompts import Mode
from jarvis.app.config import VoiceSettings
from jarvis.app.events import (
    ChatEntry,
    EntryKind,
    EventBus,
    PushToTalkPressed,
    PushToTalkReleased,
)
from jarvis.app.state import AppState, StateMachine
from jarvis.providers.router import AllProvidersFailedError, ProviderRouter
from jarvis.voice.audio_io import AudioDeviceError, Recorder
from jarvis.voice.tts import Speaker

_MIN_RECORDING_S = 0.4


class AppController:
    def __init__(
        self,
        bus: EventBus,
        state: StateMachine,
        assistant: Assistant,
        router: ProviderRouter,
        recorder: Recorder,
        speaker: Speaker,
        voice: VoiceSettings,
    ) -> None:
        self._bus = bus
        self._state = state
        self._assistant = assistant
        self._router = router
        self._recorder = recorder
        self._speaker = speaker
        self._voice = voice
        self.mode = Mode.ASSIST
        self._busy = False
        self._tasks: set[asyncio.Task[Any]] = set()
        self._max_timer: asyncio.TimerHandle | None = None
        bus.subscribe(PushToTalkPressed, self._on_ptt_pressed)
        bus.subscribe(PushToTalkReleased, self._on_ptt_released)

    @property
    def busy(self) -> bool:
        return self._busy

    @property
    def mic_level(self) -> float:
        return self._recorder.level

    @property
    def listening(self) -> bool:
        return self._recorder.recording

    # --- typed input ---

    def submit(self, text: str, mode: Mode | None = None) -> bool:
        """Start answering ``text``. Returns False if JARVIS is busy."""
        if self._busy or self._recorder.recording:
            return False
        self._busy = True  # set now, so a second click before the task starts is refused
        self._spawn(self._answer(text, mode or self.mode, spoken=False))
        return True

    # --- voice input ---

    def toggle_listening(self) -> None:
        if self._recorder.recording:
            self._spawn(self.stop_listening())
        else:
            self._spawn(self.start_listening())

    async def _on_ptt_pressed(self, _event: PushToTalkPressed) -> None:
        await self.start_listening()

    async def _on_ptt_released(self, _event: PushToTalkReleased) -> None:
        await self.stop_listening()

    async def start_listening(self) -> None:
        if self._recorder.recording:
            return
        if self._speaker.speaking or self._state.state is AppState.SPEAKING:
            self._speaker.stop()  # barge-in
        elif self._busy:
            return
        try:
            self._recorder.start()
        except AudioDeviceError as exc:
            await self._bus.publish(ChatEntry(EntryKind.ERROR, f"Microphone problem: {exc}"))
            return
        await self._state.transition(AppState.LISTENING)
        loop = asyncio.get_running_loop()
        self._max_timer = loop.call_later(
            self._voice.max_recording_s, lambda: self._spawn(self.stop_listening())
        )

    async def stop_listening(self) -> None:
        if not self._recorder.recording:
            return
        if self._max_timer is not None:
            self._max_timer.cancel()
            self._max_timer = None
        wav = self._recorder.stop()
        if self._recorder.duration_s(wav) < _MIN_RECORDING_S:
            await self._state.transition(AppState.IDLE)
            return
        await self._state.transition(AppState.THINKING)
        self._busy = True
        try:
            try:
                heard = await self._router.transcribe(wav)
            except AllProvidersFailedError as exc:
                logger.warning("Speech-to-text failed: {}", exc)
                detail = " ".join(f"{a.provider}: {a.error}." for a in exc.attempts)
                await self._bus.publish(
                    ChatEntry(
                        EntryKind.ERROR,
                        "Voice input needs Groq or Gemini, and neither answered. "
                        + (detail or "Set GROQ_API_KEY or GEMINI_API_KEY, or type instead."),
                    )
                )
                return
            if not heard.text:
                await self._bus.publish(ChatEntry(EntryKind.ERROR, "I didn't catch that."))
                return
            await self._answer_locked(heard.text, self.mode, spoken=True)
        finally:
            self._busy = False
            if self._state.state is not AppState.LISTENING:
                await self._state.transition(AppState.IDLE)

    # --- answering ---

    async def _answer(self, text: str, mode: Mode, *, spoken: bool) -> None:
        try:
            await self._answer_locked(text, mode, spoken=spoken)
        finally:
            self._busy = False
            if self._state.state not in (AppState.IDLE, AppState.LISTENING):
                await self._state.transition(AppState.IDLE)

    async def _answer_locked(self, text: str, mode: Mode, *, spoken: bool) -> None:
        reply = await self._assistant.handle(text, mode, spoken=spoken)
        if spoken and reply.ok and self._voice.speak_replies and self._speaker.available:
            await self._state.transition(AppState.SPEAKING)
            await self._speaker.speak(reply.text)

    def stop_speaking(self) -> None:
        self._speaker.stop()

    async def shutdown(self) -> None:
        if self._recorder.recording:
            self._recorder.stop()
        self._speaker.shutdown()
        for task in list(self._tasks):
            task.cancel()
        for task in list(self._tasks):
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    def _spawn(self, coro: Coroutine[Any, Any, None]) -> None:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
