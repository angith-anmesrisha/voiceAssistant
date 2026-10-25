"""The workflow builder: make, change, test and save workflows without code.

A workflow is a trigger and a list of steps (see :mod:`jarvis.agent.workflows`). The
left side lists your workflows (plus ready-made templates); the right side edits one:
its name, what starts it, and its steps, which you add from a menu, change in a small
form, drag or move up and down, and remove. Test runs the workflow as it stands, with
each step's usual confirmations; nothing is kept until you press Save.
"""

from __future__ import annotations

import copy
from collections.abc import Callable
from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from jarvis.agent.workflows import (
    ACTIONS,
    DAYS,
    MAX_STEPS,
    TRIGGER_TITLES,
    TRIGGERS,
    Setting,
    Step,
    Trigger,
    Workflow,
    WorkflowStore,
    parse_clock,
    templates,
)
from jarvis.ui import theme

STYLE = f"""
QWidget {{ background: {theme.css(theme.BG)}; color: {theme.css(theme.TEXT)};
  font-family: "{theme.BODY_FAMILY}"; font-size: 10pt; }}
QLabel#title {{ font-family: "{theme.TITLE_FAMILY}"; font-size: 13pt;
  color: {theme.css(theme.CYAN)}; letter-spacing: 2px; }}
QLabel#section {{ font-family: "{theme.LABEL_FAMILY}"; font-size: 11pt; font-weight: 600;
  color: {theme.css(theme.TEXT_DIM)}; letter-spacing: 1.5px; }}
QLabel#hint {{ color: {theme.css(theme.TEXT_MUTED)}; }}
QLabel#problem {{ color: {theme.css(theme.AMBER)}; }}
QLineEdit, QPlainTextEdit, QComboBox, QDoubleSpinBox, QListWidget {{
  background: {theme.css(theme.BG_DEEP)}; border: 1px solid {theme.css(theme.rgba(theme.CYAN, 60))};
  border-radius: 6px; padding: 4px 6px; selection-background-color: {theme.css(theme.DEEP_BLUE)}; }}
QListWidget::item {{ padding: 6px 4px; }}
QListWidget::item:selected {{ background: {theme.css(theme.rgba(theme.ELECTRIC, 90))}; }}
QPushButton {{ background: {theme.css(theme.rgba(theme.CYAN, 28))};
  border: 1px solid {theme.css(theme.rgba(theme.CYAN, 110))}; border-radius: 6px;
  padding: 5px 12px; }}
QPushButton:hover {{ background: {theme.css(theme.rgba(theme.CYAN, 60))}; }}
QPushButton:disabled {{ color: {theme.css(theme.TEXT_MUTED)};
  border-color: {theme.css(theme.rgba(theme.STEEL, 80))}; }}
QPushButton#primary {{ background: {theme.css(theme.rgba(theme.ELECTRIC, 120))}; }}
QPushButton#danger {{ border-color: {theme.css(theme.rgba(theme.DANGER, 140))}; }}
QCheckBox::indicator {{ width: 14px; height: 14px; border-radius: 3px;
  border: 1px solid {theme.css(theme.rgba(theme.CYAN, 130))};
  background: {theme.css(theme.BG_DEEP)}; }}
QCheckBox::indicator:checked {{ background: {theme.css(theme.ELECTRIC)};
  border-color: {theme.css(theme.CYAN)}; }}
QTabWidget::pane {{ border: none; }}
QTabBar::tab {{ background: {theme.css(theme.BG_DEEP)}; color: {theme.css(theme.TEXT_DIM)};
  padding: 8px 18px; border-bottom: 2px solid transparent;
  font-family: "{theme.LABEL_FAMILY}"; font-size: 11pt; letter-spacing: 1px; }}
QTabBar::tab:selected {{ color: {theme.css(theme.CYAN)};
  border-bottom: 2px solid {theme.css(theme.CYAN)}; }}
QMenu {{ background: {theme.css(theme.BG_DEEP)}; border: 1px solid {theme.css(theme.STEEL)}; }}
QMenu::item:selected {{ background: {theme.css(theme.rgba(theme.ELECTRIC, 90))}; }}
"""

