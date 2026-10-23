"""Microphone capture to an in-memory WAV, with a live level for the orb.

Audio is 16-bit little-endian mono PCM at 16 kHz, which every speech-to-text provider
accepts. PortAudio callbacks run on an audio thread, so chunks are handed back to
asyncio through ``loop.call_soon_threadsafe``. Audio is never written to disk.
"""

from __future__ import annotations

import array
import asyncio
import io
import math
import wave
from collections.abc import Callable
from typing import Any, Protocol

from loguru import logger

SAMPLE_WIDTH = 2  # bytes per int16 sample


class AudioDeviceError(RuntimeError):
    """A microphone or speaker could not be opened."""


class Microphone:
    """Captures mic audio in small blocks while started. Opened only while in use."""

    def __init__(self, sample_rate: int, block_ms: int = 32, device: str | None = None) -> None:
        self._rate = sample_rate
        self._blocksize = sample_rate * block_ms // 1000
        self._device = device
        self._stream: Any = None

    @property
    def is_open(self) -> bool:
        return self._stream is not None

    def start(self, on_chunk: Callable[[bytes], None]) -> None:
        """Start capturing; ``on_chunk`` is called on the event loop thread."""
        if self._stream is not None:
            return
        import sounddevice as sd

        loop = asyncio.get_running_loop()

        def callback(indata: Any, _frames: int, _time: Any, _status: Any) -> None:
            loop.call_soon_threadsafe(on_chunk, bytes(indata))

        try:
            self._stream = sd.RawInputStream(
                samplerate=self._rate,
                channels=1,
                dtype="int16",
                blocksize=self._blocksize,
                device=self._device,
                latency="low",
                callback=callback,
            )
            self._stream.start()
        except Exception as exc:
            self._stream = None
            raise AudioDeviceError(f"Microphone unavailable: {exc}") from exc

    def stop(self) -> None:
        stream, self._stream = self._stream, None
        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception as exc:
                logger.warning("Error closing microphone: {}", exc)


def pcm_level(pcm: bytes) -> float:
    """Loudness of a PCM16 chunk on a 0..1 scale (RMS, gently compressed)."""
    samples = array.array("h")
    samples.frombytes(pcm[: len(pcm) - len(pcm) % SAMPLE_WIDTH])
    if not samples:
        return 0.0
    rms = math.sqrt(sum(s * s for s in samples) / len(samples)) / 32768.0
    return min(1.0, math.sqrt(rms * 4.0))


def to_wav(pcm: bytes, sample_rate: int) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(SAMPLE_WIDTH)
        wav.setframerate(sample_rate)
        wav.writeframes(pcm)
    return buffer.getvalue()


class Recorder:
    """Collects mic audio between start() and stop(); exposes a smoothed level."""

    def __init__(self, microphone: MicrophoneLike, sample_rate: int) -> None:
        self._mic = microphone
        self._rate = sample_rate
        self._pcm = bytearray()
        self.level = 0.0
        self.recording = False

    def start(self) -> None:
        self._pcm.clear()
        self.level = 0.0
        self._mic.start(self._on_chunk)
        self.recording = True

    def _on_chunk(self, pcm: bytes) -> None:
        if self.recording:
            self._pcm.extend(pcm)
            self.level = 0.6 * self.level + 0.4 * pcm_level(pcm)

    def stop(self) -> bytes:
        """Stop and return the recording as WAV bytes."""
        self._mic.stop()
        self.recording = False
        self.level = 0.0
        pcm, self._pcm = bytes(self._pcm), bytearray()
        return to_wav(pcm, self._rate)

    def duration_s(self, wav: bytes) -> float:
        return max(0, len(wav) - 44) / (SAMPLE_WIDTH * self._rate)


class MicrophoneLike(Protocol):
    def start(self, on_chunk: Callable[[bytes], None]) -> None: ...
    def stop(self) -> None: ...
