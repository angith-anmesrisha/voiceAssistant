"""Spoken replies: JARVIS's neural voice when it's ready, Windows' own voice (SAPI) otherwise.

The neural voice (:mod:`jarvis.voice.neural`, Kokoro, British male) runs on this PC and
needs a one-time model download; until that's done, or if it can't run here, SAPI
speaks. Both work offline. Speech runs on its own thread (SAPI is a COM object).
``speak`` resolves when speech finishes or is stopped; ``stop`` cuts it off at once
(barge-in). ``open_stream`` speaks a reply while it's still being written.

Smooth speech (owner, 2026-10-25: the voice felt "laggy and late"): the reply is spoken
in pieces of whole clauses that grow, each sized from how fast this PC makes speech, so
the next piece is ready before the current one ends and there are no gaps. The first
piece is the first clause, so speech starts quickly.
"""

from __future__ import annotations

import asyncio
import queue
import sys
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from loguru import logger

from jarvis.voice.feed import SpeechFeed
from jarvis.voice.spoken_text import script_language, speakable

_SVSF_ASYNC = 1
_SVSF_PURGE = 2
_SENTENCE_PAUSE_S = 0.22  # between sentences: natural, not hurried
_BLOCK = 1200  # frames written at a time (50 ms), so stop() is quick
_FIRST_CHARS = 60  # the first piece: one short clause, so speech starts quickly
_LONGEST_CHARS = 280
_SAFETY = 0.75  # size the next piece to be ready with a quarter of the time to spare

__all__ = ["Speaker", "speakable"]


@dataclass
class _Job:
    feed: SpeechFeed
    done: asyncio.Future[None]
    loop: asyncio.AbstractEventLoop


class SpokenStream:
    """A reply being spoken while it's written: ``put`` text as it comes, then
    ``close``; ``wait`` resolves when it has all been said (or speech was stopped)."""

    def __init__(self, feed: SpeechFeed, done: asyncio.Future[None] | None) -> None:
        self._feed = feed
        self._done = done
        self.started = False

    def put(self, text: str) -> None:
        if text:
            self.started = True
            self._feed.put(text)

    def close(self) -> None:
        self._feed.close()

    async def wait(self) -> None:
        self._feed.close()
        if self._done is not None:
            await self._done


