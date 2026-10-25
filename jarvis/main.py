"""Entry point: single-instance lock, then Qt + asyncio (via qasync) with the JARVIS window."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import signal
import sys
import time
from collections.abc import Awaitable, Callable
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import qasync
from loguru import logger
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from jarvis import APP_NAME, __version__
from jarvis.app.config import Settings, load_settings, save_choice
from jarvis.app.events import (
    ChatEntry,
    EntryKind,
    EventBus,
    NoticeLevel,
    ProvidersChanged,
    PushToTalkPressed,
    PushToTalkReleased,
    QuitRequested,
    UserNotice,
    WakeWordHeard,
)
from jarvis.app.logging_setup import configure_logging
from jarvis.app.paths import code_dir, database_file, keep_file, voices_dir
from jarvis.app.single_instance import SingleInstance
from jarvis.app.state import AppState, StateChanged, StateMachine

if TYPE_CHECKING:
    import httpx

    from jarvis.agent.routines import Briefing, Job
    from jarvis.providers.router import ProviderRouter
    from jarvis.storage.db import HistoryStore
    from jarvis.storage.keep import Keep
    from jarvis.storage.memory import Memory
    from jarvis.tools.builtin import SlideTools


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
    from jarvis.agent.context import situation
    from jarvis.agent.guard import PromptGuard
    from jarvis.controller import AppController
    from jarvis.providers.backends import build_providers
    from jarvis.providers.openai_compat import OpenAICompatibleProvider
    from jarvis.providers.router import ProviderRouter
    from jarvis.storage.db import HistoryStore
    from jarvis.tools.builtin import build_registry
    from jarvis.tools.desktop import WindowsDesktop
    from jarvis.ui.main_window import MainWindow
    from jarvis.ui.tray import Tray
    from jarvis.voice.audio_io import Microphone, Recorder
    from jarvis.voice.hotkeys import GlobalHotkeys
    from jarvis.voice.local_stt import WindowsDictation
    from jarvis.voice.tts import Speaker
    from jarvis.voice.wake_word import WakeWordListener

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
    # Which models are really configured (settings.json can override the defaults).
    logger.info(
        "Models: Groq {} · Gemini {} (then {}) · Ollama {}",
        config.groq.chat_model,
        config.gemini.chat_model,
        ", ".join(config.gemini.fallback_models) or "none",
        config.ollama.chat_model,
    )
    router = ProviderRouter(
        build_providers(config, client),
        config.priority,
        {name: config.get(name).max_retries for name in config.priority},
        on_change=lambda: spawn(bus.publish(ProvidersChanged())),
    )
    if config.preferred:
        try:
            router.set_preferred(config.preferred)
        except KeyError:
            logger.warning("Unknown preferred provider in settings; using automatic")
    store = HistoryStore(database_file())
    from jarvis.agent.background import Background
    from jarvis.storage.keep import Keep, memory_prompt
    from jarvis.tools.steam import installed_games, steam_folder
    from jarvis.tools.system_ctl import WindowsSystem
    from jarvis.tools.undo import UndoJournal
    from jarvis.tools.windows import WindowsWindows
    from jarvis.ui.camera import capture_webcam

    keep = Keep(keep_file())
    from jarvis.tools.calendar_ics import Calendar

    calendar = Calendar(client)  # read-only, from JARVIS_CALENDAR_ICS if set
    memory = _long_memory(settings, client)
    desktop = WindowsDesktop()
    desktop.start_tracking()
    from jarvis.tools.apps import AppIndex

    installed = AppIndex()
    from jarvis.tools.ui import WindowsUi

    # Reads and operates app windows (UI Automation) for multi-step tasks inside apps.
    ui = WindowsUi(desktop.previous_window_handle, desktop.press, desktop.type_text)
    # Llama Prompt Guard on Groq screens clipboard, file and screen text for injections.
    groq = router.provider("groq")
    guard = PromptGuard(
        groq if isinstance(groq, OpenAICompatibleProvider) else None,
        settings.providers.groq.guard_threshold,
    )
    registry = build_registry(
        settings.tools,
        desktop,
        installed,
        ui=ui,
        undo=UndoJournal(),
        keep=keep,
        http=client,
        windows=WindowsWindows(),
        system=WindowsSystem(),
        steam_games=lambda: installed_games(steam_folder()),
        camera=capture_webcam,
        describe_image=lambda photo, question: _describe_photo(router, photo, question),
        code_folder=code_dir() if settings.tools.code_runner else None,
        memory=memory,
        slides=_slide_tools(settings, router, lambda: pick_upload(), bus, spawn),
        calendar=calendar,
        writing=_writing_tools(settings, router, lambda: pick_upload(), bus, spawn, guard),
    )

    from jarvis.agent.workflows import (
        WorkflowStore,
        WorkflowTriggers,
        register_workflow_tools,
        starters,
    )
    from jarvis.app.events import RunWorkflow
    from jarvis.app.paths import data_dir

    first_time = not (data_dir() / "workflows.json").exists()
    workflows = WorkflowStore(data_dir() / "workflows.json")
    if first_time:
        for starter in starters():
            workflows.put(starter)

    async def queue_workflow(workflow: Any, reason: str) -> None:
        await bus.publish(RunWorkflow(workflow.id, reason))

    register_workflow_tools(registry, workflows, queue_workflow)
    from jarvis.agent.watchtower import Watchtower, register_watch_tools
    from jarvis.agent.workflows import folder_watches
    from jarvis.tools.files import FileAccessError, default_area

    area = default_area()

    async def folder_changed(change: Any) -> None:
        if change.watch.id.startswith("wf-"):
            workflow_id = change.watch.id.removeprefix("wf-")
            await bus.publish(
                RunWorkflow(workflow_id, f"{change.path.name} arrived", file=str(change.path))
            )

    watch_plan = settings.watchtower
    tower = Watchtower(
        keep,
        interval_s=watch_plan.interval_s,
        deadlines=watch_plan.deadline_alerts,
        calendar=calendar,
        calendar_alerts=watch_plan.calendar_alerts,
        calendar_minutes=watch_plan.calendar_minutes,
        on_change=folder_changed,
        quiet_hours=(settings.routines.quiet_from, settings.routines.quiet_until),
        extra=lambda: folder_watches(workflows, area.resolve),
    )
    register_watch_tools(registry, tower, area)
    briefing = _briefing(settings, keep, client, calendar)

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
        confirm=lambda request: controller.confirm(request),
        capture_screen=window.capture_screen,
        history_messages=settings.history_messages,
        max_tool_rounds=settings.tools.max_tool_rounds,
        on_prefer=lambda name: save_choice("providers", "preferred", name or ""),
        situation=situation(
            desktop,
            router,
            about=lambda: (
                f"About you: JARVIS {__version__}, running on the user's PC; speaking with "
                f"{speaker.voice_name}; memory, reminders and history stay on this PC."
            ),
        ),
        guard=guard,
        memory=lambda: _memory(keep, memory_prompt),
        on_pair=lambda: _pair_phone(dashboard),
        long_memory=memory,
        local_first=lambda: settings.routing.local_first,
        local_timeout_s=settings.routing.local_timeout_s,
        workflows=workflows,
        briefing=lambda: briefing.compose(datetime.now().astimezone()),
        on_open_workflows=lambda tab: open_workflows(tab),
    )
    speaker = Speaker(
        settings.voice.tts_voice,
        settings.voice.tts_rate,
        engine=settings.voice.tts_engine,
        neural_voice=settings.voice.neural_voice,
        neural_speed=settings.voice.neural_speed,
        model_folder=voices_dir(),
    )
    speaker.start()
    recorder = Recorder(
        Microphone(settings.voice.input_sample_rate, device=settings.voice.input_device),
        settings.voice.input_sample_rate,
        silence_s=settings.voice.end_silence_s,
    )
    from jarvis.voice.whistle import OfflineStt, WhistleStt

    # Offline speech-to-text, used when no online provider can hear: Whistle if
    # installed (pip install cactus-needle), then Windows' own dictation.
    offline_stt = OfflineStt(
        ([WhistleStt(router.vocabulary_words)] if settings.voice.whistle else [])
        + [WindowsDictation()]
    )
    controller = AppController(
        bus, state, assistant, router, recorder, speaker, settings.voice, offline_stt
    )
    controller.set_confirm_ui(
        window.confirm, window.confirm_overlay.settle, bring_forward=window.show_and_raise
    )

    from jarvis.app import keys
    from jarvis.ui.widgets.keys import KeyField

    key_links = {
        "groq": "https://console.groq.com/keys",
        "gemini": "https://aistudio.google.com/apikey",
        "openrouter": "https://openrouter.ai/settings/keys",
    }

    def key_fields() -> list[KeyField]:
        fields = []
        for name, url in key_links.items():
            env = config.get(name).api_key_env
            if env:
                label = "OpenRouter" if name == "openrouter" else name.title()
                fields.append(KeyField(label, env, url, keys.get_key(env) is not None))
        return fields

    def save_keys(values: dict[str, str]) -> None:
        persisted = all([keys.save_key(env, value) for env, value in values.items()])
        where = "for this and future sessions" if persisted else "for this session only"
        window.show_notice(f"API key saved {where}. Checking providers…")
        spawn(router.probe_all())

    window.bind(
        bus,
        router,
        level=lambda: controller.mic_level,
        on_submit=controller.submit,
        on_mic=controller.toggle_listening,
        on_new_chat=lambda: spawn(assistant.new_conversation()),
        key_fields=key_fields,
        on_save_keys=save_keys,
        on_prefer=lambda name: window.show_notice(assistant.prefer(name)),
    )

    # Restore the last conversation into the panel.

    conversation = await assistant.conversation_id()
    for message in await store.messages(conversation, limit=60):
        if message.role in ("user", "assistant") and message.content:
            kind = EntryKind.USER if message.role == "user" else EntryKind.ASSISTANT
            window.add_entry(
                ChatEntry(kind, message.content, message.provider, message.model, message.mode)
            )

    from jarvis.remote.dashboard import Dashboard

    dashboard = Dashboard(bus, keep, controller.submit, port=settings.dashboard.port)

    async def pair_from_tray() -> None:
        shown, _said = await _pair_phone(dashboard)
        window.show_and_raise()
        await bus.publish(ChatEntry(EntryKind.ASSISTANT, shown, mode="phone"))

    async def unpair_from_tray() -> None:
        await dashboard.unpair_all()
        await bus.publish(UserNotice("Phone", "Every paired phone was forgotten."))

    async def stop_jarvis() -> None:
        await controller.interrupt()

    # Esc in JARVIS's window stops it (only there: Esc belongs to every other app).
    window.on_escape = lambda: spawn(stop_jarvis())

    # Files given to JARVIS ("upload"): dropped on the window, or picked when asked.
    dropped: list[tuple[Path, float]] = []

    async def pick_upload() -> Path | None:
        if dropped and time.monotonic() - dropped[-1][1] < 600:
            return dropped.pop()[0]
        return await window.pick_file("Choose the file for JARVIS")

    def on_drop(path: Path) -> None:
        dropped[:] = [(path, time.monotonic())]
        notice = UserNotice(
            "File received",
            f"Got {path.name}. Now say what to do with it, e.g. “make a 15 slide ppt from it”.",
        )
        spawn(bus.publish(notice))

    window.on_file_dropped = on_drop

    automation: list[Any] = []

    def open_workflows(tab: str = "workflows") -> None:
        from jarvis.ui.watchtower import AutomationWindow, WatchtowerPanel
        from jarvis.ui.workflows import WorkflowBuilder

        if not automation:

            async def add_watch(
                folder: str, pattern: str, events: list[str], deep: bool
            ) -> str | None:
                try:
                    await tower.add(area, folder, pattern, events, deep)
                except FileAccessError as exc:
                    return f"I can't watch that: {exc}."
                return None

            async def remove_watch(watch_id: str) -> None:
                await tower.remove(None, watch_id)

            def set_option(key: str, on: bool) -> None:
                setattr(tower, "deadlines" if key == "deadline_alerts" else key, on)
                save_choice("watchtower", key, on)

            panel = WatchtowerPanel(
                tower.watches,
                add_watch,
                remove_watch,
                options={
                    "deadline_alerts": tower.deadlines,
                    "calendar_alerts": tower.calendar_alerts,
                },
                on_option=set_option,
            )
            builder = WorkflowBuilder(
                workflows, lambda workflow: controller.run_workflow(workflow, "test")
            )
            automation.append(AutomationWindow(builder, panel))
        automation[0].open(tab)

    tray = Tray(
        bus,
        show_window=window.show_and_raise,
        window_visible=window.isVisible,
        on_pair=lambda: spawn(pair_from_tray()),
        on_unpair=lambda: spawn(unpair_from_tray()),
        on_stop=lambda: spawn(stop_jarvis()),
        on_workflows=lambda: open_workflows(),
    )
    tray.show()
    if not args.hidden:
        window.show()

    loop = asyncio.get_running_loop()
    hotkeys = GlobalHotkeys(bus, loop)
    hotkeys.add_hold(settings.hotkeys.push_to_talk, PushToTalkPressed(), PushToTalkReleased())
    if settings.hotkeys.dictation.strip():
        from jarvis.app.events import DictationPressed, DictationReleased
        from jarvis.voice.dictation import Dictation, Foreground

        def foreground() -> Foreground | None:
            found = desktop.foreground()
            return Foreground(*found) if found is not None else None

        controller.set_dictation(Dictation(desktop, foreground).paste)
        hotkeys.add_hold(settings.hotkeys.dictation, DictationPressed(), DictationReleased())
    if not hotkeys.start():
        await bus.publish(
            UserNotice(
                "Hotkey unavailable",
                "Couldn't watch the keyboard; use the mic button instead.",
                NoticeLevel.WARNING,
            )
        )

    # Offline wake word: paused while JARVIS records (and, unless wake_while_busy, while
    # it speaks or works), so it never hears itself; "Jarvis, stop" interrupts otherwise.
    wake = WakeWordListener(
        settings.voice.wake_phrases,
        on_wake=lambda: loop.call_soon_threadsafe(lambda: spawn(bus.publish(WakeWordHeard()))),
        min_confidence=settings.voice.wake_min_confidence,
    )

    async def pause_wake(event: StateChanged) -> None:
        busy = event.current is not AppState.IDLE
        wake.pause(
            event.current is AppState.LISTENING or (busy and not settings.voice.wake_while_busy)
        )

    async def check_wake() -> None:
        await asyncio.sleep(4)
        if not wake.running and wake.error:
            await bus.publish(
                UserNotice(
                    "Wake word unavailable",
                    f"Windows speech recognition couldn't start ({wake.error}). "
                    f"Use {hotkey_label} or the mic button.",
                    NoticeLevel.WARNING,
                )
            )

    if settings.voice.wake_word and wake.available:
        bus.subscribe(StateChanged, pause_wake)
        wake.start()
        spawn(check_wake())

    background = Background(
        bus,
        keep,
        jobs=_routines(
            settings,
            keep,
            store,
            router,
            client,
            memory,
            briefing=briefing,
            workflows=WorkflowTriggers(workflows, keep, queue_workflow),
            watchtower=tower if settings.watchtower.enabled else None,
        ),
    )
    background.start()
    spawn(_start_dashboard(dashboard, bus))
    if settings.tools.plugins:
        spawn(_load_plugins(bus, registry, keep, controller))
    spawn(asyncio.to_thread(installed.refresh))  # so "open Valorant" is instant later
    if memory is not None:
        from jarvis.tools.personal import mirror_facts

        spawn(mirror_facts(keep, memory))  # Facts.md matches what's remembered

    def voice_status(title: str, message: str) -> None:
        notice = UserNotice(title, message)
        loop.call_soon_threadsafe(lambda: spawn(bus.publish(notice)))

    spawn(asyncio.to_thread(speaker.prepare_neural, settings.voice.download_voice, voice_status))
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
        wake.stop()
        hotkeys.stop()
        desktop.stop_tracking()
        ui.close()
        await background.stop()
        await dashboard.stop()
        await controller.shutdown()
        tray.hide()
        window.quitting = True
        window.close()
        await client.aclose()
        store.close()
        keep.close()


def run(argv: list[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    settings = load_settings()
    log_file = configure_logging(args.log_level or settings.log_level)
    logger.info("Starting {} {} (log: {})", APP_NAME, __version__, log_file)

    with SingleInstance() as is_only_instance:
        if not is_only_instance:
            logger.warning("{} is already running; exiting", APP_NAME)
            return 0

        # Qt 6 makes the process per-monitor DPI aware (v2) itself; setting it here first
        # made Qt's own call fail with "Access is denied".
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


async def _pair_phone(dashboard: Any) -> tuple[str, str]:
    """Start the dashboard if needed and show a one-time code (never sent to an AI)."""
    try:
        code = await dashboard.pair()
    except OSError as exc:
        message = f"The phone dashboard couldn't start ({exc.strerror or exc})."
        return message, message
    spaced = " ".join(code)
    shown = (
        f"On your phone, on the same Wi-Fi, open {dashboard.address} and enter {code}. "
        "The code works once, for 10 minutes. (Windows may ask whether JARVIS can use "
        "private networks: allow it.)"
    )
    return shown, f"The address is on screen. Your code is {spaced}."


async def _start_dashboard(dashboard: Any, bus: Any) -> None:
    try:
        await dashboard.start_if_paired()
    except OSError as exc:
        await bus.publish(
            UserNotice("Phone", f"The phone dashboard couldn't start ({exc.strerror or exc}).")
        )


async def _load_plugins(bus: Any, registry: Any, keep: Any, controller: Any) -> None:
    """Load the user's plugins; new or changed files are shown and must be confirmed."""
    from jarvis.agent.assistant import ConfirmRequest
    from jarvis.app.paths import plugins_dir
    from jarvis.tools.plugins import PluginLoader
    from jarvis.tools.registry import Risk

    async def ask(headline: str, code: str) -> bool:
        allowed: bool = await controller.confirm(
            ConfirmRequest("load_plugin", Risk.HIGH, f"{headline}\n{code}")
        )
        return allowed

    loaded = await PluginLoader(plugins_dir(), registry, keep, ask).load_all()
    if loaded:
        tools = sum(len(names) for names in loaded.values())
        await bus.publish(UserNotice("Plugins", f"Loaded {', '.join(loaded)} ({tools} tool(s))."))


