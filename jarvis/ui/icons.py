"""App icon drawn in code: the arc-reactor mark."""

from __future__ import annotations

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPixmap

from jarvis.ui.widgets.orb import paint_reactor_logo


def _pixmap(size: int) -> QPixmap:
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor(6, 10, 16))
    painter.drawEllipse(QRectF(0, 0, size, size))
    paint_reactor_logo(painter, QRectF(size * 0.08, size * 0.08, size * 0.84, size * 0.84))
    painter.end()
    return pixmap


def app_icon() -> QIcon:
    icon = QIcon()
    for size in (16, 20, 24, 32, 48, 64, 128, 256):
        icon.addPixmap(_pixmap(size))
    return icon
