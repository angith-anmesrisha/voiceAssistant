"""Offline "JARVIS" wake word, using Windows' built-in speech recognizer (SAPI).

A command grammar that only knows the wake phrases runs on its own thread against the
default microphone. Everything stays on the PC; no audio is stored or logged. The
recognizer is paused while JARVIS is recording or speaking (so it can't hear itself),
and only confident matches count, to keep false alarms rare.

Say "Jarvis", wait for the orb, then speak: the command is recorded separately and
recording stops by itself when you go quiet.
"""

from __future__ import annotations

import sys
import threading
import time
from collections.abc import Callable
from typing import Any

from loguru import logger

# SpeechRuleState / SpeechRecognizerState / SpeechRuleAttributes / confidence values.
_SGDS_ACTIVE, _SGDS_INACTIVE = 1, 0
_SRS_ACTIVE, _SRS_INACTIVE = 1, 0
_SRA_TOP_LEVEL, _SRA_DYNAMIC = 1, 32
CONFIDENCE = {"low": -1, "normal": 0, "high": 1}


class WakeWordListener:
    """Calls ``on_wake`` (from a background thread) when a wake phrase is heard."""

    def __init__(
        self,
        phrases: list[str],
        on_wake: Callable[[], object],
        min_confidence: str = "normal",
    ) -> None:
        self.available = sys.platform == "win32" and bool(phrases)
        self._phrases = [p.strip() for p in phrases if p.strip()]
        self._on_wake = on_wake
        self._min_confidence = CONFIDENCE.get(min_confidence, 0)
        self._paused = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.error: str | None = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if not self.available or self.running:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="jarvis-wake", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def pause(self, paused: bool) -> None:
        """Stop listening while JARVIS records or talks; resume afterwards."""
        if paused:
            self._paused.set()
        else:
            self._paused.clear()

    def accepts(self, confidence: int) -> bool:
        return confidence >= self._min_confidence and not self._paused.is_set()

    def _heard(self) -> None:
        logger.info("Wake word heard")
        self._on_wake()

    def _run(self) -> None:
        import pythoncom
        import win32com.client

        pythoncom.CoInitialize()
        try:
            recognizer = win32com.client.gencache.EnsureDispatch("SAPI.SpInprocRecognizer")
            inputs = recognizer.GetAudioInputs()
            if inputs.Count == 0:
                raise RuntimeError("no microphone")
            recognizer.AudioInput = inputs.Item(0)  # the Windows default microphone
            context = recognizer.CreateRecoContext()
            events: Any = win32com.client.WithEvents(context, _WakeEvents)
            events.owner = self
            grammar = context.CreateGrammar(1)
            rule = grammar.Rules.Add("wake", _SRA_TOP_LEVEL | _SRA_DYNAMIC, 1)
            for phrase in self._phrases:
                rule.InitialState.AddWordTransition(None, phrase)
            grammar.Rules.Commit()
            grammar.CmdSetRuleState("wake", _SGDS_ACTIVE)
            recognizer.State = _SRS_ACTIVE
            logger.info("Wake word listening")
            paused = False
            while not self._stop.is_set():
                if self._paused.is_set() != paused:
                    paused = self._paused.is_set()
                    recognizer.State = _SRS_INACTIVE if paused else _SRS_ACTIVE
                pythoncom.PumpWaitingMessages()
                time.sleep(0.03)
            grammar.CmdSetRuleState("wake", _SGDS_INACTIVE)
        except Exception as exc:
            self.error = f"{type(exc).__name__}"
            logger.warning("Wake word unavailable: {}", self.error)
        finally:
            pythoncom.CoUninitialize()


class _WakeEvents:
    """SAPI recognition-context event sink (method names follow pywin32's On<Event>)."""

    owner: WakeWordListener

    def OnRecognition(  # noqa: N802
        self, _stream: Any, _position: Any, _kind: Any, result: Any
    ) -> None:
        import win32com.client

        try:
            confidence = int(win32com.client.Dispatch(result).PhraseInfo.Rule.Confidence)
        except Exception:
            confidence = 0
        if self.owner.accepts(confidence):
            self.owner._heard()
