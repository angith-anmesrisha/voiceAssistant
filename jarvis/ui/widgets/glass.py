"""Glass panel container and small painted HUD widgets (gauges, dots, buttons)."""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, Signal
from PySide6.QtGui import (
    QColor,
    QEnterEvent,
    QFont,
    QMouseEvent,
    QPainter,
    QPaintEvent,
    QPen,
    QResizeEvent,
)
from PySide6.QtWidgets import QLabel, QSizePolicy, QVBoxLayout, QWidget

from jarvis.ui import theme
from jarvis.ui.theme import CYAN, ICE, TEXT_DIM, TEXT_MUTED, rgba


class GlassPanel(QWidget):
    """Rounded translucent panel with an optional small-caps title."""

    def __init__(self, title: str = "", parent: QWidget | None = None, accent: QColor = CYAN):
        super().__init__(parent)
        self._accent = accent
        self.body = QVBoxLayout(self)
        self.body.setContentsMargins(18, 14 if not title else 40, 18, 16)
        self.body.setSpacing(10)
        self._title = title
        self._corner: QWidget | None = None

    def set_corner_button(self, button: QWidget) -> None:
        """Pin a small button over the title bar's right end."""
        button.setParent(self)
        self._corner = button
        self._place_corner()
        button.show()

    def _place_corner(self) -> None:
        if self._corner is not None:
            self._corner.move(self.width() - self._corner.width() - 16, 6)

    def resizeEvent(self, event: QResizeEvent) -> None:  # noqa: N802
        self._place_corner()
        super().resizeEvent(event)

    def paintEvent(self, _event: QPaintEvent) -> None:  # noqa: N802
        p = QPainter(self)
        rect = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        theme.paint_glass(p, rect, 16, self._accent)
        if self._title:
            p.setRenderHint(QPainter.RenderHint.Antialiasing)
            p.setPen(rgba(self._accent, 230))
            p.setFont(theme.label_font(10.5))
            p.drawText(
                QRectF(18, 12, rect.width() - 36, 18), Qt.AlignmentFlag.AlignVCenter, self._title
            )
            p.setPen(QPen(rgba(self._accent, 50), 1))
            p.drawLine(QPointF(18, 33), QPointF(rect.width() - 18, 33))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(rgba(self._accent, 220))
            p.drawRect(QRectF(rect.width() - 30, 18, 10, 3))
        p.end()


class RingGauge(QWidget):
    """Circular percentage gauge with a label underneath."""

    def __init__(self, label: str, parent: QWidget | None = None):
        super().__init__(parent)
        self._label = label
        self._value: float | None = None
        self._caption = "—"
        self._higher_is_better = False
        self.setFixedSize(QSize(78, 92))

    def set_value(
        self,
        value: float | None,
        caption: str | None = None,
        *,
        higher_is_better: bool = False,
        label: str | None = None,
    ) -> None:
        self._value = value
        if label is not None:
            self._label = label
        self._higher_is_better = higher_is_better
        self._caption = (
            caption if caption is not None else (f"{value:.0f}%" if value is not None else "—")
        )
        self.update()

    def paintEvent(self, _event: QPaintEvent) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        size = 64.0
        rect = QRectF((self.width() - size) / 2, 4, size, size)
        p.setPen(QPen(rgba(CYAN, 35), 5, c=Qt.PenCapStyle.FlatCap))
        p.drawArc(rect.adjusted(3, 3, -3, -3), 0, 360 * 16)
        if self._value is not None:
            load = 100 - self._value if self._higher_is_better else self._value
            color = theme.DANGER if load >= 90 else (theme.AMBER if load >= 75 else CYAN)
            p.setPen(QPen(rgba(color, 230), 5, c=Qt.PenCapStyle.RoundCap))
            p.drawArc(rect.adjusted(3, 3, -3, -3), 90 * 16, int(-self._value / 100 * 360 * 16))
        p.setPen(QPen(rgba(CYAN, 60), 1))
        p.drawEllipse(rect.adjusted(11, 11, -11, -11))
        p.setPen(theme.TEXT)
        p.setFont(theme.font(theme.LABEL_FAMILY, 11.5, QFont.Weight.Bold))
        p.drawText(rect, Qt.AlignmentFlag.AlignCenter, self._caption)
        p.setPen(TEXT_DIM)
        p.setFont(theme.label_font(8.5, QFont.Weight.DemiBold))
        p.drawText(QRectF(0, 72, self.width(), 18), Qt.AlignmentFlag.AlignCenter, self._label)
        p.end()


