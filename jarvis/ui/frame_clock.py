"""One shared animation clock for every animated widget.

On Windows the clock follows the display's refresh: a small thread waits on
``DwmFlush`` (which returns once per composition, i.e. per vsync) and wakes the UI thread.
Painting in step with the screen avoids the dropped or doubled frames a plain timer
causes (a 16 ms timer and a 60 Hz screen drift apart). Elsewhere, or if DWM is not
available, a precise ~60 Hz timer is used.

Subscribers ask for a frame rate (60 or 30); on faster screens frames are skipped so the
cost stays the same. The clock runs only while someone is subscribed.
"""

from __future__ import annotations

import sys
import threading
import time
from collections.abc import Callable

from loguru import logger
from PySide6.QtCore import QObject, Qt, QTimer, Signal

FrameCallback = Callable[[float], None]

_SLACK_S = 0.004  # a frame that arrives this early still counts (vsync jitter)


class FrameClock(QObject):
    _vsync = Signal()

    _instance: FrameClock | None = None

    def __init__(self) -> None:
        super().__init__()
        self._subscribers: dict[int, tuple[FrameCallback, float, list[float]]] = {}
        self._next_id = 0
        self._timer = QTimer(self)
        self._timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._timer.timeout.connect(self._on_frame)
        self._vsync.connect(self._on_vsync, Qt.ConnectionType.QueuedConnection)
        self._wake = threading.Event()
        self._consumed = threading.Event()
        self._consumed.set()
        self._thread: threading.Thread | None = None
        self._use_dwm = sys.platform == "win32"

    @classmethod
    def shared(cls) -> FrameClock:
        if cls._instance is None:
            cls._instance = FrameClock()
        return cls._instance

    def subscribe(self, callback: FrameCallback, fps: float = 60.0) -> int:
        """Call ``callback(now)`` about ``fps`` times a second until unsubscribed."""
        self._next_id += 1
        self._subscribers[self._next_id] = (callback, 1.0 / fps, [time.perf_counter()])
        self._run()
        return self._next_id

    def set_rate(self, token: int, fps: float) -> None:
        if token in self._subscribers:
            callback, _, last = self._subscribers[token]
            self._subscribers[token] = (callback, 1.0 / fps, last)

    def unsubscribe(self, token: int) -> None:
        self._subscribers.pop(token, None)
        if not self._subscribers:
            self._timer.stop()
            self._wake.clear()

    # --- frame sources ---

    def _run(self) -> None:
        if self._use_dwm and self._start_dwm():
            self._wake.set()
        elif not self._timer.isActive():
            self._timer.start(16)

    def _start_dwm(self) -> bool:
        if self._thread is not None:
            return self._thread.is_alive()
        try:
            import ctypes

            flush = ctypes.windll.dwmapi.DwmFlush  # type: ignore[attr-defined]
            if flush() != 0:  # S_OK only while desktop composition runs
                raise OSError("DwmFlush failed")
        except Exception as exc:
            logger.info("Display sync unavailable ({}); using a timer", type(exc).__name__)
            self._use_dwm = False
            return False

        def loop() -> None:
            while True:
                self._wake.wait()
                if flush() != 0:
                    time.sleep(1 / 60)
                # Skip a vsync if the UI thread hasn't drawn the previous one yet.
                if self._consumed.is_set():
                    self._consumed.clear()
                    self._vsync.emit()

        self._thread = threading.Thread(target=loop, name="jarvis-vsync", daemon=True)
        self._thread.start()
        return True

    def _on_vsync(self) -> None:
        try:
            self._on_frame()
        finally:
            self._consumed.set()

    def _on_frame(self) -> None:
        now = time.perf_counter()
        for callback, interval, last in list(self._subscribers.values()):
            if now - last[0] >= interval - _SLACK_S:
                last[0] = now
                callback(now)


__all__ = ["FrameClock"]