# Where "documents", "desktop", ... point (the same names the file tools accept).
_FOLDER_HINT = "desktop, documents, downloads… or a full path"


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
    for i, step in enumerate(workflow.steps, 1):
        spec = ACTIONS.get(step.action)
        if spec is None:
            found.append(f"Step {i} has an unknown action.")
            continue
        for setting in spec.settings:
            value = step.settings.get(setting.name)
            if setting.required and (value is None or value == "" or value == []):
                found.append(f"Step {i} ({spec.title}) needs “{setting.label}”.")
    return found


class StepEditor(QDialog):
    """A form for one step's settings, generated from its action's :class:`Setting`s."""

    def __init__(self, step: Step, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.step = Step(step.action, dict(step.settings))
        spec = ACTIONS[step.action]
        self.setWindowTitle(spec.title)
        self.setStyleSheet(STYLE)
        self.setMinimumWidth(460)
        layout = QVBoxLayout(self)
        heading = QLabel(spec.title)
        heading.setObjectName("section")
        layout.addWidget(heading)
        form = QFormLayout()
        self._readers: dict[str, Callable[[], Any]] = {}
        for setting in spec.settings:
            widget, read = self._field(setting, self.step.settings.get(setting.name))
            label = setting.label + (" *" if setting.required else "")
            form.addRow(label, widget)
            self._readers[setting.name] = read
        if not spec.settings:
            form.addRow(QLabel("This step has nothing to set."))
        layout.addLayout(form)
        tip = QLabel("In text you can use {previous}, {step 2} or {today}.")
        tip.setObjectName("hint")
        layout.addWidget(tip)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _field(self, setting: Setting, value: Any) -> tuple[QWidget, Callable[[], Any]]:
        current = setting.default if value is None else value
        if setting.kind in ("long", "list"):
            box = QPlainTextEdit()
            text = "\n".join(map(str, current)) if isinstance(current, list) else str(current)
            box.setPlainText(text)
            box.setFixedHeight(96)
            if setting.hint:
                box.setPlaceholderText(setting.hint)
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
            return spin, lambda: int(spin.value())
        if setting.kind == "choice":
            combo = QComboBox()
            combo.addItems(list(setting.choices))
            if str(current) in setting.choices:
                combo.setCurrentText(str(current))
            return combo, combo.currentText
        if setting.kind == "bool":
            check = QCheckBox()
            check.setChecked(bool(current))
            return check, check.isChecked
        line = QLineEdit(str(current or ""))
        line.setPlaceholderText(setting.hint or (_FOLDER_HINT if setting.kind == "folder" else ""))
        if setting.kind not in ("folder", "file"):
            return line, line.text
        row = QWidget()
        row_box = QHBoxLayout(row)
        row_box.setContentsMargins(0, 0, 0, 0)
        row_box.addWidget(line, 1)
        browse = QPushButton("Browse…")
        row_box.addWidget(browse)

        def pick() -> None:
            start = line.text() if Path(line.text()).exists() else str(Path.home())
            if setting.kind == "folder":
                chosen = QFileDialog.getExistingDirectory(self, setting.label, start)
            else:
                chosen = QFileDialog.getOpenFileName(self, setting.label, start)[0]
            if chosen:
                line.setText(chosen)

        browse.clicked.connect(pick)
        return row, line.text

    def accept(self) -> None:
        self.step.settings = {name: read() for name, read in self._readers.items()}
        super().accept()


class WorkflowBuilder(QWidget):
    """The builder window. ``on_test`` runs a workflow as it stands (not saved)."""

    def __init__(
        self,
        store: WorkflowStore,
        on_test: Callable[[Workflow], None],
        edit_step: Callable[[Step, QWidget], Step | None] | None = None,
    ) -> None:
        super().__init__()
        self._store = store
        self._on_test = on_test
        self._edit_step = edit_step or _edit_with_form
        self._current: Workflow | None = None
        self._dirty = False
        self._loading = False
        self.setWindowTitle("JARVIS · Workflows")
        self.setStyleSheet(STYLE)
        self.resize(1040, 700)

        root = QHBoxLayout(self)
        root.setContentsMargins(18, 18, 18, 18)
        root.setSpacing(18)

        # Left: the saved workflows.
        left = QVBoxLayout()
        title = QLabel("WORKFLOWS")
        title.setObjectName("title")
        left.addWidget(title)
        self.list = QListWidget()
        self.list.setWordWrap(True)
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.list.currentRowChanged.connect(self._pick)
        left.addWidget(self.list, 1)
        row = QHBoxLayout()
        self.new_button = QPushButton("New")
        self.new_button.clicked.connect(self.new_workflow)
        self.template_button = QPushButton("Templates")
        menu = QMenu(self.template_button)
        for template in templates():
            entry = menu.addAction(template.name)
            entry.setToolTip(template.description)
            entry.triggered.connect(lambda _=False, t=template: self.add_template(t.name))
        self.template_button.setMenu(menu)
        self.duplicate_button = QPushButton("Duplicate")
        self.duplicate_button.clicked.connect(self.duplicate)
        self.delete_button = QPushButton("Delete")
        self.delete_button.setObjectName("danger")
        self.delete_button.clicked.connect(self.delete)
        for button in (
            self.new_button,
            self.template_button,
            self.duplicate_button,
            self.delete_button,
        ):
            row.addWidget(button)
        left.addLayout(row)
        tip = QLabel(
            "Or just tell JARVIS: “Whenever I start work, open my project folder and show "
            "my tasks.” It shows you the workflow and saves it only if you say yes."
        )
        tip.setWordWrap(True)
        tip.setObjectName("hint")
        left.addWidget(tip)
        root.addLayout(left, 2)

        # Right: the one being edited.
        self.editor = QWidget()
        right = QVBoxLayout(self.editor)
        right.setContentsMargins(0, 0, 0, 0)
        form = QFormLayout()
        self.name = QLineEdit()
        self.name.textEdited.connect(self._changed)
        form.addRow("Name", self.name)
        self.enabled = QCheckBox("On")
        self.enabled.toggled.connect(self._changed)
        form.addRow("", self.enabled)
        self.description = QLineEdit()
        self.description.setPlaceholderText("What it's for (optional)")
        self.description.textEdited.connect(self._changed)
        form.addRow("About", self.description)
        right.addLayout(form)

        section = QLabel("WHEN")
        section.setObjectName("section")
        right.addWidget(section)
        trigger_form = QFormLayout()
        self.trigger_kind = QComboBox()
        for kind in TRIGGERS:
            self.trigger_kind.addItem(TRIGGER_TITLES[kind], kind)
        self.trigger_kind.currentIndexChanged.connect(self._trigger_kind_changed)
        trigger_form.addRow("Starts", self.trigger_kind)
        self.phrases = QPlainTextEdit()
        self.phrases.setPlaceholderText("One per line, e.g.\nactivate study mode\nstudy time")
        self.phrases.setFixedHeight(70)
        self.phrases.textChanged.connect(self._changed)
        self.phrases_label = QLabel("Phrases")
        trigger_form.addRow(self.phrases_label, self.phrases)
        self.at = QLineEdit()
        self.at.setPlaceholderText("07:30")
        self.at.textEdited.connect(self._changed)
        self.at_label = QLabel("Time")
        trigger_form.addRow(self.at_label, self.at)
        self.days_row = QWidget()
        days_box = QHBoxLayout(self.days_row)
        days_box.setContentsMargins(0, 0, 0, 0)
        self.days: dict[str, QCheckBox] = {}
        for day in DAYS:
            check = QCheckBox(day.title())
            check.toggled.connect(self._changed)
            self.days[day] = check
            days_box.addWidget(check)
        self.days_label = QLabel("Days (none = every day)")
        trigger_form.addRow(self.days_label, self.days_row)
        self.folder_row = QWidget()
        folder_box = QHBoxLayout(self.folder_row)
        folder_box.setContentsMargins(0, 0, 0, 0)
        self.folder = QLineEdit()
        self.folder.setPlaceholderText("A folder in your user folders")
        self.folder.textEdited.connect(self._changed)
        folder_box.addWidget(self.folder, 1)
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._pick_folder)
        folder_box.addWidget(browse)
        self.folder_label = QLabel("Folder")
        trigger_form.addRow(self.folder_label, self.folder_row)
        self.pattern = QLineEdit()
        self.pattern.setPlaceholderText("*.pdf")
        self.pattern.textEdited.connect(self._changed)
        self.pattern_label = QLabel("Files")
        trigger_form.addRow(self.pattern_label, self.pattern)
        self.file_event = QComboBox()
        self.file_event.addItem("A new file appears", "new")
        self.file_event.addItem("A file changes", "changed")
        self.file_event.currentIndexChanged.connect(self._changed)
        self.file_event_label = QLabel("When")
        trigger_form.addRow(self.file_event_label, self.file_event)
        right.addLayout(trigger_form)

        section = QLabel("THEN")
        section.setObjectName("section")
        right.addWidget(section)
        self.steps = QListWidget()
        self.steps.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.steps.model().rowsMoved.connect(self._steps_reordered)
        self.steps.itemDoubleClicked.connect(lambda _item: self.edit_step())
        right.addWidget(self.steps, 1)
        step_row = QHBoxLayout()
        self.add_button = QPushButton("Add step")
        add_menu = QMenu(self.add_button)
        groups: dict[str, QMenu] = {}
        for spec in ACTIONS.values():
            group = groups.get(spec.group)
            if group is None:
                group = groups[spec.group] = add_menu.addMenu(spec.group)
            item = group.addAction(spec.title)
            item.triggered.connect(lambda _=False, name=spec.name: self.add_step(name))
        self.add_button.setMenu(add_menu)
        self.edit_button = QPushButton("Edit")
        self.edit_button.clicked.connect(self.edit_step)
        self.up_button = QPushButton("Up")
        self.up_button.clicked.connect(lambda: self.move_step(-1))
        self.down_button = QPushButton("Down")
        self.down_button.clicked.connect(lambda: self.move_step(1))
        self.remove_button = QPushButton("Remove")
        self.remove_button.clicked.connect(self.remove_step)
        for button in (
            self.add_button,
            self.edit_button,
            self.up_button,
            self.down_button,
            self.remove_button,
        ):
            step_row.addWidget(button)
        right.addLayout(step_row)

        self.problem = QLabel("")
        self.problem.setObjectName("problem")
        self.problem.setWordWrap(True)
        right.addWidget(self.problem)
        bottom = QHBoxLayout()
        bottom.addStretch(1)
        self.test_button = QPushButton("Test")
        self.test_button.setToolTip("Run it now as it stands; each step still asks first")
        self.test_button.clicked.connect(self.test)
        self.save_button = QPushButton("Save")
        self.save_button.setObjectName("primary")
        self.save_button.clicked.connect(self.save)
        bottom.addWidget(self.test_button)
        bottom.addWidget(self.save_button)
        right.addLayout(bottom)
        root.addWidget(self.editor, 3)
        self.refresh()

    # --- the list ---

    def refresh(self, select: str | None = None) -> None:
        """Reload the list from the store (keeping or choosing the selection)."""
        keep = select or (self._current.id if self._current else None)
        self._loading = True
        self.list.clear()
        for workflow in self._store.all:
            item = QListWidgetItem(
                f"{workflow.name}{'' if workflow.enabled else '  (off)'}\n"
                f"{workflow.trigger.describe()}"
            )
            item.setData(Qt.ItemDataRole.UserRole, workflow.id)
            self.list.addItem(item)
        self._loading = False
        row = next(
            (
                i
                for i in range(self.list.count())
                if self.list.item(i).data(Qt.ItemDataRole.UserRole) == keep
            ),
            0 if self.list.count() else -1,
        )
        if self._current is not None and self._store.get(self._current.id) is None:
            # Being made, not saved yet: keep editing it.
            self._show(self._current)
            return
        self.list.setCurrentRow(row)
        if row < 0:
            self._show(None)
        else:
            self._pick(row)

    def _pick(self, row: int) -> None:
        if self._loading or row < 0:
            return
        workflow_id = self.list.item(row).data(Qt.ItemDataRole.UserRole)
        if self._current is not None and workflow_id == self._current.id:
            return
        if not self._leave():
            return
        found = self._store.get(workflow_id)
        self._show(copy.deepcopy(found) if found else None)

    def _leave(self) -> bool:
        """Whether unsaved changes may be dropped (asks)."""
        if not self._dirty or self._current is None:
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
        self._show(Workflow("New workflow", Trigger("phrase", []), []))
        self._dirty = True
        self.name.setFocus()
        self.name.selectAll()

    def add_template(self, name: str) -> None:
        if not self._leave():
            return
        template = next(t for t in templates() if t.name == name)
        template.name = self._free_name(template.name)
        self._show(template)
        self._dirty = True

    def duplicate(self) -> None:
        if self._current is None or not self._leave():
            return
        twin = copy.deepcopy(self._current)
        twin.id = Workflow("x").id
        twin.name = self._free_name(twin.name + " copy")
        self._show(twin)
        self._dirty = True

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
        self._dirty = False
        self.refresh()

    def _free_name(self, name: str) -> str:
        names = {w.name.lower() for w in self._store.all}
        base, number = name, 2
        while name.lower() in names:
            name = f"{base} {number}"
            number += 1
        return name

    # --- the editor ---

    def _show(self, workflow: Workflow | None) -> None:
        self._current = workflow
        self._dirty = False
        self.editor.setEnabled(workflow is not None)
        self.duplicate_button.setEnabled(workflow is not None)
        self.delete_button.setEnabled(
            workflow is not None and self._store.get(workflow.id) is not None
        )
        if workflow is None:
            self.steps.clear()
            self.problem.setText("Make a workflow with New, or start from a template.")
            return
        self._loading = True
        self.name.setText(workflow.name)
        self.enabled.setChecked(workflow.enabled)
        self.description.setText(workflow.description)
        trigger = workflow.trigger
        self.trigger_kind.setCurrentIndex(max(0, TRIGGERS.index(trigger.kind)))
        self.phrases.setPlainText("\n".join(trigger.phrases))
        self.at.setText(trigger.at)
        for day, check in self.days.items():
            check.setChecked(day in trigger.days)
        self.folder.setText(trigger.folder)
        self.pattern.setText(trigger.pattern)
        self.file_event.setCurrentIndex(0 if trigger.event == "new" else 1)
        self._loading = False
        self._trigger_fields()
        self._fill_steps()
        self._check()

    def _fill_steps(self, select: int | None = None) -> None:
        assert self._current is not None
        self.steps.clear()
        for i, step in enumerate(self._current.steps, 1):
            item = QListWidgetItem(f"{i}. {step.describe()}")
            item.setData(Qt.ItemDataRole.UserRole, i - 1)
            self.steps.addItem(item)
        if select is not None and 0 <= select < self.steps.count():
            self.steps.setCurrentRow(select)
        full = len(self._current.steps) >= MAX_STEPS
        self.add_button.setEnabled(not full)

    def _changed(self, *_args: Any) -> None:
        if self._loading or self._current is None:
            return
        workflow = self._current
        workflow.name = self.name.text().strip()[:60]
        workflow.enabled = self.enabled.isChecked()
        workflow.description = self.description.text().strip()[:300]
        trigger = workflow.trigger
        trigger.kind = str(self.trigger_kind.currentData())
        trigger.phrases = [
            line.strip()[:80] for line in self.phrases.toPlainText().splitlines() if line.strip()
        ][:5]
        trigger.at = self.at.text().strip()
        trigger.days = [day for day, check in self.days.items() if check.isChecked()]
        trigger.folder = self.folder.text().strip()
        trigger.pattern = self.pattern.text().strip() or "*"
        trigger.event = str(self.file_event.currentData())
        self._dirty = True
        self._check()

    def _trigger_kind_changed(self) -> None:
        self._trigger_fields()
        self._changed()

    def _trigger_fields(self) -> None:
        kind = str(self.trigger_kind.currentData())
        shown = {
            "phrase": (self.phrases_label, self.phrases),
            "time": (self.at_label, self.at, self.days_label, self.days_row),
            "folder": (
                self.folder_label,
                self.folder_row,
                self.pattern_label,
                self.pattern,
                self.file_event_label,
                self.file_event,
            ),
        }
        for name, widgets in shown.items():
            for widget in widgets:
                widget.setVisible(name == kind)

    def _pick_folder(self) -> None:
        chosen = QFileDialog.getExistingDirectory(self, "Folder to watch", str(Path.home()))
        if chosen:
            self.folder.setText(chosen)
            self._changed()

    def _check(self) -> list[str]:
        found = problems(self._current) if self._current else []
        self.problem.setText(" ".join(found))
        self.save_button.setEnabled(self._current is not None and not found)
        self.test_button.setEnabled(self._current is not None and bool(self._current.steps))
        return found

    # --- steps ---

    def _selected(self) -> int:
        return self.steps.currentRow()

    def add_step(self, action: str) -> None:
        if self._current is None or len(self._current.steps) >= MAX_STEPS:
            return
        spec = ACTIONS[action]
        step = Step(action, {s.name: s.default for s in spec.settings if s.default != ""})
        if spec.settings:
            edited = self._edit_step(step, self)
            if edited is None:
                return
            step = edited
        at = self._selected() + 1 if self._selected() >= 0 else len(self._current.steps)
        self._current.steps.insert(at, step)
        self._dirty = True
        self._fill_steps(at)
        self._check()

    def edit_step(self) -> None:
        index = self._selected()
        if self._current is None or index < 0:
            return
        step = self._current.steps[index]
        if step.action not in ACTIONS:
            return
        edited = self._edit_step(step, self)
        if edited is not None:
            self._current.steps[index] = edited
            self._dirty = True
            self._fill_steps(index)
            self._check()

    def move_step(self, by: int) -> None:
        index = self._selected()
        if self._current is None or index < 0:
            return
        target = index + by
        steps = self._current.steps
        if not 0 <= target < len(steps):
            return
        steps[index], steps[target] = steps[target], steps[index]
        self._dirty = True
        self._fill_steps(target)
        self._check()

    def remove_step(self) -> None:
        index = self._selected()
        if self._current is None or index < 0:
            return
        del self._current.steps[index]
        self._dirty = True
        self._fill_steps(min(index, len(self._current.steps) - 1))
        self._check()

    def _steps_reordered(self, *_args: Any) -> None:
        """After a drag: the list's order becomes the steps' order."""
        if self._current is None:
            return
        order = [
            int(self.steps.item(i).data(Qt.ItemDataRole.UserRole))
            for i in range(self.steps.count())
        ]
        self._current.steps = [self._current.steps[i] for i in order]
        self._dirty = True
        self._fill_steps(self._selected())
        self._check()

    # --- test and save ---

    def test(self) -> None:
        if self._current is not None and self._current.steps:
            self._on_test(copy.deepcopy(self._current))

    def save(self) -> bool:
        if self._current is None or self._check():
            return False
        self._store.put(copy.deepcopy(self._current))
        self._dirty = False
        self.refresh(self._current.id)
        self.problem.setText(f"Saved “{self._current.name}”.")
        return True


def _edit_with_form(step: Step, parent: QWidget) -> Step | None:
    dialog = StepEditor(step, parent)
    return dialog.step if dialog.exec() == QDialog.DialogCode.Accepted else None
