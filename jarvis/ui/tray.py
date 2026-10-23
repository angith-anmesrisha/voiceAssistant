"""System tray icon: show/hide the window, quit."""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from PySide6.QtGui import QAction
from PySide6.QtWidgets import QMenu, QSystemTrayIcon

from jarvis import APP_NAME, __version__
from jarvis.app.events import EventBus, NoticeLevel, QuitRequested, UserNotice
from jarvis.app.state import StateChanged
from jarvis.ui.icons import app_icon


class Tray:
    def __init__(
        self, bus: EventBus, show_window: Callable[[], None], window_visible: Callable[[], bool]
    ):
        self._bus = bus
        self._window_visible = window_visible
        self._pending: set[asyncio.Task[None]] = set()
        self._icon = QSystemTrayIcon(app_icon())
        self._menu = QMenu()
        self._status = QAction(f"{APP_NAME}: idle")
        self._status.setEnabled(False)
        show = QAction(f"Open {APP_NAME}")
        show.triggered.connect(show_window)
        quit_action = QAction(f"Quit {APP_NAME}")
        quit_action.triggered.connect(self._on_quit)
        self._actions = [self._status, show, quit_action]
        self._menu.addAction(self._status)
        self._menu.addSeparator()
        self._menu.addAction(show)
        self._menu.addAction(quit_action)
        self._icon.setContextMenu(self._menu)
        self._icon.setToolTip(f"{APP_NAME} {__version__}")
        self._icon.activated.connect(
            lambda reason: (
                show_window() if reason == QSystemTrayIcon.ActivationReason.Trigger else None
            )
        )
        bus.subscribe(StateChanged, self._on_state_changed)
        bus.subscribe(UserNotice, self._on_notice)

    def show(self) -> None:
        if QSystemTrayIcon.isSystemTrayAvailable():
            self._icon.show()

    def hide(self) -> None:
        self._icon.hide()

    def _on_quit(self) -> None:
        task = asyncio.ensure_future(self._bus.publish(QuitRequested(reason="tray")))
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)

    async def _on_state_changed(self, event: StateChanged) -> None:
        self._status.setText(f"{APP_NAME}: {event.current.value}")
        self._icon.setToolTip(f"{APP_NAME}: {event.current.value}")

    async def _on_notice(self, notice: UserNotice) -> None:
        if self._window_visible():
            return  # the window shows it
        icon = {
            NoticeLevel.INFO: QSystemTrayIcon.MessageIcon.Information,
            NoticeLevel.WARNING: QSystemTrayIcon.MessageIcon.Warning,
            NoticeLevel.ERROR: QSystemTrayIcon.MessageIcon.Critical,
        }[notice.level]
        self._icon.showMessage(notice.title, notice.message, icon)
