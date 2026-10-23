"""Gemini Live connection: send push-to-talk audio, receive streamed audio replies.

Voice activity detection is disabled on the server; push-to-talk marks the start and
end of each user turn explicitly. The connection is opened on demand, kept alive with
session resumption across server resets and network drops, and closed by the caller
after a period of inactivity.

API details verified on 2026-10-23 against ai.google.dev (Live API get-started,
capabilities and session-management guides) and google-genai 2.33 ``live.py``.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Callable
from enum import StrEnum
from typing import Any, Protocol

from google.genai import types
from loguru import logger

INPUT_MIME = "audio/pcm;rate=16000"

_BACKOFF_S = (1.0, 2.0, 4.0, 8.0, 15.0, 30.0)


class LiveErrorKind(StrEnum):
    INVALID_KEY = "invalid_key"
    QUOTA = "quota"
    REGION = "region"
    BAD_CONFIG = "bad_config"
    TRANSIENT = "transient"


def classify_error(exc: BaseException) -> LiveErrorKind:
    """Map a connection/API failure to something we can explain or retry."""
    text = str(exc).lower()
    if any(s in text for s in ("api key not valid", "api_key_invalid", "invalid api key")):
        return LiveErrorKind.INVALID_KEY
    code = getattr(exc, "code", None)
    if code == 429 or any(s in text for s in ("quota", "resource_exhausted", "rate limit")):
        return LiveErrorKind.QUOTA
    if "location is not supported" in text or "not available in your country" in text:
        return LiveErrorKind.REGION
    if code in (1007, 1008) or (isinstance(code, int) and 400 <= code < 500 and code != 429):
        if code in (401, 403):
            return LiveErrorKind.INVALID_KEY
        return LiveErrorKind.BAD_CONFIG
    return LiveErrorKind.TRANSIENT


class LiveHandler(Protocol):
    """Callbacks from the live session. All are called on the event loop thread."""

    def on_audio(self, pcm: bytes) -> None: ...
    def on_interrupted(self) -> None: ...
    def on_turn_complete(self) -> None: ...
    def on_transcript(self, role: str, text: str) -> None: ...
    def on_connection_lost(self) -> None: ...
    def on_fatal_error(self, kind: LiveErrorKind, detail: str) -> None: ...


# (model, config) -> async context manager yielding a session. Injected for tests.
Connector = Callable[[str, types.LiveConnectConfig], contextlib.AbstractAsyncContextManager[Any]]


class LiveSession:
    def __init__(
        self,
        connector: Connector,
        model: str,
        handler: LiveHandler,
        system_prompt: Callable[[], str],
        voice: str | None = None,
    ) -> None:
        self._connector = connector
        self._model = model
        self._handler = handler
        self._system_prompt = system_prompt
        self._voice = voice
        self._outgoing: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._session: Any = None
        self._connected = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._resume_handle: str | None = None
        self._go_away = False
        self._turn_active = False

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    @property
    def connected(self) -> bool:
        return self._connected.is_set()

    def start(self) -> None:
        """Connect in the background if not already running. Returns immediately."""
        if not self.running:
            self._task = asyncio.create_task(self._run(), name="live-session")

    async def stop(self, *, forget_context: bool = False) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._drop_outgoing()
        if forget_context:
            self._resume_handle = None

    # --- outgoing (queued until connected, so speech during connect isn't lost) ---

    def begin_turn(self) -> None:
        self._turn_active = True
        self._outgoing.put_nowait({"activity_start": types.ActivityStart()})

    def send_audio(self, pcm: bytes) -> None:
        self._outgoing.put_nowait({"audio": types.Blob(data=pcm, mime_type=INPUT_MIME)})

    def end_turn(self) -> None:
        self._outgoing.put_nowait({"activity_end": types.ActivityEnd()})

    # --- internals ---

    def _config(self) -> types.LiveConnectConfig:
        speech = None
        if self._voice:
            speech = types.SpeechConfig(
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=self._voice)
                )
            )
        return types.LiveConnectConfig(
            response_modalities=[types.Modality.AUDIO],
            system_instruction=self._system_prompt(),
            speech_config=speech,
            realtime_input_config=types.RealtimeInputConfig(
                automatic_activity_detection=types.AutomaticActivityDetection(disabled=True)
            ),
            input_audio_transcription=types.AudioTranscriptionConfig(),
            output_audio_transcription=types.AudioTranscriptionConfig(),
            context_window_compression=types.ContextWindowCompressionConfig(
                sliding_window=types.SlidingWindow()
            ),
            session_resumption=types.SessionResumptionConfig(handle=self._resume_handle),
        )

    def _drop_outgoing(self) -> None:
        while not self._outgoing.empty():
            self._outgoing.get_nowait()

    async def _run(self) -> None:
        attempt = 0
        while True:
            try:
                async with self._connector(self._model, self._config()) as session:
                    logger.info(
                        "Live session connected ({})", "resumed" if self._resume_handle else "new"
                    )
                    attempt = 0
                    self._go_away = False
                    self._session = session
                    self._connected.set()
                    await self._pump(session)
                    continue  # server asked us to reconnect (GoAway); resume right away
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                kind = classify_error(exc)
                if kind is not LiveErrorKind.TRANSIENT:
                    logger.error("Live session failed: {} ({})", kind.value, type(exc).__name__)
                    self._handler.on_fatal_error(kind, str(exc))
                    return
                was_connected = self._connected.is_set()
                delay = _BACKOFF_S[min(attempt, len(_BACKOFF_S) - 1)]
                attempt += 1
                logger.warning(
                    "Live connection problem ({}); retrying in {:.0f}s", type(exc).__name__, delay
                )
                self._reset_connection()
                if was_connected or self._turn_active:
                    self._turn_active = False
                    self._drop_outgoing()
                    self._handler.on_connection_lost()
                await asyncio.sleep(delay)
            finally:
                self._reset_connection()

    def _reset_connection(self) -> None:
        self._session = None
        self._connected.clear()

    async def _pump(self, session: Any) -> None:
        """Run sender and receiver until one ends; propagate errors."""
        sender = asyncio.create_task(self._send_loop(session))
        receiver = asyncio.create_task(self._receive_loop(session))
        try:
            done, _ = await asyncio.wait({sender, receiver}, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        finally:
            for task in (sender, receiver):
                task.cancel()
            await asyncio.gather(sender, receiver, return_exceptions=True)

    async def _send_loop(self, session: Any) -> None:
        while True:
            item = await self._outgoing.get()
            await session.send_realtime_input(**item)

    async def _receive_loop(self, session: Any) -> None:
        while True:
            async for message in _messages(session):
                self._dispatch(message)
                if self._go_away and not self._turn_active:
                    return
            if self._go_away and not self._turn_active:
                return

    def _dispatch(self, message: types.LiveServerMessage) -> None:
        update = message.session_resumption_update
        if update is not None and update.resumable and update.new_handle:
            self._resume_handle = update.new_handle
        if message.go_away is not None:
            logger.info("Live server will close the connection soon; will resume")
            self._go_away = True
        if message.tool_call is not None:
            logger.debug("Ignoring tool call (tools arrive in M2)")
        content = message.server_content
        if content is None:
            return
        if content.interrupted:
            self._handler.on_interrupted()
        if content.input_transcription and content.input_transcription.text:
            self._handler.on_transcript("user", content.input_transcription.text)
        if content.output_transcription and content.output_transcription.text:
            self._handler.on_transcript("assistant", content.output_transcription.text)
        if content.model_turn and content.model_turn.parts:
            for part in content.model_turn.parts:
                if part.inline_data and part.inline_data.data:
                    self._handler.on_audio(part.inline_data.data)
        if content.turn_complete:
            self._turn_active = False
            self._handler.on_turn_complete()


async def _messages(session: Any) -> AsyncIterator[types.LiveServerMessage]:
    # session.receive() yields one model turn, then stops; the caller loops.
    async for message in session.receive():
        yield message
