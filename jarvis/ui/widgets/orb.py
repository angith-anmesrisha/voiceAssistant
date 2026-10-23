"""The animated JARVIS orb.

At rest it is a slowly turning globe of light particles. When JARVIS is listening,
thinking, speaking or acting it morphs into a glowing ring with orbiting arcs. Every
change is eased, and motion is time based, so it stays smooth at any frame rate.

Only QPainter primitives are used (no blur effects or shaders). The static HUD marks are
cached in a pixmap, the timer runs only while the widget is visible, and the frame rate
drops automatically when painting is slow, to stay light on integrated GPUs.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from enum import StrEnum

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, QTimer
from PySide6.QtGui import (
    QColor,
    QFont,
    QHideEvent,
    QPainter,
    QPaintEvent,
    QPen,
    QPixmap,
    QRadialGradient,
    QShowEvent,
)
from PySide6.QtWidgets import QSizePolicy, QWidget

from jarvis.ui import theme
from jarvis.ui.theme import CYAN, ELECTRIC, ICE, STEEL, rgba


class VisualState(StrEnum):
    IDLE = "idle"
    LISTENING = "listening"
    THINKING = "thinking"
    SPEAKING = "speaking"
    ACTING = "executing"
    OFFLINE = "offline"


# Frame rates. Motion is time based, so a lower rate only looks less fluid, never slower.
_ACTIVE_FPS = 60
_CALM_FPS = 30
_SLOW_PAINT_MS = 10.0  # if painting takes longer than this, use _CALM_FPS everywhere

_SPEEDS = {
    VisualState.IDLE: 0.6,
    VisualState.LISTENING: 1.0,
    VisualState.THINKING: 3.6,
    VisualState.SPEAKING: 1.4,
    VisualState.ACTING: 2.6,
    VisualState.OFFLINE: 0.15,
}

_PARTICLES = 240


def _sphere_points(n: int) -> list[tuple[float, float, float]]:
    """Evenly spread points on a unit sphere (Fibonacci lattice)."""
    golden = math.pi * (3 - math.sqrt(5))
    points = []
    for i in range(n):
        y = 1 - (i + 0.5) * 2 / n
        r = math.sqrt(1 - y * y)
        a = golden * i
        points.append((math.cos(a) * r, y, math.sin(a) * r))
    return points


_SPHERE = _sphere_points(_PARTICLES)


def _ease(current: float, target: float, dt: float, rate: float) -> float:
    return current + (target - current) * min(1.0, dt * rate)


class OrbHud(QWidget):
    def __init__(self, level: Callable[[], float] | None = None, parent: QWidget | None = None):
        super().__init__(parent)
        self._level_source = level
        self._state = VisualState.IDLE
        self._fixed = False
        self._started = time.perf_counter()
        self._last_tick = self._started
        self._t = 0.0
        # Eased values: state changes glide instead of jumping.
        self._level = 0.0
        self._morph = 0.0  # 0 = particle globe, 1 = glowing ring
        self._speed = _SPEEDS[VisualState.IDLE]
        self._phase = 0.0  # integrated rotation time
        self._energy = self._target_energy()
        self._paint_ms = 0.0
        self._decor: QPixmap | None = None
        self._decor_key: tuple[int, int, int] | None = None
        self._timer = QTimer(self)
        self._timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._timer.timeout.connect(self._tick)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMinimumSize(140, 140)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)

    def sizeHint(self) -> QSize:  # noqa: N802
        return QSize(420, 420)

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
        """Pin the animation to a settled moment (used for renders and tests)."""
        self._fixed = True
        self._timer.stop()
        self._t = t
        self._level = level
        self._morph = self._target_morph()
        self._speed = _SPEEDS[self._state]
        self._phase = t * self._speed
        self._energy = self._target_energy()
        self.update()

    def _restart_timer(self) -> None:
        if self._fixed or not self.isVisible():
            return
        calm = self._state in (VisualState.IDLE, VisualState.OFFLINE) and self._morph < 0.01
        fps = _CALM_FPS if calm or self._paint_ms > _SLOW_PAINT_MS else _ACTIVE_FPS
        self._last_tick = time.perf_counter()
        self._timer.start(round(1000 / fps))

    def showEvent(self, event: QShowEvent) -> None:  # noqa: N802
        super().showEvent(event)
        self._restart_timer()

    def hideEvent(self, event: QHideEvent) -> None:  # noqa: N802
        super().hideEvent(event)
        self._timer.stop()

    def _target_morph(self) -> float:
        return 0.0 if self._state in (VisualState.IDLE, VisualState.OFFLINE) else 1.0

    def _target_energy(self) -> float:
        t, s = self._t, self._state
        if s is VisualState.OFFLINE:
            return 0.15
        if s is VisualState.LISTENING:
            return 0.55 + 0.45 * self._level
        if s is VisualState.THINKING:
            return 0.7 + 0.1 * math.sin(t * 4.2)
        if s is VisualState.SPEAKING:
            return 0.6 + 0.35 * abs(math.sin(t * 6.1) * math.sin(t * 1.7 + 1))
        if s is VisualState.ACTING:
            return 0.8
        return 0.45 + 0.05 * math.sin(t * 1.2)

    def _tick(self) -> None:
        now = time.perf_counter()
        dt = min(0.1, now - self._last_tick)
        self._last_tick = now
        self._t = now - self._started
        raw = self._level_source() if self._level_source else 0.0
        # Fast attack, slower release: follows the voice without flicker.
        self._level = _ease(self._level, raw, dt, 16.0 if raw > self._level else 6.0)
        was_calm = self._morph < 0.01
        self._morph = _ease(self._morph, self._target_morph(), dt, 4.0)
        self._speed = _ease(self._speed, _SPEEDS[self._state], dt, 2.5)
        self._phase += self._speed * dt
        self._energy = _ease(self._energy, self._target_energy(), dt, 7.0)
        if was_calm != (self._morph < 0.01):
            self._restart_timer()
        self.update()

    # --- painting ---

    def _colors(self) -> tuple[QColor, QColor, QColor]:
        if self._state is VisualState.OFFLINE:
            return STEEL, QColor(70, 86, 104), QColor(170, 186, 202)
        if self._state is VisualState.ACTING:
            return ELECTRIC, theme.DEEP_BLUE, ICE
        return CYAN, ELECTRIC, ICE

    def paintEvent(self, _event: QPaintEvent) -> None:  # noqa: N802
        started = time.perf_counter()
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        c = QPointF(w / 2, h / 2)
        outer = min(w, h) / 2 * 0.96
        ring = outer * 0.5
        primary, secondary, highlight = self._colors()
        morph, energy = self._morph, self._energy

        self._paint_halo(p, c, ring, outer, primary, energy, morph)
        self._paint_decor(p, c, outer, primary, highlight, 0.3 + 0.7 * morph)
        if morph < 0.99:
            self._paint_globe(p, c, ring * 0.92, primary, highlight, 1.0 - morph)
        if morph > 0.01:
            pulse = 1.0 + 0.035 * (energy - 0.5) + 0.05 * self._level
            self._paint_ring(p, c, ring * pulse, primary, highlight, energy, morph)
            self._paint_arcs(p, c, ring, primary, secondary, highlight, morph)
            if self._state is VisualState.SPEAKING:
                self._paint_ripples(p, c, ring, outer, primary, morph)
        self._paint_name(p, c, ring, highlight, morph)
        p.end()

        cost = (time.perf_counter() - started) * 1000
        was_slow = self._paint_ms > _SLOW_PAINT_MS
        self._paint_ms = 0.9 * self._paint_ms + 0.1 * cost
        if (self._paint_ms > _SLOW_PAINT_MS) != was_slow and self._timer.isActive():
            self._restart_timer()

    def _paint_halo(
        self,
        p: QPainter,
        c: QPointF,
        ring: float,
        outer: float,
        color: QColor,
        energy: float,
        morph: float,
    ) -> None:
        reach = outer * 0.95
        strength = (0.35 + 0.65 * morph) * energy
        glow = QRadialGradient(c, reach)
        inner = ring / reach
        glow.setColorAt(0.0, rgba(color, int(26 * strength)))
        glow.setColorAt(max(0.0, inner - 0.12), rgba(color, int(40 * strength)))
        glow.setColorAt(inner, rgba(color, int(150 * strength * morph + 50 * strength)))
        glow.setColorAt(min(1.0, inner + 0.16), rgba(color, int(36 * strength)))
        glow.setColorAt(1.0, rgba(color, 0))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(glow)
        p.drawEllipse(c, reach, reach)

    def _paint_decor(
        self, p: QPainter, c: QPointF, outer: float, color: QColor, hi: QColor, opacity: float
    ) -> None:
        """Dotted ring, compass ticks and diagonal markers: static, so cached."""
        size = int(outer * 2) + 4
        key = (size, color.rgb(), int(self.devicePixelRatioF() * 100))
        if self._decor is None or self._decor_key != key:
            self._decor = _render_decor(size, color, hi, self.devicePixelRatioF())
            self._decor_key = key
        p.save()
        p.setOpacity(opacity)
        p.drawPixmap(QPointF(c.x() - size / 2, c.y() - size / 2), self._decor)
        p.restore()

    def _paint_globe(
        self, p: QPainter, c: QPointF, r: float, color: QColor, hi: QColor, alpha: float
    ) -> None:
        yaw = self._phase * 0.35
        tilt = 0.38
        cy, sy = math.cos(yaw), math.sin(yaw)
        ct, st = math.cos(tilt), math.sin(tilt)
        buckets: list[list[QPointF]] = [[], [], []]
        for x0, y0, z0 in _SPHERE:
            x, z = x0 * cy + z0 * sy, -x0 * sy + z0 * cy
            y, z = y0 * ct - z * st, y0 * st + z * ct
            depth = (z + 1) / 2  # 0 = back, 1 = front
            buckets[min(2, int(depth * 3))].append(QPointF(c.x() + x * r, c.y() + y * r))
        # Soft inner haze so the particles float in light, not on black.
        haze = QRadialGradient(c, r * 1.1)
        haze.setColorAt(0.0, rgba(color, int(46 * alpha)))
        haze.setColorAt(0.6, rgba(color, int(18 * alpha)))
        haze.setColorAt(1.0, rgba(color, 0))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(haze)
        p.drawEllipse(c, r * 1.1, r * 1.1)
        p.setBrush(Qt.BrushStyle.NoBrush)
        for i, points in enumerate(buckets):
            tone = hi if i == 2 else color
            size = 1.4 + 1.0 * i
            # A wide faint dot under a small bright one reads as a glowing particle.
            glow = QPen(rgba(color, int((18 + 22 * i) * alpha)), size * 2.6)
            glow.setCapStyle(Qt.PenCapStyle.RoundCap)
            p.setPen(glow)
            p.drawPoints(points)
            core = QPen(rgba(tone, int((70 + 80 * i) * alpha)), size)
            core.setCapStyle(Qt.PenCapStyle.RoundCap)
            p.setPen(core)
            p.drawPoints(points)
        # Soft limb so the globe reads as a sphere.
        p.setPen(QPen(rgba(color, int(70 * alpha)), 1.2))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawEllipse(c, r * 1.04, r * 1.04)

    def _paint_ring(
        self,
        p: QPainter,
        c: QPointF,
        r: float,
        color: QColor,
        hi: QColor,
        energy: float,
        morph: float,
    ) -> None:
        a = morph
        # Dark lens inside the ring.
        lens = QRadialGradient(c, r)
        lens.setColorAt(0.0, QColor(10, 16, 24, int(235 * a)))
        lens.setColorAt(0.85, QColor(6, 10, 16, int(245 * a)))
        lens.setColorAt(1.0, rgba(color, int(60 * a)))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(lens)
        p.drawEllipse(c, r, r)
        p.setBrush(Qt.BrushStyle.NoBrush)
        width = r * 0.075
        # Layered strokes fake a bloom: wide and faint, then narrow and bright.
        p.setPen(QPen(rgba(color, int(45 * a * energy)), width * 2.6))
        p.drawEllipse(c, r, r)
        p.setPen(QPen(rgba(color, int(150 * a * (0.5 + 0.5 * energy))), width * 1.3))
        p.drawEllipse(c, r, r)
        p.setPen(QPen(rgba(hi, int(235 * a)), max(1.2, width * 0.35)))
        p.drawEllipse(c, r, r)
        p.setPen(QPen(rgba(hi, int(120 * a)), 1.0))
        p.drawEllipse(c, r * 0.83, r * 0.83)

    def _paint_arcs(
        self,
        p: QPainter,
        c: QPointF,
        r: float,
        color: QColor,
        second: QColor,
        hi: QColor,
        morph: float,
    ) -> None:
        spin = self._phase
        a = morph
        cap = Qt.PenCapStyle.RoundCap
        arcs: list[tuple[float, float, float, QColor, float, float]] = [
            # radius factor, start angle, span, colour, alpha, width
            (1.2, -spin * 55 + 200, 48, hi, 230, 2.2),
            (1.2, -spin * 55 + 20, 14, color, 160, 2.2),
            (1.32, spin * 32 + 90, 30, color, 140, 1.4),
        ]
        if self._state in (VisualState.THINKING, VisualState.ACTING):
            arcs += [
                (1.42, -spin * 80 + 300, 70, second, 150, 1.6),
                (0.72, spin * 120, 90, hi, 120, 1.4),
            ]
        if self._state is VisualState.LISTENING:
            span = 20 + 120 * self._level
            arcs.append((1.42, 270 - span / 2, span, hi, 200, 2.0))
            arcs.append((1.42, 90 - span / 2, span, hi, 200, 2.0))
        for factor, start, span, tone, alpha, width in arcs:
            rr = r * factor
            rect = QRectF(c.x() - rr, c.y() - rr, rr * 2, rr * 2)
            p.setPen(QPen(rgba(tone, int(alpha * a)), width, c=cap))
            p.drawArc(rect, int((start % 360) * 16), int(span * 16))

    def _paint_ripples(
        self, p: QPainter, c: QPointF, r: float, outer: float, color: QColor, morph: float
    ) -> None:
        p.setBrush(Qt.BrushStyle.NoBrush)
        for k in range(3):
            phase = (self._t * 0.7 + k / 3) % 1.0
            rr = r + (outer * 0.9 - r) * phase
            alpha = int(110 * (1 - phase) * morph * self._energy)
            p.setPen(QPen(rgba(color, alpha), 1.4))
            p.drawEllipse(c, rr, rr)

    def _paint_name(self, p: QPainter, c: QPointF, r: float, hi: QColor, morph: float) -> None:
        size = max(7.0, r * 0.13)
        f = theme.font(theme.BODY_FAMILY, size, QFont.Weight.Medium, spacing=size * 0.32)
        p.setFont(f)
        p.setPen(rgba(hi, int(200 + 40 * morph)))
        box = QRectF(c.x() - r, c.y() - size, r * 2, size * 2)
        p.drawText(box, Qt.AlignmentFlag.AlignCenter, "JARVIS")


def _render_decor(size: int, color: QColor, hi: QColor, dpr: float) -> QPixmap:
    pm = QPixmap(int(size * dpr), int(size * dpr))
    pm.setDevicePixelRatio(dpr)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    c = QPointF(size / 2, size / 2)
    outer = (size - 4) / 2
    # Dotted ring.
    dots = 72
    for i in range(dots):
        angle = 2 * math.pi * i / dots
        rr = outer * 0.86
        bright = i % 6 == 0
        p.setPen(QPen(rgba(hi if bright else color, 200 if bright else 90), 2.0 if bright else 1.4))
        p.drawPoint(QPointF(c.x() + math.cos(angle) * rr, c.y() + math.sin(angle) * rr))
    # Compass ticks.
    p.setPen(QPen(rgba(color, 150), 1.4))
    for i in range(4):
        angle = math.pi / 2 * i
        dx, dy = math.cos(angle), math.sin(angle)
        p.drawLine(
            QPointF(c.x() + dx * outer * 0.92, c.y() + dy * outer * 0.92),
            QPointF(c.x() + dx * outer * 0.99, c.y() + dy * outer * 0.99),
        )
    # Diagonal "=" markers.
    p.setPen(QPen(rgba(hi, 170), 1.4))
    for i in range(4):
        angle = math.pi / 4 + math.pi / 2 * i
        dx, dy = math.cos(angle), math.sin(angle)
        px, py = -dy, dx  # perpendicular
        for offset in (-2.5, 2.5):
            mid = QPointF(c.x() + dx * (outer * 0.8 + offset), c.y() + dy * (outer * 0.8 + offset))
            p.drawLine(
                QPointF(mid.x() - px * 5, mid.y() - py * 5),
                QPointF(mid.x() + px * 5, mid.y() + py * 5),
            )
    # Faint inner reference circle.
    p.setPen(QPen(rgba(color, 40), 1.0))
    p.drawEllipse(c, outer * 0.7, outer * 0.7)
    p.end()
    return pm


def paint_reactor_logo(p: QPainter, rect: QRectF) -> None:
    """Small static ring mark (header, tray and window icon)."""
    p.save()
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    c = rect.center()
    r = min(rect.width(), rect.height()) / 2
    glow = QRadialGradient(c, r)
    glow.setColorAt(0.0, QColor(8, 14, 22, 255))
    glow.setColorAt(0.55, QColor(8, 14, 22, 255))
    glow.setColorAt(0.68, rgba(CYAN, 230))
    glow.setColorAt(0.8, rgba(ELECTRIC, 110))
    glow.setColorAt(1.0, rgba(ELECTRIC, 0))
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(glow)
    p.drawEllipse(c, r, r)
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.setPen(QPen(rgba(ICE, 235), max(1.0, r * 0.07)))
    p.drawEllipse(c, r * 0.64, r * 0.64)
    ring = QRectF(c.x() - r * 0.86, c.y() - r * 0.86, r * 1.72, r * 1.72)
    p.setPen(QPen(rgba(ICE, 200), max(1.0, r * 0.06), c=Qt.PenCapStyle.RoundCap))
    p.drawArc(ring, 200 * 16, 60 * 16)
    p.restore()
