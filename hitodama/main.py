"""Entry point: single-instance lock, then Qt + asyncio (via qasync) with a tray icon."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import signal
import sys

import qasync
from loguru import logger
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from hitodama import APP_NAME, __version__
from hitodama.app.config import Settings, load_settings
from hitodama.app.events import EventBus, QuitRequested
from hitodama.app.logging_setup import configure_logging
from hitodama.app.secrets import has_api_key
from hitodama.app.single_instance import SingleInstance
from hitodama.app.state import StateMachine
from hitodama.ui.tray import Tray


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="hitodama", description=f"{APP_NAME} companion")
    parser.add_argument("--log-level", default=None, help="override log level (e.g. DEBUG)")
    parser.add_argument(
        "--exit-after",
        type=float,
        default=None,
        metavar="SECONDS",
        help="quit automatically after N seconds (for smoke tests)",
    )
    return parser.parse_args(argv)


async def _run_app(settings: Settings, exit_after: float | None) -> None:
    bus = EventBus()
    state = StateMachine(bus)
    stop = asyncio.Event()

    async def on_quit(event: QuitRequested) -> None:
        logger.info("Quit requested ({})", event.reason)
        stop.set()

    bus.subscribe(QuitRequested, on_quit)

    key_set = has_api_key()
    if not key_set:
        logger.info("No Gemini API key stored yet; onboarding will ask for one (M12)")

    tray = Tray(bus, api_key_set=key_set)
    tray.show()
    logger.info("{} {} running (state: {})", APP_NAME, __version__, state.state.value)

    loop = asyncio.get_running_loop()
    if exit_after is not None:
        loop.call_later(exit_after, lambda: stop.set())
    with contextlib.suppress(ValueError):  # ValueError: not on the main thread
        signal.signal(signal.SIGINT, lambda *_: loop.call_soon_threadsafe(stop.set))

    try:
        await stop.wait()
    finally:
        tray.hide()


def run(argv: list[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    settings = load_settings()
    log_file = configure_logging(args.log_level or settings.log_level)
    logger.info("Starting {} {} (log: {})", APP_NAME, __version__, log_file)

    with SingleInstance() as is_only_instance:
        if not is_only_instance:
            logger.warning("{} is already running; exiting", APP_NAME)
            return 0

        app = QApplication.instance() or QApplication(sys.argv)
        assert isinstance(app, QApplication)
        app.setApplicationName(APP_NAME)
        app.setApplicationVersion(__version__)
        app.setQuitOnLastWindowClosed(False)

        # Wake the event loop periodically so Python can deliver Ctrl+C.
        signal_timer = QTimer()
        signal_timer.start(250)
        signal_timer.timeout.connect(lambda: None)

        loop = qasync.QEventLoop(app)
        asyncio.set_event_loop(loop)
        try:
            with loop:
                loop.run_until_complete(_run_app(settings, args.exit_after))
        except Exception:
            logger.exception("Fatal error")
            return 1
        finally:
            signal_timer.stop()

    logger.info("{} exited cleanly", APP_NAME)
    logger.complete()
    return 0
