"""Full-screen HUD pieces: backdrop, top bar, centre caption, reply card, status strip."""

from __future__ import annotations

import math
from datetime import datetime

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontMetricsF,
    QLinearGradient,
    QMouseEvent,
    QPainter,
    QPaintEvent,
    QPen,
    QPixmap,
    QRadialGradient,
    QResizeEvent,
)
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from jarvis.ui import theme
from jarvis.ui.theme import CYAN, DANGER, ICE, STEEL, TEXT_DIM, TEXT_MUTED, rgba
from jarvis.ui.widgets.orb import paint_reactor_logo

GRID = 30


class HudBackdrop(QWidget):
    """Charcoal screen with a fine grid, a ruler along the top, corner brackets and a
    top-centre tab. The static part is cached; only the edge glow changes over time."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._cache: QPixmap | None = None
        self.glow = 0.0  # 0..1, edge light when JARVIS is active
        self.glow_color = CYAN
        self.alarm = 0.0  # 0..1, red tint after a command failed

    def resizeEvent(self, event: QResizeEvent) -> None:  # noqa: N802
        self._cache = None
        super().resizeEvent(event)

    def _render(self) -> QPixmap:
        w, h = max(1, self.width()), max(1, self.height())
        dpr = self.devicePixelRatioF()
        pm = QPixmap(int(w * dpr), int(h * dpr))
        pm.setDevicePixelRatio(dpr)
        p = QPainter(pm)
        base = QLinearGradient(0, 0, 0, h)
        base.setColorAt(0.0, QColor(20, 24, 29))
        base.setColorAt(1.0, QColor(11, 13, 16))
        p.fillRect(0, 0, w, h, base)
        center = QRadialGradient(QPointF(w * 0.5, h * 0.45), max(w, h) * 0.5)
        center.setColorAt(0.0, QColor(30, 50, 66, 70))
        center.setColorAt(1.0, QColor(30, 50, 66, 0))
        p.fillRect(0, 0, w, h, center)
        # Grid: faint minor lines, slightly brighter every fourth.
        for i, x in enumerate(range(GRID, w, GRID)):
            p.setPen(QPen(QColor(140, 175, 200, 16 if i % 4 == 3 else 8), 1))
            p.drawLine(x, 0, x, h)
        for i, y in enumerate(range(GRID, h, GRID)):
            p.setPen(QPen(QColor(140, 175, 200, 16 if i % 4 == 3 else 8), 1))
            p.drawLine(0, y, w, y)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        # Ruler of dots along the top, as on the reference HUD.
        p.setPen(QPen(rgba(CYAN, 70), 1.6))
        for x in range(GRID, w - GRID + 1, GRID // 2):
            p.drawPoint(QPointF(x, 22))
        # Top-centre tab.
        tab_w = min(320.0, w * 0.24)
        x0, x1 = (w - tab_w) / 2, (w + tab_w) / 2
        p.setPen(QPen(rgba(CYAN, 120), 1.4))
        p.drawPolyline(
            [
                QPointF(x0 - 40, 6),
                QPointF(x0, 6),
                QPointF(x0 + 12, 14),
                QPointF(x1 - 12, 14),
                QPointF(x1, 6),
                QPointF(x1 + 40, 6),
            ]
        )
        # Corner brackets.
        p.setPen(QPen(rgba(CYAN, 110), 1.4))
        m, s = 10.0, 18.0
        for cx, cy, dx, dy in (
            (m, m, 1, 1),
            (w - m, m, -1, 1),
            (m, h - m, 1, -1),
            (w - m, h - m, -1, -1),
        ):
            p.drawLine(QPointF(cx, cy), QPointF(cx + dx * s, cy))
            p.drawLine(QPointF(cx, cy), QPointF(cx, cy + dy * s))
        vignette = QRadialGradient(QPointF(w / 2, h / 2), max(w, h) * 0.75)
        vignette.setColorAt(0.65, QColor(0, 0, 0, 0))
        vignette.setColorAt(1.0, QColor(0, 0, 0, 140))
        p.fillRect(0, 0, w, h, vignette)
        p.end()
        return pm

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802
        if self._cache is None:
            self._cache = self._render()
        p = QPainter(self)
        # Blit only the damaged area of the cached backdrop.
        area = QRectF(event.rect())
        dpr = self._cache.devicePixelRatio()
        source = QRectF(area.x() * dpr, area.y() * dpr, area.width() * dpr, area.height() * dpr)
        p.drawPixmap(area, self._cache, source)
        if self.alarm > 0.01:
            p.fillRect(area, QColor(160, 22, 40, int(62 * self.alarm)))
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect())
        for color, amount in (
            (self.glow_color, self.glow * (1 - self.alarm)),
            (DANGER, self.alarm),
        ):
            if amount <= 0.01:
                continue
            for inset, width, alpha in ((2.0, 10.0, 22), (2.0, 4.0, 70), (1.5, 1.6, 220)):
                p.setPen(QPen(rgba(color, int(alpha * amount)), width))
                p.drawRoundedRect(r.adjusted(inset, inset, -inset, -inset), 6, 6)
        p.end()


class TopBar(QWidget):
    """Logo, "JARVIS — Your personal AI assistant", and the AI status pill."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setFixedHeight(64)
        self._status_text = "CHECKING"
        self._status_color = STEEL
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)

    def set_status(self, text: str, color: QColor) -> None:
        self._status_text, self._status_color = text, color
        self.update()

    def status_rect(self) -> QRectF:
        text_w = QFontMetricsF(theme.label_font(9.5)).horizontalAdvance(self._status_text) + 34
        return QRectF(self.width() - 128 - text_w, 22, text_w, 26)

    def paintEvent(self, _event: QPaintEvent) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        h = self.height()
        paint_reactor_logo(p, QRectF(28, 20, 30, 30))
        p.setFont(theme.font(theme.BODY_FAMILY, 15, QFont.Weight.DemiBold, spacing=7))
        p.setPen(ICE)
        p.drawText(QRectF(70, 8, 220, h - 8), Qt.AlignmentFlag.AlignVCenter, "JARVIS")
        title_w = p.fontMetrics().horizontalAdvance("JARVIS") + 7 * 6
        p.setFont(theme.font(theme.BODY_FAMILY, 10.5, QFont.Weight.Normal, spacing=1.0))
        p.setPen(TEXT_DIM)
        p.drawText(
            QRectF(70 + title_w + 6, 8, 420, h - 8),
            Qt.AlignmentFlag.AlignVCenter,
            "—  Your personal AI assistant",
        )
        pill = self.status_rect()
        p.setFont(theme.label_font(9.5))
        p.setPen(QPen(rgba(self._status_color, 150), 1))
        p.setBrush(rgba(self._status_color, 22))
        p.drawRoundedRect(pill, 13, 13)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(self._status_color)
        p.drawEllipse(QPointF(pill.left() + 14, pill.center().y()), 3.2, 3.2)
        p.setPen(self._status_color if self._status_color != STEEL else TEXT_DIM)
        p.drawText(pill.adjusted(24, 0, -8, 0), Qt.AlignmentFlag.AlignVCenter, self._status_text)
        p.end()


