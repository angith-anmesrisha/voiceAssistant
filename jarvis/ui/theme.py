"""Colours, fonts and shared painting helpers for the JARVIS look."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QFontDatabase, QLinearGradient, QPainter, QPen

FONT_DIR = Path(__file__).resolve().parent.parent / "assets" / "fonts"

# Palette: charcoal/black base, cyan to electric blue to blue-white glow. No greens.
BG_DEEP = QColor(4, 7, 12)
BG = QColor(8, 13, 20)
PANEL = QColor(12, 22, 34, 165)
PANEL_TOP = QColor(22, 40, 58, 150)
CYAN = QColor(34, 211, 238)
ELECTRIC = QColor(0, 156, 255)
DEEP_BLUE = QColor(16, 72, 210)
ICE = QColor(214, 245, 255)
TEXT = QColor(226, 244, 255)
TEXT_DIM = QColor(128, 166, 186)
TEXT_MUTED = QColor(78, 104, 122)
AMBER = QColor(255, 181, 71)
DANGER = QColor(255, 77, 94)
STEEL = QColor(96, 118, 140)

TITLE_FAMILY = "Orbitron"
LABEL_FAMILY = "Rajdhani"
BODY_FAMILY = "Inter"


def rgba(color: QColor, alpha: int) -> QColor:
    c = QColor(color)
    c.setAlpha(max(0, min(255, alpha)))
    return c


def css(color: QColor) -> str:
    return f"rgba({color.red()}, {color.green()}, {color.blue()}, {color.alphaF():.3f})"


def load_fonts() -> None:
    for path in sorted(FONT_DIR.glob("*.ttf")):
        QFontDatabase.addApplicationFont(str(path))


def font(
    family: str, size: float, weight: QFont.Weight = QFont.Weight.Normal, spacing: float = 0
) -> QFont:
    f = QFont(family)
    f.setPointSizeF(size)
    f.setWeight(weight)
    if spacing:
        f.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, spacing)
    f.setHintingPreference(QFont.HintingPreference.PreferNoHinting)
    return f


def label_font(size: float = 10.5, weight: QFont.Weight = QFont.Weight.DemiBold) -> QFont:
    return font(LABEL_FAMILY, size, weight, spacing=1.6)


def paint_glass(painter: QPainter, rect: QRectF, radius: float = 16, accent: QColor = CYAN) -> None:
    """A frosted panel: translucent fill, soft top sheen, thin glowing edge."""
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    fill = QLinearGradient(rect.topLeft(), rect.bottomLeft())
    fill.setColorAt(0.0, PANEL_TOP)
    fill.setColorAt(0.35, PANEL)
    fill.setColorAt(1.0, rgba(PANEL, 190))
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(fill)
    painter.drawRoundedRect(rect, radius, radius)

    edge = QLinearGradient(rect.topLeft(), rect.bottomRight())
    edge.setColorAt(0.0, rgba(accent, 120))
    edge.setColorAt(0.5, rgba(accent, 26))
    edge.setColorAt(1.0, rgba(accent, 70))
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.setPen(QPen(edge, 1.0))
    painter.drawRoundedRect(rect.adjusted(0.5, 0.5, -0.5, -0.5), radius, radius)

    sheen = QLinearGradient(QPointF(rect.left(), 0), QPointF(rect.right(), 0))
    sheen.setColorAt(0.0, rgba(ICE, 0))
    sheen.setColorAt(0.5, rgba(ICE, 60))
    sheen.setColorAt(1.0, rgba(ICE, 0))
    painter.setPen(QPen(sheen, 1.0))
    painter.drawLine(
        QPointF(rect.left() + radius, rect.top() + 1),
        QPointF(rect.right() - radius, rect.top() + 1),
    )
    painter.restore()


def paint_corner_brackets(painter: QPainter, rect: QRectF, color: QColor, size: float = 10) -> None:
    """Little HUD corner marks."""
    painter.save()
    painter.setPen(QPen(color, 1.4))
    left, top, right, bottom = rect.left(), rect.top(), rect.right(), rect.bottom()
    corners = (
        (left, top, 1, 1),
        (right, top, -1, 1),
        (left, bottom, 1, -1),
        (right, bottom, -1, -1),
    )
    for x, y, dx, dy in corners:
        painter.drawLine(QPointF(x, y), QPointF(x + dx * size, y))
        painter.drawLine(QPointF(x, y), QPointF(x, y + dy * size))
    painter.restore()


APP_STYLESHEET = f"""
QWidget {{ color: {css(TEXT)}; font-family: "{BODY_FAMILY}", "Segoe UI", sans-serif; }}
QToolTip {{ background: {css(BG)}; color: {css(TEXT)}; border: 1px solid {css(rgba(CYAN, 120))};
           padding: 6px; border-radius: 6px; }}
QScrollArea, QScrollArea > QWidget > QWidget {{ background: transparent; border: none; }}
QScrollBar:vertical {{ background: transparent; width: 8px; margin: 4px 2px; }}
QScrollBar::handle:vertical {{
    background: {css(rgba(CYAN, 70))}; border-radius: 3px; min-height: 30px;
}}
QScrollBar::handle:vertical:hover {{ background: {css(rgba(CYAN, 130))}; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical,
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ height: 0; background: none; }}
QLineEdit {{
    background: {css(rgba(BG_DEEP, 160))};
    border: 1px solid {css(rgba(CYAN, 70))};
    border-radius: 12px; padding: 0 16px; font-size: 11pt;
    selection-background-color: {css(rgba(ELECTRIC, 160))};
}}
QLineEdit:focus {{
    border: 1px solid {css(rgba(CYAN, 190))}; background: {css(rgba(BG_DEEP, 200))};
}}
QLineEdit:disabled {{ color: {css(TEXT_MUTED)}; }}
"""
