"""System tray icon and menu."""

from __future__ import annotations

import asyncio

from loguru import logger
from PySide6.QtGui import QAction
from PySide6.QtWidgets import QMenu, QSystemTrayIcon

from hitodama import APP_NAME, __version__
from hitodama.app.events import EventBus, QuitRequested
from hitodama.app.state import AppState, StateChanged
from hitodama.ui.icons import app_icon


class Tray:
    def __init__(self, bus: EventBus, *, api_key_set: bool) -> None:
        self._bus = bus
        self._pending: set[asyncio.Task[None]] = set()
        self._icon = QSystemTrayIcon(app_icon())
        self._menu = QMenu()

        self._status = QAction(self._status_text(AppState.IDLE))
        self._status.setEnabled(False)
        self._key_status = QAction(
            "Gemini API key: set" if api_key_set else "Gemini API key: not set"
        )
        self._key_status.setEnabled(False)
        self._settings = QAction("Settings…")
        self._settings.setEnabled(False)  # settings window arrives in M3/M12
        self._quit = QAction(f"Quit {APP_NAME}")
        self._quit.triggered.connect(self._on_quit)

        for action in (self._status, self._key_status):
            self._menu.addAction(action)
        self._menu.addSeparator()
        self._menu.addAction(self._settings)
        self._menu.addSeparator()
        self._menu.addAction(self._quit)

        self._icon.setContextMenu(self._menu)
        self._icon.setToolTip(f"{APP_NAME} {__version__}")
        bus.subscribe(StateChanged, self._on_state_changed)

    @staticmethod
    def _status_text(state: AppState) -> str:
        return f"{APP_NAME}: {state.value}"

    def show(self) -> None:
        if not QSystemTrayIcon.isSystemTrayAvailable():
            logger.warning("System tray is not available on this desktop")
        self._icon.show()

    def hide(self) -> None:
        self._icon.hide()

    def _on_quit(self) -> None:
        task = asyncio.ensure_future(self._bus.publish(QuitRequested(reason="tray")))
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)

    async def _on_state_changed(self, event: StateChanged) -> None:
        self._status.setText(self._status_text(event.current))
        self._icon.setToolTip(f"{APP_NAME}: {event.current.value}")
