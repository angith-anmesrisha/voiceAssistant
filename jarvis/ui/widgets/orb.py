"""The animated JARVIS orb.

At rest it is a slowly turning globe of light particles. When JARVIS is listening,
thinking, speaking or acting it morphs into a glowing ring with orbiting arcs. Every
change is eased, and motion is time based, so it stays smooth at any frame rate.

Only QPainter primitives are used (no blur effects or shaders). Everything that doesn't
move on its own (glow, ring, HUD marks, particle sprites, the name) is drawn once into
cached pixmaps at a few fixed sizes and then just blitted, scaled and faded, so a frame
costs a few milliseconds even while the orb glides between sizes. Frames come from the
shared display-synced clock and stop while the widget is hidden.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from enum import StrEnum

from PySide6.QtCore import QPointF, QRectF, QSize, Qt
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontMetricsF,
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
from jarvis.ui.frame_clock import FrameClock
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


_ALARM = (theme.DANGER, QColor(150, 40, 54), QColor(255, 200, 206))


def _mix(a: QColor, b: QColor, t: float) -> QColor:
    if t <= 0.0:
        return a
    if t >= 1.0:
        return b
    return QColor(
        round(a.red() + (b.red() - a.red()) * t),
        round(a.green() + (b.green() - a.green()) * t),
        round(a.blue() + (b.blue() - a.blue()) * t),
    )


def _ease(current: float, target: float, dt: float, rate: float) -> float:
    return current + (target - current) * (1.0 - math.exp(-dt * rate))


# Cached layers are drawn at the smallest of these sizes that is at least the widget's
# size, then scaled down a little, so gliding between sizes reuses a handful of pixmaps.
_BUCKETS = tuple(round(96 * 1.25**k) for k in range(12))


def _bucket(size: float) -> int:
    for bucket in _BUCKETS:
        if bucket >= size:
            return bucket
    return _BUCKETS[-1]


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
        self._layers: dict[tuple[object, ...], QPixmap] = {}
        self._last_size = 0
        self._alarm_on = False
        self._alarm = 0.0  # 0 = normal blue, 1 = red (a request failed)
        self._sprite_sets: dict[tuple[int, ...], list[QPixmap]] = {}
        self._clock_token: int | None = None
        self._fps = 0
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMinimumSize(96, 96)
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
            self._schedule()
            self.update()

    def set_alarm(self, on: bool) -> None:
        """Turn red (a command failed) or back to blue; the colour change is eased."""
        self._alarm_on = on
        if self._fixed:
            self._alarm = 1.0 if on else 0.0
        self.update()

    def freeze(self, t: float, level: float = 0.0) -> None:
        """Pin the animation to a settled moment (used for renders and tests)."""
        self._fixed = True
        self._stop_clock()
        self._t = t
        self._level = level
        self._morph = self._target_morph()
        self._speed = _SPEEDS[self._state]
        self._phase = t * self._speed
        self._energy = self._target_energy()
        self.update()

    def _schedule(self) -> None:
        """Subscribe to the frame clock at the rate this state needs."""
        if self._fixed or not self.isVisible():
            self._stop_clock()
            return
        calm = self._state in (VisualState.IDLE, VisualState.OFFLINE) and self._morph < 0.01
        fps = _CALM_FPS if calm or self._paint_ms > _SLOW_PAINT_MS else _ACTIVE_FPS
        clock = FrameClock.shared()
        if self._clock_token is None:
            self._last_tick = time.perf_counter()
            self._clock_token = clock.subscribe(self._tick, fps)
        elif fps != self._fps:
            clock.set_rate(self._clock_token, fps)
        self._fps = fps

    def _stop_clock(self) -> None:
        if self._clock_token is not None:
            FrameClock.shared().unsubscribe(self._clock_token)
            self._clock_token = None

    def showEvent(self, event: QShowEvent) -> None:  # noqa: N802
        super().showEvent(event)
        self._schedule()

    def hideEvent(self, event: QHideEvent) -> None:  # noqa: N802
        super().hideEvent(event)
        self._stop_clock()

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

    def _tick(self, now: float) -> None:
        dt = min(0.1, now - self._last_tick)
        self._last_tick = now
        self._t = now - self._started
        raw = self._level_source() if self._level_source else 0.0
        # Fast attack, slower release: follows the voice without flicker.
        self._level = _ease(self._level, raw, dt, 14.0 if raw > self._level else 5.0)
        was_calm = self._morph < 0.01
        target = self._target_morph()
        # Smoothstep-shaped morph: eases out of the old look and into the new one.
        step = dt * 2.2
        self._morph = (
            min(target, self._morph + step)
            if target > self._morph
            else max(target, self._morph - step)
        )
        self._speed = _ease(self._speed, _SPEEDS[self._state], dt, 2.0)
        self._phase += self._speed * dt
        self._energy = _ease(self._energy, self._target_energy(), dt, 6.0)
        alarm_target = 1.0 if self._alarm_on else 0.0
        self._alarm = _ease(self._alarm, alarm_target, dt, 5.0)
        if abs(self._alarm - alarm_target) < 0.002:
            self._alarm = alarm_target
        if was_calm != (self._morph < 0.01):
            self._schedule()
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
        base = self._colors()
        alarm = self._alarm * self._alarm * (3 - 2 * self._alarm)
        # Live strokes blend toward red; cached layers cross-fade between both palettes.
        step = round(alarm * 10) / 10
        primary, secondary, highlight = (
            _mix(b, r, step) for b, r in zip(base, _ALARM, strict=True)
        )
        energy = self._energy
        morph = self._morph * self._morph * (3 - 2 * self._morph)  # smoothstep
        size = round(outer * 2)
        # While the orb glides between sizes, reuse layers drawn at a few fixed sizes and
        # scale them; once it settles, draw them at exactly this size (a plain copy).
        moving = size != self._last_size
        self._last_size = size

        def layer(kind: str, opacity: float) -> None:
            if alarm < 0.999:
                self._draw_layer(
                    p, kind, c, size, (base[0], base[2]), opacity * (1 - alarm), moving
                )
            if alarm > 0.001:
                self._draw_layer(p, kind, c, size, (_ALARM[0], _ALARM[2]), opacity * alarm, moving)

        # Glow: a soft base, plus the bright band of the ring as it forms.
        strength = (0.35 + 0.65 * morph) * energy
        layer("halo", strength)
        layer("band", strength * morph)
        layer("decor", 0.3 + 0.7 * morph)
        if morph < 0.99:
            self._paint_globe(p, c, ring * 0.92, primary, highlight, 1.0 - morph)
        if morph > 0.01:
            layer("bloom", morph * energy)
            layer("ring", morph)
            # A thin bright line that swells with the voice.
            swell = ring * (1.0 + 0.03 * (energy - 0.5) + 0.06 * self._level)
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.setPen(QPen(rgba(highlight, int(150 * morph * energy)), 1.0))
            p.drawEllipse(c, swell, swell)
            self._paint_arcs(p, c, ring, primary, secondary, highlight, morph)
            if self._state is VisualState.SPEAKING:
                self._paint_ripples(p, c, ring, outer, primary, morph)
        self._paint_name(p, c, ring, highlight, morph)
        p.end()

        cost = (time.perf_counter() - started) * 1000
        was_slow = self._paint_ms > _SLOW_PAINT_MS
        self._paint_ms = 0.9 * self._paint_ms + 0.1 * cost
        if (self._paint_ms > _SLOW_PAINT_MS) != was_slow:
            self._schedule()

    def _draw_layer(
        self,
        p: QPainter,
        kind: str,
        c: QPointF,
        size: int,
        colors: tuple[QColor, QColor],
        opacity: float,
        moving: bool,
    ) -> None:
        if opacity <= 0.004:
            return
        dpr = self.devicePixelRatioF()
        drawn_at = _bucket(size) if moving else size
        key = (kind, drawn_at, colors[0].rgb(), colors[1].rgb(), round(dpr * 100))
        pm = self._layers.get(key)
        if pm is None:
            if len(self._layers) > 60:
                self._layers.clear()
            pm = _render_layer(kind, drawn_at, colors[0], colors[1], dpr)
            self._layers[key] = pm
        p.setOpacity(min(1.0, opacity))
        if moving:
            soft = kind in ("halo", "band", "bloom")  # blurry anyway: the fast scaler will do
            p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, not soft)
            half = size / 2
            target = QRectF(c.x() - half, c.y() - half, size, size)
            p.drawPixmap(target, pm, QRectF(pm.rect()))
        else:
            p.drawPixmap(QPointF(round(c.x() - size / 2), round(c.y() - size / 2)), pm)
        p.setOpacity(1.0)

    def _sprites(self, color: QColor, hi: QColor, scale: float) -> list[QPixmap]:
        dpr = self.devicePixelRatioF()
        key = (round(scale * 10), color.rgb(), hi.rgb(), round(dpr * 100))
        sprites = self._sprite_sets.get(key)
        if sprites is None:
            strip = _render_sprites(color, hi, round(scale * 10) / 10, dpr)
            cell = strip.height()
            sprites = [strip.copy(i * cell, 0, cell, cell) for i in range(3)]
            for sprite in sprites:
                sprite.setDevicePixelRatio(dpr)
            if len(self._sprite_sets) > 24:
                self._sprite_sets.clear()
            self._sprite_sets[key] = sprites
        return sprites

    def _paint_globe(
        self, p: QPainter, c: QPointF, r: float, color: QColor, hi: QColor, alpha: float
    ) -> None:
        # Soft inner haze so the particles float in light, not on black.
        haze = QRadialGradient(c, r * 1.1)
        haze.setColorAt(0.0, rgba(color, int(46 * alpha)))
        haze.setColorAt(0.6, rgba(color, int(18 * alpha)))
        haze.setColorAt(1.0, rgba(color, 0))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(haze)
        p.drawEllipse(c, r * 1.1, r * 1.1)
        p.setBrush(Qt.BrushStyle.NoBrush)

        sprites = self._sprites(color, hi, max(0.6, min(1.4, r / 115)))
        half = sprites[0].width() / sprites[0].devicePixelRatioF() / 2

        yaw = self._phase * 0.35
        tilt = 0.38
        cy, sy = math.cos(yaw), math.sin(yaw)
        ct, st = math.cos(tilt), math.sin(tilt)
        cx0, cy0 = c.x() - half, c.y() - half
        p.setOpacity(alpha)
        for x0, y0, z0 in _SPHERE:
            x, z = x0 * cy + z0 * sy, -x0 * sy + z0 * cy
            y, z = y0 * ct - z * st, y0 * st + z * ct
            depth = min(2, int((z + 1) * 1.5))  # 0 = back, 2 = front
            p.drawPixmap(QPointF(cx0 + x * r, cy0 + y * r), sprites[depth])
        p.setOpacity(1.0)
        # Soft limb so the globe reads as a sphere.
        p.setPen(QPen(rgba(color, int(70 * alpha)), 1.0))
        p.drawEllipse(c, r * 1.04, r * 1.04)

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
            p.drawArc(rect, round((start % 360) * 16), round(span * 16))

    def _paint_ripples(
        self, p: QPainter, c: QPointF, r: float, outer: float, color: QColor, morph: float
    ) -> None:
        p.setBrush(Qt.BrushStyle.NoBrush)
        for k in range(3):
            phase = (self._t * 0.7 + k / 3) % 1.0
            rr = r + (outer * 0.9 - r) * phase
            alpha = int(110 * (1 - phase) * morph * self._energy)
            # 1 px lines take Qt's fast antialiased path; a second, fainter one adds body.
            p.setPen(QPen(rgba(color, alpha), 1.0))
            p.drawEllipse(c, rr, rr)
            p.setPen(QPen(rgba(color, alpha // 2), 1.0))
            p.drawEllipse(c, rr + 1.0, rr + 1.0)

    def _paint_name(self, p: QPainter, c: QPointF, r: float, hi: QColor, morph: float) -> None:
        size = max(7.0, r * 0.13)
        dpr = self.devicePixelRatioF()
        key = ("name", round(size * 2), hi.rgb(), round(dpr * 100))
        pm = self._layers.get(key)
        if pm is None:
            pm = _render_name(round(size * 2) / 2, hi, dpr)
            self._layers[key] = pm
        w, h = pm.width() / dpr, pm.height() / dpr
        p.setOpacity((200 + 40 * morph) / 255)
        p.drawPixmap(QPointF(round(c.x() - w / 2), round(c.y() - h / 2)), pm)
        p.setOpacity(1.0)


def _new_pixmap(width: int, height: int, dpr: float) -> QPixmap:
    pm = QPixmap(max(1, round(width * dpr)), max(1, round(height * dpr)))
    pm.setDevicePixelRatio(dpr)
    pm.fill(Qt.GlobalColor.transparent)
    return pm


def _render_layer(kind: str, size: int, color: QColor, hi: QColor, dpr: float) -> QPixmap:
    """One cached layer, drawn for an orb whose outer radius is ``size / 2``."""
    if kind == "decor":
        return _render_decor(size, color, hi, dpr)
    pm = _new_pixmap(size, size, dpr)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    c = QPointF(size / 2, size / 2)
    outer = size / 2
    ring = outer * 0.5
    if kind in ("halo", "band"):
        reach = outer * 0.95
        inner = ring / reach
        glow = QRadialGradient(c, reach)
        if kind == "halo":
            stops = ((0.0, 26), (inner - 0.12, 40), (inner, 50), (inner + 0.16, 36), (1.0, 0))
        else:
            stops = ((0.0, 0), (inner - 0.12, 0), (inner, 150), (inner + 0.16, 0), (1.0, 0))
        for at, alpha in stops:
            glow.setColorAt(max(0.0, min(1.0, at)), rgba(color, alpha))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(glow)
        p.drawEllipse(c, reach, reach)
    elif kind == "bloom":
        # Wide, faint strokes: the part of the glow that breathes with the voice.
        width = ring * 0.075
        p.setPen(QPen(rgba(color, 45), width * 2.6))
        p.drawEllipse(c, ring, ring)
        p.setPen(QPen(rgba(color, 75), width * 1.3))
        p.drawEllipse(c, ring, ring)
    elif kind == "ring":
        lens = QRadialGradient(c, ring)
        lens.setColorAt(0.0, QColor(10, 16, 24, 235))
        lens.setColorAt(0.85, QColor(6, 10, 16, 245))
        lens.setColorAt(1.0, rgba(color, 60))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(lens)
        p.drawEllipse(c, ring, ring)
        p.setBrush(Qt.BrushStyle.NoBrush)
        width = ring * 0.075
        p.setPen(QPen(rgba(color, 75), width * 1.3))
        p.drawEllipse(c, ring, ring)
        p.setPen(QPen(rgba(hi, 235), max(1.2, width * 0.35)))
        p.drawEllipse(c, ring, ring)
        p.setPen(QPen(rgba(hi, 120), 1.0))
        p.drawEllipse(c, ring * 0.83, ring * 0.83)
    p.end()
    return pm


def _render_sprites(color: QColor, hi: QColor, scale: float, dpr: float) -> QPixmap:
    """Three glowing particle sprites side by side: back, middle and front of the globe."""
    cell = math.ceil(3.4 * 2.6 * scale) + 4
    pm = _new_pixmap(cell * 3, cell, dpr)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setPen(Qt.PenStyle.NoPen)
    for i in range(3):
        c = QPointF(cell * i + cell / 2, cell / 2)
        size = (1.4 + 1.0 * i) * scale
        p.setBrush(rgba(color, 18 + 22 * i))
        p.drawEllipse(c, size * 1.3, size * 1.3)
        p.setBrush(rgba(hi if i == 2 else color, 70 + 80 * i))
        p.drawEllipse(c, size / 2, size / 2)
    p.end()
    return pm


def _render_name(size: float, hi: QColor, dpr: float) -> QPixmap:
    f = theme.font(theme.BODY_FAMILY, size, QFont.Weight.Medium, spacing=size * 0.32)
    metrics = QFontMetricsF(f)
    w = math.ceil(metrics.horizontalAdvance("JARVIS") + size) + 4
    h = math.ceil(metrics.height()) + 4
    pm = _new_pixmap(w, h, dpr)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setRenderHint(QPainter.RenderHint.TextAntialiasing)
    p.setFont(f)
    p.setPen(hi)
    p.drawText(QRectF(0, 0, w, h), Qt.AlignmentFlag.AlignCenter, "JARVIS")
    p.end()
    return pm


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