async def _describe_photo(router: ProviderRouter, photo: bytes, question: str) -> str:
    """A vision model looks at one webcam photo (Gemini or a local model that can see)."""
    from jarvis.agent.prompts import system_prompt
    from jarvis.providers.openai_compat import image_part
    from jarvis.providers.router import AllProvidersFailedError
    from jarvis.voice.spoken_text import without_thinking

    prompt = (
        system_prompt(spoken=False)
        + "\n\nYou are given one photo from the user's webcam, taken just now with their "
        "permission. Answer their question about it kindly and briefly; describe people "
        "without guessing identity, age or anything sensitive."
    )
    try:
        result = await router.chat(
            [
                {"role": "system", "content": prompt},
                {
                    "role": "user",
                    "content": [{"type": "text", "text": question}, image_part(photo)],
                },
            ],
            vision=True,
        )
    except AllProvidersFailedError:
        return "No AI that can see images answered; the photo was discarded."
    return without_thinking(result.content or "") or "Nothing notable in the photo."


def _routines(
    settings: Settings,
    keep: Keep,
    store: HistoryStore,
    router: ProviderRouter,
    client: httpx.AsyncClient,
    memory: Memory | None = None,
    briefing: Job | None = None,
    workflows: Job | None = None,
    watchtower: Job | None = None,
) -> list[Job]:
    """The things JARVIS brings up by itself, as configured in ``routines``."""
    from jarvis.agent.prompts import SUMMARY_PROMPT
    from jarvis.agent.routines import (
        CheckIns,
        HardwareWatch,
        MarketWatch,
        SessionSummaries,
        TopicWatch,
    )
    from jarvis.tools import live, web
    from jarvis.voice.spoken_text import without_thinking

    plan = settings.routines
    locale = settings.tools.news_locale
    quiet_hours = (plan.quiet_from, plan.quiet_until)

    async def topic_news(topic: str) -> list[web.Headline]:
        return await web.news(client, topic, locale, limit=3)

    async def summarize(transcript: str) -> str:
        result = await router.chat(
            [
                {"role": "system", "content": SUMMARY_PROMPT},
                {"role": "user", "content": transcript},
            ]
        )
        return without_thinking(result.content or "")

    async def price(name: str) -> live.Quote:
        return await live.price_of(client, name, locale)

    from jarvis.voice.vocabulary import VocabularyRefresh

    jobs: list[Job] = [
        TopicWatch(keep, topic_news, quiet_hours),
        VocabularyRefresh(keep, settings.voice.custom_words, router.set_vocabulary),
    ]
    if plan.briefing and briefing is not None:
        jobs.append(briefing)
    if workflows is not None:
        jobs.append(workflows)
    if watchtower is not None:
        jobs.append(watchtower)
    if plan.market_watch:
        jobs.append(MarketWatch(keep, price, quiet_hours))
    if plan.hardware_alerts:
        jobs.append(HardwareWatch(battery_low=plan.battery_low))
    if plan.check_in_hours > 0:
        jobs.append(CheckIns(plan.check_in_hours, quiet_hours=quiet_hours))
    local = router.provider("ollama")
    if settings.routing.local_first and settings.routing.keep_alive and local is not None:
        from jarvis.agent.routing import KeepLocalWarm

        keep_alive = settings.routing.keep_alive
        jobs.append(KeepLocalWarm(router.local_ready, lambda: local.keep_warm(keep_alive)))  # type: ignore[attr-defined]
    if plan.summaries:
        jobs.append(
            SessionSummaries(
                store, keep, summarize, memory=memory, learn_facts=settings.memory.learn_facts
            )
        )
    return jobs


