"""Entry point: single-instance lock, then Qt + asyncio (via qasync) with the JARVIS window."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import signal
import sys
from typing import Any

import qasync
from loguru import logger
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from jarvis import APP_NAME, __version__
from jarvis.app.config import Settings, load_settings
from jarvis.app.events import (
    EventBus,
    NoticeLevel,
    ProvidersChanged,
    PushToTalkPressed,
    PushToTalkReleased,
    QuitRequested,
    UserNotice,
)
from jarvis.app.logging_setup import configure_logging
from jarvis.app.paths import database_file
from jarvis.app.single_instance import SingleInstance
from jarvis.app.state import StateMachine


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="jarvis", description=f"{APP_NAME} assistant")
    parser.add_argument("--log-level", default=None, help="override log level (e.g. DEBUG)")
    parser.add_argument("--hidden", action="store_true", help="start in the tray (for startup)")
    parser.add_argument(
        "--exit-after",
        type=float,
        default=None,
        metavar="SECONDS",
        help="quit automatically after N seconds (for smoke tests)",
    )
    return parser.parse_args(argv)


async def _run_app(settings: Settings, args: argparse.Namespace) -> None:
    import httpx

    from jarvis.agent.assistant import Assistant
    from jarvis.controller import AppController
    from jarvis.providers.backends import build_providers
    from jarvis.providers.router import ProviderRouter
    from jarvis.storage.db import HistoryStore
    from jarvis.tools.builtin import build_registry
    from jarvis.tools.desktop import WindowsDesktop
    from jarvis.ui.main_window import MainWindow
    from jarvis.ui.tray import Tray
    from jarvis.voice.audio_io import Microphone, Recorder
    from jarvis.voice.hotkeys import GlobalHotkeys
    from jarvis.voice.tts import Speaker

    bus = EventBus()
    state = StateMachine(bus)
    stop = asyncio.Event()
    tasks: set[asyncio.Task[Any]] = set()

    def spawn(coro: Any) -> None:
        task = asyncio.ensure_future(coro)
        tasks.add(task)
        task.add_done_callback(tasks.discard)

    async def on_quit(event: QuitRequested) -> None:
        logger.info("Quit requested ({})", event.reason)
        stop.set()

    bus.subscribe(QuitRequested, on_quit)

    client = httpx.AsyncClient()
    config = settings.providers
    router = ProviderRouter(
        build_providers(config, client),
        config.priority,
        {name: config.get(name).max_retries for name in ("groq", "gemini", "ollama")},
        on_change=lambda: spawn(bus.publish(ProvidersChanged())),
    )
    store = HistoryStore(database_file())
    desktop = WindowsDesktop()
    desktop.start_tracking()
    registry = build_registry(settings.tools, desktop)

    hotkey_label = "+".join(
        part.strip().title() for part in settings.hotkeys.push_to_talk.split("+")
    )
    window = MainWindow(hotkey_label)
    assistant = Assistant(
        router,
        store,
        registry,
        bus,
        state,
        confirm=window.confirm,
        capture_screen=window.capture_screen,
        history_messages=settings.history_messages,
        max_tool_rounds=settings.tools.max_tool_rounds,
    )
    speaker = Speaker(settings.voice.tts_voice, settings.voice.tts_rate)
    speaker.start()
    recorder = Recorder(
        Microphone(settings.voice.input_sample_rate, device=settings.voice.input_device),
        settings.voice.input_sample_rate,
    )
    controller = AppController(bus, state, assistant, router, recorder, speaker, settings.voice)

    def set_mode(mode: Any) -> None:
        controller.mode = mode

    window.bind(
        bus,
        router,
        level=lambda: controller.mic_level,
        on_submit=controller.submit,
        on_mic=controller.toggle_listening,
        on_mode=set_mode,
        on_new_chat=lambda: spawn(assistant.new_conversation()),
    )

    # Restore the last conversation into the panel.
    from jarvis.app.events import ChatEntry, EntryKind

    conversation = await assistant.conversation_id()
    for message in await store.messages(conversation, limit=60):
        if message.role in ("user", "assistant") and message.content:
            kind = EntryKind.USER if message.role == "user" else EntryKind.ASSISTANT
            window.add_entry(
                ChatEntry(kind, message.content, message.provider, message.model, message.mode)
            )

    tray = Tray(bus, show_window=window.show_and_raise, window_visible=window.isVisible)
    tray.show()
    if not args.hidden:
        window.show()

    loop = asyncio.get_running_loop()
    hotkeys = GlobalHotkeys(bus, loop)
    hotkeys.add_hold(settings.hotkeys.push_to_talk, PushToTalkPressed(), PushToTalkReleased())
    if not hotkeys.start():
        await bus.publish(
            UserNotice(
                "Hotkey unavailable",
                "Couldn't watch the keyboard; use the mic button instead.",
                NoticeLevel.WARNING,
            )
        )

    probes = asyncio.create_task(router.run_probes(config.probe_interval_s))
    logger.info("{} {} running", APP_NAME, __version__)

    if args.exit_after is not None:
        loop.call_later(args.exit_after, stop.set)
    with contextlib.suppress(ValueError):  # ValueError: not on the main thread
        signal.signal(signal.SIGINT, lambda *_: loop.call_soon_threadsafe(stop.set))

    try:
        await stop.wait()
    finally:
        probes.cancel()
        hotkeys.stop()
        desktop.stop_tracking()
        await controller.shutdown()
        tray.hide()
        window.quitting = True
        window.close()
        await client.aclose()
        store.close()


def run(argv: list[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    settings = load_settings()
    log_file = configure_logging(args.log_level or settings.log_level)
    logger.info("Starting {} {} (log: {})", APP_NAME, __version__, log_file)

    with SingleInstance() as is_only_instance:
        if not is_only_instance:
            logger.warning("{} is already running; exiting", APP_NAME)
            return 0

        if sys.platform == "win32":
            import ctypes

            with contextlib.suppress(Exception):  # crisp text on high-DPI screens
                ctypes.windll.shcore.SetProcessDpiAwareness(2)  # type: ignore[attr-defined]

        app = QApplication.instance() or QApplication(sys.argv)
        assert isinstance(app, QApplication)
        app.setApplicationName(APP_NAME)
        app.setApplicationVersion(__version__)
        app.setQuitOnLastWindowClosed(False)

        from jarvis.ui import theme
        from jarvis.ui.icons import app_icon

        theme.load_fonts()
        app.setStyleSheet(theme.APP_STYLESHEET)
        app.setWindowIcon(app_icon())

        # Wake the event loop periodically so Python can deliver Ctrl+C.
        signal_timer = QTimer()
        signal_timer.start(250)
        signal_timer.timeout.connect(lambda: None)

        loop = qasync.QEventLoop(app)
        asyncio.set_event_loop(loop)
        try:
            with loop:
                loop.run_until_complete(_run_app(settings, args))
        except Exception:
            logger.exception("Fatal error")
            return 1
        finally:
            signal_timer.stop()

    logger.info("{} exited cleanly", APP_NAME)
    logger.complete()
    return 0
