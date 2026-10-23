"""Glue between the UI, voice input, the assistant and spoken replies.

Handles one request at a time. Typed requests are answered in text; spoken requests
are transcribed, answered, and (optionally) read aloud. Starting to talk while JARVIS
is speaking cuts the speech off (barge-in).
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Coroutine
from typing import Any, Protocol

from loguru import logger

from jarvis.agent.assistant import Assistant
from jarvis.app.config import VoiceSettings
from jarvis.app.events import (
    ChatEntry,
    EntryKind,
    EventBus,
    PushToTalkPressed,
    PushToTalkReleased,
    UserNotice,
)
from jarvis.app.state import AppState, InvalidTransitionError, StateMachine
from jarvis.providers.router import AllProvidersFailedError, ProviderRouter
from jarvis.voice.audio_io import AudioDeviceError, Recorder
from jarvis.voice.local_stt import DISPLAY_NAME as LOCAL_STT_NAME
from jarvis.voice.tts import Speaker

_MIN_RECORDING_S = 0.4


class LocalTranscriber(Protocol):
    available: bool

    async def transcribe(self, wav: bytes) -> str: ...


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
        local_stt: LocalTranscriber | None = None,
    ) -> None:
        self._bus = bus
        self._state = state
        self._assistant = assistant
        self._router = router
        self._recorder = recorder
        self._speaker = speaker
        self._voice = voice
        self._local_stt = local_stt
        # The request being handled. "Busy" means this task is still running, so the
        # flag can never get stuck after an error.
        self._active: asyncio.Task[None] | None = None
        self._tasks: set[asyncio.Task[Any]] = set()
        self._max_timer: asyncio.TimerHandle | None = None
        bus.subscribe(PushToTalkPressed, self._on_ptt_pressed)
        bus.subscribe(PushToTalkReleased, self._on_ptt_released)

    @property
    def busy(self) -> bool:
        return self._active is not None and not self._active.done()

    @property
    def mic_level(self) -> float:
        return self._recorder.level

    @property
    def listening(self) -> bool:
        return self._recorder.recording

    # --- typed input ---

    def submit(self, text: str) -> bool:
        """Start answering ``text``. Returns False if JARVIS is busy."""
        if self._recorder.recording:
            self._discard_recording()  # typing wins over a recording in progress
        if self.busy:
            return False
        self._active = self._spawn(self._answer(text, spoken=False))
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
        elif self.busy:
            return
        try:
            self._recorder.start()
        except AudioDeviceError as exc:
            await self._bus.publish(ChatEntry(EntryKind.ERROR, f"Microphone problem: {exc}"))
            return
        await self._move_to(AppState.LISTENING)
        loop = asyncio.get_running_loop()
        self._max_timer = loop.call_later(
            self._voice.max_recording_s, lambda: self._spawn(self.stop_listening())
        )

    async def stop_listening(self) -> None:
        if not self._recorder.recording:
            return
        self._cancel_max_timer()
        wav = self._recorder.stop()
        if self._recorder.duration_s(wav) < _MIN_RECORDING_S:
            await self._settle()
            return
        self._active = self._spawn(self._voice_turn(wav, self._active))

    async def _voice_turn(self, wav: bytes, previous: asyncio.Task[None] | None) -> None:
        if previous is not None and not previous.done():
            # Barge-in: let the interrupted reply wind down first.
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await asyncio.wait_for(asyncio.shield(previous), timeout=10)
        try:
            await self._move_to(AppState.THINKING)
            heard = await self._transcribe(wav)
            if heard is None:
                return
            if not heard:
                await self._bus.publish(ChatEntry(EntryKind.ERROR, "I didn't catch that."))
                return
            await self._answer_locked(heard, spoken=True)
        finally:
            await self._settle()

    async def _transcribe(self, wav: bytes) -> str | None:
        """Speech to text: online providers first, then Windows speech. None on failure."""
        try:
            return (await self._router.transcribe(wav)).text
        except AllProvidersFailedError as exc:
            online_error = " ".join(f"{a.provider}: {a.error}." for a in exc.attempts)
        local = self._local_stt
        if local is not None and local.available:
            try:
                text = await local.transcribe(wav)
            except Exception as exc:
                logger.warning("Offline speech-to-text failed: {}", type(exc).__name__)
                online_error += f" {LOCAL_STT_NAME}: {exc}."
            else:
                await self._bus.publish(
                    UserNotice(
                        "Voice", f"Heard with {LOCAL_STT_NAME}; add a Groq key for better accuracy."
                    )
                )
                return text
        logger.warning("Speech-to-text failed on every provider")
        await self._bus.publish(
            ChatEntry(
                EntryKind.ERROR,
                "I couldn't turn your speech into text. "
                + (online_error.strip() or "Add a Groq or Gemini key, or type instead."),
            )
        )
        return None

    # --- answering ---

    async def _answer(self, text: str, *, spoken: bool) -> None:
        try:
            await self._answer_locked(text, spoken=spoken)
        finally:
            await self._settle()

    async def _answer_locked(self, text: str, *, spoken: bool) -> None:
        reply = await self._assistant.handle(text, spoken=spoken)
        if spoken and reply.ok and self._voice.speak_replies and self._speaker.available:
            await self._move_to(AppState.SPEAKING)
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

    # --- helpers ---

    def _discard_recording(self) -> None:
        self._cancel_max_timer()
        self._recorder.stop()
        self._spawn(self._settle())

    def _cancel_max_timer(self) -> None:
        if self._max_timer is not None:
            self._max_timer.cancel()
            self._max_timer = None

    async def _move_to(self, target: AppState) -> None:
        try:
            await self._state.transition(target)
        except InvalidTransitionError as exc:
            logger.warning("Ignored state change: {}", exc)

    async def _settle(self) -> None:
        """Back to IDLE after a request, unless a new recording has started."""
        if self._recorder.recording:
            return
        await self._move_to(AppState.IDLE)

    def _spawn(self, coro: Coroutine[Any, Any, None]) -> asyncio.Task[None]:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        task.add_done_callback(_log_failure)
        return task


def _log_failure(task: asyncio.Task[None]) -> None:
    if not task.cancelled() and (exc := task.exception()) is not None:
        logger.opt(exception=exc).error("Request failed")
