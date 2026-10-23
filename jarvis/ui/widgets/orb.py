"""The animated JARVIS orb with circular HUD rings.

Everything is drawn with QPainter gradients and lines (no blur effects, no shaders), and
the timer only runs while the widget is visible, so it stays light on integrated GPUs.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from enum import StrEnum

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer
from PySide6.QtGui import (
    QColor,
    QConicalGradient,
    QFont,
    QHideEvent,
    QPainter,
    QPaintEvent,
    QPen,
    QRadialGradient,
    QShowEvent,
)
from PySide6.QtWidgets import QSizePolicy, QWidget

from jarvis.ui import theme
from jarvis.ui.theme import CYAN, DEEP_BLUE, ELECTRIC, ICE, STEEL, rgba


class VisualState(StrEnum):
    IDLE = "idle"
    LISTENING = "listening"
    THINKING = "thinking"
    SPEAKING = "speaking"
    ACTING = "executing"
    OFFLINE = "offline"


_ACTIVE_FPS = 30
_CALM_FPS = 20


class OrbHud(QWidget):
    def __init__(self, level: Callable[[], float] | None = None, parent: QWidget | None = None):
        super().__init__(parent)
        self._level_source = level
        self._state = VisualState.IDLE
        self._t = 0.0
        self._fixed_time: float | None = None
        self._fixed_level: float | None = None
        self._started = time.perf_counter()
        self._level = 0.0
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMinimumSize(320, 320)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, False)

    # --- control ---

    @property
    def state(self) -> VisualState:
        return self._state

    def set_state(self, state: VisualState) -> None:
        if state != self._state:
            self._state = state
            self._restart_timer()
            self.update()

    def freeze(self, t: float, level: float = 0.0) -> None:
        """Pin the animation to a moment (used for renders and tests)."""
        self._fixed_time = t
        self._fixed_level = level
        self._timer.stop()
        self._t = t
        self._level = level
        self.update()

    def _restart_timer(self) -> None:
        if self._fixed_time is not None or not self.isVisible():
            return
        calm = self._state in (VisualState.IDLE, VisualState.OFFLINE)
        self._timer.start(int(1000 / (_CALM_FPS if calm else _ACTIVE_FPS)))

    def showEvent(self, event: QShowEvent) -> None:  # noqa: N802
        super().showEvent(event)
        self._restart_timer()

    def hideEvent(self, event: QHideEvent) -> None:  # noqa: N802
        super().hideEvent(event)
        self._timer.stop()

    def _tick(self) -> None:
        self._t = time.perf_counter() - self._started
        raw = self._level_source() if self._level_source else 0.0
        self._level = 0.55 * self._level + 0.45 * raw
        self.update()

    # --- painting ---

    def _palette(self) -> tuple[QColor, QColor, QColor]:
        if self._state is VisualState.OFFLINE:
            return STEEL, QColor(60, 76, 96), QColor(150, 170, 190)
        if self._state is VisualState.ACTING:
            return ELECTRIC, DEEP_BLUE, ICE
        return CYAN, ELECTRIC, ICE

    def _energy(self) -> float:
        t, s = self._t, self._state
        if s is VisualState.OFFLINE:
            return 0.18
        if s is VisualState.LISTENING:
            return 0.55 + 0.45 * self._level
        if s is VisualState.THINKING:
            return 0.72 + 0.08 * math.sin(t * 5)
        if s is VisualState.SPEAKING:
            return 0.62 + 0.3 * abs(math.sin(t * 7.3) * math.sin(t * 2.1 + 1))
        if s is VisualState.ACTING:
            return 0.8
        return 0.42 + 0.06 * math.sin(t * 1.4)

    def paintEvent(self, _event: QPaintEvent) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        c = QPointF(w / 2, h / 2)
        radius = min(w, h) / 2 * 0.94
        primary, secondary, highlight = self._palette()
        energy = self._energy()
        t = self._t
        speed = {
            VisualState.THINKING: 4.0,
            VisualState.ACTING: 3.0,
            VisualState.OFFLINE: 0.25,
        }.get(self._state, 1.0)

        self._paint_ambient(p, c, radius, primary, energy)
        self._paint_outer_rings(p, c, radius, primary, highlight, t, speed)
        self._paint_segment_ring(p, c, radius * 0.74, primary, highlight, t, speed)
        self._paint_data_arcs(p, c, radius * 0.63, primary, secondary, t, speed)
        if self._state is VisualState.LISTENING:
            self._paint_voice_bars(p, c, radius * 0.47, radius * 0.13, primary, highlight, t)
        elif self._state is VisualState.SPEAKING:
            self._paint_speech_waves(p, c, radius * 0.36, radius * 0.6, primary, t)
        self._paint_core(p, c, radius * 0.33 * (0.9 + 0.14 * energy), energy, t, speed)
        p.end()

    def _paint_ambient(self, p: QPainter, c: QPointF, r: float, color: QColor, e: float) -> None:
        glow = QRadialGradient(c, r * 1.05)
        glow.setColorAt(0.0, rgba(color, int(110 * e)))
        glow.setColorAt(0.35, rgba(color, int(45 * e)))
        glow.setColorAt(1.0, rgba(color, 0))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(glow)
        p.drawEllipse(c, r * 1.05, r * 1.05)

    def _paint_outer_rings(
        self,
        p: QPainter,
        c: QPointF,
        r: float,
        color: QColor,
        hi: QColor,
        t: float,
        speed: float,
    ) -> None:
        p.setBrush(Qt.BrushStyle.NoBrush)
        # Faint boundary circle and rotating bracket arcs.
        p.setPen(QPen(rgba(color, 45), 1))
        p.drawEllipse(c, r, r)
        p.setPen(QPen(rgba(color, 170), 2.2, c=Qt.PenCapStyle.FlatCap))
        rect = QRectF(c.x() - r, c.y() - r, 2 * r, 2 * r)
        base = int((t * 9 * speed) % 360 * 16)
        for i in range(4):
            p.drawArc(rect, base + i * 90 * 16, 26 * 16)

        # Tick ring.
        tr = r * 0.9
        p.save()
        p.translate(c)
        p.rotate(-t * 3.5 * speed)
        for i in range(120):
            major = i % 10 == 0
            length = r * (0.05 if major else 0.022)
            alpha = 200 if major else 70
            p.setPen(QPen(rgba(hi if major else color, alpha), 1.4 if major else 1))
            p.drawLine(QPointF(tr, 0), QPointF(tr - length, 0))
            p.rotate(3)
        p.restore()

        # Degree labels (static, readable).
        p.setFont(theme.label_font(max(7.0, r * 0.028), QFont.Weight.Medium))
        p.setPen(rgba(color, 150))
        lr = r * 0.82
        for deg in (0, 90, 180, 270):
            a = math.radians(deg - 90)
            pos = QPointF(c.x() + lr * math.cos(a), c.y() + lr * math.sin(a))
            p.drawText(
                QRectF(pos.x() - 20, pos.y() - 8, 40, 16),
                Qt.AlignmentFlag.AlignCenter,
                f"{deg:03d}",
            )

    def _paint_segment_ring(
        self, p: QPainter, c: QPointF, r: float, color: QColor, hi: QColor, t: float, speed: float
    ) -> None:
        rect = QRectF(c.x() - r, c.y() - r, 2 * r, 2 * r)
        sweep = (t * 70 * speed) % 360
        for i in range(48):
            angle = i * 7.5
            dist = (angle - sweep) % 360
            boost = max(0.0, 1 - dist / 120)
            p.setPen(
                QPen(
                    rgba(hi if boost > 0.7 else color, int(40 + 190 * boost)),
                    3.2,
                    c=Qt.PenCapStyle.FlatCap,
                )
            )
            p.drawArc(rect, int(-angle * 16), int(-5.0 * 16))

    def _paint_data_arcs(
        self, p: QPainter, c: QPointF, r: float, a: QColor, b: QColor, t: float, speed: float
    ) -> None:
        for k, (rr, width, span, rate, color) in enumerate(
            (
                (r, 2.0, 110, 22, a),
                (r * 0.93, 5.0, 60, -34, b),
                (r * 0.86, 1.2, 200, 14, a),
            )
        ):
            rect = QRectF(c.x() - rr, c.y() - rr, 2 * rr, 2 * rr)
            p.setPen(QPen(rgba(color, 150 if k != 1 else 120), width, c=Qt.PenCapStyle.RoundCap))
            start = (t * rate * speed + k * 70) % 360
            p.drawArc(rect, int(start * 16), int(span * 16))
            p.drawArc(rect, int((start + 180) * 16), int(span * 0.35 * 16))

    def _paint_voice_bars(
        self, p: QPainter, c: QPointF, r: float, max_len: float, color: QColor, hi: QColor, t: float
    ) -> None:
        bars = 72
        level = max(0.06, self._level)
        for i in range(bars):
            a = 2 * math.pi * i / bars
            wobble = 0.5 + 0.5 * math.sin(i * 1.7 + t * 9) * math.sin(i * 0.53 - t * 4.3)
            length = max_len * (0.12 + 0.88 * level * (0.35 + 0.65 * wobble))
            inner = QPointF(c.x() + r * math.cos(a), c.y() + r * math.sin(a))
            outer = QPointF(c.x() + (r + length) * math.cos(a), c.y() + (r + length) * math.sin(a))
            p.setPen(QPen(rgba(hi if wobble > 0.8 else color, 210), 2.2, c=Qt.PenCapStyle.RoundCap))
            p.drawLine(inner, outer)

    def _paint_speech_waves(
        self, p: QPainter, c: QPointF, r0: float, r1: float, color: QColor, t: float
    ) -> None:
        p.setBrush(Qt.BrushStyle.NoBrush)
        for k in range(3):
            phase = (t * 0.9 + k / 3) % 1.0
            rr = r0 + (r1 - r0) * phase
            p.setPen(QPen(rgba(color, int(200 * (1 - phase))), 2.0))
            p.drawEllipse(c, rr, rr)

    def _paint_core(
        self, p: QPainter, c: QPointF, r: float, e: float, t: float, speed: float
    ) -> None:
        primary, secondary, highlight = self._palette()
        halo = QRadialGradient(c, r * 2.2)
        halo.setColorAt(0.0, rgba(highlight, int(200 * e)))
        halo.setColorAt(0.4, rgba(primary, int(120 * e)))
        halo.setColorAt(0.7, rgba(secondary, int(40 * e)))
        halo.setColorAt(1.0, rgba(secondary, 0))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(halo)
        p.drawEllipse(c, r * 2.2, r * 2.2)

        core = QRadialGradient(c, r)
        core.setColorAt(0.0, rgba(highlight, 255))
        core.setColorAt(0.22, rgba(highlight, 235))
        core.setColorAt(0.5, rgba(primary, 220))
        core.setColorAt(0.85, rgba(secondary, 170))
        core.setColorAt(1.0, rgba(secondary, 60))
        p.setBrush(core)
        p.drawEllipse(c, r, r)

        # Swirl inside the core.
        swirl = QConicalGradient(c, -(t * 40 * speed) % 360)
        swirl.setColorAt(0.0, rgba(highlight, int(110 * e)))
        swirl.setColorAt(0.25, rgba(highlight, 0))
        swirl.setColorAt(0.5, rgba(highlight, int(70 * e)))
        swirl.setColorAt(0.75, rgba(highlight, 0))
        swirl.setColorAt(1.0, rgba(highlight, int(110 * e)))
        p.setBrush(swirl)
        p.drawEllipse(c, r * 0.96, r * 0.96)

        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(rgba(highlight, 140), 1.2))
        p.drawEllipse(c, r * 0.62, r * 0.62)
        p.setPen(QPen(rgba(highlight, 70), 1))
        p.drawEllipse(c, r * 0.38, r * 0.38)
        p.setPen(QPen(rgba(highlight, 200), 1.6))
        p.drawEllipse(c, r * 1.0, r * 1.0)
        p.setPen(QPen(rgba(primary, 90), 4))
        p.drawEllipse(c, r * 1.06, r * 1.06)


def paint_reactor_logo(p: QPainter, rect: QRectF) -> None:
    """Small static arc-reactor mark for the header."""
    p.save()
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    c = rect.center()
    r = min(rect.width(), rect.height()) / 2
    glow = QRadialGradient(c, r)
    glow.setColorAt(0.0, rgba(ICE, 255))
    glow.setColorAt(0.35, rgba(CYAN, 220))
    glow.setColorAt(0.75, rgba(ELECTRIC, 90))
    glow.setColorAt(1.0, rgba(ELECTRIC, 0))
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(glow)
    p.drawEllipse(c, r, r)
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.setPen(QPen(rgba(ICE, 220), 1.4))
    p.drawEllipse(c, r * 0.62, r * 0.62)
    ring = QRectF(c.x() - r * 0.9, c.y() - r * 0.9, r * 1.8, r * 1.8)
    p.setPen(QPen(rgba(CYAN, 230), 1.6, c=Qt.PenCapStyle.FlatCap))
    for i in range(3):
        p.drawArc(ring, (i * 120 + 20) * 16, 80 * 16)
    p.restore()
