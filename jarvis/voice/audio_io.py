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


class EndOfSpeech:
    """Notices when someone has finished talking (for hands-free recordings).

    Speech is a level clearly above the room's noise floor. Recording ends after
    ``silence_s`` of quiet following at least ``min_speech_s`` of speech, or after
    ``no_speech_s`` if nobody speaks at all.
    """

    def __init__(
        self, silence_s: float = 1.1, no_speech_s: float = 6.0, min_speech_s: float = 0.25
    ) -> None:
        self.silence_s = silence_s
        self.no_speech_s = no_speech_s
        self.min_speech_s = min_speech_s
        self.noise = 0.0
        self.speech_s = 0.0
        self.quiet_s = 0.0
        self.elapsed_s = 0.0

    @property
    def heard_speech(self) -> bool:
        return self.speech_s >= self.min_speech_s

    def feed(self, level: float, seconds: float) -> bool:
        """Add one chunk's level; returns True when the recording should stop."""
        self.elapsed_s += seconds
        if self.elapsed_s < 0.15:  # settle on the noise floor first
            self.noise = min(0.3, max(self.noise, level))
            return False
        threshold = max(0.16, self.noise * 1.8)
        if level > threshold:
            self.speech_s += seconds
            self.quiet_s = 0.0
        else:
            self.quiet_s += seconds
            self.noise = 0.95 * self.noise + 0.05 * level  # slowly track the room
        if self.heard_speech:
            return self.quiet_s >= self.silence_s
        return self.elapsed_s >= self.no_speech_s


class Recorder:
    """Collects mic audio between start() and stop(); exposes a smoothed level."""

    def __init__(
        self, microphone: MicrophoneLike, sample_rate: int, *, silence_s: float = 0.6
    ) -> None:
        self._mic = microphone
        self._rate = sample_rate
        self._silence_s = silence_s
        self._pcm = bytearray()
        self.level = 0.0
        self.recording = False
        self._endpoint: EndOfSpeech | None = None
        self._on_done: Callable[[], object] | None = None

    @property
    def heard_speech(self) -> bool:
        """For hands-free recordings: whether anyone spoke (True for push-to-talk)."""
        return self._endpoint is None or self._endpoint.heard_speech

    def start(
        self, on_done: Callable[[], object] | None = None, *, no_speech_s: float = 6.0
    ) -> None:
        """Start recording. With ``on_done``, it is called once the speaker goes quiet
        (or after ``no_speech_s`` if nobody speaks)."""
        self._pcm.clear()
        self.level = 0.0
        self._endpoint = (
            EndOfSpeech(silence_s=self._silence_s, no_speech_s=no_speech_s)
            if on_done is not None
            else None
        )
        self._on_done = on_done
        self._mic.start(self._on_chunk)
        self.recording = True

    def _on_chunk(self, pcm: bytes) -> None:
        if not self.recording:
            return
        self._pcm.extend(pcm)
        level = pcm_level(pcm)
        self.level = 0.6 * self.level + 0.4 * level
        if self._endpoint is not None and self._on_done is not None:
            seconds = len(pcm) / (SAMPLE_WIDTH * self._rate)
            if self._endpoint.feed(level, seconds):
                done, self._on_done = self._on_done, None
                done()

    def stop(self) -> bytes:
        """Stop and return the recording as WAV bytes."""
        self._mic.stop()
        self.recording = False
        self._on_done = None
        self.level = 0.0
        pcm, self._pcm = bytes(self._pcm), bytearray()
        return to_wav(pcm, self._rate)

    def duration_s(self, wav: bytes) -> float:
        return max(0, len(wav) - 44) / (SAMPLE_WIDTH * self._rate)


class MicrophoneLike(Protocol):
    def start(self, on_chunk: Callable[[bytes], None]) -> None: ...
    def stop(self) -> None: ...
