"""Spoken replies through Windows' built-in speech engine (SAPI). Works offline.

SAPI is a COM object, so it lives on its own thread. ``speak`` resolves when speech
finishes or is stopped; ``stop`` cuts it off immediately (barge-in).
"""

from __future__ import annotations

import asyncio
import queue
import re
import sys
import threading
from dataclasses import dataclass

from loguru import logger

_SVSF_ASYNC = 1
_SVSF_PURGE = 2

_URL = re.compile(r"https?://\S+")
_MARKUP = re.compile(r"[*_#`>|]+")


def speakable(text: str) -> str:
    """Strip markdown and URLs so they aren't read out."""
    text = _URL.sub("link", text)
    text = _MARKUP.sub("", text)
    return re.sub(r"\s+", " ", text).strip()


@dataclass
class _Job:
    text: str
    done: asyncio.Future[None]
    loop: asyncio.AbstractEventLoop


class Speaker:
    """Text-to-speech. A silent no-op where SAPI isn't available (tests, Linux)."""

    def __init__(self, voice_hint: str = "", rate: int = 1) -> None:
        self._voice_hint = voice_hint.lower()
        self._rate = max(-10, min(10, rate))
        self._jobs: queue.Queue[_Job | None] = queue.Queue()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.available = sys.platform == "win32"
        self.speaking = False

    def start(self) -> None:
        if self.available and self._thread is None:
            self._thread = threading.Thread(target=self._worker, name="tts", daemon=True)
            self._thread.start()

    def shutdown(self) -> None:
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

    def _worker(self) -> None:
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
        except Exception as exc:
            logger.error("Speech engine unavailable: {}", type(exc).__name__)
            self.available = False
            voice = None
        while True:
            job = self._jobs.get()
            if job is None:
                return
            try:
                if voice is not None:
                    self.speaking = True
                    voice.Speak(job.text, _SVSF_ASYNC | _SVSF_PURGE)
                    while not voice.WaitUntilDone(50):
                        if self._stop.is_set():
                            voice.Speak("", _SVSF_ASYNC | _SVSF_PURGE)
                            break
            except Exception as exc:
                logger.warning("Speech failed: {}", type(exc).__name__)
            finally:
                self.speaking = False
                job.loop.call_soon_threadsafe(_resolve, job.done)


def _resolve(future: asyncio.Future[None]) -> None:
    if not future.done():
        future.set_result(None)
