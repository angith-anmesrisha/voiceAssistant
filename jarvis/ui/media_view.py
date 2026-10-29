"""The preview window for a generated picture or 3D model: Save, Save as… or Discard.

Nothing is written to the user's folders until Save is pressed; closing the window is the
same as Discard. A 3D model is shown with Qt Quick 3D (``model_view.qml``), which loads
it from a file, so the model sits in JARVIS's own cache folder while the window is open
and is deleted when it closes. Without Qt Quick 3D the window shows the picture the model
was made from instead.
"""

from __future__ import annotations

import contextlib
import re
import tempfile
from datetime import datetime
from pathlib import Path

from loguru import logger
from PySide6.QtCore import QRectF, Qt, QUrl
from PySide6.QtGui import QColor, QImage, QPainter, QPaintEvent, QPen, QPixmap, QResizeEvent
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from jarvis.tools.images import Picture
from jarvis.tools.threed import Model3D
from jarvis.ui import theme
from jarvis.ui.theme import BG_DEEP, CYAN, TEXT_DIM, rgba
from jarvis.ui.widgets.glass import body_label, hud_label
from jarvis.ui.widgets.panels import ConfirmOverlay

QML = Path(__file__).resolve().parent / "model_view.qml"


def file_name(prompt: str, suffix: str) -> str:
    """A short, safe file name from the description, e.g. "red panda astronaut.png"."""
    words = re.sub(r'[<>:"/\\|?*\x00-\x1f.,;!]', " ", prompt).split()
    words = [w for w in words if w.lower() not in {"a", "an", "the", "style", "of"}]
    name = " ".join(words)[:60].strip() or "JARVIS image"
    return name + suffix


def unique_path(folder: Path, name: str) -> Path:
    path = folder / name
    if not path.exists():
        return path
    stem, suffix = path.stem, path.suffix
    stamp = datetime.now().strftime("%Y-%m-%d %H%M%S")
    path = folder / f"{stem} ({stamp}){suffix}"
    count = 2
    while path.exists():
        path = folder / f"{stem} ({stamp} {count}){suffix}"
        count += 1
    return path


