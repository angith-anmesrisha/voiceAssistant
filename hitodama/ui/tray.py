"""System tray icon and menu."""

from __future__ import annotations

import asyncio

from loguru import logger
from PySide6.QtGui import QAction
from PySide6.QtWidgets import QInputDialog, QLineEdit, QMenu, QSystemTrayIcon

from hitodama import APP_NAME, __version__
from hitodama.app import secrets
from hitodama.app.events import (
    ApiKeyChanged,
    Event,
    EventBus,
    NoticeLevel,
    QuitRequested,
    UserNotice,
)
from hitodama.app.state import AppState, StateChanged
from hitodama.ui.icons import app_icon


class Tray:
    def __init__(self, bus: EventBus, *, api_key_set: bool, talk_hotkey: str) -> None:
        self._bus = bus
        self._talk_hotkey = talk_hotkey
        self._pending: set[asyncio.Task[None]] = set()
        self._icon = QSystemTrayIcon(app_icon())
        self._menu = QMenu()

        self._status = QAction(self._status_text(AppState.IDLE))
        self._status.setEnabled(False)
        self._key_status = QAction(self._key_text(api_key_set))
        self._key_status.setEnabled(False)
        self._set_key = QAction("Set Gemini API key…")
        self._set_key.triggered.connect(self._ask_for_key)
        self._key_dialog: QInputDialog | None = None
        self._settings = QAction("Settings…")
        self._settings.setEnabled(False)  # settings window arrives in M3/M12
        self._quit = QAction(f"Quit {APP_NAME}")
        self._quit.triggered.connect(self._on_quit)

        for action in (self._status, self._key_status):
            self._menu.addAction(action)
        self._menu.addSeparator()
        self._menu.addAction(self._set_key)
        self._menu.addAction(self._settings)
        self._menu.addSeparator()
        self._menu.addAction(self._quit)

        self._icon.setContextMenu(self._menu)
        self._icon.setToolTip(f"{APP_NAME} {__version__}")
        bus.subscribe(StateChanged, self._on_state_changed)
        bus.subscribe(UserNotice, self._on_notice)

    @staticmethod
    def _key_text(is_set: bool) -> str:
        return "Gemini API key: set" if is_set else "Gemini API key: not set"

    @staticmethod
    def _status_text(state: AppState) -> str:
        return f"{APP_NAME}: {state.value}"

    def show(self) -> None:
        if not QSystemTrayIcon.isSystemTrayAvailable():
            logger.warning("System tray is not available on this desktop")
        self._icon.show()

    def hide(self) -> None:
        self._icon.hide()

    def _publish(self, event: Event) -> None:
        task = asyncio.ensure_future(self._bus.publish(event))
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)

    def _on_quit(self) -> None:
        self._publish(QuitRequested(reason="tray"))

    def _ask_for_key(self) -> None:
        # Non-modal open() instead of exec(), so the asyncio loop keeps running.
        if self._key_dialog is not None:
            self._key_dialog.raise_()
            return
        dialog = QInputDialog()
        dialog.setWindowTitle(f"{APP_NAME}: Gemini API key")
        dialog.setLabelText(
            "Paste your Gemini API key from Google AI Studio.\n"
            "It is stored in Windows Credential Manager, not in a file."
        )
        dialog.setTextEchoMode(QLineEdit.EchoMode.Password)
        dialog.textValueSelected.connect(self._save_key)
        dialog.finished.connect(self._dialog_closed)
        self._key_dialog = dialog
        dialog.open()

    def _dialog_closed(self, _result: int) -> None:
        if self._key_dialog is not None:
            self._key_dialog.deleteLater()
            self._key_dialog = None

    def _save_key(self, key: str) -> None:
        if not key.strip():
            return
        try:
            secrets.set_api_key(key)
        except Exception as exc:
            logger.error("Could not save API key: {}", type(exc).__name__)
            self._icon.showMessage(
                APP_NAME, "Couldn't save the key.", QSystemTrayIcon.MessageIcon.Critical
            )
            return
        self._key_status.setText(self._key_text(True))
        self._icon.showMessage(APP_NAME, f"API key saved. Hold {self._talk_hotkey} to talk.")
        self._publish(ApiKeyChanged())

    async def _on_notice(self, notice: UserNotice) -> None:
        icon = {
            NoticeLevel.INFO: QSystemTrayIcon.MessageIcon.Information,
            NoticeLevel.WARNING: QSystemTrayIcon.MessageIcon.Warning,
            NoticeLevel.ERROR: QSystemTrayIcon.MessageIcon.Critical,
        }[notice.level]
        self._icon.showMessage(notice.title, notice.message, icon)

    async def _on_state_changed(self, event: StateChanged) -> None:
        self._status.setText(self._status_text(event.current))
        self._icon.setToolTip(f"{APP_NAME}: {event.current.value}")
