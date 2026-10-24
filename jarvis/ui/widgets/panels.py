"""Header, status panels, mode switch, command bar, confirmation card, background."""

from __future__ import annotations

import asyncio

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import (
    QColor,
    QFont,
    QPainter,
    QPaintEvent,
    QResizeEvent,
)
from PySide6.QtWidgets import (
    QGridLayout,
    QHBoxLayout,
    QLineEdit,
    QPushButton,
    QWidget,
)

from jarvis.providers.base import ProviderState, ProviderStatus
from jarvis.tools.registry import Risk
from jarvis.ui import theme
from jarvis.ui.theme import AMBER, CYAN, DANGER, ICE, STEEL, TEXT_DIM, TEXT_MUTED, rgba
from jarvis.ui.widgets.glass import (
    GlassPanel,
    HudButton,
    RingGauge,
    StatusDot,
    body_label,
    hud_label,
)

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
        # Sends each AI a one-word request and reports exactly what came back.
        self.test = ConfirmOverlay._button("TEST AI PROVIDERS", CYAN)
        self.test.setMinimumSize(0, 32)
        self.test.setToolTip("Send each AI a tiny request and show exactly what happens")
        self.body.addWidget(self.test)


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
        (CYAN, "LOW", "Apps, sites, search, copy, listing files, new folders: runs directly"),
        (AMBER, "MEDIUM", "Typing, paste, moving or deleting a file, lock, sleep: asks first"),
        (DANGER, "HIGH", "Deleting a folder, shutdown or restart: you must type CONFIRM"),
        (TEXT_MUTED, "BLOCKED", "Shell commands, running programs, system folders and settings"),
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

    def settle(self, allowed: bool) -> None:
        """Answered some other way (out loud): close the card with that answer."""
        self._finish(allowed)

    def _finish(self, allowed: bool) -> None:
        self.hide()
        if self._future is not None and not self._future.done():
            self._future.set_result(allowed)

    def _place_card(self) -> None:
        layout = self.card.layout()
        width = self.card.width()
        if layout is not None:
            layout.activate()
            height = layout.totalHeightForWidth(width)
            if height < 0:  # -1 when the layout has no height-for-width
                height = layout.totalSizeHint().height()
            self.card.setFixedHeight(height)
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


__all__ = [
    "CommandBar",
    "ConfirmOverlay",
    "NetworkPanel",
    "SafetyPanel",
    "SystemPanel",
]
