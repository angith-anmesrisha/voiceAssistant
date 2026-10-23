"""Render the JARVIS window in each state to PNG files (no network, no audio).

Usage:  QT_QPA_PLATFORM=offscreen python scripts/render_ui.py [output_dir]
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime
from pathlib import Path

import httpx
import qasync
from PySide6.QtWidgets import QApplication

from jarvis.agent.prompts import Mode
from jarvis.app.config import Settings
from jarvis.app.events import ChatEntry, EntryKind
from jarvis.app.state import AppState
from jarvis.providers.backends import build_providers
from jarvis.providers.base import ProviderState, ProviderStatus
from jarvis.providers.network import NetworkStatus
from jarvis.providers.router import ProviderRouter
from jarvis.tools.registry import Risk
from jarvis.ui import theme
from jarvis.ui.main_window import MainWindow

SIZE = (1440, 900)


def demo_router(online: bool) -> ProviderRouter:
    config = Settings().providers
    router = ProviderRouter(build_providers(config, httpx.AsyncClient()), config.priority, {})
    if online:
        router.statuses.update(
            groq=ProviderStatus(ProviderState.READY, latency_ms=86),
            gemini=ProviderStatus(ProviderState.READY, latency_ms=142),
            ollama=ProviderStatus(ProviderState.MODEL_MISSING, "run: ollama pull llama3.2"),
        )
        router.network = NetworkStatus(True, 24)
        router.last_used = ("Groq", "openai/gpt-oss-120b")
    else:
        router.statuses.update(
            groq=ProviderStatus(ProviderState.OFFLINE, "unreachable (ConnectError)"),
            gemini=ProviderStatus(ProviderState.OFFLINE, "unreachable (ConnectError)"),
            ollama=ProviderStatus(ProviderState.OFFLINE, "Ollama isn't running"),
        )
        router.network = NetworkStatus(False)
    return router


CONVERSATION = [
    ChatEntry(EntryKind.USER, "What's on my calendar vibe today? Keep it short.", mode="assist"),
    ChatEntry(
        EntryKind.ASSISTANT,
        "I can't see your calendar yet, but it's Friday afternoon: a good time to clear small "
        "tasks and plan next week. Want me to open your mail?",
        "Groq",
        "openai/gpt-oss-120b",
        "assist",
    ),
    ChatEntry(EntryKind.USER, "Open YouTube and search lo-fi coding music", mode="execute"),
    ChatEntry(EntryKind.ACTION, "✓ Search youtube for lo-fi coding music."),
    ChatEntry(
        EntryKind.ASSISTANT,
        "Done. YouTube is open with lo-fi coding music results.",
        "Groq",
        "openai/gpt-oss-120b",
        "execute",
    ),
    ChatEntry(EntryKind.USER, "Type a quick thank-you note into Notepad", mode="execute"),
]


async def render(out: Path) -> None:
    window = MainWindow()
    window.resize(*SIZE)
    window.header.freeze_clock(datetime(2026, 10, 23, 18, 42))
    window._router = demo_router(True)
    window.refresh_providers()
    window.network.cpu.set_value(23)
    window.network.ram.set_value(61)
    window.network.battery.set_value(84, "84%+", higher_is_better=True)
    for entry in CONVERSATION:
        window.add_entry(entry)
    window.show()

    async def shot(name: str, state: AppState, t: float, level: float = 0.0) -> None:
        window.set_app_state(state)
        window.orb.freeze(t, level)
        for _ in range(5):
            QApplication.processEvents()
            await asyncio.sleep(0.02)
        path = out / f"{name}.png"
        window.grab().save(str(path))
        print(path)

    await shot("01-idle", AppState.IDLE, 2.0)
    window.command.input.setText("Summarize the error on my screen")
    window.modes.set_mode(Mode.ANALYZE)
    await shot("02-listening", AppState.LISTENING, 3.3, 0.75)
    window.command.input.clear()
    window.modes.set_mode(Mode.ASSIST)
    await shot("03-thinking", AppState.THINKING, 4.1)
    await shot("04-speaking", AppState.SPEAKING, 5.6)
    window.modes.set_mode(Mode.EXECUTE)
    await shot("05-executing", AppState.ACTING, 6.2)

    future = window.confirm_overlay.ask(
        "Type 34 characters into your last window: “Thanks for today, see you Monday!”",
        Risk.MEDIUM,
    )
    await shot("06-confirm-medium", AppState.THINKING, 7.0)
    window.confirm_overlay.ask(
        "Shutdown the PC in 60 seconds. Unsaved work in open apps may be lost.", Risk.HIGH
    )
    await shot("07-confirm-high", AppState.THINKING, 7.4)
    window.confirm_overlay.hide()
    del future

    window._router = demo_router(False)
    window.refresh_providers()
    window.modes.set_mode(Mode.ASSIST)
    window.chat.clear()
    window.add_entry(
        ChatEntry(
            EntryKind.ERROR,
            "I couldn't reach any AI provider. Groq: unreachable. Gemini: unreachable. "
            "Ollama: Ollama isn't running.",
        )
    )
    await shot("08-offline", AppState.IDLE, 8.0)
    window.quitting = True
    window.close()


def main() -> None:
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "docs/renders")
    out.mkdir(parents=True, exist_ok=True)
    app = QApplication(sys.argv)
    theme.load_fonts()
    app.setStyleSheet(theme.APP_STYLESHEET)
    loop = qasync.QEventLoop(app)
    asyncio.set_event_loop(loop)
    with loop:
        loop.run_until_complete(render(out))


if __name__ == "__main__":
    main()
