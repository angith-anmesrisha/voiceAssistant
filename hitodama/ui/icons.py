"""Programmatic app icon: a small spirit flame, so M0 ships without image assets."""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPixmap, QRadialGradient


def _flame_pixmap(size: int, core: QColor, glow: QColor) -> QPixmap:
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)

    s = float(size)
    path = QPainterPath()
    path.moveTo(s * 0.50, s * 0.06)
    path.cubicTo(s * 0.62, s * 0.30, s * 0.86, s * 0.42, s * 0.84, s * 0.64)
    path.arcTo(QRectF(s * 0.16, s * 0.30, s * 0.68, s * 0.66), 0, -180)
    path.cubicTo(s * 0.16, s * 0.46, s * 0.40, s * 0.36, s * 0.50, s * 0.06)
    path.closeSubpath()

    gradient = QRadialGradient(QPointF(s * 0.5, s * 0.68), s * 0.42)
    gradient.setColorAt(0.0, QColor(255, 255, 255))
    gradient.setColorAt(0.35, core)
    gradient.setColorAt(1.0, glow)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(gradient)
    painter.drawPath(path)
    painter.end()
    return pixmap


def app_icon() -> QIcon:
    icon = QIcon()
    for size in (16, 20, 24, 32, 48, 64, 128, 256):
        icon.addPixmap(_flame_pixmap(size, QColor(120, 200, 255), QColor(40, 90, 255, 200)))
    return icon
