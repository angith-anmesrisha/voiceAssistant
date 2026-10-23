"""In-window card for adding API keys (saved as user environment variables)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPainter, QPaintEvent, QResizeEvent
from PySide6.QtWidgets import QHBoxLayout, QLineEdit, QWidget

from jarvis.ui import theme
from jarvis.ui.theme import CYAN, TEXT_DIM
from jarvis.ui.widgets.glass import GlassPanel, body_label, hud_label
from jarvis.ui.widgets.panels import ConfirmOverlay


@dataclass(frozen=True)
class KeyField:
    provider: str  # display name
    env: str  # environment variable name
    url: str  # where to get a key
    saved: bool  # a key is already set


class KeysOverlay(QWidget):
    """Lets the user paste keys. Empty fields leave the saved key unchanged."""

    def __init__(self, parent: QWidget, on_save: Callable[[dict[str, str]], None]):
        super().__init__(parent)
        self._on_save = on_save
        self._inputs: dict[str, QLineEdit] = {}
        self.hide()
        self.card = GlassPanel("API KEYS", self)
        self.card.setFixedWidth(560)
        intro = body_label(
            "Groq is the main online brain and also turns your voice into text. "
            "Gemini is the backup and reads the screen. Paste a key to add or replace it.",
            10,
        )
        intro.setWordWrap(True)
        self.card.body.addWidget(intro)
        self._rows = QWidget()
        self._rows_layout = QHBoxLayout()  # replaced in open()
        self.card.body.addWidget(self._rows)
        note = body_label(
            "Keys are saved as Windows user environment variables (the same as setx). "
            "JARVIS never writes them to its own files or logs.",
            8.8,
            TEXT_DIM,
        )
        note.setWordWrap(True)
        self.card.body.addWidget(note)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.cancel = ConfirmOverlay._button("Cancel", TEXT_DIM)
        self.save = ConfirmOverlay._button("Save", CYAN)
        self.cancel.clicked.connect(self.hide)
        self.save.clicked.connect(self._save)
        buttons.addWidget(self.cancel)
        buttons.addWidget(self.save)
        self.card.body.addLayout(buttons)

    def open(self, fields: list[KeyField]) -> None:
        from PySide6.QtWidgets import QVBoxLayout

        old = self._rows
        self._rows = QWidget()
        layout = QVBoxLayout(self._rows)
        layout.setContentsMargins(0, 4, 0, 4)
        layout.setSpacing(6)
        self._inputs = {}
        for field in fields:
            head = QHBoxLayout()
            head.addWidget(hud_label(field.provider.upper(), 10, CYAN))
            state = "KEY SAVED" if field.saved else "NO KEY"
            head.addWidget(hud_label(state, 8.6, CYAN if field.saved else theme.AMBER))
            head.addStretch(1)
            link = body_label(f'<a href="{field.url}" style="color:#7fd8ff">Get a free key</a>', 9)
            link.setTextFormat(Qt.TextFormat.RichText)
            link.setOpenExternalLinks(True)
            head.addWidget(link)
            layout.addLayout(head)
            edit = QLineEdit()
            edit.setEchoMode(QLineEdit.EchoMode.Password)
            edit.setMinimumHeight(38)
            edit.setPlaceholderText(
                "Saved. Paste a new key to replace it" if field.saved else "Paste your key here"
            )
            edit.returnPressed.connect(self._save)
            self._inputs[field.env] = edit
            layout.addWidget(edit)
        self.card.body.replaceWidget(old, self._rows)
        old.deleteLater()
        parent = self.parentWidget()
        if parent is not None:
            self.setGeometry(parent.rect())
        self.raise_()
        self.show()
        self._place_card()
        if self._inputs:
            next(iter(self._inputs.values())).setFocus()

    def _save(self) -> None:
        values = {env: edit.text().strip() for env, edit in self._inputs.items()}
        for edit in self._inputs.values():
            edit.clear()
        self.hide()
        changed = {env: value for env, value in values.items() if value}
        if changed:
            self._on_save(changed)

    def keyPressEvent(self, event: object) -> None:  # noqa: N802
        from PySide6.QtGui import QKeyEvent

        if isinstance(event, QKeyEvent) and event.key() == Qt.Key.Key_Escape:
            self.hide()
            return
        super().keyPressEvent(event)  # type: ignore[arg-type]

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

    def paintEvent(self, _event: QPaintEvent) -> None:  # noqa: N802
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(2, 4, 8, 175))
        p.end()