class StatusDot(QWidget):
    def __init__(self, color: QColor = TEXT_MUTED, parent: QWidget | None = None):
        super().__init__(parent)
        self._color = color
        self.setFixedSize(14, 14)

    def set_color(self, color: QColor) -> None:
        self._color = color
        self.update()

    def paintEvent(self, _event: QPaintEvent) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        c = QPointF(7, 7)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(rgba(self._color, 60))
        p.drawEllipse(c, 6.5, 6.5)
        p.setBrush(self._color)
        p.drawEllipse(c, 3.5, 3.5)
        p.end()


def hud_label(text: str, size: float = 9.5, color: QColor = TEXT_DIM) -> QLabel:
    label = QLabel(text)
    label.setFont(theme.label_font(size))
    label.setStyleSheet(f"color: {theme.css(color)}; background: transparent;")
    return label


def body_label(text: str = "", size: float = 10.0, color: QColor = theme.TEXT) -> QLabel:
    label = QLabel(text)
    label.setFont(theme.font(theme.BODY_FAMILY, size))
    label.setStyleSheet(f"color: {theme.css(color)}; background: transparent;")
    return label


IconPainter = Callable[[QPainter, QRectF, QColor], None]


class HudButton(QWidget):
    """Painted round/pill button with an icon, glow on hover/active."""

    clicked = Signal()

    def __init__(
        self,
        icon: IconPainter,
        tooltip: str,
        diameter: int = 48,
        accent: QColor = CYAN,
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self._icon = icon
        self._accent = accent
        self._hover = False
        self._active = False
        self._pulse = 0.0
        self.setToolTip(tooltip)
        self.setFixedSize(diameter, diameter)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)

    def set_active(self, active: bool, pulse: float = 0.0) -> None:
        if active != self._active or abs(pulse - self._pulse) > 0.02:
            self._active = active
            self._pulse = pulse
            self.update()

    def enterEvent(self, event: QEnterEvent) -> None:  # noqa: N802
        self._hover = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event: object) -> None:  # noqa: N802
        self._hover = False
        self.update()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton and self.rect().contains(
            event.position().toPoint()
        ):
            self.clicked.emit()

    def paintEvent(self, _event: QPaintEvent) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(self.rect()).adjusted(2, 2, -2, -2)
        c = rect.center()
        r = rect.width() / 2
        if self._active:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(rgba(self._accent, int(60 + 80 * self._pulse)))
            p.drawEllipse(c, r, r)
        fill = rgba(theme.BG_DEEP, 200)
        p.setBrush(fill)
        edge = rgba(self._accent, 230 if (self._hover or self._active) else 120)
        p.setPen(QPen(edge, 1.4))
        p.drawEllipse(c, r - 3, r - 3)
        icon_color = ICE if (self._hover or self._active) else rgba(CYAN, 230)
        s = r * 0.9
        self._icon(p, QRectF(c.x() - s / 2, c.y() - s / 2, s, s), icon_color)
        p.end()


# --- icons -------------------------------------------------------------------------


def icon_mic(p: QPainter, r: QRectF, color: QColor) -> None:
    p.setPen(QPen(color, 1.8, c=Qt.PenCapStyle.RoundCap))
    p.setBrush(Qt.BrushStyle.NoBrush)
    w = r.width()
    body = QRectF(r.center().x() - w * 0.17, r.top() + w * 0.05, w * 0.34, w * 0.55)
    p.drawRoundedRect(body, w * 0.17, w * 0.17)
    arc = QRectF(r.center().x() - w * 0.3, r.top() + w * 0.2, w * 0.6, w * 0.55)
    p.drawArc(arc, 200 * 16, 140 * 16)
    p.drawLine(
        QPointF(r.center().x(), r.top() + w * 0.76), QPointF(r.center().x(), r.bottom() - w * 0.06)
    )
    p.drawLine(
        QPointF(r.center().x() - w * 0.16, r.bottom() - w * 0.06),
        QPointF(r.center().x() + w * 0.16, r.bottom() - w * 0.06),
    )


def icon_send(p: QPainter, r: QRectF, color: QColor) -> None:
    p.setPen(QPen(color, 1.8, c=Qt.PenCapStyle.RoundCap, j=Qt.PenJoinStyle.RoundJoin))
    p.setBrush(rgba(color, 60))
    w = r.width()
    x, y = r.left(), r.top()
    pts = [
        QPointF(x + w * 0.12, y + w * 0.18),
        QPointF(x + w * 0.9, y + w * 0.5),
        QPointF(x + w * 0.12, y + w * 0.82),
        QPointF(x + w * 0.26, y + w * 0.5),
    ]
    p.drawPolygon(pts)
    p.drawLine(pts[3], QPointF(x + w * 0.55, y + w * 0.5))