class _PictureView(QLabel):
    """The picture, scaled to fit and kept sharp on resize."""

    def __init__(self, image: QImage) -> None:
        super().__init__()
        self._pixmap = QPixmap.fromImage(image)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setMinimumSize(320, 240)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setStyleSheet("background: transparent;")

    def resizeEvent(self, event: QResizeEvent) -> None:  # noqa: N802
        if not self._pixmap.isNull():
            self.setPixmap(
                self._pixmap.scaled(
                    self.size(),
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
            )
        super().resizeEvent(event)


def _model_widget(url: QUrl) -> QWidget | None:
    """A Qt Quick 3D view of the model, or None when Qt Quick 3D isn't available."""
    try:
        from PySide6.QtQuick3D import QQuick3D  # noqa: F401  (loads the module)
        from PySide6.QtQuickWidgets import QQuickWidget
    except ImportError as exc:
        logger.info("3D preview unavailable ({})", type(exc).__name__)
        return None
    view = QQuickWidget()
    view.setResizeMode(QQuickWidget.ResizeMode.SizeRootObjectToView)
    view.rootContext().setContextProperty("modelUrl", url)
    view.setSource(QUrl.fromLocalFile(str(QML)))
    if view.status() == QQuickWidget.Status.Error:
        logger.info("3D preview failed to load: {}", len(view.errors()))
        view.deleteLater()
        return None
    view.setMinimumSize(320, 240)
    return view


class MediaPreview(QWidget):
    """One generated picture or model, until the user saves or discards it."""

    def __init__(self, item: Picture | Model3D, folder: Path, cache: Path | None = None):
        super().__init__()
        self._item = item
        self._folder = folder
        self._temp: Path | None = None
        self._saved: Path | None = None
        is_model = isinstance(item, Model3D)
        picture = item.picture if isinstance(item, Model3D) else item
        self.setWindowTitle("JARVIS · 3D model" if is_model else "JARVIS · Image")
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setMinimumSize(560, 520)
        self.resize(820, 760)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(26, 22, 26, 20)
        layout.setSpacing(10)
        head = QHBoxLayout()
        head.addWidget(hud_label("3D MODEL" if is_model else "GENERATED IMAGE", 12, CYAN))
        head.addStretch(1)
        self.credit = hud_label(item.credit.upper(), 8.6, TEXT_DIM)
        head.addWidget(self.credit)
        layout.addLayout(head)
        prompt = body_label(picture.prompt.split(". Style:")[0][:300], 10, TEXT_DIM)
        prompt.setWordWrap(True)
        layout.addWidget(prompt)

        model_view = self._model_view(item, cache) if isinstance(item, Model3D) else None
        view: QWidget = _PictureView(QImage.fromData(picture.data))
        if model_view is not None:
            # The 3D view, with the picture it came from one press away (Picture/3D view).
            stack = QStackedWidget()
            stack.addWidget(model_view)
            stack.addWidget(view)
            view = stack
        elif is_model:
            note = body_label(
                "The 3D view isn't available on this PC; this is the picture the model "
                "was made from. Save keeps the 3D file.",
                9,
                theme.AMBER,
            )
            note.setWordWrap(True)
            layout.addWidget(note)
        self.view = view
        layout.addWidget(view, 1)

        hint = "Drag to turn it, scroll to zoom. " if model_view is not None else ""
        self.status = body_label(
            hint + "Not saved yet. Discard or close to throw it away.", 9, TEXT_DIM
        )
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        buttons = QHBoxLayout()
        self.discard = ConfirmOverlay._button("Discard", TEXT_DIM)
        buttons.addWidget(self.discard)
        buttons.addStretch(1)
        self.copy = ConfirmOverlay._button("Copy", CYAN)
        self.copy.setVisible(not is_model)
        self.flip = ConfirmOverlay._button("Picture", CYAN)
        self.flip.setVisible(isinstance(view, QStackedWidget))
        self.flip.clicked.connect(self._flip)
        buttons.addWidget(self.flip)
        self.save_as = ConfirmOverlay._button("Save as…", CYAN)
        self.save = ConfirmOverlay._button("Save", CYAN)
        for button in (self.copy, self.save_as, self.save):
            buttons.addWidget(button)
        layout.addLayout(buttons)
        self.discard.clicked.connect(self.close)
        self.copy.clicked.connect(self._copy)
        self.save.clicked.connect(self._save)
        self.save_as.clicked.connect(self._save_as)

    # --- the model's temporary file (Qt Quick 3D loads from a file) ---

    def _model_view(self, model: Model3D, cache: Path | None) -> QWidget | None:
        try:
            folder = cache or Path(tempfile.gettempdir())
            folder.mkdir(parents=True, exist_ok=True)
            path = unique_path(folder, f"preview-{id(self):x}{model.suffix}")
            path.write_bytes(model.data)
            self._temp = path
        except OSError as exc:
            logger.info("3D preview: no cache file ({})", type(exc).__name__)
            return None
        return _model_widget(QUrl.fromLocalFile(str(self._temp)))

    def closeEvent(self, event: object) -> None:  # noqa: N802
        if self._temp is not None:
            self.view.deleteLater()  # let go of the file first
            with contextlib.suppress(OSError):  # else removed at the next start (clear_cache)
                self._temp.unlink(missing_ok=True)
            self._temp = None
        super().closeEvent(event)  # type: ignore[arg-type]

    # --- saving ---

    @property
    def _data(self) -> bytes:
        return self._item.data

    @property
    def _suffix(self) -> str:
        return self._item.suffix if isinstance(self._item, Model3D) else self._item.extension

    def _prompt(self) -> str:
        item = self._item
        picture = item.picture if isinstance(item, Model3D) else item
        return picture.prompt.split(". Style:")[0].split(". a single object")[0]

    def _write(self, path: Path, *, overwrite: bool) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("wb" if overwrite else "xb") as handle:
                handle.write(self._data)
        except OSError as exc:
            self._show_status(f"Couldn't save it ({type(exc).__name__}).", theme.DANGER)
            return
        self._saved = path
        self.save.setEnabled(False)
        self.save.setText("Saved")
        self._show_status(f"Saved to {path}", CYAN)

    def _save(self) -> None:
        name = file_name(self._prompt(), self._suffix)
        self._write(unique_path(self._folder, name), overwrite=False)

    def _save_as(self) -> None:
        start = str(self._folder / file_name(self._prompt(), self._suffix))
        kind = "3D model" if isinstance(self._item, Model3D) else "Image"
        dialog = QFileDialog(self, "Save the " + kind.lower(), start)
        dialog.setAcceptMode(QFileDialog.AcceptMode.AcceptSave)
        dialog.setNameFilters([f"{kind} (*{self._suffix})"])
        dialog.setDefaultSuffix(self._suffix.lstrip("."))
        dialog.fileSelected.connect(self._chosen)
        dialog.open()  # the dialog itself asks before replacing a file

    def _flip(self) -> None:
        if isinstance(self.view, QStackedWidget):
            showing_model = self.view.currentIndex() == 0
            self.view.setCurrentIndex(1 if showing_model else 0)
            self.flip.setText("3D view" if showing_model else "Picture")

    def _chosen(self, name: str) -> None:
        if name:
            self._write(Path(name), overwrite=True)

    def _copy(self) -> None:
        if isinstance(self._item, Picture):
            QApplication.clipboard().setImage(QImage.fromData(self._item.data))
            self._show_status("Copied to the clipboard.", CYAN)

    def _show_status(self, text: str, color: QColor) -> None:
        self.status.setText(text)
        self.status.setStyleSheet(f"color: {theme.css(color)}; background: transparent;")

    # --- the HUD backdrop ---

    def paintEvent(self, _event: QPaintEvent) -> None:  # noqa: N802
        p = QPainter(self)
        p.fillRect(self.rect(), BG_DEEP)
        p.setPen(QPen(rgba(CYAN, 14), 1))
        for x in range(0, self.width(), 32):
            p.drawLine(x, 0, x, self.height())
        for y in range(0, self.height(), 32):
            p.drawLine(0, y, self.width(), y)
        frame = QRectF(self.rect()).adjusted(10, 10, -10, -10)
        theme.paint_corner_brackets(p, frame, rgba(CYAN, 170), 16)
        stage = QRectF(self.view.geometry()).adjusted(-6, -6, 6, 6)
        theme.paint_glass(p, stage, 14)
        p.end()


class Previews:
    """Keeps open preview windows alive and opens new ones in front."""

    def __init__(self, folder: Path, cache: Path | None = None) -> None:
        self._folder = folder
        self._cache = cache
        self._open: list[MediaPreview] = []
        clear_cache(cache)

    def show(self, item: Picture | Model3D) -> MediaPreview:
        window = MediaPreview(item, self._folder, self._cache)
        self._open.append(window)
        window.destroyed.connect(lambda *_: self._forget(window))
        window.show()
        window.raise_()
        window.activateWindow()
        return window

    def _forget(self, window: MediaPreview) -> None:
        if window in self._open:
            self._open.remove(window)


def clear_cache(cache: Path | None) -> None:
    """Remove model previews a crash left behind."""
    if cache is None or not cache.is_dir():
        return
    for path in cache.glob("preview-*"):
        with contextlib.suppress(OSError):
            path.unlink()
