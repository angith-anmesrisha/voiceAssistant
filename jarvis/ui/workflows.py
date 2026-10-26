"""The workflow builder: a node canvas for making, testing and saving workflows.

Left: your workflows. Middle: the canvas (see :mod:`jarvis.ui.canvas`), with the
trigger on the left and each step as a node; the + at the top right (or at the end of
any open output, or Tab) opens the node panel with Gmail, Calendar, Notes, Tasks, AI,
web, apps, PC and flow nodes. Right: the selected node's settings, applied as you type
(or, with nothing selected, the workflow's own details). Bottom: zoom, tidy-up and
undo, the Test workflow button, and a log of the last test, while each node lights up
as it runs. Nothing is kept until you press Save.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from PySide6.QtCore import QRectF, QSize, Qt, Signal
from PySide6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QFontMetricsF,
    QIcon,
    QKeySequence,
    QPainter,
    QPaintEvent,
    QPen,
    QShortcut,
)
from PySide6.QtWidgets import (
    QAbstractButton,
    QCheckBox,
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QStyle,
    QStyledItemDelegate,
    QVBoxLayout,
    QWidget,
)

from jarvis.agent.workflows import (
    ACTIONS,
    DAYS,
    GROUPS,
    MAX_STEPS,
    TRIGGER_ID,
    TRIGGER_TITLES,
    TRIGGERS,
    Link,
    Setting,
    Step,
    Trigger,
    Workflow,
    WorkflowStore,
    parse_clock,
    run_order,
    templates,
    would_loop,
)
from jarvis.app.events import PlanUpdated, StepStatus
from jarvis.ui import theme
from jarvis.ui.canvas import FlowScene, FlowView, NodeView, snap
from jarvis.ui.connections import ConnectDialog, Connections, LoginsDialog
from jarvis.ui.node_icons import GROUP_STYLE, accent, icon, paint_icon

STYLE = f"""
QWidget {{ background: {theme.css(theme.BG)}; color: {theme.css(theme.TEXT)};
  font-family: "{theme.BODY_FAMILY}"; font-size: 10pt; }}
QLabel {{ background: transparent; }}
QLabel#title {{ font-family: "{theme.TITLE_FAMILY}"; font-size: 12pt;
  color: {theme.css(theme.CYAN)}; letter-spacing: 2px; }}
QLabel#section {{ font-family: "{theme.LABEL_FAMILY}"; font-size: 10.5pt; font-weight: 600;
  color: {theme.css(theme.TEXT_DIM)}; letter-spacing: 1.5px; }}
QLabel#hint {{ color: {theme.css(theme.TEXT_MUTED)}; font-size: 9pt; }}
QLabel#problem {{ color: {theme.css(theme.AMBER)}; }}
QLabel#saved {{ color: {theme.css(theme.TEXT_DIM)}; font-family: "{theme.LABEL_FAMILY}";
  font-size: 10.5pt; letter-spacing: 1px; }}
QLabel#unsaved {{ color: {theme.css(theme.AMBER)}; font-family: "{theme.LABEL_FAMILY}";
  font-size: 10.5pt; letter-spacing: 1px; }}
QLineEdit, QPlainTextEdit, QComboBox, QDoubleSpinBox, QListWidget {{
  background: {theme.css(theme.BG_DEEP)}; border: 1px solid {theme.css(theme.rgba(theme.CYAN, 55))};
  border-radius: 6px; padding: 4px 6px; selection-background-color: {theme.css(theme.DEEP_BLUE)}; }}
QLineEdit:focus, QPlainTextEdit:focus, QComboBox:focus {{
  border-color: {theme.css(theme.rgba(theme.CYAN, 170))}; }}
QLineEdit#name {{ font-family: "{theme.TITLE_FAMILY}"; font-size: 12pt; letter-spacing: 1px;
  background: transparent; border: 1px solid transparent; color: {theme.css(theme.ICE)}; }}
QLineEdit#name:hover {{ border-color: {theme.css(theme.rgba(theme.CYAN, 50))}; }}
QLineEdit#name:focus {{ border-color: {theme.css(theme.rgba(theme.CYAN, 150))};
  background: {theme.css(theme.BG_DEEP)}; }}
QListWidget {{ outline: 0; }}
QListWidget::item {{ padding: 7px 6px; border-radius: 6px; }}
QListWidget::item:selected {{ background: {theme.css(theme.rgba(theme.ELECTRIC, 80))};
  color: {theme.css(theme.ICE)}; }}
QListWidget#plain {{ background: transparent; border: none; }}
QPushButton {{ background: {theme.css(theme.rgba(theme.CYAN, 24))};
  border: 1px solid {theme.css(theme.rgba(theme.CYAN, 100))}; border-radius: 6px;
  padding: 5px 12px; }}
QPushButton:hover {{ background: {theme.css(theme.rgba(theme.CYAN, 60))}; }}
QPushButton:disabled {{ color: {theme.css(theme.TEXT_MUTED)};
  border-color: {theme.css(theme.rgba(theme.STEEL, 80))}; }}
QPushButton#primary {{ background: {theme.css(theme.rgba(theme.ELECTRIC, 120))};
  font-weight: 600; }}
QPushButton#primary:hover {{ background: {theme.css(theme.rgba(theme.ELECTRIC, 190))}; }}
QPushButton#danger {{ border-color: {theme.css(theme.rgba(theme.DANGER, 140))}; }}
QPushButton#tool {{ background: {theme.css(theme.rgba(theme.BG, 235))};
  border: 1px solid {theme.css(theme.rgba(theme.STEEL, 120))}; border-radius: 7px;
  padding: 0; font-size: 12pt; }}
QPushButton#tool:hover {{ border-color: {theme.css(theme.CYAN)}; }}
QPushButton#run {{ background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
  stop:0 {theme.css(theme.rgba(theme.AMBER, 235))}, stop:1 rgba(255, 128, 64, 0.92));
  color: {theme.css(theme.BG_DEEP)}; border: none; border-radius: 19px; padding: 8px 24px;
  font-family: "{theme.LABEL_FAMILY}"; font-size: 12pt; font-weight: 700; letter-spacing: 1px; }}
QPushButton#run:hover {{ background: {theme.css(theme.AMBER)}; }}
QPushButton#run:disabled {{ background: {theme.css(theme.rgba(theme.STEEL, 120))};
  color: {theme.css(theme.TEXT_DIM)}; }}
QPushButton#add {{ background: {theme.css(theme.rgba(theme.ELECTRIC, 150))}; border: none;
  border-radius: 8px; font-size: 16pt; font-weight: 600; padding: 0; }}
QPushButton#add:hover {{ background: {theme.css(theme.ELECTRIC)}; }}
QCheckBox {{ background: transparent; }}
QCheckBox::indicator {{ width: 14px; height: 14px; border-radius: 3px;
  border: 1px solid {theme.css(theme.rgba(theme.CYAN, 130))};
  background: {theme.css(theme.BG_DEEP)}; }}
QCheckBox::indicator:checked {{ background: {theme.css(theme.ELECTRIC)};
  border-color: {theme.css(theme.CYAN)}; }}
QFrame#panel {{ background: {theme.css(theme.rgba(theme.BG, 245))};
  border-left: 1px solid {theme.css(theme.rgba(theme.CYAN, 45))}; }}
QFrame#sidebar {{ background: {theme.css(theme.BG)};
  border-right: 1px solid {theme.css(theme.rgba(theme.CYAN, 35))}; }}
QFrame#topbar {{ background: {theme.css(theme.BG)};
  border-bottom: 1px solid {theme.css(theme.rgba(theme.CYAN, 35))}; }}
QFrame#logs {{ background: {theme.css(theme.rgba(theme.BG, 245))};
  border-top: 1px solid {theme.css(theme.rgba(theme.CYAN, 40))}; }}
QFrame#card {{ background: {theme.css(theme.rgba(theme.PANEL_TOP, 120))};
  border: 1px solid {theme.css(theme.rgba(theme.CYAN, 50))}; border-radius: 12px; }}
QFrame#card:hover {{ border-color: {theme.css(theme.rgba(theme.CYAN, 160))}; }}
QFrame#group {{ background: {theme.css(theme.rgba(theme.PANEL_TOP, 90))};
  border: 1px solid {theme.css(theme.rgba(theme.CYAN, 38))}; border-radius: 10px; }}
QFrame#connect {{ background: {theme.css(theme.rgba(theme.PANEL_TOP, 110))};
  border: 1px solid {theme.css(theme.rgba(theme.AMBER, 120))}; border-radius: 10px; }}
QFrame#connected {{ background: {theme.css(theme.rgba(theme.PANEL_TOP, 110))};
  border: 1px solid {theme.css(theme.rgba(theme.CYAN, 110))}; border-radius: 10px; }}
QFrame#pill {{ background: {theme.css(theme.rgba(theme.BG, 235))};
  border: 1px solid {theme.css(theme.rgba(theme.STEEL, 120))}; border-radius: 17px; }}
QFrame#pill QPushButton {{ background: transparent; border: none; border-radius: 15px;
  padding: 0; font-size: 12pt; }}
