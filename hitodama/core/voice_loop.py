"""Push-to-talk conversation loop: hotkey -> mic -> Live session -> speaker.

Owns the IDLE/LISTENING/THINKING/SPEAKING transitions for voice turns, barge-in,
reply timeouts and the idle disconnect.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from typing import Any, Protocol

from loguru import logger

from hitodama.app.config import Settings
from hitodama.app.events import (
    ApiKeyChanged,
    EventBus,
    NoticeLevel,
    PushToTalkPressed,
    PushToTalkReleased,
    Transcript,
    UserNotice,
)
from hitodama.app.state import AppState, StateMachine
from hitodama.core.audio_io import AudioDeviceError
from hitodama.core.live_session import LiveErrorKind, LiveSession

_FATAL_MESSAGES: dict[LiveErrorKind, str] = {
    LiveErrorKind.INVALID_KEY: (
        "Your Gemini API key was rejected. Right-click the tray icon and choose "
        "'Set Gemini API key…' to paste a new one."
    ),
    LiveErrorKind.QUOTA: (
        "You've hit the Gemini usage limit for now. Try again in a little while, "
        "or check your quota in Google AI Studio."
    ),
    LiveErrorKind.REGION: "The Gemini API isn't available in your region with this key.",
    LiveErrorKind.ACCOUNT: (
        "Gemini says your API key's project isn't set up for this yet (precondition check "
        "failed). This is usually billing or a free-tier region limit. Check the key's "
        "project in Google AI Studio."
    ),
}


class MicrophoneLike(Protocol):
    def start(self, on_chunk: Callable[[bytes], None]) -> None: ...
    def stop(self) -> None: ...


class SpeakerLike(Protocol):
    @property
    def has_pending_audio(self) -> bool: ...
    def open(self) -> None: ...
    def write(self, pcm: bytes) -> None: ...
    def clear(self) -> None: ...
    def close(self) -> None: ...


class VoiceLoop:
    def __init__(
        self,
        bus: EventBus,
        state: StateMachine,
        settings: Settings,
        session_factory: Callable[[VoiceLoop], LiveSession | None],
        microphone: MicrophoneLike,
        speaker_factory: Callable[[Callable[[], None]], SpeakerLike],
    ) -> None:
        self._bus = bus
        self._state = state
        self._settings = settings
        self._session_factory = session_factory
        self._mic = microphone
        self._speaker = speaker_factory(self._on_speaker_drained)
        self._session: LiveSession | None = None
        self._tasks: set[asyncio.Task[None]] = set()
        self._holding = False
        self._reply_done = True
        self._reply_timer: asyncio.TimerHandle | None = None
        self._idle_timer: asyncio.TimerHandle | None = None
        self._hold_timer: asyncio.TimerHandle | None = None

        bus.subscribe(PushToTalkPressed, self._on_pressed)
        bus.subscribe(PushToTalkReleased, self._on_released)
        bus.subscribe(ApiKeyChanged, self._on_api_key_changed)

    async def shutdown(self) -> None:
        self._cancel_timers()
        self._mic.stop()
        self._speaker.close()
        if self._session is not None:
            await self._session.stop()

    # --- push-to-talk ---

    async def _on_pressed(self, _event: PushToTalkPressed) -> None:
        if self._holding:
            return
        self._cancel_timers()
        if self._session is None:
            self._session = self._session_factory(self)
            if self._session is None:
                self._notify(
                    "No API key yet",
                    "Right-click the tray icon and choose 'Set Gemini API key…' first.",
                    NoticeLevel.WARNING,
                )
                return

        if self._state.state in (AppState.SPEAKING, AppState.THINKING):
            # Barge-in. While the key is held, any audio still arriving belongs to the
            # reply being talked over and is dropped (see on_audio).
            self._speaker.clear()
            logger.debug("Barge-in")

        try:
            self._speaker.open()
            self._mic.start(self._on_mic_chunk)
        except AudioDeviceError as exc:
            logger.error("{}", exc)
            self._mic.stop()
            self._notify("Audio device problem", str(exc), NoticeLevel.ERROR)
            await self._go_idle()
            return

        self._holding = True
        self._session.start()
        self._session.begin_turn()
        self._reply_done = False
        await self._state.transition(AppState.LISTENING)
        self._hold_timer = self._later(self._settings.live.max_utterance_s, self._force_release)

    async def _on_released(self, _event: PushToTalkReleased) -> None:
        if not self._holding:
            return
        self._holding = False
        self._mic.stop()
        if self._hold_timer is not None:
            self._hold_timer.cancel()
            self._hold_timer = None
        if self._session is not None:
            self._session.end_turn()
        await self._state.transition(AppState.THINKING)
        self._reply_timer = self._later(self._settings.live.reply_timeout_s, self._reply_timeout)

    def _on_mic_chunk(self, pcm: bytes) -> None:
        if self._holding and self._session is not None:
            self._session.send_audio(pcm)

    def _force_release(self) -> None:
        logger.warning("Push-to-talk held too long; ending the turn")
        self._spawn(self._on_released(PushToTalkReleased()))

    def _reply_timeout(self) -> None:
        if self._state.state is AppState.THINKING:
            self._notify("No reply", "Gemini didn't answer in time. Please try again.")
            self._reply_done = True
            self._spawn(self._go_idle())

    # --- LiveHandler callbacks ---

    def on_audio(self, pcm: bytes) -> None:
        if self._holding:
            return
        if self._reply_timer is not None:
            self._reply_timer.cancel()
            self._reply_timer = None
        self._speaker.write(pcm)
        if self._state.state in (AppState.THINKING, AppState.IDLE):
            self._spawn(self._state.transition(AppState.SPEAKING))

    def on_interrupted(self) -> None:
        self._speaker.clear()

    def on_turn_complete(self) -> None:
        if self._holding:
            return  # end of the reply that was talked over
        self._reply_done = True
        if self._reply_timer is not None:
            self._reply_timer.cancel()
            self._reply_timer = None
        if not self._holding and not self._speaker.has_pending_audio:
            self._spawn(self._go_idle())

    def on_transcript(self, role: str, text: str) -> None:
        if self._settings.show_captions:
            self._spawn(self._bus.publish(Transcript(role=role, text=text)))

    def on_connection_lost(self) -> None:
        if self._state.state is not AppState.IDLE:
            self._notify(
                "Connection lost",
                "Lost the connection to Gemini. Reconnecting; please ask again in a moment.",
                NoticeLevel.WARNING,
            )
            self._holding = False
            self._mic.stop()
            self._speaker.clear()
            self._reply_done = True
            self._spawn(self._go_idle())

    def on_fatal_error(self, kind: LiveErrorKind, detail: str) -> None:
        message = _FATAL_MESSAGES.get(kind) or f"Gemini refused the session setup: {detail}"
        self._notify("Gemini problem", message, NoticeLevel.ERROR)
        self._holding = False
        self._mic.stop()
        self._speaker.clear()
        self._reply_done = True
        self._session = None  # rebuilt (with a fresh key) on the next press
        self._spawn(self._go_idle())

    # --- helpers ---

    def _on_speaker_drained(self) -> None:
        if self._reply_done and not self._holding and self._state.state is AppState.SPEAKING:
            self._spawn(self._go_idle())

    async def _go_idle(self) -> None:
        await self._state.transition(AppState.IDLE)
        if self._idle_timer is not None:
            self._idle_timer.cancel()
        self._idle_timer = self._later(self._settings.live.idle_disconnect_s, self._idle_disconnect)

    def _idle_disconnect(self) -> None:
        if self._state.state is AppState.IDLE and not self._holding:
            logger.info("Idle; closing Live connection")
            self._speaker.close()
            if self._session is not None:
                self._spawn(self._session.stop())

    async def _on_api_key_changed(self, _event: ApiKeyChanged) -> None:
        if self._session is not None:
            session, self._session = self._session, None
            await session.stop(forget_context=True)

    def _cancel_timers(self) -> None:
        for timer in (self._reply_timer, self._idle_timer, self._hold_timer):
            if timer is not None:
                timer.cancel()
        self._reply_timer = self._idle_timer = self._hold_timer = None

    def _later(self, delay: float, callback: Callable[[], None]) -> asyncio.TimerHandle:
        return asyncio.get_running_loop().call_later(delay, callback)

    def _notify(self, title: str, message: str, level: NoticeLevel = NoticeLevel.INFO) -> None:
        self._spawn(self._bus.publish(UserNotice(title=title, message=message, level=level)))

    def _spawn(self, coro: Coroutine[Any, Any, None]) -> None:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
