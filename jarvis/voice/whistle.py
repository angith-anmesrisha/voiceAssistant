"""Offline speech-to-text with Cactus's Whistle model (cactus-compute/needle, Apache-2.0).

Whistle is a small (about 17 MB) speech model that runs on the CPU and handles English,
German, French, Spanish, Italian, Dutch and Polish. It's optional: if the ``needle``
package is installed (``pip install cactus-needle``), JARVIS uses it when no online
provider can transcribe, before Windows' own dictation. Its telemetry is switched off
before it loads. The model is fetched by the package on first use and then stays on the
PC; recordings never leave it.

Names JARVIS should expect (``voice.custom_words`` and remembered names) are passed as
Whistle's keywords.
"""

from __future__ import annotations

import asyncio
import importlib.util
import io
import os
import threading
import wave
from collections.abc import Callable
from typing import Any

from loguru import logger

DISPLAY_NAME = "Whistle (offline)"


def whistle_installed() -> bool:
    try:
        return importlib.util.find_spec("needle") is not None
    except (ImportError, ValueError):
        return False


def pcm_float32(wav: bytes) -> Any:
    """16-bit mono WAV bytes → float32 samples in -1..1 (what Whistle takes)."""
    import numpy as np

    with wave.open(io.BytesIO(wav), "rb") as reader:
        if reader.getnchannels() != 1 or reader.getsampwidth() != 2:
            raise ValueError("needs 16-bit mono audio")
        if reader.getframerate() != 16_000:
            raise ValueError("needs 16 kHz audio")
        frames = reader.readframes(reader.getnframes())
    return np.frombuffer(frames, dtype="<i2").astype(np.float32) / 32768.0


class WhistleStt:
    name = DISPLAY_NAME

    def __init__(
        self,
        keywords: Callable[[], list[str]] = list,
        *,
        loader: Callable[[], Any] | None = None,
    ) -> None:
        self._keywords = keywords
        self._loader = loader or _load_whistle
        self._model: Any = None
        self._lock = threading.Lock()
        self.available = loader is not None or whistle_installed()

    def _transcribe(self, wav: bytes) -> str:
        with self._lock:
            if self._model is None:
                self._model = self._loader()
            words = self._keywords() or None
            result = self._model.transcribe(pcm_float32(wav), keywords=words)
        text = result.get("text", "") if isinstance(result, dict) else str(result or "")
        return " ".join(str(text).split())

    async def transcribe(self, wav: bytes) -> str:
        return await asyncio.to_thread(self._transcribe, wav)


def _load_whistle() -> Any:
    os.environ.setdefault("NEEDLE_TELEMETRY", "0")  # off unless the user turned it on
    from needle.agent.whistle import Whistle  # type: ignore[import-not-found]

    logger.info("Loading the Whistle speech model")
    return Whistle()


class OfflineStt:
    """The offline speech-to-text engines in order; the first that answers wins."""

    def __init__(self, engines: list[Any]) -> None:
        self._engines = engines
        self.name = ""

    @property
    def available(self) -> bool:
        return any(engine.available for engine in self._engines)

    async def transcribe(self, wav: bytes) -> str:
        problem: Exception | None = None
        for engine in self._engines:
            if not engine.available:
                continue
            try:
                text = str(await engine.transcribe(wav))
            except Exception as exc:
                logger.warning("{} failed: {}", getattr(engine, "name", "Offline STT"), exc)
                problem = exc
                continue
            self.name = getattr(engine, "name", "")
            return text
        raise problem or RuntimeError("no offline speech-to-text is available")