class ProviderSwitch(QWidget):
    """AUTO · GROQ · GEMINI · OLLAMA: pick which AI answers first."""

    chosen = Signal(object)  # provider name, or None for automatic

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedHeight(26)
        self.setToolTip("Choose the AI. Auto uses the first one that works.")
        self._items: list[tuple[str | None, str, QColor]] = [(None, "AUTO", CYAN)]
        self._current: str | None = None
        self._fit()

    def set_items(self, items: list[tuple[str, str, QColor]], current: str | None) -> None:
        """``items``: (name, label, status colour) per provider."""
        self._items = [(None, "AUTO", CYAN), *items]
        self._current = current
        self._fit()
        self.update()

    def _widths(self) -> list[float]:
        fm = QFontMetricsF(theme.label_font(8.6))
        return [fm.horizontalAdvance(label) + 24 for _, label, _ in self._items]

    def _fit(self) -> None:
        self.setFixedWidth(int(sum(self._widths())) + 4)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() != Qt.MouseButton.LeftButton:
            return
        x = 2.0
        for (name, _, _), width in zip(self._items, self._widths(), strict=True):
            if x <= event.position().x() < x + width:
                if name != self._current:
                    self.chosen.emit(name)
                return
            x += width

    def paintEvent(self, _event: QPaintEvent) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        p.setPen(QPen(rgba(CYAN, 70), 1))
        p.setBrush(QColor(12, 17, 23, 200))
        p.drawRoundedRect(r, 13, 13)
        p.setFont(theme.label_font(8.6))
        x = 2.0
        for (name, label, color), width in zip(self._items, self._widths(), strict=True):
            cell = QRectF(x, 2, width, r.height() - 3)
            selected = name == self._current
            if selected:
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(rgba(CYAN, 46))
                p.drawRoundedRect(cell, 11, 11)
            if name is not None:
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(color)
                p.drawEllipse(QPointF(cell.left() + 9, cell.center().y()), 2.4, 2.4)
            p.setPen(ICE if selected else TEXT_DIM)
            text_rect = cell.adjusted(14 if name is not None else 0, 0, 0, 0)
            align = Qt.AlignmentFlag.AlignCenter
            p.drawText(text_rect.adjusted(0, 0, -4 if name is not None else 0, 0), align, label)
            x += width
        p.end()


