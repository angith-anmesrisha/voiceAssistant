"""Header, status panels, mode switch, command bar, confirmation card, background."""

from __future__ import annotations

import asyncio
from datetime import datetime

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import (
    QColor,
    QFont,
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
    QGridLayout,
    QHBoxLayout,
    QLineEdit,
    QPushButton,
    QSizePolicy,
    QWidget,
)

from jarvis.agent.prompts import Mode
from jarvis.providers.base import ProviderState, ProviderStatus
from jarvis.tools.registry import Risk
from jarvis.ui import theme
from jarvis.ui.theme import AMBER, CYAN, DANGER, ELECTRIC, ICE, STEEL, TEXT_DIM, TEXT_MUTED, rgba
from jarvis.ui.widgets.glass import (
    GlassPanel,
    HudButton,
    IconPainter,
    RingGauge,
    StatusDot,
    body_label,
    hud_label,
    icon_analyze,
    icon_assist,
    icon_execute,
)
from jarvis.ui.widgets.orb import paint_reactor_logo

STATE_COLORS: dict[ProviderState, QColor] = {
    ProviderState.READY: CYAN,
    ProviderState.CHECKING: STEEL,
    ProviderState.NO_KEY: TEXT_MUTED,
    ProviderState.OFFLINE: TEXT_MUTED,
    ProviderState.AUTH_ERROR: DANGER,
    ProviderState.MODEL_MISSING: AMBER,
    ProviderState.ERROR: AMBER,
    ProviderState.DISABLED: TEXT_MUTED,
}