def _briefing(
    settings: Settings, keep: Keep, client: httpx.AsyncClient, calendar: Any = None
) -> Briefing:
    """The morning briefing: said once a day by itself, and the "briefing" workflow step."""
    from jarvis.agent.routines import Briefing
    from jarvis.tools import live, web
    from jarvis.tools.personal import home_city

    plan = settings.routines
    locale = settings.tools.news_locale

    async def weather() -> web.Forecast | None:
        place = await home_city(keep, settings.tools.home_place)
        return await web.forecast(client, place) if place else None

    async def headlines() -> list[web.Headline]:
        if plan.briefing_headlines <= 0:
            return []
        return await web.news(client, "", locale, limit=plan.briefing_headlines)

    async def markets() -> list[live.Quote]:
        found = await asyncio.gather(
            *(live.price_of(client, name, locale) for name in plan.briefing_markets[:5]),
            return_exceptions=True,
        )
        return [q for q in found if isinstance(q, live.Quote)]

    return Briefing(
        keep,
        weather,
        headlines,
        markets=markets if plan.briefing_markets else None,
        calendar=calendar,
    )


def _slide_tools(
    settings: Settings,
    router: ProviderRouter,
    pick_file: Callable[[], Awaitable[Path | None]],
    bus: Any,
    spawn: Callable[[Any], Any],
) -> SlideTools:
    """PowerPoint decks, written only by free models (OpenRouter's, then Groq's)."""
    from jarvis.app.paths import presentations_dir
    from jarvis.tools.builtin import SlideTools
    from jarvis.tools.slides import DeckWriter

    plan = settings.slides
    writer = DeckWriter(
        router.provider("openrouter"),
        router.provider("groq"),
        plan.models,
        groq_model=plan.groq_model,
        timeout_s=plan.timeout_s,
        notify=lambda text: spawn(bus.publish(UserNotice("Slides", text))),
    )
    return SlideTools(
        writer,
        presentations_dir(plan.folder),
        pick_file=pick_file,
        open_when_done=plan.open_when_done,
        max_slides=plan.max_slides,
    )