QFrame#pill QPushButton:hover {{ background: {theme.css(theme.rgba(theme.CYAN, 50))}; }}
QLabel#chip {{ background: {theme.css(theme.BG_DEEP)}; color: {theme.css(theme.ICE)};
  border: 1px solid {theme.css(theme.rgba(theme.STEEL, 140))}; border-radius: 5px;
  padding: 1px 7px; font-family: "{theme.LABEL_FAMILY}"; font-size: 9.5pt; }}
QLabel#help {{ color: {theme.css(theme.TEXT_DIM)}; font-size: 9pt;
  border: 1px solid {theme.css(theme.rgba(theme.STEEL, 120))}; border-radius: 9px;
  padding: 1px 7px; }}
QSplitter::handle {{ background: {theme.css(theme.rgba(theme.CYAN, 22))}; }}
QSplitter::handle:hover {{ background: {theme.css(theme.rgba(theme.CYAN, 120))}; }}
QScrollArea {{ border: none; background: transparent; }}
QScrollBar:vertical {{ background: transparent; width: 8px; }}
QScrollBar::handle:vertical {{ background: {theme.css(theme.rgba(theme.STEEL, 120))};
  border-radius: 4px; min-height: 30px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; }}
QTabWidget::pane {{ border: none; }}
QTabBar::tab {{ background: {theme.css(theme.BG_DEEP)}; color: {theme.css(theme.TEXT_DIM)};
  padding: 8px 18px; border-bottom: 2px solid transparent;
  font-family: "{theme.LABEL_FAMILY}"; font-size: 11pt; letter-spacing: 1px; }}
QTabBar::tab:selected {{ color: {theme.css(theme.CYAN)};
  border-bottom: 2px solid {theme.css(theme.CYAN)}; }}
QMenu {{ background: {theme.css(theme.BG_DEEP)}; border: 1px solid {theme.css(theme.STEEL)}; }}
QMenu::item:selected {{ background: {theme.css(theme.rgba(theme.ELECTRIC, 90))}; }}
QToolTip {{ background: {theme.css(theme.BG_DEEP)}; color: {theme.css(theme.TEXT)};
  border: 1px solid {theme.css(theme.rgba(theme.CYAN, 120))}; }}
