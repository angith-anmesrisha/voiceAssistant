"""Glue between the UI, voice input, the assistant and spoken replies.

Handles one request at a time. Typed requests are answered in text; spoken requests
are transcribed, answered, and (optionally) read aloud. Starting to talk while JARVIS
is speaking cuts the speech off (barge-in).
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable, Coroutine
from typing import Any, Protocol

from loguru import logger

from jarvis.agent.assistant import Assistant, ConfirmRequest
from jarvis.app.config import VoiceSettings
from jarvis.app.events import (
    Announce,
    ChatEntry,
    ConversationChanged,
    EntryKind,
    EventBus,
    PushToTalkPressed,
    PushToTalkReleased,
    UserNotice,
    WakeWordHeard,
)
from jarvis.app.state import AppState, InvalidTransitionError, StateMachine
from jarvis.providers.router import AllProvidersFailedError, ProviderRouter
from jarvis.tools.registry import Risk
from jarvis.voice.audio_io import AudioDeviceError, Recorder
from jarvis.voice.confirm import is_echo, parse_yes_no, spoken_question
from jarvis.voice.local_stt import DISPLAY_NAME as LOCAL_STT_NAME
from jarvis.voice.tts import Speaker

_MIN_RECORDING_S = 0.4
_ANSWER_WAIT_S = 6.0  # silence before a spoken confirmation counts as no
_ACK_AFTER_S = 2.5  # a spoken request still working after this gets a short "On it."
_ACKS = ("On it.", "Right away.", "One moment.")


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
        self._ui_forward: Callable[[], None] | None = None
        self._last_said = ""  # to ignore the mic hearing it during follow-ups
        self._acks = 0
        self._voice = voice
        self._local_stt = local_stt
        # The request being handled. "Busy" means this task is still running, so the
        # flag can never get stuck after an error.
        self._active: asyncio.Task[None] | None = None
        self._tasks: set[asyncio.Task[Any]] = set()
        self._max_timer: asyncio.TimerHandle | None = None
        # A wake-word conversation: after each reply JARVIS listens again for a while,
        # so follow-ups need no wake word. Silence, typing or a mic problem ends it.
        self._conversation = False
        # Confirmation buttons in the window (set by main); voice asks alongside them.
        self._ui_ask: Callable[[ConfirmRequest, bool], Awaitable[bool]] | None = None
        self._ui_dismiss: Callable[[bool], None] | None = None
        bus.subscribe(PushToTalkPressed, self._on_ptt_pressed)
        bus.subscribe(PushToTalkReleased, self._on_ptt_released)
        bus.subscribe(WakeWordHeard, self._on_wake_word)
        bus.subscribe(Announce, self._on_announce)

    @property
    def busy(self) -> bool:
        return self._active is not None and not self._active.done()

    @property
    def mic_level(self) -> float:
        return self._recorder.level

    @property
    def listening(self) -> bool:
        return self._recorder.recording

    @property
    def in_conversation(self) -> bool:
        return self._conversation

    # --- typed input ---

    def submit(self, text: str) -> bool:
        """Start answering ``text``. Returns False if JARVIS is busy."""
        if self._recorder.recording:
            self._discard_recording()  # typing wins over a recording in progress
        self._end_conversation()
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

    async def _on_wake_word(self, _event: WakeWordHeard) -> None:
        if self._voice.follow_up_s > 0 and not self._conversation:
            self._conversation = True
            await self._bus.publish(ConversationChanged(True))
        await self.start_listening(hands_free=True)

    async def start_listening(self, *, hands_free: bool = False, no_speech_s: float = 6.0) -> None:
        """Start recording. Hands-free recordings stop by themselves when you go quiet."""
        if self._recorder.recording:
            return
        if self._speaker.speaking or self._state.state is AppState.SPEAKING:
            self._speaker.stop()  # barge-in
        elif self.busy:
            return
        on_done = (lambda: self._spawn(self._finish_hands_free())) if hands_free else None
        try:
            self._recorder.start(on_done, no_speech_s=no_speech_s)
        except AudioDeviceError as exc:
            await self._end_conversation_async()
            await self._bus.publish(ChatEntry(EntryKind.ERROR, f"Microphone problem: {exc}"))
            return
        await self._move_to(AppState.LISTENING)
        loop = asyncio.get_running_loop()
        self._max_timer = loop.call_later(
            self._voice.max_recording_s, lambda: self._spawn(self.stop_listening())
        )

    async def _finish_hands_free(self) -> None:
        if not self._recorder.recording:
            return
        if not self._recorder.heard_speech:
            self._discard_recording()  # woken, but nobody said anything
            await self._end_conversation_async()  # back to needing the wake word
            return
        await self.stop_listening()

    async def stop_listening(self) -> None:
        if not self._recorder.recording:
            return
        self._cancel_max_timer()
        wav = self._recorder.stop()
        if self._recorder.duration_s(wav) < _MIN_RECORDING_S:
            await self._end_conversation_async()
            await self._settle()
            return
        self._active = self._spawn(self._voice_turn(wav, self._active))
        if self._conversation:
            self._active.add_done_callback(lambda _t: self._spawn(self._follow_up()))

    async def _follow_up(self) -> None:
        """After a reply in a conversation, listen again without the wake word."""
        if not self._conversation or self._recorder.recording or self.busy:
            return
        await self._bus.publish(ConversationChanged(True, listening_again=True))
        await self.start_listening(hands_free=True, no_speech_s=self._voice.follow_up_s)

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
            if self._conversation and is_echo(heard, self._last_said):
                logger.info("Ignored the microphone hearing JARVIS's own reply")
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
        voiced = spoken and self._voice.speak_replies and self._speaker.available
        ack = None
        if voiced and self._voice.acknowledge:
            loop = asyncio.get_running_loop()
            ack = loop.call_later(_ACK_AFTER_S, lambda: self._spawn(self._acknowledge()))
        try:
            reply = await self._assistant.handle(text, spoken=spoken)
        finally:
            if ack is not None:
                ack.cancel()
        if voiced and reply.ok:
            await self._move_to(AppState.SPEAKING)
            await self._say(reply.text)

    async def _acknowledge(self) -> None:
        """A short "On it." when a spoken request takes a while (never over a question)."""
        if self._recorder.recording or self._speaker.speaking:
            return
        if self._state.state not in (AppState.THINKING, AppState.ACTING):
            return
        line = _ACKS[self._acks % len(_ACKS)]
        self._acks += 1
        await self._say(line)

    async def _say(self, text: str) -> None:
        self._last_said = text
        await self._speaker.speak(text)

    # --- things JARVIS brings up by itself ---

    async def _on_announce(self, event: Announce) -> None:
        if self._voice.speak_replies and self._speaker.available:
            self._spawn(self._say_when_free(event.text))

    async def _say_when_free(self, text: str, wait_s: float = 600.0) -> None:
        """Say ``text`` once nothing else is going on (never over you or a reply)."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + wait_s
        while self.busy or self._recorder.recording or self._state.state is not AppState.IDLE:
            if loop.time() > deadline:
                return
            await asyncio.sleep(0.5)
        self._active = asyncio.current_task()  # busy while speaking it
        try:
            await self._move_to(AppState.SPEAKING)
            await self._say(text)
        finally:
            await self._settle()

    # --- confirmations ---

    def set_confirm_ui(
        self,
        ask: Callable[[ConfirmRequest, bool], Awaitable[bool]],
        dismiss: Callable[[bool], None],
        bring_forward: Callable[[], None] | None = None,
    ) -> None:
        """``ask(request, focus)``: show the buttons; with focus False they appear without
        taking the user away from the app JARVIS is working in (a spoken answer is coming).
        ``bring_forward`` raises them later if the spoken answer can't be heard."""
        self._ui_ask, self._ui_dismiss = ask, dismiss
        self._ui_forward = bring_forward

    async def confirm(self, request: ConfirmRequest) -> bool:
        """Ask before a risky step. Medium risk is asked out loud too; the first answer wins."""
        aloud = (
            request.risk < Risk.HIGH
            and self._voice.voice_confirm
            and self._speaker.available
            and not self._recorder.recording
        )
        ui: asyncio.Future[bool] | None = None
        if self._ui_ask is not None:
            # Asked out loud, the buttons stay in the background: switching windows
            # mid-task would break what JARVIS is doing in the other app.
            ui = asyncio.ensure_future(self._ui_ask(request, not aloud))
        if not aloud:
            return await ui if ui is not None else False
        voice = asyncio.ensure_future(self._ask_aloud(request.description))
        waiting: set[asyncio.Future[Any]] = {voice} if ui is None else {voice, ui}
        done, _pending = await asyncio.wait(waiting, return_when=asyncio.FIRST_COMPLETED)
        if ui is not None and ui in done:
            voice.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await voice
            return ui.result()
        answer = voice.result()
        if answer is None:  # no microphone or speech-to-text: the buttons decide
            if self._ui_forward is not None:
                self._ui_forward()
            return await ui if ui is not None else False
        if self._ui_dismiss is not None:
            self._ui_dismiss(answer)
        return answer

    async def _ask_aloud(self, description: str) -> bool | None:
        """Ask, listen, and read a yes or no. None if JARVIS can't hear an answer at all."""
        resume = self._state.state
        question = spoken_question(description)
        try:
            for attempt in range(2):
                await self._move_to(AppState.SPEAKING)
                await self._speaker.speak(question)
                heard = await self._hear_answer()
                if heard is None:
                    return None
                answer = parse_yes_no(heard, asked=question)
                if answer is not None:
                    await self._bus.publish(
                        UserNotice("Confirmation", "You said yes." if answer else "You said no.")
                    )
                    return answer
                if not heard.strip() or attempt == 1:
                    break
                question = "Sorry, was that a yes or a no?"
            await self._move_to(AppState.SPEAKING)
            await self._speaker.speak("I'll take that as a no.")
            return False
        finally:
            if self._recorder.recording:
                self._cancel_max_timer()
                self._recorder.stop()
            if self._speaker.speaking:
                self._speaker.stop()
            if self._state.state in (AppState.SPEAKING, AppState.LISTENING):
                await self._move_to(AppState.THINKING if resume is not AppState.IDLE else resume)

    async def _hear_answer(self) -> str | None:
        """Record one hands-free answer and turn it into text ("" if nothing was said)."""
        loop = asyncio.get_running_loop()
        finished: asyncio.Future[None] = loop.create_future()

        def done() -> None:
            if not finished.done():
                finished.set_result(None)

        try:
            self._recorder.start(done, no_speech_s=_ANSWER_WAIT_S)
        except AudioDeviceError:
            return None
        await self._move_to(AppState.LISTENING)
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(finished, timeout=_ANSWER_WAIT_S + 10)
        heard_speech = self._recorder.heard_speech
        wav = self._recorder.stop()
        await self._move_to(AppState.THINKING)
        if not heard_speech or self._recorder.duration_s(wav) < _MIN_RECORDING_S:
            return ""
        try:
            return (await self._router.transcribe(wav)).text
        except AllProvidersFailedError:
            pass
        local = self._local_stt
        if local is not None and local.available:
            with contextlib.suppress(Exception):
                return await local.transcribe(wav)
        return None

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

    def _end_conversation(self) -> None:
        if self._conversation:
            self._conversation = False
            self._spawn(self._bus.publish(ConversationChanged(False)))

    async def _end_conversation_async(self) -> None:
        if self._conversation:
            self._conversation = False
            await self._bus.publish(ConversationChanged(False))

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