class Speaker:
    """Text-to-speech. A silent no-op where SAPI isn't available (tests, Linux)."""

    def __init__(
        self,
        voice_hint: str = "",
        rate: int = 1,
        *,
        engine: str = "sapi",
        neural_voice: str = "bm_george",
        neural_speed: float = 1.15,
        model_folder: Path | None = None,
    ) -> None:
        self._voice_hint = voice_hint.lower()
        self._rate = max(-10, min(10, rate))
        self._engine = engine
        self._neural_voice = neural_voice
        self._neural_speed = neural_speed
        self._model_folder = model_folder
        self._neural: Any = None  # a loaded KokoroVoice once ready
        self._jobs: queue.Queue[_Job | None] = queue.Queue()
        self._seconds_per_char = 0.02  # how long making speech takes here (learned)
        self._stop = threading.Event()
        self._closed = threading.Event()
        self._thread: threading.Thread | None = None
        self.available = sys.platform == "win32"
        self.speaking = False

    @property
    def voice_name(self) -> str:
        """What is speaking right now, for the UI and diagnostics."""
        return f"Kokoro {self._neural.voice}" if self._neural is not None else "Windows voice"

    def start(self) -> None:
        if self.available and self._thread is None:
            self._thread = threading.Thread(target=self._worker, name="tts", daemon=True)
            self._thread.start()

    def shutdown(self) -> None:
        self._closed.set()  # also ends a model download early
        self.stop()
        self._jobs.put(None)

    async def speak(self, text: str) -> None:
        text = speakable(text)
        if not text or not self.available:
            return
        await self._queue(SpeechFeed(text, closed=True))

    def open_stream(self) -> SpokenStream:
        """Start speaking a reply that is still being written (see :class:`SpokenStream`)."""
        feed = SpeechFeed()
        if not self.available:
            return SpokenStream(feed, None)
        loop = asyncio.get_running_loop()
        done: asyncio.Future[None] = loop.create_future()
        self._stop.clear()
        self._jobs.put(_Job(feed, done, loop))
        return SpokenStream(feed, done)

    async def _queue(self, feed: SpeechFeed) -> None:
        loop = asyncio.get_running_loop()
        done: asyncio.Future[None] = loop.create_future()
        self._stop.clear()
        self._jobs.put(_Job(feed, done, loop))
        await done

    def _budget(self, seconds_playing: float) -> int:
        """How much text can be made while ``seconds_playing`` of speech plays."""
        chars = (seconds_playing + _SENTENCE_PAUSE_S) / self._seconds_per_char * _SAFETY
        return int(max(40, min(_LONGEST_CHARS, chars)))

    def _learn(self, text: str, seconds: float) -> None:
        if len(text) >= 8:
            self._seconds_per_char = 0.7 * self._seconds_per_char + 0.3 * seconds / len(text)

    def stop(self) -> None:
        self._stop.set()

    # --- neural voice ---

    def prepare_neural(
        self, download: bool, status: Callable[[str, str], None] | None = None
    ) -> None:
        """Download (if allowed and needed) and load the neural voice. Blocking: run it
        on a background thread. ``status(title, message)`` reports what happened."""
        if self._engine != "kokoro" or self._model_folder is None or not self.available:
            return
        from jarvis.voice import neural

        say = status or (lambda _title, _message: None)
        folder = self._model_folder
        fetched = False
        try:
            if neural.missing_files(folder):
                if not download:
                    logger.info("Neural voice not downloaded; using the Windows voice")
                    return
                megabytes = sum(f.size for f in neural.missing_files(folder)) // 1_000_000
                say(
                    "Getting JARVIS's voice",
                    f"Downloading the voice model once ({megabytes} MB). The Windows voice "
                    "speaks until it's ready.",
                )
                self._download(neural, folder)
                fetched = True
            self._neural = neural.KokoroVoice(folder, self._neural_voice, self._neural_speed)
            logger.info("Neural voice ready ({})", self._neural.voice)
            if fetched:
                say("JARVIS's voice is ready", "Replies are now spoken in the British voice.")
        except Exception as exc:
            logger.warning("Neural voice unavailable, using the Windows voice: {}", exc)
            say("Using the Windows voice", f"JARVIS's own voice couldn't start ({exc}).")

    def _download(self, neural: Any, folder: Path) -> None:
        import httpx

        marks = {25, 50, 75}

        def progress(done: int, total: int) -> None:
            if self._closed.is_set():
                raise neural.VoiceModelError("download stopped; it resumes next time")
            percent = done * 100 // max(total, 1)
            for mark in sorted(marks):
                if percent >= mark:
                    marks.discard(mark)
                    logger.info("Voice model download {}%", mark)

        with httpx.Client(timeout=httpx.Timeout(60.0, connect=15.0)) as client:
            neural.download(folder, client, progress)

    def _play_neural(self, feed: SpeechFeed, first: str) -> None:
        """Speak ``first`` and then the rest of ``feed``, making each next piece while
        the current one plays."""
        import numpy as np
        import sounddevice as sd

        from jarvis.voice.neural import SAMPLE_RATE

        voice = self._neural
        pause = np.zeros(int(_SENTENCE_PAUSE_S * SAMPLE_RATE), dtype=np.float32)
        pool = ThreadPoolExecutor(1, thread_name_prefix="tts-synth")
        stream = sd.OutputStream(samplerate=SAMPLE_RATE, channels=1, dtype="float32", latency="low")

        def make(piece: str) -> tuple[str, Any, float]:
            started = time.perf_counter()
            audio = voice.synthesize(speakable(piece.rstrip(",;: ")))
            return piece, audio, time.perf_counter() - started

        try:
            pending: Future[Any] | None = pool.submit(make, first)
            stream.start()
            while pending is not None:
                piece, audio, took = pending.result()
                self._learn(piece, took)
                if self._stop.is_set():
                    return
                playing = len(audio) / SAMPLE_RATE
                following = feed.take(self._budget(playing), stop=self._stop)
                pending = pool.submit(make, following) if following else None
                if following is not None:
                    audio = np.concatenate([audio, pause])
                for start in range(0, len(audio), _BLOCK):
                    if self._stop.is_set():
                        stream.abort()
                        return
                    stream.write(audio[start : start + _BLOCK].reshape(-1, 1))
                if pending is None and following is not None:
                    # The reply is still being written: wait for its next clause.
                    following = feed.take(self._budget(playing), wait_s=120, stop=self._stop)
                    pending = pool.submit(make, following) if following else None
            stream.stop()  # let the last block finish
        finally:
            pool.shutdown(wait=False, cancel_futures=True)
            stream.close()

    # --- worker ---

    def _open_sapi(self) -> Any:
        try:
            import pythoncom
            import win32com.client

            pythoncom.CoInitialize()
            voice = win32com.client.Dispatch("SAPI.SpVoice")
            voice.Rate = self._rate
            if self._voice_hint:
                for token in voice.GetVoices():
                    if self._voice_hint in token.GetDescription().lower():
                        voice.Voice = token
                        break
            return voice
        except Exception as exc:
            logger.error("Windows speech engine unavailable: {}", type(exc).__name__)
            return None

    def _speak_sapi(self, voice: Any, text: str, language_id: int | None = None) -> None:
        usual = voice.Voice
        match = _voice_for(voice, language_id) if language_id is not None else None
        if match is not None:
            voice.Voice = match
        try:
            voice.Speak(text, _SVSF_ASYNC | _SVSF_PURGE)
            while not voice.WaitUntilDone(50):
                if self._stop.is_set():
                    voice.Speak("", _SVSF_ASYNC | _SVSF_PURGE)
                    break
        finally:
            if match is not None:
                voice.Voice = usual

    def _worker(self) -> None:
        sapi = self._open_sapi()
        if sapi is None and self._engine != "kokoro":
            self.available = False
        while True:
            job = self._jobs.get()
            if job is None:
                return
            try:
                first = job.feed.take(_FIRST_CHARS, wait_s=120, stop=self._stop, single=True)
                if not first:
                    continue
                self.speaking = True
                spoken = False
                # The British voice only speaks English; Hindi, Malayalam, Arabic... go to
                # a Windows voice for that language when one is installed.
                language = script_language(first)
                if self._neural is not None and language is None:
                    try:
                        self._play_neural(job.feed, first)
                        spoken = True
                    except Exception as exc:
                        logger.warning("Neural voice failed: {}", type(exc).__name__)
                if not spoken and sapi is not None:
                    piece: str | None = first
                    while piece and not self._stop.is_set():
                        text = speakable(piece.rstrip(",;: "))
                        self._speak_sapi(sapi, text, language[1] if language else None)
                        piece = job.feed.take(_LONGEST_CHARS, wait_s=120, stop=self._stop)
            except Exception as exc:
                logger.warning("Speech failed: {}", type(exc).__name__)
            finally:
                self.speaking = False
                job.loop.call_soon_threadsafe(_resolve, job.done)


def _voice_for(voice: Any, language_id: int) -> Any:
    """An installed SAPI voice whose language has this primary language id, if any."""
    for token in voice.GetVoices():
        try:
            codes = str(token.GetAttribute("Language")).split(";")
            if any(int(code, 16) & 0x3FF == language_id for code in codes if code):
                return token
        except Exception:
            continue
    return None


def _resolve(future: asyncio.Future[None]) -> None:
    if not future.done():
        future.set_result(None)