def icon_stop(p: QPainter, r: QRectF, color: QColor) -> None:
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(color)
    w = r.width() * 0.42
    p.drawRoundedRect(QRectF(r.center().x() - w / 2, r.center().y() - w / 2, w, w), 3, 3)


def icon_analyze(p: QPainter, r: QRectF, color: QColor) -> None:
    p.setPen(QPen(color, 1.6, c=Qt.PenCapStyle.RoundCap))
    p.setBrush(Qt.BrushStyle.NoBrush)
    w = r.width()
    c = r.center()
    p.drawEllipse(c, w * 0.22, w * 0.22)
    p.drawEllipse(c, w * 0.06, w * 0.06)
    for dx, dy in ((-1, -1), (1, -1), (-1, 1), (1, 1)):
        corner = QPointF(c.x() + dx * w * 0.45, c.y() + dy * w * 0.45)
        p.drawLine(corner, QPointF(corner.x() - dx * w * 0.18, corner.y()))
        p.drawLine(corner, QPointF(corner.x(), corner.y() - dy * w * 0.18))


def icon_assist(p: QPainter, r: QRectF, color: QColor) -> None:
    p.setPen(QPen(color, 1.6, j=Qt.PenJoinStyle.RoundJoin))
    p.setBrush(Qt.BrushStyle.NoBrush)
    w = r.width()
    bubble = QRectF(r.left() + w * 0.08, r.top() + w * 0.12, w * 0.84, w * 0.58)
    p.drawRoundedRect(bubble, w * 0.14, w * 0.14)
    p.drawLine(
        QPointF(r.left() + w * 0.3, bubble.bottom()),
        QPointF(r.left() + w * 0.24, r.bottom() - w * 0.08),
    )
    p.drawLine(
        QPointF(r.left() + w * 0.24, r.bottom() - w * 0.08),
        QPointF(r.left() + w * 0.46, bubble.bottom()),
    )
    for i in range(3):
        p.drawPoint(QPointF(bubble.left() + bubble.width() * (0.3 + 0.2 * i), bubble.center().y()))


def icon_execute(p: QPainter, r: QRectF, color: QColor) -> None:
    p.setPen(QPen(color, 1.4, j=Qt.PenJoinStyle.RoundJoin))
    p.setBrush(rgba(color, 70))
    w = r.width()
    x, y = r.left(), r.top()
    p.drawPolygon(
        [
            QPointF(x + w * 0.58, y + w * 0.04),
            QPointF(x + w * 0.2, y + w * 0.56),
            QPointF(x + w * 0.48, y + w * 0.56),
            QPointF(x + w * 0.4, y + w * 0.96),
            QPointF(x + w * 0.8, y + w * 0.42),
            QPointF(x + w * 0.52, y + w * 0.42),
        ]
    )


def icon_new_chat(p: QPainter, r: QRectF, color: QColor) -> None:
    p.setPen(QPen(color, 1.6, c=Qt.PenCapStyle.RoundCap))
    c = r.center()
    w = r.width() * 0.3
    p.drawLine(QPointF(c.x() - w, c.y()), QPointF(c.x() + w, c.y()))
    p.drawLine(QPointF(c.x(), c.y() - w), QPointF(c.x(), c.y() + w))


def icon_key(p: QPainter, r: QRectF, color: QColor) -> None:
    p.setPen(QPen(color, 1.6, c=Qt.PenCapStyle.RoundCap, j=Qt.PenJoinStyle.RoundJoin))
    p.setBrush(Qt.BrushStyle.NoBrush)
    w = r.width()
    x, y = r.left(), r.top()
    p.drawEllipse(QPointF(x + w * 0.3, y + w * 0.5), w * 0.17, w * 0.17)
    p.drawLine(QPointF(x + w * 0.47, y + w * 0.5), QPointF(x + w * 0.9, y + w * 0.5))
    p.drawLine(QPointF(x + w * 0.76, y + w * 0.5), QPointF(x + w * 0.76, y + w * 0.66))
    p.drawLine(QPointF(x + w * 0.88, y + w * 0.5), QPointF(x + w * 0.88, y + w * 0.62))
