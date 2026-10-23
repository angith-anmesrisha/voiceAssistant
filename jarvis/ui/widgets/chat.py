"""Conversation panel: user, assistant, action and error entries."""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QPainter, QPaintEvent, QPen
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QScrollArea, QVBoxLayout, QWidget

from jarvis.app.events import EntryKind
from jarvis.ui import theme
from jarvis.ui.theme import AMBER, CYAN, DANGER, ELECTRIC, TEXT_DIM, rgba

_MAX_ENTRIES = 200


class _Bubble(QFrame):
    def __init__(self, kind: EntryKind, text: str, meta: str) -> None:
        super().__init__()
        self._kind = kind
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 10, 14, 11)
        layout.setSpacing(4)
        if meta:
            header = QLabel(meta)
            header.setFont(theme.label_font(8.8))
            color = {EntryKind.USER: TEXT_DIM, EntryKind.ERROR: DANGER}.get(kind, CYAN)
            header.setStyleSheet(f"color: {theme.css(rgba(color, 220))}; background: transparent;")
            if kind is EntryKind.USER:
                header.setAlignment(Qt.AlignmentFlag.AlignRight)
            layout.addWidget(header)
        body = QLabel(text)
        body.setWordWrap(True)
        body.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        body.setTextFormat(Qt.TextFormat.PlainText)
        size = 9.2 if kind is EntryKind.ACTION else 10.2
        body.setFont(theme.font(theme.BODY_FAMILY, size))
        color = {EntryKind.ACTION: theme.TEXT_DIM, EntryKind.ERROR: QColor(255, 200, 206)}.get(
            kind, theme.TEXT
        )
        body.setStyleSheet(f"color: {theme.css(color)}; background: transparent;")
        layout.addWidget(body)

    def paintEvent(self, _event: QPaintEvent) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        if self._kind is EntryKind.USER:
            p.setBrush(rgba(ELECTRIC, 38))
            p.setPen(QPen(rgba(ELECTRIC, 120), 1))
        elif self._kind is EntryKind.ERROR:
            p.setBrush(rgba(DANGER, 26))
            p.setPen(QPen(rgba(DANGER, 130), 1))
        elif self._kind is EntryKind.ACTION:
            p.setBrush(rgba(theme.BG_DEEP, 120))
            accent = AMBER if self._text().startswith("Declined") else CYAN
            p.setPen(QPen(rgba(accent, 90), 1, Qt.PenStyle.DashLine))
        else:
            p.setBrush(rgba(theme.BG_DEEP, 150))
            p.setPen(QPen(rgba(CYAN, 70), 1))
        p.drawRoundedRect(r, 12, 12)
        if self._kind is EntryKind.ASSISTANT:
            p.setPen(QPen(rgba(CYAN, 210), 2, c=Qt.PenCapStyle.RoundCap))
            p.drawLine(
                QPointF(r.left() + 1.5, r.top() + 12), QPointF(r.left() + 1.5, r.bottom() - 12)
            )
        p.end()

    def _text(self) -> str:
        labels = self.findChildren(QLabel)
        return labels[-1].text() if labels else ""


class ChatView(QScrollArea):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setFrameShape(QFrame.Shape.NoFrame)
        container = QWidget()
        container.setStyleSheet("background: transparent;")
        self._list = QVBoxLayout(container)
        self._list.setContentsMargins(2, 4, 8, 4)
        self._list.setSpacing(10)
        self._list.addStretch(1)
        self.setWidget(container)
        self.viewport().setStyleSheet("background: transparent;")
        self._empty = QLabel(
            "Conversation history appears here.\nAsk anything, or switch to Execute to act."
        )
        self._empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._empty.setFont(theme.font(theme.BODY_FAMILY, 9.5))
        self._empty.setStyleSheet(f"color: {theme.css(theme.TEXT_MUTED)}; background: transparent;")
        self._list.insertWidget(0, self._empty)

    def clear(self) -> None:
        while self._list.count() > 2:
            item = self._list.takeAt(1)
            if item is not None and (w := item.widget()) is not None:
                w.deleteLater()
        self._empty.show()

    def add(self, kind: EntryKind, text: str, meta: str = "") -> None:
        self._empty.hide()
        bubble = _Bubble(kind, text, meta)
        row = QWidget()
        row.setStyleSheet("background: transparent;")
        h = QHBoxLayout(row)
        h.setContentsMargins(0, 0, 0, 0)
        if kind is EntryKind.USER:
            h.addStretch(1)
            h.addWidget(bubble, 6)
        elif kind is EntryKind.ACTION:
            h.addSpacing(18)
            h.addWidget(bubble, 1)
        else:
            h.addWidget(bubble, 7)
            h.addStretch(1)
        self._list.insertWidget(self._list.count() - 1, row)
        while self._list.count() - 2 > _MAX_ENTRIES:
            item = self._list.takeAt(1)
            if item is not None and (w := item.widget()) is not None:
                w.deleteLater()
        QTimer.singleShot(0, self._scroll_to_end)

    def _scroll_to_end(self) -> None:
        bar = self.verticalScrollBar()
        bar.setValue(bar.maximum())
