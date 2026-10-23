"""Offline speech-to-text with Windows' built-in speech recognizer (SAPI dictation).

Used only when no online provider can transcribe. The recording is handed to SAPI in
memory; nothing is written to disk. Accuracy is below Whisper or Gemini, so JARVIS says
when it used this.
"""

from __future__ import annotations

import asyncio
import io
import sys
import time
import wave
from typing import Any

from loguru import logger

DISPLAY_NAME = "Windows speech (offline)"

# SpeechAudioFormatType values for 16-bit mono PCM, by sample rate.
_SAFT_16BIT_MONO = {
    8000: 6,
    11025: 10,
    12000: 14,
    16000: 18,
    22050: 22,
    24000: 26,
    32000: 30,
    44100: 34,
    48000: 38,
}
_SGDS_ACTIVE = 1
_SGDS_INACTIVE = 0


class LocalSttError(Exception):
    pass


class WindowsDictation:
    """Transcribes a WAV recording with SAPI. Unavailable off Windows."""

    def __init__(self) -> None:
        self.available = sys.platform == "win32"

    async def transcribe(self, wav: bytes) -> str:
        if not self.available:
            raise LocalSttError("only available on Windows")
        return await asyncio.to_thread(_recognize, wav)


def _recognize(wav: bytes) -> str:
    with wave.open(io.BytesIO(wav), "rb") as reader:
        rate = reader.getframerate()
        if reader.getnchannels() != 1 or reader.getsampwidth() != 2:
            raise LocalSttError("needs 16-bit mono audio")
        pcm = reader.readframes(reader.getnframes())
    format_type = _SAFT_16BIT_MONO.get(rate)
    if format_type is None:
        raise LocalSttError(f"unsupported sample rate {rate}")
    duration = len(pcm) / (2 * rate)

    import pythoncom
    import win32com.client

    pythoncom.CoInitialize()
    try:
        try:
            recognizer = win32com.client.gencache.EnsureDispatch("SAPI.SpInprocRecognizer")
            tokens = recognizer.GetRecognizers()
        except Exception as exc:
            raise LocalSttError("Windows speech recognition isn't available") from exc
        if tokens.Count == 0:
            raise LocalSttError(
                "no Windows speech recognizer is installed (Settings > Time & language > Speech)"
            )
        recognizer.Recognizer = tokens.Item(0)

        audio_format = win32com.client.gencache.EnsureDispatch("SAPI.SpAudioFormat")
        audio_format.Type = format_type
        stream = win32com.client.gencache.EnsureDispatch("SAPI.SpMemoryStream")
        stream.Format = audio_format
        stream.SetData(pcm)
        stream.Seek(0, 0)  # read from the start
        recognizer.AudioInputStream = stream

        context = recognizer.CreateRecoContext()
        events: Any = win32com.client.WithEvents(context, _ContextEvents)
        events.parts = []
        events.done = False
        grammar = context.CreateGrammar()
        grammar.DictationLoad()
        grammar.DictationSetState(_SGDS_ACTIVE)

        deadline = time.monotonic() + max(10.0, duration * 3)
        while not events.done and time.monotonic() < deadline:
            pythoncom.PumpWaitingMessages()
            time.sleep(0.02)
        grammar.DictationSetState(_SGDS_INACTIVE)
        if not events.done:
            logger.warning("Windows speech recognition timed out")
        return " ".join(part for part in events.parts if part).strip()
    except LocalSttError:
        raise
    except Exception as exc:
        raise LocalSttError(f"Windows speech recognition failed ({type(exc).__name__})") from exc
    finally:
        pythoncom.CoUninitialize()


class _ContextEvents:
    """SAPI recognition-context event sink (method names follow pywin32's On<Event>)."""

    parts: list[str]
    done: bool

    def OnRecognition(  # noqa: N802
        self, _stream: Any, _position: Any, _kind: Any, result: Any
    ) -> None:
        import win32com.client

        text = win32com.client.Dispatch(result).PhraseInfo.GetText()
        self.parts.append(str(text or ""))

    def OnEndStream(self, _stream: Any, _position: Any, _released: Any) -> None:  # noqa: N802
        self.done = True
