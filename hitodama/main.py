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
from hitodama.app.events import (
    EventBus,
    NoticeLevel,
    PushToTalkPressed,
    PushToTalkReleased,
    QuitRequested,
    UserNotice,
)
from hitodama.app.logging_setup import configure_logging
from hitodama.app.secrets import get_api_key, has_api_key
from hitodama.app.single_instance import SingleInstance
from hitodama.app.state import StateMachine
from hitodama.core.audio_io import Microphone, Speaker
from hitodama.core.hotkeys import GlobalHotkeys
from hitodama.core.live_session import LiveHandler, LiveSession
from hitodama.core.personality import build_system_prompt
from hitodama.core.voice_loop import VoiceLoop
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
        logger.info("No Gemini API key stored yet; set one from the tray menu")

    talk_hotkey = settings.hotkeys.push_to_talk
    tray = Tray(bus, api_key_set=key_set, talk_hotkey=talk_hotkey.title())
    tray.show()

    voice = VoiceLoop(
        bus,
        state,
        settings,
        session_factory=lambda handler: _make_live_session(settings, handler),
        microphone=Microphone(settings.audio.input_sample_rate, device=settings.audio.input_device),
        speaker_factory=lambda on_drained: Speaker(
            settings.audio.output_sample_rate, on_drained, device=settings.audio.output_device
        ),
    )

    loop = asyncio.get_running_loop()
    hotkeys = GlobalHotkeys(bus, loop)
    hotkeys.add_hold(talk_hotkey, PushToTalkPressed(), PushToTalkReleased())
    if not hotkeys.start():
        await bus.publish(
            UserNotice(
                title="Hotkeys unavailable",
                message="Couldn't watch the keyboard, so push-to-talk won't work.",
                level=NoticeLevel.ERROR,
            )
        )
    logger.info("{} {} running (state: {})", APP_NAME, __version__, state.state.value)

    if exit_after is not None:
        loop.call_later(exit_after, lambda: stop.set())
    with contextlib.suppress(ValueError):  # ValueError: not on the main thread
        signal.signal(signal.SIGINT, lambda *_: loop.call_soon_threadsafe(stop.set))

    try:
        await stop.wait()
    finally:
        hotkeys.stop()
        await voice.shutdown()
        tray.hide()


def _make_live_session(settings: Settings, handler: LiveHandler) -> LiveSession | None:
    api_key = get_api_key()
    if not api_key:
        return None
    from google import genai

    client = genai.Client(api_key=api_key)
    return LiveSession(
        connector=lambda model, config: client.aio.live.connect(model=model, config=config),
        model=settings.models.live,
        handler=handler,
        system_prompt=lambda: build_system_prompt(settings.loadout),
        voice=settings.loadout.voice,
    )


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
