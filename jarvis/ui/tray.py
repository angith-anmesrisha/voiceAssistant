"""System tray icon: show/hide the window, stop JARVIS, start with Windows, quit."""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from PySide6.QtGui import QAction
from PySide6.QtWidgets import QMenu, QSystemTrayIcon

from jarvis import APP_NAME, __version__
from jarvis.app import autostart
from jarvis.app.events import EventBus, NoticeLevel, QuitRequested, UserNotice
from jarvis.app.state import StateChanged
from jarvis.ui.icons import app_icon


class Tray:
    def __init__(
        self,
        bus: EventBus,
        show_window: Callable[[], None],
        window_visible: Callable[[], bool],
        on_pair: Callable[[], None] | None = None,
        on_unpair: Callable[[], None] | None = None,
        on_stop: Callable[[], None] | None = None,
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
        self._autostart = QAction("Start with Windows")
        self._autostart.setCheckable(True)
        self._autostart.setChecked(autostart.enabled())
        self._autostart.toggled.connect(self._on_autostart)
        quit_action = QAction(f"Quit {APP_NAME}")
        quit_action.triggered.connect(self._on_quit)
        self._actions = [self._status, show, self._autostart, quit_action]
        self._menu.addAction(self._status)
        self._menu.addSeparator()
        self._menu.addAction(show)
        if on_stop is not None:
            stop = QAction(f"Stop {APP_NAME}")
            stop.triggered.connect(on_stop)
            self._actions.append(stop)
            self._menu.addAction(stop)
        self._menu.addAction(self._autostart)
        for label, callback in (("Pair a phone…", on_pair), ("Unpair phones", on_unpair)):
            if callback is not None:
                action = QAction(label)
                action.triggered.connect(callback)
                self._actions.append(action)
                self._menu.addAction(action)
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

    def _on_autostart(self, on: bool) -> None:
        try:
            autostart.set_enabled(on)
            notice = UserNotice(
                "Start with Windows",
                f"{APP_NAME} will start in the tray when you sign in."
                if on
                else f"{APP_NAME} won't start by itself any more.",
            )
        except autostart.AutostartError as exc:
            self._autostart.blockSignals(True)
            self._autostart.setChecked(autostart.enabled())
            self._autostart.blockSignals(False)
            notice = UserNotice("Start with Windows", str(exc), NoticeLevel.WARNING)
        task = asyncio.ensure_future(self._bus.publish(notice))
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)

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
