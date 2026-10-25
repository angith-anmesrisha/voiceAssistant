"""The Watchtower tab: which folders JARVIS watches, and the automation window that holds
it next to the workflow builder.

You choose every folder here (or by voice: "watch my Downloads for new PDFs"); only
folders inside your own folders are accepted. Deadline and calendar alerts can be
switched off here too.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from jarvis.agent.watchtower import Watch
from jarvis.ui.workflows import STYLE, WorkflowBuilder

LoadFn = Callable[[], Awaitable[list[Watch]]]
AddFn = Callable[[str, str, list[str], bool], Awaitable[str | None]]  # → problem or None
RemoveFn = Callable[[str], Awaitable[None]]


class WatchtowerPanel(QWidget):
    def __init__(
        self,
        load: LoadFn,
        add: AddFn,
        remove: RemoveFn,
        *,
        options: dict[str, bool] | None = None,
        on_option: Callable[[str, bool], None] | None = None,
    ) -> None:
        super().__init__()
        self._load, self._add, self._remove = load, add, remove
        self._tasks: set[asyncio.Task[Any]] = set()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 18, 18, 18)
        title = QLabel("WATCHTOWER")
        title.setObjectName("title")
        layout.addWidget(title)
        intro = QLabel(
            "JARVIS lists these folders every 30 seconds on this PC (it never opens your "
            "files) and tells you about new or changed files. Workflows can start from a "
            "folder too."
        )
        intro.setWordWrap(True)
        intro.setObjectName("hint")
        layout.addWidget(intro)
        self.list = QListWidget()
        self.list.setWordWrap(True)
        layout.addWidget(self.list, 1)

        form = QFormLayout()
        row = QWidget()
        row_box = QHBoxLayout(row)
        row_box.setContentsMargins(0, 0, 0, 0)
        self.folder = QLineEdit()
        self.folder.setPlaceholderText("downloads, documents/Projects, or a full path")
        row_box.addWidget(self.folder, 1)
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse)
        row_box.addWidget(browse)
        form.addRow("Folder", row)
        self.pattern = QLineEdit("*")
        self.pattern.setPlaceholderText("*.pdf")
        form.addRow("Files", self.pattern)
        self.events = QComboBox()
        self.events.addItem("New files", ["new"])
        self.events.addItem("Changed files", ["changed"])
        self.events.addItem("New and changed files", ["new", "changed"])
        form.addRow("Tell me about", self.events)
        self.subfolders = QCheckBox("Include subfolders")
        form.addRow("", self.subfolders)
        layout.addLayout(form)
        buttons = QHBoxLayout()
        self.add_button = QPushButton("Watch this folder")
        self.add_button.setObjectName("primary")
        self.add_button.clicked.connect(lambda: self.spawn(self.add()))
        self.remove_button = QPushButton("Stop watching")
        self.remove_button.setObjectName("danger")
        self.remove_button.clicked.connect(lambda: self.spawn(self.remove_selected()))
        buttons.addWidget(self.add_button)
        buttons.addWidget(self.remove_button)
        buttons.addStretch(1)
        layout.addLayout(buttons)
        self.message = QLabel("")
        self.message.setObjectName("problem")
        self.message.setWordWrap(True)
        layout.addWidget(self.message)

        self.options: dict[str, QCheckBox] = {}
        for key, label in (
            ("deadline_alerts", "Deadline alerts (a day before, an hour before, when overdue)"),
            ("calendar_alerts", "Calendar heads-up before events"),
        ):
            check = QCheckBox(label)
            check.setChecked((options or {}).get(key, True))
            if on_option is not None:
                check.toggled.connect(lambda on, k=key: on_option(k, on))
            self.options[key] = check
            layout.addWidget(check)

    def spawn(self, coro: Any) -> None:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _browse(self) -> None:
        start = self.folder.text() if Path(self.folder.text()).is_dir() else str(Path.home())
        chosen = QFileDialog.getExistingDirectory(self, "Folder to watch", start)
        if chosen:
            self.folder.setText(chosen)

    async def refresh(self) -> None:
        self.list.clear()
        for watch in await self._load():
            item = QListWidgetItem(watch.describe())
            item.setData(Qt.ItemDataRole.UserRole, watch.id)
            self.list.addItem(item)
        if not self.list.count():
            self.message.setText("Nothing is watched yet.")

    async def add(self) -> None:
        folder = self.folder.text().strip()
        if not folder:
            self.message.setText("Choose a folder first.")
            return
        problem = await self._add(
            folder,
            self.pattern.text().strip() or "*",
            list(self.events.currentData()),
            self.subfolders.isChecked(),
        )
        if problem:
            self.message.setText(problem)
            return
        self.folder.clear()
        await self.refresh()
        self.message.setText("Watching it now.")

    async def remove_selected(self) -> None:
        item = self.list.currentItem()
        if item is None:
            self.message.setText("Pick a watched folder first.")
            return
        await self._remove(str(item.data(Qt.ItemDataRole.UserRole)))
        await self.refresh()
        self.message.setText("Stopped watching it.")


class AutomationWindow(QWidget):
    """Workflows and Watchtower in one window."""

    def __init__(self, builder: WorkflowBuilder, watchtower: WatchtowerPanel) -> None:
        super().__init__()
        self.builder, self.watchtower = builder, watchtower
        self.setWindowTitle("JARVIS · Workflows and Watchtower")
        self.setStyleSheet(STYLE)
        self.resize(1060, 720)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.tabs = QTabWidget()
        self.tabs.addTab(builder, "Workflows")
        self.tabs.addTab(watchtower, "Watchtower")
        layout.addWidget(self.tabs)

    def open(self, tab: str = "workflows") -> None:
        self.builder.refresh()
        self.watchtower.spawn(self.watchtower.refresh())
        self.tabs.setCurrentIndex(1 if tab == "watchtower" else 0)
        self.show()
        self.raise_()
        self.activateWindow()
