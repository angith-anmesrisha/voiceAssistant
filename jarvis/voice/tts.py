"""Spoken replies: JARVIS's neural voice when it's ready, Windows' own voice (SAPI) otherwise.

The neural voice (:mod:`jarvis.voice.neural`, Kokoro, British male) runs on this PC and
needs a one-time model download; until that's done, or if it can't run here, SAPI
speaks. Both work offline. Speech runs on its own thread (SAPI is a COM object).
``speak`` resolves when speech finishes or is stopped; ``stop`` cuts it off at once
(barge-in).
"""

from __future__ import annotations

import asyncio
import queue
import sys
import threading
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from loguru import logger

from jarvis.voice.spoken_text import speakable

_SVSF_ASYNC = 1
_SVSF_PURGE = 2
_SENTENCE_PAUSE_S = 0.32  # between sentences: measured, not hurried
_BLOCK = 1200  # frames written at a time (50 ms), so stop() is quick

__all__ = ["Speaker", "speakable"]


@dataclass
class _Job:
    text: str
    done: asyncio.Future[None]
    loop: asyncio.AbstractEventLoop


class Speaker:
    """Text-to-speech. A silent no-op where SAPI isn't available (tests, Linux)."""

    def __init__(
        self,
        voice_hint: str = "",
        rate: int = 1,
        *,
        engine: str = "sapi",
        neural_voice: str = "bm_george",
        neural_speed: float = 0.95,
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
        loop = asyncio.get_running_loop()
        done: asyncio.Future[None] = loop.create_future()
        self._stop.clear()
        self._jobs.put(_Job(text, done, loop))
        await done

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

    def _play_neural(self, text: str) -> None:
        import numpy as np
        import sounddevice as sd

        from jarvis.voice.neural import SAMPLE_RATE, sentences

        voice = self._neural
        parts = sentences(text)
        if not parts:
            return
        pause = np.zeros(int(_SENTENCE_PAUSE_S * SAMPLE_RATE), dtype=np.float32)
        pool = ThreadPoolExecutor(1, thread_name_prefix="tts-synth")
        stream = sd.OutputStream(samplerate=SAMPLE_RATE, channels=1, dtype="float32")
        try:
            # Make the next sentence while this one plays.
            pending: Future[Any] = pool.submit(voice.synthesize, parts[0])
            stream.start()
            for index in range(len(parts)):
                audio = pending.result()
                if self._stop.is_set():
                    return
                if index + 1 < len(parts):
                    pending = pool.submit(voice.synthesize, parts[index + 1])
                    audio = np.concatenate([audio, pause])
                for start in range(0, len(audio), _BLOCK):
                    if self._stop.is_set():
                        stream.abort()
                        return
                    stream.write(audio[start : start + _BLOCK].reshape(-1, 1))
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

    def _speak_sapi(self, voice: Any, text: str) -> None:
        voice.Speak(text, _SVSF_ASYNC | _SVSF_PURGE)
        while not voice.WaitUntilDone(50):
            if self._stop.is_set():
                voice.Speak("", _SVSF_ASYNC | _SVSF_PURGE)
                break

    def _worker(self) -> None:
        sapi = self._open_sapi()
        if sapi is None and self._engine != "kokoro":
            self.available = False
        while True:
            job = self._jobs.get()
            if job is None:
                return
            try:
                self.speaking = True
                spoken = False
                if self._neural is not None:
                    try:
                        self._play_neural(job.text)
                        spoken = True
                    except Exception as exc:
                        logger.warning("Neural voice failed: {}", type(exc).__name__)
                if not spoken and sapi is not None and not self._stop.is_set():
                    self._speak_sapi(sapi, job.text)
            except Exception as exc:
                logger.warning("Speech failed: {}", type(exc).__name__)
            finally:
                self.speaking = False
                job.loop.call_soon_threadsafe(_resolve, job.done)


def _resolve(future: asyncio.Future[None]) -> None:
    if not future.done():
        future.set_result(None)