class CenterCaption(QWidget):
    """Under the orb: a large clock when idle, otherwise the state and a hint."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self._title = ""
        self._hint = ""
        self._color = CYAN
        self._clock_mode = True
        self._fixed_now: datetime | None = None
        self.opacity = 1.0
        self._clock = QTimer(self)
        self._clock.timeout.connect(self.update)
        self._clock.start(10_000)

    def sizeHint(self) -> QSize:  # noqa: N802
        return QSize(600, 96)

    def show_clock(self, hint: str) -> None:
        self._clock_mode, self._hint = True, hint
        self.update()

    def show_state(self, title: str, hint: str, color: QColor = CYAN) -> None:
        self._clock_mode = False
        self._title, self._hint, self._color = title, hint, color
        self.update()

    def freeze_clock(self, now: datetime) -> None:
        self._fixed_now = now
        self._clock.stop()
        self.update()

    def paintEvent(self, _event: QPaintEvent) -> None:  # noqa: N802
        p = QPainter(self)
        p.setOpacity(self.opacity)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w = self.width()
        if self._clock_mode:
            now = self._fixed_now or datetime.now()
            p.setFont(theme.font(theme.BODY_FAMILY, 26, QFont.Weight.DemiBold, spacing=6))
            p.setPen(ICE)
            p.drawText(QRectF(0, 0, w, 44), Qt.AlignmentFlag.AlignCenter, now.strftime("%H:%M"))
            p.setFont(theme.font(theme.BODY_FAMILY, 8, QFont.Weight.Medium, spacing=2.6))
            p.setPen(TEXT_DIM)
            p.drawText(
                QRectF(0, 44, w, 18),
                Qt.AlignmentFlag.AlignCenter,
                now.strftime("%A %d %B").upper(),
            )
        else:
            p.setFont(theme.font(theme.BODY_FAMILY, 13, QFont.Weight.DemiBold, spacing=8))
            p.setPen(rgba(self._color, 240))
            p.drawText(QRectF(0, 6, w, 34), Qt.AlignmentFlag.AlignCenter, self._title)
        p.setFont(theme.font(theme.BODY_FAMILY, 9.2))
        p.setPen(TEXT_MUTED if self._clock_mode else TEXT_DIM)
        hint_y = 66 if self._clock_mode else 44
        p.drawText(QRectF(0, hint_y, w, 22), Qt.AlignmentFlag.AlignCenter, self._hint)
        p.end()


class ReplyCard(QWidget):
    """The latest exchange, shown large in the middle while the orb docks in the corner."""

    closed = Signal()

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(30, 22, 30, 24)
        layout.setSpacing(10)
        head = QHBoxLayout()
        self.asked = QLabel()
        self.asked.setWordWrap(True)
        self.asked.setFont(theme.font(theme.BODY_FAMILY, 10.5))
        self.asked.setStyleSheet(f"color: {theme.css(TEXT_DIM)}; background: transparent;")
        self.close_button = QPushButton("✕")
        self.close_button.setFlat(True)
        self.close_button.setFixedSize(28, 28)
        self.close_button.setFont(theme.font(theme.BODY_FAMILY, 11))
        self.close_button.setStyleSheet(
            f"QPushButton {{ color: {theme.css(TEXT_DIM)}; background: transparent;"
            " border: none; }"
            f"QPushButton:hover {{ color: {theme.css(ICE)}; }}"
        )
        self.close_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.close_button.setToolTip("Close (the orb returns to the centre)")
        self.close_button.clicked.connect(self.closed.emit)
        head.addWidget(self.asked, 1)
        head.addWidget(self.close_button, 0, Qt.AlignmentFlag.AlignTop)
        layout.addLayout(head)
        self.meta = QLabel()
        self.meta.setFont(theme.label_font(9))
        self.meta.setStyleSheet(f"color: {theme.css(CYAN)}; background: transparent;")
        layout.addWidget(self.meta)
        self.body = QLabel()
        self.body.setWordWrap(True)
        self.body.setTextFormat(Qt.TextFormat.PlainText)
        self.body.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.body.setFont(theme.font(theme.BODY_FAMILY, 13))
        self.body.setStyleSheet(f"color: {theme.css(theme.TEXT)}; background: transparent;")
        self.body.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setWidget(self.body)
        scroll.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        layout.addWidget(scroll, 1)
        self.action_list = QLabel()
        self.action_list.setWordWrap(True)
        self.action_list.setFont(theme.font(theme.BODY_FAMILY, 9.4))
        self.action_list.setStyleSheet(f"color: {theme.css(TEXT_DIM)}; background: transparent;")
        layout.addWidget(self.action_list)
        self._accent = CYAN

    def show_exchange(
        self, asked: str, meta: str, reply: str, actions: list[str], error: bool = False
    ) -> None:
        self.asked.setText(f"“{asked}”" if asked else "")
        self.meta.setText(meta)
        self.body.setText(reply)
        self.action_list.setText("\n".join(actions))
        self.action_list.setVisible(bool(actions))
        self._accent = theme.DANGER if error else CYAN
        color = theme.DANGER if error else CYAN
        self.meta.setStyleSheet(f"color: {theme.css(color)}; background: transparent;")
        self.update()

    def fit_height(self, width: int) -> int:
        """The height that shows the whole exchange at this width (the body scrolls past it)."""
        inner = width - 60
        parts = [
            max(28, self.asked.heightForWidth(inner - 28 - 6)),
            self.meta.sizeHint().height(),
            self.body.heightForWidth(inner - 4) + 6,
        ]
        if self.action_list.isVisibleTo(self):
            parts.append(self.action_list.heightForWidth(inner))
        return 22 + 24 + sum(parts) + 10 * (len(parts) - 1)

    def paintEvent(self, _event: QPaintEvent) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        fill = QLinearGradient(r.topLeft(), r.bottomLeft())
        fill.setColorAt(0.0, QColor(24, 32, 42, 225))
        fill.setColorAt(1.0, QColor(13, 17, 22, 235))
        p.setPen(QPen(rgba(self._accent, 70), 1))
        p.setBrush(fill)
        p.drawRoundedRect(r, 14, 14)
        # Bright notch at the top, echoing the backdrop tab.
        p.setPen(QPen(rgba(self._accent, 200), 2, c=Qt.PenCapStyle.RoundCap))
        p.drawLine(
            QPointF(r.center().x() - 40, r.top() + 1), QPointF(r.center().x() + 40, r.top() + 1)
        )
        theme.paint_corner_brackets(p, r.adjusted(6, 6, -6, -6), rgba(self._accent, 120), 10)
        p.end()


class StatusStrip(QWidget):
    """A row of small status dots (providers and network); click for details."""

    clicked = Signal()

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip("AI providers, network and system: click for details")
        self.setFixedHeight(26)
        self._items: list[tuple[str, QColor]] = []

    def set_items(self, items: list[tuple[str, QColor]]) -> None:
        self._items = items
        self.update()

    def sizeHint(self) -> QSize:  # noqa: N802
        return QSize(420, 26)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()

    def paintEvent(self, _event: QPaintEvent) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setFont(theme.label_font(8.6))
        fm = p.fontMetrics()
        widths = [fm.horizontalAdvance(label) + 30 for label, _ in self._items]
        x = (self.width() - sum(widths)) / 2
        y = self.height() / 2
        for (label, color), width in zip(self._items, widths, strict=True):
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(rgba(color, 60))
            p.drawEllipse(QPointF(x + 7, y), 5, 5)
            p.setBrush(color)
            p.drawEllipse(QPointF(x + 7, y), 2.6, 2.6)
            p.setPen(TEXT_DIM)
            p.drawText(
                QRectF(x + 16, 0, width - 16, self.height()), Qt.AlignmentFlag.AlignVCenter, label
            )
            x += width
        p.end()


class CornerText(QWidget):
    """Tiny HUD text in a bottom corner."""

    def __init__(self, align: Qt.AlignmentFlag, parent: QWidget | None = None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self._align = align
        self._text = ""
        self.setFixedHeight(20)

    def set_text(self, text: str) -> None:
        if text != self._text:
            self._text = text
            self.update()

    def paintEvent(self, _event: QPaintEvent) -> None:  # noqa: N802
        p = QPainter(self)
        p.setFont(theme.label_font(8.4))
        p.setPen(TEXT_MUTED)
        p.drawText(QRectF(self.rect()), self._align | Qt.AlignmentFlag.AlignVCenter, self._text)
        p.end()


class Tween:
    """A critically damped spring toward a target (see MainWindow).

    Unlike a plain exponential ease it starts from rest, so movements accelerate in and
    settle out with no jolt and no overshoot. ``rate`` is the spring's stiffness: about
    4.7 / rate seconds to settle.
    """

    def __init__(self, value: float = 0.0, rate: float = 7.0) -> None:
        self.value = value
        self.target = value
        self.rate = rate
        self.velocity = 0.0

    def step(self, dt: float) -> bool:
        """Advance by ``dt`` seconds; returns True while still moving."""
        gap = self.value - self.target
        if abs(gap) < 0.0005 and abs(self.velocity) < 0.005:
            self.value, self.velocity = self.target, 0.0
            return False
        # Exact solution of x'' = -w^2 x - 2w x', so any dt is stable.
        w = self.rate
        decay = math.exp(-w * dt)
        drift = self.velocity + w * gap
        self.value = self.target + (gap + drift * dt) * decay
        self.velocity = (self.velocity - w * drift * dt) * decay
        return True

    def settle(self) -> None:
        self.value, self.velocity = self.target, 0.0


__all__ = [
    "CenterCaption",
    "CornerText",
    "HudBackdrop",
    "ProviderSwitch",
    "ReplyCard",
    "StatusStrip",
    "TopBar",
    "Tween",
]
