"""The corner orb: a small always-on-top JARVIS over other windows while it works.

Shown when JARVIS is activated (wake word, hotkey) and the main window isn't in front, so
the rest of the UI stays out of the way. It never takes focus, so typing into the app you
were using keeps working. A short caption next to the orb says what's happening. Click
it to open the full window.
"""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QGuiApplication, QMouseEvent, QPainter, QPaintEvent, QPen
from PySide6.QtWidgets import QLabel, QWidget

from jarvis.ui import theme
from jarvis.ui.theme import CYAN, DANGER, rgba
from jarvis.ui.widgets.orb import OrbHud

ORB_SIZE = 190
_CAPTION_W = 300
_MARGIN = 22


class _Caption(QWidget):
    """A small glass bubble with the state and the latest words."""

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.title = QLabel(self)
        self.title.setFont(theme.label_font(8.8))
        self.body = QLabel(self)
        self.body.setWordWrap(True)
        self.body.setFont(theme.font(theme.BODY_FAMILY, 10))
        self.body.setStyleSheet(f"color: {theme.css(theme.TEXT)}; background: transparent;")
        self.accent = CYAN

    def set_text(self, title: str, body: str, accent: QColor) -> None:
        self.accent = accent
        self.title.setText(title)
        self.title.setStyleSheet(f"color: {theme.css(accent)}; background: transparent;")
        if len(body) > 180:
            body = body[:177].rstrip() + "…"
        self.body.setText(body)
        self.body.setVisible(bool(body))
        self._fit()
        self.update()

    def _fit(self) -> None:
        inner = self.width() - 32
        self.title.setGeometry(16, 12, inner, 18)
        height = self.body.heightForWidth(inner) if self.body.isVisibleTo(self) else 0
        self.body.setGeometry(16, 34, inner, max(0, height))
        self.setFixedHeight(34 + height + (14 if height else 0))

    def paintEvent(self, _event: QPaintEvent) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        p.setPen(QPen(rgba(self.accent, 90), 1))
        p.setBrush(QColor(12, 17, 23, 228))
        p.drawRoundedRect(r, 12, 12)
        p.setPen(QPen(rgba(self.accent, 210), 2, c=Qt.PenCapStyle.RoundCap))
        p.drawLine(QPointF(r.left() + 16, r.top() + 1), QPointF(r.left() + 56, r.top() + 1))
        p.end()


class OrbOverlay(QWidget):
    clicked = Signal()

    def __init__(self, level: Callable[[], float] | None = None) -> None:
        super().__init__(
            None,
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowDoesNotAcceptFocus,
        )
        self.setWindowTitle("JARVIS")
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip("Open JARVIS")
        self.orb = OrbHud(level, self)
        self.caption = _Caption(self)
        self.caption.setFixedWidth(_CAPTION_W)
        self.caption.hide()
        self.alarm = False
        self.resize(_CAPTION_W + 12 + ORB_SIZE, ORB_SIZE + 40)

    def say(self, title: str, body: str = "", *, error: bool = False) -> None:
        self.caption.set_text(title, body, DANGER if error or self.alarm else CYAN)
        self.caption.setVisible(bool(title or body))
        self._layout()

    def set_alarm(self, on: bool) -> None:
        """Red orb and caption: the last command didn't work."""
        if on == self.alarm:
            return
        self.alarm = on
        self.orb.set_alarm(on)
        caption = self.caption
        caption.set_text(caption.title.text(), caption.body.text(), DANGER if on else CYAN)

    def place(self) -> None:
        """Bottom-right of the screen with the mouse (or the primary screen)."""
        from PySide6.QtGui import QCursor

        screen = QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()
        if screen is None:
            return
        area = screen.availableGeometry()
        self.move(
            area.right() - self.width() - _MARGIN + 1, area.bottom() - self.height() - _MARGIN + 1
        )

    def _layout(self) -> None:
        w, h = self.width(), self.height()
        self.orb.setGeometry(w - ORB_SIZE, h - ORB_SIZE, ORB_SIZE, ORB_SIZE)
        ch = self.caption.height()
        self.caption.move(0, h - ORB_SIZE // 2 - ch // 2)

    def resizeEvent(self, event: object) -> None:  # noqa: N802
        super().resizeEvent(event)  # type: ignore[arg-type]
        self._layout()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()


__all__ = ["ORB_SIZE", "OrbOverlay"]