class Backdrop(QWidget):
    """Cinematic background: deep vignette, faint grid, soft blue bloom. Cached per size."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._cache: QPixmap | None = None

    def resizeEvent(self, event: QResizeEvent) -> None:  # noqa: N802
        self._cache = None
        super().resizeEvent(event)

    def _render(self) -> QPixmap:
        w, h = max(1, self.width()), max(1, self.height())
        pm = QPixmap(w, h)
        pm.fill(theme.BG_DEEP)
        p = QPainter(pm)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        base = QLinearGradient(0, 0, 0, h)
        base.setColorAt(0.0, QColor(9, 14, 22))
        base.setColorAt(1.0, QColor(3, 5, 9))
        p.fillRect(0, 0, w, h, base)
        bloom = QRadialGradient(QPointF(w * 0.5, h * 0.48), max(w, h) * 0.55)
        bloom.setColorAt(0.0, rgba(ELECTRIC, 34))
        bloom.setColorAt(0.5, rgba(theme.DEEP_BLUE, 12))
        bloom.setColorAt(1.0, rgba(theme.DEEP_BLUE, 0))
        p.fillRect(0, 0, w, h, bloom)
        p.setPen(QPen(rgba(CYAN, 9), 1))
        step = 36
        for x in range(0, w, step):
            p.drawLine(x, 0, x, h)
        for y in range(0, h, step):
            p.drawLine(0, y, w, y)
        p.setPen(QPen(rgba(CYAN, 22), 1))
        for x in range(0, w, step * 4):
            for y in range(0, h, step * 4):
                p.drawLine(x - 3, y, x + 3, y)
                p.drawLine(x, y - 3, x, y + 3)
        vignette = QRadialGradient(QPointF(w / 2, h / 2), max(w, h) * 0.75)
        vignette.setColorAt(0.6, QColor(0, 0, 0, 0))
        vignette.setColorAt(1.0, QColor(0, 0, 0, 170))
        p.fillRect(0, 0, w, h, vignette)
        p.end()
        return pm

    def paintEvent(self, _event: QPaintEvent) -> None:  # noqa: N802
        if self._cache is None or self._cache.size() != self.size():
            self._cache = self._render()
        p = QPainter(self)
        p.drawPixmap(0, 0, self._cache)
        p.end()


class Header(QWidget):
    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setFixedHeight(64)
        self._status_text = "CHECKING"
        self._status_color = STEEL
        self._clock = QTimer(self)
        self._clock.timeout.connect(self.update)
        self._clock.start(15_000)
        self._fixed_now: datetime | None = None

    def set_status(self, text: str, color: QColor) -> None:
        self._status_text, self._status_color = text, color
        self.update()

    def freeze_clock(self, now: datetime) -> None:
        self._fixed_now = now
        self._clock.stop()
        self.update()

    def paintEvent(self, _event: QPaintEvent) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        paint_reactor_logo(p, QRectF(24, 14, 36, 36))
        p.setFont(theme.font(theme.TITLE_FAMILY, 19, QFont.Weight.Bold, spacing=6))
        p.setPen(ICE)
        title_rect = QRectF(74, 0, 260, h)
        p.drawText(title_rect, Qt.AlignmentFlag.AlignVCenter, "JARVIS")
        title_w = p.fontMetrics().horizontalAdvance("JARVIS")
        p.setFont(theme.font(theme.LABEL_FAMILY, 13.5, QFont.Weight.Medium, spacing=1.2))
        p.setPen(TEXT_DIM)
        p.drawText(
            QRectF(74 + title_w + 14, 0, 460, h),
            Qt.AlignmentFlag.AlignVCenter,
            "—  Your personal AI assistant",
        )
        # Right side: status pill and clock.
        now = self._fixed_now or datetime.now()
        p.setFont(theme.font(theme.TITLE_FAMILY, 13, QFont.Weight.DemiBold, spacing=2))
        p.setPen(ICE)
        clock = now.strftime("%H:%M")
        p.drawText(
            QRectF(w - 120, 8, 96, 26),
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
            clock,
        )
        p.setFont(theme.label_font(9))
        p.setPen(TEXT_DIM)
        p.drawText(
            QRectF(w - 220, 32, 196, 18),
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
            now.strftime("%a %d %b %Y").upper(),
        )
        p.setFont(theme.label_font(9.5))
        text_w = p.fontMetrics().horizontalAdvance(self._status_text) + 34
        pill = QRectF(w - 150 - text_w, 18, text_w, 28)
        p.setPen(QPen(rgba(self._status_color, 160), 1))
        p.setBrush(rgba(self._status_color, 28))
        p.drawRoundedRect(pill, 14, 14)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(self._status_color)
        p.drawEllipse(QPointF(pill.left() + 14, pill.center().y()), 3.5, 3.5)
        p.setPen(rgba(self._status_color, 255) if self._status_color != STEEL else TEXT_DIM)
        p.drawText(pill.adjusted(24, 0, -8, 0), Qt.AlignmentFlag.AlignVCenter, self._status_text)
        # Thin separator with a bright centre.
        line = QLinearGradient(0, 0, w, 0)
        line.setColorAt(0.0, rgba(CYAN, 0))
        line.setColorAt(0.5, rgba(CYAN, 110))
        line.setColorAt(1.0, rgba(CYAN, 0))
        p.setPen(QPen(line, 1))
        p.drawLine(QPointF(0, h - 1), QPointF(w, h - 1))
        p.end()


class ProviderRow(QWidget):
    def __init__(self, name: str, role: str, parent: QWidget | None = None):
        super().__init__(parent)
        self._role = role
        layout = QGridLayout(self)
        layout.setContentsMargins(0, 2, 0, 2)
        layout.setHorizontalSpacing(10)
        layout.setVerticalSpacing(1)
        self.dot = StatusDot()
        self.name = body_label(name, 10.5)
        self.name.setFont(theme.font(theme.LABEL_FAMILY, 12.5, QFont.Weight.Bold, spacing=0.6))
        self.state = hud_label("CHECKING", 9, TEXT_DIM)
        self.state.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.detail = body_label("", 8.4, TEXT_MUTED)
        self.detail.setWordWrap(True)
        layout.addWidget(self.dot, 0, 0, Qt.AlignmentFlag.AlignVCenter)
        layout.addWidget(self.name, 0, 1)
        layout.addWidget(self.state, 0, 2)
        layout.addWidget(self.detail, 1, 1, 1, 2)
        layout.setColumnStretch(1, 1)

    def set_status(self, status: ProviderStatus, model: str, active: bool) -> None:
        color = STATE_COLORS[status.state]
        self.dot.set_color(ICE if active else color)
        label = status.state.value.upper()
        if status.state is ProviderState.READY and status.latency_ms is not None:
            label = f"READY · {status.latency_ms} MS"
        if active:
            label = "ACTIVE"
        self.state.setText(label)
        self.state.setStyleSheet(
            f"color: {theme.css(ICE if active else color)}; background: transparent;"
        )
        shown = status.detail if status.detail and not status.usable else model
        if status.state is ProviderState.NO_KEY:
            shown = "use the key button above"
        self.detail.setText(f"{self._role}  ·  {shown}")


class SystemPanel(GlassPanel):
    def __init__(self, parent: QWidget | None = None):
        super().__init__("AI PROVIDERS", parent)
        self.rows: dict[str, ProviderRow] = {}
        for key, name, role in (
            ("groq", "Groq", "PRIMARY"),
            ("gemini", "Gemini", "BACKUP"),
            ("ollama", "Ollama", "LOCAL"),
        ):
            row = ProviderRow(name, role)
            self.rows[key] = row
            self.body.addWidget(row)
        self.active = body_label("Last answer: —", 8.6, TEXT_DIM)
        self.active.setWordWrap(True)
        self.body.addWidget(self.active)


class NetworkPanel(GlassPanel):
    def __init__(self, parent: QWidget | None = None):
        super().__init__("NETWORK  ·  SYSTEM", parent)
        top = QHBoxLayout()
        top.setSpacing(10)
        self.dot = StatusDot(STEEL)
        self.state = body_label("Checking…", 11)
        self.state.setFont(theme.font(theme.LABEL_FAMILY, 13, QFont.Weight.Bold, spacing=0.8))
        self.latency = hud_label("", 9, TEXT_DIM)
        top.addWidget(self.dot)
        top.addWidget(self.state)
        top.addStretch(1)
        top.addWidget(self.latency)
        self.body.addLayout(top)
        gauges = QHBoxLayout()
        gauges.setSpacing(4)
        self.cpu = RingGauge("CPU")
        self.ram = RingGauge("MEMORY")
        self.battery = RingGauge("BATTERY")
        for g in (self.cpu, self.ram, self.battery):
            gauges.addWidget(g)
        self.body.addLayout(gauges)

    def set_network(self, online: bool, latency_ms: int | None) -> None:
        self.dot.set_color(CYAN if online else DANGER)
        self.state.setText("ONLINE" if online else "OFFLINE")
        self.latency.setText(f"{latency_ms} MS" if online and latency_ms is not None else "")


class SafetyPanel(GlassPanel):
    """Always-visible summary of what JARVIS may do on its own."""

    _ROWS = (
        (CYAN, "LOW", "Read-only and approved apps or sites: runs directly"),
        (AMBER, "MEDIUM", "Typing, shortcuts, other sites, lock or sleep: asks first"),
        (DANGER, "HIGH", "Shutdown or restart: you must type CONFIRM"),
        (TEXT_MUTED, "BLOCKED", "Shell commands, file deletion, system settings"),
    )

    def __init__(self, parent: QWidget | None = None):
        super().__init__("PERMISSIONS", parent)
        self.body.setSpacing(7)
        for color, level, text in self._ROWS:
            row = QHBoxLayout()
            row.setSpacing(10)
            badge = hud_label(level, 8.6, color)
            badge.setFixedWidth(66)
            detail = body_label(text, 8.6, TEXT_DIM)
            detail.setWordWrap(True)
            row.addWidget(badge, 0, Qt.AlignmentFlag.AlignTop)
            row.addWidget(detail, 1)
            self.body.addLayout(row)


class ModeSwitch(QWidget):
    """Analyze / Assist / Execute segmented control."""

    changed = Signal(object)

    _ITEMS: tuple[tuple[Mode, str, IconPainter, str], ...] = (
        (Mode.ANALYZE, "ANALYZE", icon_analyze, "Look at your screen and answer about it"),
        (Mode.ASSIST, "ASSIST", icon_assist, "Chat, explain, write and plan"),
        (Mode.EXECUTE, "EXECUTE", icon_execute, "Act on your PC with approved tools"),
    )

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.mode = Mode.ASSIST
        self._hover: int | None = None
        self.setFixedHeight(44)
        self.setMinimumWidth(420)
        self.setMouseTracking(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def sizeHint(self) -> QSize:  # noqa: N802
        return QSize(460, 44)

    def set_mode(self, mode: Mode) -> None:
        if mode != self.mode:
            self.mode = mode
            self.update()
            self.changed.emit(mode)

    def _segment(self, i: int) -> QRectF:
        w = self.width() / 3
        return QRectF(i * w, 0, w, self.height()).adjusted(3, 3, -3, -3)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        i = int(event.position().x() // (self.width() / 3))
        if i != self._hover:
            self._hover = i
            self.setToolTip(self._ITEMS[min(i, 2)][3])
            self.update()

    def leaveEvent(self, _event: object) -> None:  # noqa: N802
        self._hover = None
        self.update()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        i = min(2, int(event.position().x() // (self.width() / 3)))
        self.set_mode(self._ITEMS[i][0])

    def paintEvent(self, _event: QPaintEvent) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        outer = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        p.setPen(QPen(rgba(CYAN, 70), 1))
        p.setBrush(rgba(theme.BG_DEEP, 150))
        p.drawRoundedRect(outer, 22, 22)
        for i, (mode, label, icon, _tip) in enumerate(self._ITEMS):
            seg = self._segment(i)
            active = mode is self.mode
            if active:
                fill = QLinearGradient(seg.topLeft(), seg.bottomLeft())
                fill.setColorAt(0, rgba(ELECTRIC, 120))
                fill.setColorAt(1, rgba(theme.DEEP_BLUE, 90))
                p.setBrush(fill)
                p.setPen(QPen(rgba(CYAN, 200), 1))
                p.drawRoundedRect(seg, 19, 19)
            elif self._hover == i:
                p.setBrush(rgba(CYAN, 20))
                p.setPen(Qt.PenStyle.NoPen)
                p.drawRoundedRect(seg, 19, 19)
            color = ICE if active else TEXT_DIM
            p.setFont(theme.label_font(10.5, QFont.Weight.Bold))
            text_w = p.fontMetrics().horizontalAdvance(label)
            total = 18 + 8 + text_w
            x0 = seg.center().x() - total / 2
            icon(p, QRectF(x0, seg.center().y() - 9, 18, 18), color)
            p.setPen(color)
            p.drawText(
                QRectF(x0 + 26, seg.top(), text_w + 4, seg.height()),
                Qt.AlignmentFlag.AlignVCenter,
                label,
            )
        p.end()


class CommandBar(GlassPanel):
    submitted = Signal(str)

    def __init__(self, mic: HudButton, send: HudButton, parent: QWidget | None = None):
        super().__init__("", parent)
        self.body.setContentsMargins(14, 10, 12, 10)
        row = QHBoxLayout()
        row.setSpacing(10)
        self.input = QLineEdit()
        self.input.setPlaceholderText("Ask JARVIS anything…")
        self.input.setMinimumHeight(46)
        self.input.setFont(theme.font(theme.BODY_FAMILY, 11))
        self.input.returnPressed.connect(self._submit)
        send.clicked.connect(self._submit)
        row.addWidget(self.input, 1)
        row.addWidget(mic)
        row.addWidget(send)
        self.body.addLayout(row)

    def _submit(self) -> None:
        self.submitted.emit(self.input.text())


class ConfirmOverlay(QWidget):
    """Modal card asking the user to allow a MEDIUM or HIGH risk action."""

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self._future: asyncio.Future[bool] | None = None
        self._risk = Risk.MEDIUM
        self.hide()
        self.card = GlassPanel("CONFIRM ACTION", self, accent=AMBER)
        self.card.setFixedWidth(520)
        self.badge = hud_label("", 10, AMBER)
        self.text = body_label("", 11.5)
        self.text.setWordWrap(True)
        self.hint = body_label("", 9.2, TEXT_DIM)
        self.hint.setWordWrap(True)
        self.typed = QLineEdit()
        self.typed.setPlaceholderText("Type CONFIRM to allow")
        self.typed.setMinimumHeight(40)
        self.typed.textChanged.connect(self._sync)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.cancel = self._button("Cancel", TEXT_DIM)
        self.allow = self._button("Allow", AMBER)
        self.cancel.clicked.connect(lambda: self._finish(False))
        self.allow.clicked.connect(lambda: self._finish(True))
        buttons.addWidget(self.cancel)
        buttons.addWidget(self.allow)
        for w in (self.badge, self.text, self.hint, self.typed):
            self.card.body.addWidget(w)
        self.card.body.addLayout(buttons)

    @staticmethod
    def _button(text: str, color: QColor) -> QPushButton:
        b = QPushButton(text)
        b.setCursor(Qt.CursorShape.PointingHandCursor)
        b.setMinimumSize(110, 38)
        b.setFont(theme.label_font(11, QFont.Weight.Bold))
        b.setStyleSheet(
            f"QPushButton {{ color: {theme.css(color)}; background: {theme.css(rgba(color, 22))};"
            f" border: 1px solid {theme.css(rgba(color, 150))}; border-radius: 10px; }}"
            f"QPushButton:hover {{ background: {theme.css(rgba(color, 50))}; }}"
            f"QPushButton:disabled {{ color: {theme.css(TEXT_MUTED)};"
            f" border-color: {theme.css(rgba(TEXT_MUTED, 90))}; background: transparent; }}"
        )
        return b

    def ask(self, description: str, risk: Risk) -> asyncio.Future[bool]:
        if self._future is not None and not self._future.done():
            self._future.set_result(False)
        self._future = asyncio.get_running_loop().create_future()
        self._risk = risk
        high = risk >= Risk.HIGH
        color = DANGER if high else AMBER
        self.card._accent = color
        self.badge.setText("HIGH RISK · STRONG CONFIRMATION" if high else "MEDIUM RISK")
        self.badge.setStyleSheet(f"color: {theme.css(color)}; background: transparent;")
        self.text.setText(description)
        self.hint.setText(
            "This could lose unsaved work or is hard to undo. Type CONFIRM to allow it."
            if high
            else "JARVIS will only do this if you allow it."
        )
        self.typed.setVisible(high)
        self.typed.clear()
        self._sync()
        parent = self.parentWidget()
        if parent is not None:
            self.setGeometry(parent.rect())
        self.raise_()
        self.show()
        self._place_card()
        (self.typed if high else self.allow).setFocus()
        return self._future

    def _sync(self) -> None:
        self.allow.setEnabled(self._risk < Risk.HIGH or self.typed.text().strip() == "CONFIRM")

    def _finish(self, allowed: bool) -> None:
        self.hide()
        if self._future is not None and not self._future.done():
            self._future.set_result(allowed)

    def _place_card(self) -> None:
        layout = self.card.layout()
        width = self.card.width()
        if layout is not None:
            layout.activate()
            self.card.setFixedHeight(layout.totalHeightForWidth(width))
        self.card.move((self.width() - width) // 2, (self.height() - self.card.height()) // 2)

    def resizeEvent(self, event: QResizeEvent) -> None:  # noqa: N802
        self._place_card()
        super().resizeEvent(event)

    def showEvent(self, event: object) -> None:  # noqa: N802
        self._place_card()

    def paintEvent(self, _event: QPaintEvent) -> None:  # noqa: N802
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(2, 4, 8, 175))
        p.end()


class StateCaption(QWidget):
    """Big state word under the orb, with a hint line."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setFixedHeight(62)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._title = "IDLE"
        self._hint = ""
        self._color = CYAN

    def set_text(self, title: str, hint: str, color: QColor = CYAN) -> None:
        self._title, self._hint, self._color = title, hint, color
        self.update()

    def paintEvent(self, _event: QPaintEvent) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w = self.width()
        p.setFont(theme.font(theme.TITLE_FAMILY, 15, QFont.Weight.DemiBold, spacing=7))
        p.setPen(rgba(self._color, 240))
        p.drawText(QRectF(0, 0, w, 30), Qt.AlignmentFlag.AlignCenter, self._title)
        tw = p.fontMetrics().horizontalAdvance(self._title) + len(self._title) * 7
        p.setPen(QPen(rgba(self._color, 120), 1))
        for sign in (-1, 1):
            x = w / 2 + sign * (tw / 2 + 16)
            p.drawLine(QPointF(x, 15), QPointF(x + sign * 46, 15))
        p.setFont(theme.font(theme.BODY_FAMILY, 9.6))
        p.setPen(TEXT_DIM)
        p.drawText(QRectF(0, 34, w, 22), Qt.AlignmentFlag.AlignCenter, self._hint)
        p.end()


__all__ = [
    "Backdrop",
    "CommandBar",
    "ConfirmOverlay",
    "Header",
    "ModeSwitch",
    "NetworkPanel",
    "SafetyPanel",
    "StateCaption",
    "SystemPanel",
]