def _writing_tools(
    settings: Settings,
    router: ProviderRouter,
    pick_file: Callable[[], Awaitable[Path | None]],
    bus: Any,
    spawn: Callable[[Any], Any],
    guard: Any,
) -> Any:
    """Document Assistant, Research Agent and Writing Assistant (free models first)."""
    from jarvis.app.paths import research_dir, writing_dir
    from jarvis.tools.builtin import WritingTools
    from jarvis.tools.writer import FreeWriter

    plan = settings.slides  # the same free models as the decks
    writer = FreeWriter(
        router.provider("openrouter"),
        router.provider("groq"),
        plan.models,
        groq_model=plan.groq_model,
        timeout_s=plan.timeout_s,
        notify=lambda text: spawn(bus.publish(UserNotice("Writing", text))),
        fallback=router.chat,
    )
    return WritingTools(
        writer,
        writing_dir(),
        research_dir(),
        pick_file=pick_file,
        screen=guard.suspicious,
        open_when_done=plan.open_when_done,
    )


def _long_memory(settings: Settings, client: httpx.AsyncClient) -> Memory | None:
    """The memory vault (Markdown notes), with Hindsight if a server is configured."""
    from jarvis.app.paths import memory_dir
    from jarvis.storage.hindsight import Hindsight
    from jarvis.storage.memory import Memory
    from jarvis.storage.vault import Vault

    plan = settings.memory
    if not plan.vault:
        return None
    vault = Vault(memory_dir(plan.vault_path))
    try:
        vault.ensure()
    except OSError as exc:
        logger.warning(
            "Memory folder unavailable ({}); long-term memory is off", type(exc).__name__
        )
        return None
    hindsight = (
        Hindsight(client, plan.hindsight_url, plan.hindsight_bank)
        if plan.hindsight_url.strip()
        else None
    )
    return Memory(
        vault,
        hindsight=hindsight,
        log_conversations=plan.log_conversations,
        auto_recall=plan.auto_recall,
        budget=plan.recall_chars,
    )


async def _memory(keep: Any, render: Any) -> str:
    return str(render(await keep.facts()))