"""

_FOLDER_HINT = "desktop, documents, downloads… or a full path"
_TRIGGER_ICONS = {
    "phrase": "mic",
    "time": "timer",
    "startup": "power",
    "folder": "folder",
    "manual": "hand",
}
_TRIGGER_NAMES = {
    "phrase": "When I say…",
    "time": "On a schedule",
    "startup": "When JARVIS starts",
    "folder": "When a file arrives",
    "manual": "When I run it",
}
_TIP = (
    "In text, {previous} is the result of the node before, {step 2} the result of node 2, "
    "{today} today's date, and {file} the file that started a folder workflow."
)
_SHORTCUT_HELP = (
    ("Tab", "Add a node"),
    ("Del", "Delete the selection"),
    ("Ctrl+D", "Duplicate a node"),
    ("Ctrl+Z", "Undo (Ctrl+Shift+Z redoes)"),
    ("Ctrl+S", "Save"),
    ("F", "Fit the view"),
)
_SERVICE_TITLES = {
    "gmail": "Gmail account",
    "calendar": "Calendar feed",
    "telegram": "Telegram bot",
    "logins": "Saved logins",
}
_COLUMN = 180.0  # the canvas's distance between columns...
_ROW = 150.0  # ... and rows


def problems(workflow: Workflow) -> list[str]:
    """What stops ``workflow`` from being saved (empty when it's fine)."""
    found = []
    if not workflow.name.strip():
        found.append("Give it a name.")
    trigger = workflow.trigger
    if trigger.kind == "phrase" and not [p for p in trigger.phrases if p.strip()]:
        found.append("Add at least one phrase that starts it.")
    if trigger.kind == "time" and parse_clock(trigger.at) is None:
        found.append("Set the time as HH:MM, e.g. 07:30.")
    if trigger.kind == "folder" and not trigger.folder.strip():
        found.append("Choose the folder to watch.")
    if not workflow.steps:
        found.append("Add at least one step.")
    elif not run_order(workflow):
        found.append("Link the trigger to the first step.")
    for i, step in enumerate(workflow.steps, 1):
        spec = ACTIONS.get(step.action)
        if spec is None:
            found.append(f"Step {i} has an unknown action.")
            continue
        for setting in spec.settings:
            value = step.settings.get(setting.name)
            if setting.required and (value is None or value == "" or value == []):
                found.append(f"Step {i} ({spec.title}) needs “{setting.label}”.")
        if step.action == "condition":
            test = str(step.settings.get("test") or "contains")
            needs = test in (
                "contains",
                "doesn't contain",
                "equals",
                "number above",
                "number below",
            )
            if needs and not str(step.settings.get("compare") or "").strip():
                found.append(f"Step {i} (If) needs a value to compare with.")
    return found


def warnings(workflow: Workflow) -> list[str]:
    """What's odd but allowed (steps that won't run)."""
    order = set(run_order(workflow))
    return [
        f"Step {i} ({ACTIONS[s.action].title if s.action in ACTIONS else s.action}) isn't "
        "linked, so it won't run."
        for i, s in enumerate(workflow.steps, 1)
        if i - 1 not in order
    ]


def layout(workflow: Workflow) -> None:
    """Place every node in columns by how far it is from the trigger (Tidy up)."""
    links = workflow.graph()
    depth: dict[str, int] = {TRIGGER_ID: 0}
    for i in run_order(workflow):
        step = workflow.steps[i]
        before = [depth[link.src] for link in links if link.dst == step.id and link.src in depth]
        depth[step.id] = max(before, default=0) + 1
    columns: dict[int, list[Step]] = {}
    for step in workflow.steps:
        if step.id in depth:
            columns.setdefault(depth[step.id], []).append(step)
    workflow.trigger.x, workflow.trigger.y = 0.0, 0.0
    rows: dict[str, float] = {TRIGGER_ID: 0.0}

    def parent_row(step: Step) -> tuple[float, int]:
        for link in links:
            if link.dst == step.id and link.src in rows:
                source = workflow.step(link.src)
                outputs = source.outputs if source is not None else ("out",)
                return rows[link.src], outputs.index(link.port) if link.port in outputs else 0
        return 0.0, 0

    for column in sorted(columns):
        steps = sorted(columns[column], key=parent_row)
        middle = sum(parent_row(s)[0] for s in steps) / len(steps)
        top = middle - (len(steps) - 1) * _ROW / 2
        for n, step in enumerate(steps):
            step.x, step.y = column * _COLUMN, snap(top + n * _ROW)
        for step in steps:
            rows[step.id] = step.y or 0.0
    loose = [step for step in workflow.steps if step.id not in depth]
    bottom = max([s.y or 0.0 for s in workflow.steps if s.id in depth] + [0.0]) + _ROW * 1.3
    for n, step in enumerate(loose):
        step.x, step.y = (n + 1) * _COLUMN, snap(bottom)


def setting_field(
    setting: Setting,
    value: Any,
    parent: QWidget,
    changed: Callable[[], None],
    logins: list[str] | None = None,
) -> tuple[QWidget, Callable[[], Any]]:
    """An input for one setting and a function that reads it; ``changed`` is called on
    every edit. ``logins``: the saved login names, for a "login" setting."""
    current = setting.default if value is None else value
    if setting.kind == "login":
        names = QComboBox()
        names.setEditable(True)
        names.addItems(logins or [])
        names.setCurrentText(str(current or ""))
        line = names.lineEdit()
        if line is not None:
            line.setPlaceholderText("Pick a saved login")
        names.currentTextChanged.connect(lambda _text: changed())
        return names, lambda: names.currentText().strip()
    if setting.kind in ("long", "list"):
        box = QPlainTextEdit()
        text = "\n".join(map(str, current)) if isinstance(current, list) else str(current)
        box.setPlainText(text)
        box.setFixedHeight(84 if setting.kind == "long" else 104)
        if setting.hint:
            box.setPlaceholderText(setting.hint)
        box.textChanged.connect(changed)
        if setting.kind == "list":
            return box, lambda: [
                line.strip() for line in box.toPlainText().splitlines() if line.strip()
            ]
        return box, box.toPlainText
    if setting.kind == "number":
        spin = QDoubleSpinBox()
        spin.setRange(0, 100000)
        spin.setDecimals(0)
        try:
            spin.setValue(float(current or 0))
        except (TypeError, ValueError):
            spin.setValue(0)
        spin.valueChanged.connect(changed)
        return spin, lambda: int(spin.value())
    if setting.kind == "choice":
        combo = QComboBox()
        combo.addItems(list(setting.choices))
        if str(current) in setting.choices:
            combo.setCurrentText(str(current))
        combo.currentIndexChanged.connect(changed)
        return combo, combo.currentText
    if setting.kind == "bool":
        check = QCheckBox("Yes")
        check.setChecked(bool(current))
        check.toggled.connect(changed)
        return check, check.isChecked
    line = QLineEdit(str(current or ""))
    line.setPlaceholderText(setting.hint or (_FOLDER_HINT if setting.kind == "folder" else ""))
    line.textEdited.connect(changed)
    if setting.kind not in ("folder", "file"):
        return line, line.text
    row = QWidget()
    row_box = QHBoxLayout(row)
    row_box.setContentsMargins(0, 0, 0, 0)
    row_box.addWidget(line, 1)
    browse = QPushButton("…")
    browse.setFixedWidth(34)
    browse.setToolTip("Browse")
    row_box.addWidget(browse)

    def pick() -> None:
        start = line.text() if Path(line.text()).exists() else str(Path.home())
        if setting.kind == "folder":
            chosen = QFileDialog.getExistingDirectory(parent, setting.label, start)
        else:
            chosen = QFileDialog.getOpenFileName(parent, setting.label, start)[0]
        if chosen:
            line.setText(chosen)
            changed()

    browse.clicked.connect(pick)
    return row, line.text


class ListLook(QStyledItemDelegate):
    """List rows as an icon, a name and a dim second line (group headers in their colour)."""

    def paint(self, painter: QPainter, option: Any, index: Any) -> None:
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(option.rect).adjusted(2, 2, -2, -2)
        text = str(index.data(Qt.ItemDataRole.DisplayRole) or "")
        if not index.flags() & Qt.ItemFlag.ItemIsEnabled:  # a group header
            brush = index.data(Qt.ItemDataRole.ForegroundRole)
            painter.setPen(brush.color() if isinstance(brush, QBrush) else theme.TEXT_DIM)
            painter.setFont(theme.label_font(9.5))
            painter.drawText(
                rect.adjusted(8, 0, 0, -4),
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignBottom,
                text,
            )
            painter.restore()
            return
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        hover = bool(option.state & QStyle.StateFlag.State_MouseOver)
        if selected or hover:
            painter.setPen(QPen(theme.rgba(theme.CYAN, 120), 1) if selected else Qt.PenStyle.NoPen)
            painter.setBrush(
                theme.rgba(theme.ELECTRIC, 70) if selected else theme.rgba(theme.CYAN, 18)
            )
            painter.drawRoundedRect(rect, 7, 7)
        left = rect.left() + 8
        decoration = index.data(Qt.ItemDataRole.DecorationRole)
        if isinstance(decoration, QIcon):
            side = 24
            decoration.paint(painter, int(left), int(rect.center().y() - side / 2), side, side)
            left += side + 10
        title, _, sub = text.partition("\n")
        width = rect.right() - left - 6
        title_font = theme.font(theme.LABEL_FAMILY, 11, QFont.Weight.Bold, 0.3)
        sub_font = theme.font(theme.BODY_FAMILY, 8.2)
        painter.setFont(title_font)
        painter.setPen(theme.ICE if selected else theme.TEXT)
        title = QFontMetricsF(title_font).elidedText(title, Qt.TextElideMode.ElideRight, width)
        top = rect.top() + (6 if sub else (rect.height() - 20) / 2)
        painter.drawText(
            QRectF(left, top, width, 20),
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            title,
        )
        if sub:
            painter.setFont(sub_font)
            painter.setPen(theme.TEXT_DIM)
            metrics = QFontMetricsF(sub_font)
            lines = [metrics.elidedText(" ".join(sub.split()), Qt.TextElideMode.ElideRight, width)]
            painter.drawText(
                QRectF(left, top + 20, width, 16),
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                lines[0],
            )
        painter.restore()

    def sizeHint(self, option: Any, index: Any) -> QSize:  # noqa: N802
        if not index.flags() & Qt.ItemFlag.ItemIsEnabled:
            return QSize(100, 32)
        text = str(index.data(Qt.ItemDataRole.DisplayRole) or "")
        return QSize(100, 50 if "\n" in text else 36)


class ToggleSwitch(QAbstractButton):
    """A small on/off switch (the workflow's Active toggle)."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedSize(40, 22)

    def sizeHint(self) -> QSize:  # noqa: N802
        return QSize(40, 22)

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        on = self.isChecked()
        track = QRectF(1, 1, self.width() - 2, self.height() - 2)
        painter.setPen(QPen(theme.rgba(theme.CYAN, 160 if on else 70), 1))
        painter.setBrush(theme.rgba(theme.ELECTRIC, 200) if on else theme.BG_DEEP)
        painter.drawRoundedRect(track, track.height() / 2, track.height() / 2)
        knob = track.height() - 6
        x = track.right() - knob - 3 if on else track.left() + 3
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(theme.ICE if on else theme.TEXT_DIM)
        painter.drawEllipse(QRectF(x, track.top() + 3, knob, knob))
        painter.end()


class NodePanel(QFrame):
    """The + panel: every kind of node, by group, with a search box."""

    chosen = Signal(str)

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setObjectName("panel")
        self.setFixedWidth(330)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        head = QHBoxLayout()
        title = QLabel("WHAT HAPPENS NEXT?")
        title.setObjectName("title")
        head.addWidget(title, 1)
        close = QPushButton("✕")
        close.setObjectName("tool")
        close.setFixedSize(28, 28)
        close.clicked.connect(self.hide)
        head.addWidget(close)
        layout.addLayout(head)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search nodes… (email, calendar, note, if)")
        self.search.textChanged.connect(self._filter)
        self.search.returnPressed.connect(self._pick_first)
        layout.addWidget(self.search)
        self.list = QListWidget()
        self.list.setObjectName("plain")
        self.list.setItemDelegate(ListLook(self.list))
        self.list.setMouseTracking(True)
        self.list.itemClicked.connect(self._activated)
        layout.addWidget(self.list, 1)
        self._fill()
        self.hide()

    def _fill(self) -> None:
        for group in GROUPS:
            colour = accent(group)
            header = QListWidgetItem(group.upper())
            header.setFlags(Qt.ItemFlag.NoItemFlags)
            header.setForeground(colour)
            header.setFont(theme.label_font(10))
            header.setData(Qt.ItemDataRole.UserRole + 1, group)
            self.list.addItem(header)
            for action in ACTIONS.values():
                if action.group != group:
                    continue
                text = action.title + (f"\n{action.blurb}" if action.blurb else "")
                item = QListWidgetItem(icon(action.icon or GROUP_STYLE[group][1], colour), text)
                item.setData(Qt.ItemDataRole.UserRole, action.name)
                item.setData(Qt.ItemDataRole.UserRole + 1, group)
                item.setToolTip(action.blurb or action.title)
                self.list.addItem(item)

    def open(self) -> None:
        self.search.clear()
        self._filter("")
        self.show()
        self.raise_()
        self.search.setFocus()

    def _filter(self, text: str) -> None:
        words = text.lower().split()
        groups_shown: set[str] = set()
        for i in range(self.list.count()):
            item = self.list.item(i)
            name = item.data(Qt.ItemDataRole.UserRole)
            if not name:
                continue
            spec = ACTIONS[name]
            haystack = f"{spec.title} {spec.blurb} {spec.group} {spec.name}".lower()
            hit = all(word in haystack for word in words)
            item.setHidden(not hit)
            if hit:
                groups_shown.add(spec.group)
        for i in range(self.list.count()):
            item = self.list.item(i)
            if not item.data(Qt.ItemDataRole.UserRole):
                item.setHidden(item.data(Qt.ItemDataRole.UserRole + 1) not in groups_shown)

    def visible_names(self) -> list[str]:
        return [
            str(self.list.item(i).data(Qt.ItemDataRole.UserRole))
            for i in range(self.list.count())
            if self.list.item(i).data(Qt.ItemDataRole.UserRole) and not self.list.item(i).isHidden()
        ]

    def _pick_first(self) -> None:
        names = self.visible_names()
        if names:
            self.hide()
            self.chosen.emit(names[0])

    def _activated(self, item: QListWidgetItem) -> None:
        name = item.data(Qt.ItemDataRole.UserRole)
        if name:
            self.hide()
            self.chosen.emit(str(name))

    def keyPressEvent(self, event: Any) -> None:  # noqa: N802
        if event.key() == Qt.Key.Key_Escape:
            self.hide()
            return
        super().keyPressEvent(event)


class IconBadge(QWidget):
    """A node's icon in its accent colour, as on the canvas."""

    def __init__(self, name: str, colour: QColor, size: int = 40) -> None:
        super().__init__()
        self._name, self._colour = name, colour
        self.setFixedSize(size, size)

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(1, 1, self.width() - 2, self.height() - 2)
        painter.setPen(QPen(theme.rgba(self._colour, 150), 1.2))
        painter.setBrush(theme.rgba(theme.PANEL_TOP, 200))
        painter.drawRoundedRect(rect, 9, 9)
        inset = rect.width() * 0.2
        paint_icon(painter, self._name, rect.adjusted(inset, inset, -inset, -inset), self._colour)
        painter.end()


class Inspector(QFrame):
    """The right-hand panel: the selected node's settings, the trigger's, or the
    workflow's own details. Every edit is passed on straight away."""

    def __init__(self, builder: WorkflowBuilder) -> None:
        super().__init__()
        self._builder = builder
        self.setObjectName("panel")
        self.setMinimumWidth(280)  # the splitter handle on its left resizes it
        self.setMaximumWidth(720)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self.area = QScrollArea()
        self.area.setWidgetResizable(True)
        self.area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        outer.addWidget(self.area)
        self.readers: dict[str, Callable[[], Any]] = {}
        self.fields: dict[str, QWidget] = {}
        self.showing = ""  # "", "trigger" or a step id
        self._body = QWidget()
        self.area.setWidget(self._body)

    def _fresh(self) -> QVBoxLayout:
        self.readers, self.fields = {}, {}
        self._body = QWidget()
        layout = QVBoxLayout(self._body)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(10)
        self.area.setWidget(self._body)
        return layout

    def _header(self, layout: QVBoxLayout, name: str, colour: QColor, title: str, sub: str) -> None:
        row = QHBoxLayout()
        row.setSpacing(12)
        row.addWidget(IconBadge(name, colour, 44))
        words = QVBoxLayout()
        words.setSpacing(0)
        heading = QLabel(title)
        heading.setFont(theme.font(theme.LABEL_FAMILY, 13.5, QFont.Weight.Bold, 0.5))
        heading.setWordWrap(True)
        words.addWidget(heading)
        caption = QLabel(sub.upper())
        caption.setFont(theme.label_font(8.5))
        caption.setStyleSheet(f"color: {theme.css(colour)};")
        words.addWidget(caption)
        row.addLayout(words, 1)
        layout.addLayout(row)

    def _label(self, text: str) -> QLabel:
        label = QLabel(text)
        label.setObjectName("section")
        return label

    def _hint(self, layout: QVBoxLayout, text: str) -> None:
        label = QLabel(text)
        label.setWordWrap(True)
        label.setObjectName("hint")
        layout.addWidget(label)

    def _group(self, layout: QVBoxLayout, title: str) -> QFormLayout:
        """A rounded card of fields under a small heading."""
        box = QFrame()
        box.setObjectName("group")
        inner = QVBoxLayout(box)
        inner.setContentsMargins(12, 10, 12, 12)
        inner.setSpacing(6)
        inner.addWidget(self._label(title))
        form = QFormLayout()
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapAllRows)
        form.setVerticalSpacing(8)
        inner.addLayout(form)
        layout.addWidget(box)
        return form

    def _connection(self, layout: QVBoxLayout, service: str) -> None:
        """The "Connect" card for a node that needs a service set up once."""
        connections = self._builder.connections
        if connections is None:
            return
        on = connections.connected(service)
        card = QFrame()
        card.setObjectName("connected" if on else "connect")
        row = QHBoxLayout(card)
        row.setContentsMargins(12, 9, 10, 9)
        dot = QLabel("●")
        dot.setStyleSheet(f"color: {theme.css(theme.CYAN if on else theme.AMBER)};")
        row.addWidget(dot)
        words = QVBoxLayout()
        words.setSpacing(0)
        what = QLabel(_SERVICE_TITLES.get(service, service.title()))
        what.setFont(theme.font(theme.LABEL_FAMILY, 11, QFont.Weight.Bold, 0.5))
        words.addWidget(what)
        status = QLabel(connections.status(service))
        status.setObjectName("hint")
        status.setWordWrap(True)
        words.addWidget(status)
        row.addLayout(words, 1)
        button = QPushButton("Manage" if service == "logins" else ("Change" if on else "Connect"))
        if not on:
            button.setObjectName("primary")
        button.clicked.connect(lambda: self._builder.open_connection(service))
        row.addWidget(button)
        self.fields["connection"] = button
        layout.addWidget(card)

    def show_step(self, workflow: Workflow, step: Step) -> None:
        self.showing = step.id
        layout = self._fresh()
        spec = ACTIONS.get(step.action)
        if spec is None:
            layout.addWidget(QLabel(f"Unknown action “{step.action}”."))
            layout.addStretch(1)
            return
        number = workflow.index(step.id) + 1
        colour = accent(spec.group)
        name = spec.icon or GROUP_STYLE[spec.group][1]
        self._header(layout, name, colour, spec.title, f"Node {number} · {spec.group}")
        if spec.blurb:
            self._hint(layout, spec.blurb)
        if spec.connection:
            self._connection(layout, spec.connection)
        connections = self._builder.connections
        logins = connections.login_names() if connections is not None else []
        if spec.settings:
            form = self._group(layout, "SETTINGS")
            for setting in spec.settings:
                widget, read = setting_field(
                    setting,
                    step.settings.get(setting.name),
                    self,
                    self._changed_by(setting.name),
                    logins,
                )
                label = self._label(setting.label + (" *" if setting.required else ""))
                label.setObjectName("hint")
                if setting.kind not in ("text", "login", "folder", "file") and setting.hint:
                    label.setToolTip(setting.hint)
                form.addRow(label, widget)
                if setting.kind == "list" and setting.hint:
                    tip = QLabel(setting.hint)
                    tip.setObjectName("hint")
                    tip.setWordWrap(True)
                    form.addRow(tip)
                self.readers[setting.name] = read
                self.fields[setting.name] = widget
        else:
            self._hint(layout, "Nothing to set: it just runs.")
        if spec.outputs != ("out",):
            self._hint(
                layout,
                f"Two outputs, {spec.outputs[0]} and {spec.outputs[1]}: link each to what "
                "should happen next. One with nothing linked ends there.",
            )
        tips = QHBoxLayout()
        variables = QLabel("{ } Variables")
        variables.setObjectName("help")
        variables.setToolTip(_TIP)
        tips.addWidget(variables)
        tips.addStretch(1)
        duplicate = QPushButton("Duplicate")
        duplicate.clicked.connect(lambda: self._builder.duplicate_node(step.id))
        remove = QPushButton("Delete")
        remove.setObjectName("danger")
        remove.clicked.connect(lambda: self._builder.remove_node(step.id))
        tips.addWidget(duplicate)
        tips.addWidget(remove)
        layout.addLayout(tips)
        layout.addStretch(1)

    def show_trigger(self, workflow: Workflow) -> None:
        self.showing = "trigger"
        layout = self._fresh()
        trigger = workflow.trigger
        self._header(
            layout,
            _TRIGGER_ICONS.get(trigger.kind, "bolt"),
            theme.AMBER,
            _TRIGGER_NAMES.get(trigger.kind, "Trigger"),
            "Trigger · starts the workflow",
        )
        form = QFormLayout()
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapAllRows)
        kind = QComboBox()
        for name in TRIGGERS:
            kind.addItem(icon(_TRIGGER_ICONS[name], theme.AMBER), TRIGGER_TITLES[name], name)
        kind.setCurrentIndex(TRIGGERS.index(trigger.kind) if trigger.kind in TRIGGERS else 0)
        kind.currentIndexChanged.connect(
            lambda: self._builder.set_trigger(kind=str(kind.currentData()))
        )
        self.fields["kind"] = kind
        form.addRow(self._label("Starts"), kind)
        if trigger.kind == "phrase":
            phrases = QPlainTextEdit("\n".join(trigger.phrases))
            phrases.setPlaceholderText("One per line, e.g.\nactivate study mode\nstudy time")
            phrases.setFixedHeight(90)
            phrases.textChanged.connect(
                lambda: self._builder.set_trigger(
                    phrases=[p.strip() for p in phrases.toPlainText().splitlines() if p.strip()]
                )
            )
            self.fields["phrases"] = phrases
            form.addRow(self._label("Phrases"), phrases)
        elif trigger.kind == "time":
            at = QLineEdit(trigger.at)
            at.setPlaceholderText("07:30")
            at.textEdited.connect(lambda text: self._builder.set_trigger(at=text.strip()))
            self.fields["at"] = at
            form.addRow(self._label("Time"), at)
            days_row = QWidget()
            days_box = QGridLayout(days_row)
            days_box.setContentsMargins(0, 0, 0, 0)
            checks: dict[str, QCheckBox] = {}
            for n, day in enumerate(DAYS):
                check = QCheckBox(day.title())
                check.setChecked(day in trigger.days)
                checks[day] = check
                days_box.addWidget(check, n // 4, n % 4)
            for check in checks.values():
                check.toggled.connect(
                    lambda _on: self._builder.set_trigger(
                        days=[d for d, c in checks.items() if c.isChecked()]
                    )
                )
            self.fields["days"] = days_row
            form.addRow(self._label("Days (none = every day)"), days_row)
        elif trigger.kind == "folder":
            readers: list[Callable[[], Any]] = []
            folder, read = setting_field(
                Setting("folder", "Folder", "folder", True),
                trigger.folder,
                self,
                lambda: self._builder.set_trigger(folder=str(readers[0]()).strip()),
            )
            readers.append(read)
            self.fields["folder"] = folder
            form.addRow(self._label("Folder"), folder)
            pattern = QLineEdit(trigger.pattern)
            pattern.setPlaceholderText("*.pdf")
            pattern.textEdited.connect(
                lambda text: self._builder.set_trigger(pattern=text.strip() or "*")
            )
            form.addRow(self._label("Files"), pattern)
            event = QComboBox()
            event.addItem("New files", "new")
            event.addItem("Changed files", "changed")
            event.setCurrentIndex(0 if trigger.event == "new" else 1)
            event.currentIndexChanged.connect(
                lambda: self._builder.set_trigger(event=str(event.currentData()))
            )
            form.addRow(self._label("When there are"), event)
        layout.addLayout(form)
        self._hint(
            layout,
            {
                "phrase": "Say any of these to JARVIS, or “run the <name> workflow”.",
                "time": "Runs once a day at this time, up to 30 minutes late if the PC slept.",
                "startup": "Runs 10 seconds after JARVIS starts.",
                "folder": "Watched on this PC only. {file} is the file that arrived.",
                "manual": "Runs only from Test, or when you ask JARVIS to run it.",
            }.get(trigger.kind, ""),
        )
        layout.addStretch(1)

    def show_workflow(self, workflow: Workflow | None) -> None:
        self.showing = ""
        layout = self._fresh()
        if workflow is None:
            self._hint(layout, "Make a workflow with New, or start from a template.")
            layout.addStretch(1)
            return
        self._header(layout, "branch", theme.CYAN, workflow.name or "Workflow", "Workflow")
        about = QPlainTextEdit(workflow.description)
        about.setPlaceholderText("What it's for (optional)")
        about.setFixedHeight(72)
        about.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        about.textChanged.connect(
            lambda: self._builder.set_description(about.toPlainText().strip())
        )
        self.fields["description"] = about
        layout.addWidget(self._label("ABOUT"))
        layout.addWidget(about)
        found = problems(workflow) + warnings(workflow)
        layout.addWidget(self._label("TO DO" if found else "READY"))
        for line in found or ["Everything is set. Test it, then Save."]:
            text = QLabel(("• " if found else "") + line)
            text.setWordWrap(True)
            text.setObjectName("problem" if found else "hint")
            layout.addWidget(text)
        layout.addWidget(self._label("SHORTCUTS"))
        keys = QGridLayout()
        keys.setHorizontalSpacing(8)
        keys.setVerticalSpacing(6)
        for n, (key, what) in enumerate(_SHORTCUT_HELP):
            chip = QLabel(key)
            chip.setObjectName("chip")
            keys.addWidget(chip, n, 0, Qt.AlignmentFlag.AlignLeft)
            text = QLabel(what)
            text.setObjectName("hint")
            keys.addWidget(text, n, 1)
        keys.setColumnStretch(1, 1)
        layout.addLayout(keys)
        self._hint(
            layout,
            "Drag from a node's dot to link it, or to empty canvas to add a node there. "
            "Or just tell JARVIS: “Make me a workflow that logs into my college portal "
            "every morning and tells me my timetable.”",
        )
        layout.addStretch(1)

    def read(self) -> dict[str, Any]:
        return {name: read() for name, read in self.readers.items()}

    def _changed_by(self, name: str) -> Callable[[], None]:
        return lambda: self._builder.setting_changed(name)


class TemplateGallery(QDialog):
    """Ready-made workflows as cards; choosing one opens it in the builder."""

    def __init__(self, parent: QWidget, choose: Callable[[str], None]) -> None:
        super().__init__(parent)
        self._choose = choose
        self.setWindowTitle("Workflow templates")
        self.setStyleSheet(STYLE)
        self.resize(840, 640)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        title = QLabel("TEMPLATES")
        title.setObjectName("title")
        layout.addWidget(title)
        intro = QLabel("Start from one of these, then change anything you like before saving.")
        intro.setObjectName("hint")
        layout.addWidget(intro)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        body = QWidget()
        grid = QGridLayout(body)
        grid.setSpacing(14)
        for n, template in enumerate(templates()):
            grid.addWidget(self._card(template), n // 2, n % 2)
        scroll.setWidget(body)
        layout.addWidget(scroll, 1)

    def _card(self, template: Workflow) -> QFrame:
        card = QFrame()
        card.setObjectName("card")
        card.setMinimumHeight(140)
        box = QVBoxLayout(card)
        box.setContentsMargins(14, 12, 14, 12)
        name = QLabel(template.name)
        name.setFont(theme.font(theme.LABEL_FAMILY, 13, QFont.Weight.Bold, 0.5))
        box.addWidget(name)
        about = QLabel(template.description)
        about.setWordWrap(True)
        about.setObjectName("hint")
        box.addWidget(about)
        row = QHBoxLayout()
        row.setSpacing(4)
        row.addWidget(IconBadge(_TRIGGER_ICONS.get(template.trigger.kind, "bolt"), theme.AMBER, 28))
        for i in run_order(template)[:7]:
            spec = ACTIONS.get(template.steps[i].action)
            if spec is not None:
                badge = IconBadge(spec.icon or GROUP_STYLE[spec.group][1], accent(spec.group), 28)
                row.addWidget(badge)
        row.addStretch(1)
        use = QPushButton("Use")
        use.setObjectName("primary")
        use.clicked.connect(lambda: self._use(template.name))
        row.addWidget(use)
        box.addStretch(1)
        box.addLayout(row)
        return card

    def _use(self, name: str) -> None:
        self.accept()
        self._choose(name)


class CanvasPane(QWidget):
    """The canvas with its floating controls: + (top right), zoom/tidy/undo (bottom
    left), Test workflow (bottom centre) and the node panel."""

    def __init__(self, view: FlowView, builder: WorkflowBuilder) -> None:
        super().__init__()
        self.view = view
        self.view.setParent(self)
        self.add_button = QPushButton("+", self)
        self.add_button.setObjectName("add")
        self.add_button.setFixedSize(40, 40)
        self.add_button.setToolTip("Add a node (Tab)")
        self.add_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.tools: list[QPushButton] = []
        self.pill = QFrame(self)
        self.pill.setObjectName("pill")
        pill = QHBoxLayout(self.pill)
        pill.setContentsMargins(4, 3, 4, 3)
        pill.setSpacing(1)
        for text, tip, slot in (
            ("⤢", "Zoom to fit (F)", view.fit),
            ("+", "Zoom in", view.zoom_in),
            ("\u2212", "Zoom out", view.zoom_out),
            ("⊞", "Tidy up", builder.tidy),
            ("↶", "Undo (Ctrl+Z)", builder.undo),
            ("↷", "Redo (Ctrl+Shift+Z)", builder.redo),
        ):
            if len(self.tools) in (3, 4):  # zoom | tidy | undo, redo
                gap = QFrame()
                gap.setFixedSize(1, 18)
                gap.setStyleSheet(f"background: {theme.css(theme.rgba(theme.STEEL, 120))};")
                pill.addWidget(gap)
            button = QPushButton(text)
            button.setFixedSize(30, 30)
            button.setToolTip(tip)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.clicked.connect(slot)
            pill.addWidget(button)
            self.tools.append(button)
        self.pill.adjustSize()
        self.run_button = QPushButton("▶  Test workflow", self)
        self.run_button.setObjectName("run")
        self.run_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.node_panel = NodePanel(self)

    def resizeEvent(self, event: Any) -> None:  # noqa: N802
        w, h = self.width(), self.height()
        self.view.setGeometry(0, 0, w, h)
        self.add_button.move(w - 56, 16)
        self.pill.move(16, h - 54)
        self.run_button.resize(max(self.run_button.sizeHint().width(), 200), 40)
        self.run_button.move((w - self.run_button.width()) // 2, h - 58)
        self.node_panel.setGeometry(w - self.node_panel.width(), 0, self.node_panel.width(), h)
        super().resizeEvent(event)


class WorkflowBuilder(QWidget):
    """The builder. ``on_test`` runs a workflow as it stands (not saved)."""

    def __init__(
        self,
        store: WorkflowStore,
        on_test: Callable[[Workflow], None],
        connections: Connections | None = None,
    ) -> None:
        super().__init__()
        self._store = store
        self._on_test = on_test
        self.connections = connections
        self._current: Workflow | None = None
        self._saved = ""  # the workflow as last saved/opened (JSON), to spot changes
        self._undo: list[str] = []
        self._redo: list[str] = []
        self._last_edit = ""  # typing in one field is one undo step
        self._pending: tuple[str, str, float, float] | None = None  # where + adds
        self._selected = ""
        self._loading = False
        self._run_seen: dict[str, str] = {}
        self.setWindowTitle("JARVIS · Workflows")
        self.setStyleSheet(STYLE)
        self.resize(1280, 780)

        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # Left: the saved workflows.
        sidebar = QFrame()
        sidebar.setObjectName("sidebar")
        sidebar.setMinimumWidth(170)
        sidebar.setMaximumWidth(460)
        left = QVBoxLayout(sidebar)
        left.setContentsMargins(14, 16, 14, 14)
        title = QLabel("WORKFLOWS")
        title.setObjectName("title")
        left.addWidget(title)
        self.new_button = QPushButton("+  New workflow")
        self.new_button.setObjectName("primary")
        self.new_button.clicked.connect(self.new_workflow)
        left.addWidget(self.new_button)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search workflows")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._filter)
        left.addWidget(self.search)
        self.list = QListWidget()
        self.list.setObjectName("plain")
        self.list.setItemDelegate(ListLook(self.list))
        self.list.setMouseTracking(True)
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.list.currentRowChanged.connect(self._pick)
        left.addWidget(self.list, 1)
        grid = QGridLayout()
        grid.setSpacing(6)
        self.template_button = QPushButton("Templates")
        self.template_button.clicked.connect(self.open_templates)
        self.duplicate_button = QPushButton("Duplicate")
        self.duplicate_button.clicked.connect(self.duplicate)
        self.delete_button = QPushButton("Delete")
        self.delete_button.setObjectName("danger")
        self.delete_button.clicked.connect(self.delete)
        grid.addWidget(self.template_button, 0, 0, 1, 2)
        grid.addWidget(self.duplicate_button, 1, 0)
        grid.addWidget(self.delete_button, 1, 1)
        left.addLayout(grid)
        # Every panel can be resized by dragging the lines between them.
        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.splitter.setChildrenCollapsible(False)
        self.splitter.setHandleWidth(5)
        self.splitter.addWidget(sidebar)

        # Middle: top bar, canvas, logs.
        middle = QVBoxLayout()
        middle.setContentsMargins(0, 0, 0, 0)
        middle.setSpacing(0)
        topbar = QFrame()
        topbar.setObjectName("topbar")
        topbar.setFixedHeight(56)
        bar = QHBoxLayout(topbar)
        bar.setContentsMargins(14, 8, 14, 8)
        self.name = QLineEdit()
        self.name.setObjectName("name")
        self.name.setPlaceholderText("Name this workflow")
        self.name.textChanged.connect(self._name_edited)
        bar.addWidget(self.name, 1)
        self.state = QLabel("")
        self.state.setObjectName("saved")
        bar.addWidget(self.state)
        bar.addSpacing(14)
        active = QLabel("ACTIVE")
        active.setObjectName("section")
        bar.addWidget(active)
        self.active = ToggleSwitch()
        self.active.setToolTip("Off: it never starts by itself or by its phrases")
        self.active.toggled.connect(self._active_toggled)
        bar.addWidget(self.active)
        bar.addSpacing(10)
        self.save_button = QPushButton("Save")
        self.save_button.setObjectName("primary")
        self.save_button.setFixedWidth(84)
        self.save_button.clicked.connect(self.save)
        bar.addWidget(self.save_button)
        middle.addWidget(topbar)

        self.scene = FlowScene()
        self.view = FlowView(self.scene)
        self.pane = CanvasPane(self.view, self)
        self.pane.add_button.clicked.connect(lambda: self.open_palette())
        self.pane.run_button.clicked.connect(self.test)
        self.pane.node_panel.chosen.connect(self._palette_chosen)
        self.canvas_split = QSplitter(Qt.Orientation.Vertical)
        self.canvas_split.setChildrenCollapsible(False)
        self.canvas_split.setHandleWidth(5)
        self.canvas_split.addWidget(self.pane)
        middle.addWidget(self.canvas_split, 1)

        self.logs = QFrame()
        self.logs.setObjectName("logs")
        logs_box = QVBoxLayout(self.logs)
        logs_box.setContentsMargins(14, 6, 14, 8)
        logs_head = QHBoxLayout()
        logs_title = QLabel("LOGS")
        logs_title.setObjectName("section")
        logs_head.addWidget(logs_title)
        self.log_state = QLabel("No test yet")
        self.log_state.setObjectName("hint")
        logs_head.addWidget(self.log_state, 1)
        self.logs_toggle = QPushButton("Show")
        self.logs_toggle.setFixedWidth(64)
        self.logs_toggle.clicked.connect(self.toggle_logs)
        logs_head.addWidget(self.logs_toggle)
        logs_box.addLayout(logs_head)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMinimumHeight(60)
        self.log.setFont(theme.font(theme.BODY_FAMILY, 9))
        self.log.setVisible(False)
        self._logs_open = False
        logs_box.addWidget(self.log)
        self.canvas_split.addWidget(self.logs)
        self.canvas_split.setStretchFactor(0, 1)
        self.canvas_split.setStretchFactor(1, 0)
        holder = QWidget()
        holder.setLayout(middle)
        holder.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        holder.setMinimumWidth(420)
        self.splitter.addWidget(holder)

        # Right: settings.
        self.inspector = Inspector(self)
        self.splitter.addWidget(self.inspector)
        self.splitter.setStretchFactor(0, 0)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setStretchFactor(2, 0)
        self.splitter.setSizes([240, 700, 340])
        root.addWidget(self.splitter)

        self.scene.node_selected.connect(self._node_selected)
        self.scene.nodes_moved.connect(self._nodes_moved)
        self.scene.link_requested.connect(self.connect_nodes)
        self.scene.link_removed.connect(self.unlink)
        self.scene.add_from.connect(self._add_from)
        self.scene.plus_clicked.connect(self._plus)
        self.scene.node_opened.connect(self._node_opened)

        for keys, slot in (
            (QKeySequence.StandardKey.Undo, self.undo),
            (QKeySequence.StandardKey.Redo, self.redo),
            (QKeySequence("Ctrl+Shift+Z"), self.redo),
            (QKeySequence.StandardKey.Save, self.save),
        ):
            shortcut = QShortcut(QKeySequence(keys), self)
            shortcut.activated.connect(slot)
        for text, action in (
            ("Delete", self.delete_selected),
            ("Backspace", self.delete_selected),
            ("Tab", self.open_palette),
            ("F", self.view.fit),
            ("Ctrl+D", self._duplicate_selected),
        ):
            shortcut = QShortcut(QKeySequence(text), self.view)
            shortcut.activated.connect(action)
            shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        self.refresh()

    # --- the list ---

    def refresh(self, select: str | None = None) -> None:
        """Reload the list from the store (keeping or choosing the selection)."""
        keep = select or (self._current.id if self._current else None)
        self._loading = True
        self.list.clear()
        for workflow in self._store.all:
            colour = theme.AMBER if workflow.enabled else theme.STEEL
            item = QListWidgetItem(
                icon(_TRIGGER_ICONS.get(workflow.trigger.kind, "bolt"), colour),
                f"{workflow.name}{'' if workflow.enabled else '  (off)'}\n"
                f"{workflow.trigger.describe()}",
            )
            item.setData(Qt.ItemDataRole.UserRole, workflow.id)
            self.list.addItem(item)
        self._filter(self.search.text())
        if self._current is not None and self._store.get(self._current.id) is None:
            self._loading = False
            self._show(self._current, dirty=True)  # being made, not saved yet
            return
        row = self._row_of(keep or "")
        if row < 0 and self.list.count():
            row = 0
        self.list.setCurrentRow(row)
        self._loading = False
        if row < 0:
            self._show(None)
            return
        workflow_id = self.list.item(row).data(Qt.ItemDataRole.UserRole)
        if self._current is None or self._current.id != workflow_id or not self.dirty:
            found = self._store.get(workflow_id)
            self._show(copy.deepcopy(found) if found else None)

    def _pick(self, row: int) -> None:
        if self._loading or row < 0:
            return
        workflow_id = self.list.item(row).data(Qt.ItemDataRole.UserRole)
        if self._current is not None and workflow_id == self._current.id:
            return
        if not self._leave():
            self._loading = True
            self.list.setCurrentRow(self._row_of(self._current.id if self._current else ""))
            self._loading = False
            return
        found = self._store.get(workflow_id)
        self._show(copy.deepcopy(found) if found else None)

    def _filter(self, text: str) -> None:
        words = text.lower().split()
        for i in range(self.list.count()):
            item = self.list.item(i)
            item.setHidden(not all(w in item.text().lower() for w in words))

    def visible_workflows(self) -> list[str]:
        return [
            self.list.item(i).text().split("\n")[0]
            for i in range(self.list.count())
            if not self.list.item(i).isHidden()
        ]

    def open_connection(self, service: str) -> None:
        """Set up a node's service (or manage saved logins), then show the node again."""
        if self.connections is None:
            return
        dialog: QDialog = (
            LoginsDialog(self, self.connections)
            if service == "logins"
            else ConnectDialog(self, self.connections, service)
        )
        dialog.setStyleSheet(STYLE)
        dialog.exec()
        showing = self.inspector.showing
        if showing not in ("", "trigger") and self._current is not None:
            step = self._current.step(showing)
            if step is not None:
                self.inspector.show_step(self._current, step)

    def _row_of(self, workflow_id: str) -> int:
        for i in range(self.list.count()):
            if self.list.item(i).data(Qt.ItemDataRole.UserRole) == workflow_id:
                return i
        return -1

    @property
    def current(self) -> Workflow | None:
        return self._current

    @property
    def dirty(self) -> bool:
        return self._current is not None and self._json() != self._saved

    def _json(self) -> str:
        return json.dumps(self._current.to_json(), sort_keys=True) if self._current else ""

    def _leave(self) -> bool:
        """Whether unsaved changes may be dropped (asks)."""
        if not self.dirty or self._current is None:
            return True
        answer = QMessageBox.question(
            self,
            "Unsaved changes",
            f"Drop the changes to “{self._current.name}”?",
            QMessageBox.StandardButton.Discard | QMessageBox.StandardButton.Cancel,
        )
        return answer == QMessageBox.StandardButton.Discard

    def new_workflow(self) -> None:
        if not self._leave():
            return
        self._show(Workflow("New workflow", Trigger("phrase", []), [], links=[]), dirty=True)
        self.name.setFocus()
        self.name.selectAll()

    def open_templates(self) -> None:
        TemplateGallery(self, self.add_template).exec()

    def add_template(self, name: str) -> None:
        if not self._leave():
            return
        template = next(t for t in templates() if t.name == name)
        template.name = self._free_name(template.name)
        self._show(template, dirty=True)

    def duplicate(self) -> None:
        if self._current is None or not self._leave():
            return
        twin = copy.deepcopy(self._current)
        twin.id = Workflow("x").id
        twin.name = self._free_name(twin.name + " copy")
        self._show(twin, dirty=True)

    def delete(self) -> None:
        if self._current is None:
            return
        answer = QMessageBox.question(
            self, "Delete workflow", f"Delete “{self._current.name}”? This can't be undone."
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._store.remove(self._current.id)
        self._current = None
        self.refresh()

    def _free_name(self, name: str) -> str:
        names = {w.name.lower() for w in self._store.all}
        base, number = name, 2
        while name.lower() in names:
            name = f"{base} {number}"
            number += 1
        return name

    # --- showing a workflow ---

    def _show(self, workflow: Workflow | None, dirty: bool = False) -> None:
        self._current = workflow
        self._undo, self._redo, self._last_edit = [], [], ""
        self._selected = ""
        self._run_seen = {}
        self.scene.clear_status()
        enabled = workflow is not None
        for widget in (self.name, self.active, self.save_button, self.pane):
            widget.setEnabled(enabled)
        self.duplicate_button.setEnabled(enabled)
        self.delete_button.setEnabled(
            workflow is not None and self._store.get(workflow.id) is not None
        )
        if workflow is not None and (
            workflow.trigger.x is None or any(s.x is None or s.y is None for s in workflow.steps)
        ):
            layout(workflow)
        self._saved = "" if dirty else self._json()
        self._loading = True
        self.name.setText(workflow.name if workflow else "")
        self.active.setChecked(workflow.enabled if workflow else False)
        self._loading = False
        self._draw()
        self.inspector.show_workflow(workflow)
        self.view.fit()
        self._update_state()

    def _views(self) -> list[NodeView]:
        assert self._current is not None
        workflow = self._current
        trigger = workflow.trigger
        nodes = [
            NodeView(
                TRIGGER_ID,
                _TRIGGER_NAMES.get(trigger.kind, "Trigger"),
                trigger.describe(),
                _TRIGGER_ICONS.get(trigger.kind, "bolt"),
                QColor(theme.AMBER),
                trigger=True,
                x=trigger.x or 0.0,
                y=trigger.y or 0.0,
            )
        ]
        for n, step in enumerate(workflow.steps, 1):
            spec = ACTIONS.get(step.action)
            group = spec.group if spec else "Core"
            nodes.append(
                NodeView(
                    step.id,
                    spec.title if spec else step.action,
                    step.describe(),
                    (spec.icon if spec else "") or GROUP_STYLE.get(group, (theme.CYAN, "gear"))[1],
                    accent(group),
                    step.outputs,
                    n,
                    x=step.x or 0.0,
                    y=step.y or 0.0,
                )
            )
        return nodes

    def _draw(self) -> None:
        if self._current is None:
            self.scene.show([], [])
            return
        links = [(link.src, link.port, link.dst) for link in self._current.graph()]
        self.scene.show(self._views(), links, self._selected)

    def _update_state(self) -> None:
        if self._current is None:
            self.state.setText("")
            self.pane.run_button.setEnabled(False)
            return
        found = problems(self._current)
        self.state.setObjectName("unsaved" if self.dirty else "saved")
        self.state.setText("● UNSAVED CHANGES" if self.dirty else "✓ SAVED")
        self.state.setToolTip("\n".join(found))
        self.state.style().unpolish(self.state)
        self.state.style().polish(self.state)
        self.save_button.setEnabled(not found)
        self.save_button.setToolTip("\n".join(found) or "Save (Ctrl+S)")
        self.pane.run_button.setEnabled(bool(run_order(self._current)))

    def _say(self, text: str) -> None:
        self.state.setObjectName("unsaved")
        self.state.setText(text)
        self.state.style().unpolish(self.state)
        self.state.style().polish(self.state)

    # --- changing it ---

    def _remember(self, key: str = "") -> None:
        """Keep the current state for Undo (typing in one field is one step)."""
        if self._current is None or (key and key == self._last_edit):
            return
        self._last_edit = key
        self._undo.append(self._json())
        del self._undo[:-60]
        self._redo.clear()

    def _changed(self) -> None:
        self._draw()
        self._update_state()

    def _links(self) -> list[Link]:
        """The workflow's links, written out (older workflows only had an order)."""
        assert self._current is not None
        if self._current.links is None:
            self._current.links = self._current.graph()
        return self._current.links

    def undo(self) -> None:
        self._step_history(self._undo, self._redo)

    def redo(self) -> None:
        self._step_history(self._redo, self._undo)

    def _step_history(self, source: list[str], target: list[str]) -> None:
        if self._current is None or not source:
            return
        target.append(self._json())
        restored = Workflow.from_json(json.loads(source.pop()))
        self._current = restored
        self._last_edit = ""
        if self._selected != TRIGGER_ID and restored.step(self._selected) is None:
            self._selected = ""
        self._loading = True
        self.name.setText(restored.name)
        self.active.setChecked(restored.enabled)
        self._loading = False
        self._draw()
        self._inspect(self._selected)
        self._update_state()

    def _name_edited(self, text: str) -> None:
        if self._loading or self._current is None:
            return
        self._remember("name")
        self._current.name = text.strip()
        self._update_state()

    def _active_toggled(self, on: bool) -> None:
        if self._loading or self._current is None:
            return
        self._remember()
        self._current.enabled = on
        self._update_state()

    def set_description(self, text: str) -> None:
        if self._current is None:
            return
        self._remember("description")
        self._current.description = text[:300]
        self._update_state()

    def set_trigger(self, **fields: Any) -> None:
        if self._current is None:
            return
        self._remember("trigger:" + ",".join(sorted(fields)))
        trigger = self._current.trigger
        kind_changed = "kind" in fields and fields["kind"] != trigger.kind
        for key, value in fields.items():
            setattr(trigger, key, value)
        if kind_changed and trigger.kind == "time" and not trigger.at:
            trigger.at = "07:30"
        item = self.scene.nodes.get(TRIGGER_ID)
        if kind_changed or item is None:
            self._draw()
            self.inspector.show_trigger(self._current)
        else:
            item.view.subtitle = trigger.describe()
            item.setToolTip(item.view.subtitle)
            item.update()
        self._update_state()

    def setting_changed(self, name: str) -> None:
        if self._current is None:
            return
        step = self._current.step(self.inspector.showing)
        if step is None:
            return
        self._remember(f"{step.id}:{name}")
        step.settings = self.inspector.read()
        item = self.scene.nodes.get(step.id)
        if item is not None:
            item.view.subtitle = step.describe()
            item.setToolTip(item.view.subtitle)
            item.update()
        self._update_state()

    def set_setting(self, step_id: str, name: str, value: Any) -> None:
        """Change one setting of a node (as typing in its field would)."""
        if self._current is None or (step := self._current.step(step_id)) is None:
            return
        self._remember(f"{step_id}:{name}")
        step.settings[name] = value
        if self.inspector.showing == step_id:
            self.inspector.show_step(self._current, step)
        self._changed()

    def add_node(
        self,
        action: str,
        after: tuple[str, str] | None = None,
        at: tuple[float, float] | None = None,
    ) -> Step | None:
        """Add an ``action`` node, linked from ``after`` (node id, output) if given."""
        if self._current is None or action not in ACTIONS:
            return None
        if len(self._current.steps) >= MAX_STEPS:
            self._say(f"A workflow can have up to {MAX_STEPS} nodes.")
            return None
        self._remember()
        links = self._links()
        spec = ACTIONS[action]
        step = Step(action, {s.name: s.default for s in spec.settings if s.default != ""})
        x, y = at if at is not None else self._spot_after(after)
        step.x, step.y = snap(x), snap(y)
        self._current.steps.append(step)
        if after is not None:
            links.append(Link(after[0], after[1], step.id))
        self._selected = step.id
        self._changed()
        self._inspect(step.id)
        return step

    def _spot_after(self, after: tuple[str, str] | None) -> tuple[float, float]:
        assert self._current is not None
        workflow = self._current
        if after is None:
            right = max([s.x or 0.0 for s in workflow.steps] + [workflow.trigger.x or 0.0])
            x, y = right + _COLUMN, workflow.trigger.y or 0.0
        else:
            source = workflow.step(after[0])
            outputs: tuple[str, ...]
            if source is None:
                x, y, outputs = workflow.trigger.x or 0.0, workflow.trigger.y or 0.0, ("out",)
            else:
                x, y, outputs = source.x or 0.0, source.y or 0.0, source.outputs
            x += _COLUMN
            if len(outputs) > 1 and after[1] in outputs:
                y += (outputs.index(after[1]) - (len(outputs) - 1) / 2) * _ROW
        taken = {(round(s.x or 0), round(s.y or 0)) for s in workflow.steps}
        while (round(snap(x)), round(snap(y))) in taken:
            y += _ROW
        return x, y

    def _next_free(self) -> tuple[str, str] | None:
        """Where a node added with + goes: after the selected node, else after the last
        one, on the first output with nothing linked."""
        assert self._current is not None
        workflow = self._current
        used = {(link.src, link.port) for link in workflow.graph()}
        candidates: list[tuple[str, tuple[str, ...]]] = []
        if self._selected == TRIGGER_ID:
            candidates.append((TRIGGER_ID, ("out",)))
        elif (chosen := workflow.step(self._selected)) is not None:
            candidates.append((chosen.id, chosen.outputs))
        order = run_order(workflow)
        if order:
            last = workflow.steps[order[-1]]
            candidates.append((last.id, last.outputs))
        if not workflow.steps:
            candidates.append((TRIGGER_ID, ("out",)))
        for node, outputs in candidates:
            for port in outputs:
                if (node, port) not in used:
                    return node, port
        return None

    def open_palette(self) -> None:
        if self._current is None:
            return
        target = self._next_free()
        self._pending = (*target, *self._spot_after(target)) if target else None
        self.pane.node_panel.open()

    def _add_from(self, src: str, port: str, x: float, y: float) -> None:
        self._pending = (src, port, x, y)
        self.pane.node_panel.open()

    def _plus(self, src: str, port: str) -> None:
        if self._current is not None:
            self._add_from(src, port, *self._spot_after((src, port)))

    def _palette_chosen(self, action: str) -> None:
        pending, self._pending = self._pending, None
        if pending is None:
            self.add_node(action)
        else:
            self.add_node(action, (pending[0], pending[1]), (pending[2], pending[3]))

    def connect_nodes(self, src: str, port: str, dst: str) -> bool:
        """Link ``src``'s ``port`` to ``dst`` (never into the trigger, never a loop)."""
        if self._current is None or dst == TRIGGER_ID:
            return False
        workflow = self._current
        if workflow.step(dst) is None or (src != TRIGGER_ID and workflow.step(src) is None):
            return False
        link = Link(src, port, dst)
        if link in workflow.graph():
            return False
        if would_loop(workflow, src, dst):
            self._say("That link would make a loop.")
            return False
        self._remember()
        self._links().append(link)
        self._changed()
        return True

    def unlink(self, src: str, port: str, dst: str) -> None:
        if self._current is None:
            return
        self._remember()
        links = self._links()
        self._current.links = [l for l in links if (l.src, l.port, l.dst) != (src, port, dst)]  # noqa: E741
        self._changed()

    def remove_node(self, step_id: str) -> None:
        if self._current is None or self._current.step(step_id) is None:
            return
        self._remember()
        links = self._links()
        # Keep a chain whole: what led into it now leads to what came after.
        into = [link for link in links if link.dst == step_id]
        out = [link for link in links if link.src == step_id]
        kept = [link for link in links if step_id not in (link.src, link.dst)]
        if len(into) == 1 and len(out) == 1:
            bridge = Link(into[0].src, into[0].port, out[0].dst)
            if bridge not in kept:
                kept.append(bridge)
        self._current.links = kept
        self._current.steps = [s for s in self._current.steps if s.id != step_id]
        if self._selected == step_id:
            self._selected = ""
        self._changed()
        self._inspect(self._selected)

    def duplicate_node(self, step_id: str) -> None:
        if self._current is None or (step := self._current.step(step_id)) is None:
            return
        if len(self._current.steps) >= MAX_STEPS:
            return
        self._remember()
        self._links()
        twin = Step(
            step.action,
            copy.deepcopy(step.settings),
            x=(step.x or 0) + 40,
            y=(step.y or 0) + 40,
        )
        self._current.steps.append(twin)
        self._selected = twin.id
        self._changed()
        self._inspect(twin.id)

    def _duplicate_selected(self) -> None:
        self.duplicate_node(self._selected)

    def delete_selected(self) -> None:
        link = self.scene.selected_link()
        if link is not None:
            self.unlink(link.src, link.port, link.dst)
        elif self._selected and self._selected != TRIGGER_ID:
            self.remove_node(self._selected)

    def tidy(self) -> None:
        if self._current is None:
            return
        self._remember()
        layout(self._current)
        self._changed()
        self.view.fit()

    def _nodes_moved(self, moved: list[tuple[str, float, float]]) -> None:
        if self._current is None:
            return
        self._remember()
        for node_id, x, y in moved:
            if node_id == TRIGGER_ID:
                self._current.trigger.x, self._current.trigger.y = x, y
            elif (step := self._current.step(node_id)) is not None:
                step.x, step.y = x, y
        self._update_state()

    def _node_selected(self, node_id: str) -> None:
        self._selected = node_id
        self._last_edit = ""
        self._inspect(node_id)

    def _node_opened(self, node_id: str) -> None:
        self._inspect(node_id)
        first = next(iter(self.inspector.fields.values()), None)
        if first is not None:
            first.setFocus()

    def _inspect(self, node_id: str) -> None:
        if self._current is None:
            self.inspector.show_workflow(None)
        elif node_id == TRIGGER_ID:
            self.inspector.show_trigger(self._current)
        elif node_id and (step := self._current.step(node_id)) is not None:
            self.inspector.show_step(self._current, step)
        else:
            self.inspector.show_workflow(self._current)

    def select_node(self, node_id: str) -> None:
        """Select a node on the canvas ("" for none), as a click would."""
        self.scene.clearSelection()
        item = self.scene.nodes.get(node_id)
        if item is not None:
            item.setSelected(True)
        else:
            self._node_selected("")

    # --- test and save ---

    def test(self) -> None:
        if self._current is None or not run_order(self._current):
            return
        self.scene.clear_status()
        self._run_seen = {}
        self.log.clear()
        self._log(f"▶ Testing “{self._current.name}”")
        self.log_state.setText("Running…")
        if not self._logs_open:
            self.toggle_logs()
        self._on_test(copy.deepcopy(self._current))

    def show_run(self, event: PlanUpdated) -> None:
        """Light up the nodes of the workflow being tested as it runs, and log it."""
        if self._current is None or event.workflow_id != self._current.id or not event.ids:
            return
        ran: set[str] = {TRIGGER_ID}
        self.scene.set_status(TRIGGER_ID, "done")
        marks = {"running": "…", "done": "✓", "failed": "✕", "skipped": "\u2013"}
        for node_id, step in zip(event.ids, event.steps, strict=False):
            status = {
                StepStatus.RUNNING: "running",
                StepStatus.DONE: "done",
                StepStatus.FAILED: "failed",
                StepStatus.SKIPPED: "skipped",
            }.get(step.status, "")
            if status in ("done", "failed", "running"):
                ran.add(node_id)
            self.scene.set_status(node_id, status, step.note)
            if status and self._run_seen.get(node_id) != status:
                self._run_seen[node_id] = status
                number = self._current.index(node_id) + 1
                note = " ".join(step.note.split())[:160]
                tail = f": {note}" if note and status != "running" else ""
                self._log(f"{marks[status]} {number}. {step.title}{tail}")
        self.scene.set_fired(ran)
        if event.finished:
            done = sum(s.status is StepStatus.DONE for s in event.steps)
            failed = sum(s.status is StepStatus.FAILED for s in event.steps)
            summary = f"Finished: {done} done" + (f", {failed} failed" if failed else "")
            self._log(summary)
            self.log_state.setText(summary)

    def _log(self, line: str) -> None:
        self.log.appendPlainText(f"{datetime.now():%H:%M:%S}  {line}")

    def toggle_logs(self) -> None:
        self._logs_open = not self._logs_open
        self.log.setVisible(self._logs_open)
        self.logs_toggle.setText("Hide" if self._logs_open else "Show")
        if self._logs_open:  # open at a useful height; drag the line above to resize
            total = sum(self.canvas_split.sizes())
            self.canvas_split.setSizes([max(200, total - 190), 190])

    def save(self) -> bool:
        if self._current is None:
            return False
        found = problems(self._current)
        if found:
            self._say(found[0])
            self.inspector.show_workflow(self._current)
            return False
        self._store.put(copy.deepcopy(self._current))
        self._saved = self._json()
        self.refresh(self._current.id)
        self._update_state()
        return True
