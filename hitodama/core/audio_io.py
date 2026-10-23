"""Microphone capture and speaker playback.

Both use raw 16-bit little-endian mono PCM, which is what the Gemini Live API takes
(16 kHz in) and returns (24 kHz out). PortAudio callbacks run on audio threads, so
everything handed back to asyncio goes through ``loop.call_soon_threadsafe``.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable
from typing import Any

from loguru import logger

SAMPLE_WIDTH = 2  # bytes per int16 sample


class AudioDeviceError(RuntimeError):
    """A microphone or speaker could not be opened."""


class PlaybackBuffer:
    """Thread-safe FIFO of PCM bytes, read in fixed-size blocks by the audio callback."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._data = bytearray()

    def write(self, chunk: bytes) -> None:
        with self._lock:
            self._data.extend(chunk)

    def read(self, nbytes: int) -> tuple[bytes, bool]:
        """Return exactly ``nbytes`` (zero-padded) and whether any real audio was in it."""
        with self._lock:
            had_audio = bool(self._data)
            out = bytes(self._data[:nbytes])
            del self._data[:nbytes]
        if len(out) < nbytes:
            out += b"\x00" * (nbytes - len(out))
        return out, had_audio

    def clear(self) -> None:
        with self._lock:
            self._data.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._data)


class Speaker:
    """Streams queued PCM to the output device. Calls ``on_drained`` when the queue empties."""

    def __init__(
        self,
        sample_rate: int,
        on_drained: Callable[[], None],
        device: str | None = None,
    ) -> None:
        self._rate = sample_rate
        self._device = device
        self._on_drained = on_drained
        self._buffer = PlaybackBuffer()
        self._stream: Any = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._was_playing = False

    @property
    def is_open(self) -> bool:
        return self._stream is not None

    @property
    def has_pending_audio(self) -> bool:
        return len(self._buffer) > 0

    def open(self) -> None:
        if self._stream is not None:
            return
        import sounddevice as sd

        self._loop = asyncio.get_running_loop()
        try:
            self._stream = sd.RawOutputStream(
                samplerate=self._rate,
                channels=1,
                dtype="int16",
                device=self._device,
                latency="low",
                callback=self._callback,
            )
            self._stream.start()
        except Exception as exc:
            self._stream = None
            raise AudioDeviceError(f"Speaker unavailable: {exc}") from exc

    def write(self, pcm: bytes) -> None:
        self._buffer.write(pcm)

    def clear(self) -> None:
        """Drop everything not yet played (barge-in / interruption)."""
        self._buffer.clear()

    def close(self) -> None:
        stream, self._stream = self._stream, None
        self._buffer.clear()
        self._was_playing = False
        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception as exc:
                logger.warning("Error closing speaker: {}", exc)

    def _callback(self, outdata: Any, frames: int, _time: Any, status: Any) -> None:
        data, had_audio = self._buffer.read(frames * SAMPLE_WIDTH)
        outdata[:] = data
        if had_audio:
            self._was_playing = True
        elif self._was_playing:
            self._was_playing = False
            if self._loop is not None:
                self._loop.call_soon_threadsafe(self._on_drained)


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
