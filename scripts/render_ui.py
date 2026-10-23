"""Render the JARVIS window in each state to PNG files (no network, no audio).

Usage:  QT_QPA_PLATFORM=offscreen python scripts/render_ui.py [output_dir]
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
import qasync
from PySide6.QtGui import QColor, QImage, QLinearGradient, QPainter
from PySide6.QtWidgets import QApplication

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
from jarvis.ui.widgets.orb import VisualState

SIZE = (1440, 900)


def demo_router(online: bool) -> ProviderRouter:
    config = Settings().providers
    router = ProviderRouter(build_providers(config, httpx.AsyncClient()), config.priority, {})
    if online:
        router.statuses.update(
            groq=ProviderStatus(ProviderState.READY, latency_ms=86),
            gemini=ProviderStatus(ProviderState.READY, latency_ms=142),
            ollama=ProviderStatus(ProviderState.MODEL_MISSING, "run: ollama pull qwen3.5"),
        )
        router.network = NetworkStatus(True, 24)
        router.last_used = ("Groq", "qwen/qwen3.8-27b")
    else:
        router.statuses.update(
            groq=ProviderStatus(ProviderState.OFFLINE, "unreachable (ConnectError)"),
            gemini=ProviderStatus(ProviderState.OFFLINE, "unreachable (ConnectError)"),
            ollama=ProviderStatus(ProviderState.OFFLINE, "Ollama isn't running"),
        )
        router.network = NetworkStatus(False)
    return router


CONVERSATION = [
    ChatEntry(
        EntryKind.USER,
        "What's on my calendar vibe today? Keep it short.",
    ),
    ChatEntry(
        EntryKind.ASSISTANT,
        "I can't see your calendar yet, but it's Friday afternoon: a good time to clear small "
        "tasks and plan next week. Want me to open your mail?",
        "Groq",
        "qwen/qwen3.8-27b",
    ),
    ChatEntry(
        EntryKind.USER,
        "Open YouTube and search lo-fi coding music",
    ),
    ChatEntry(EntryKind.ACTION, "✓ Search youtube for lo-fi coding music."),
    ChatEntry(
        EntryKind.ASSISTANT,
        "Done. YouTube is open with lo-fi coding music results.",
        "Groq",
        "qwen/qwen3.8-27b",
        "used tools",
    ),
    ChatEntry(
        EntryKind.USER,
        "Type a quick thank-you note into Notepad",
    ),
]


async def render(out: Path) -> None:
    window = MainWindow()
    window.resize(*SIZE)
    window.caption.freeze_clock(datetime(2026, 10, 23, 18, 42))
    window._router = demo_router(True)
    window.refresh_providers()
    window.network.cpu.set_value(23)
    window.network.ram.set_value(61)
    window.network.battery.set_value(84, label="CHARGING", higher_is_better=True)
    window.corner_right.set_text("CPU 23%  ·  MEM 61%  ·  BAT 84% CHARGING")
    for entry in CONVERSATION:
        window.add_entry(entry)
    window.close_card()
    window.show()

    async def shot(name: str, state: AppState, t: float, level: float = 0.0) -> None:
        window.set_app_state(state)
        window.settle_animations()
        window.orb.freeze(t, level)
        for _ in range(5):
            QApplication.processEvents()
            await asyncio.sleep(0.02)
        path = out / f"{name}.png"
        window.grab().save(str(path))
        print(path)

    await shot("01-idle", AppState.IDLE, 2.0)
    window.command.input.setText("What's this error on my screen?")
    await shot("02-listening", AppState.LISTENING, 3.3, 0.75)
    window.command.input.clear()
    await shot("03-thinking", AppState.THINKING, 4.1)

    window.add_entry(ChatEntry(EntryKind.USER, "What's this error on my screen?"))
    window.add_entry(
        ChatEntry(
            EntryKind.ASSISTANT,
            "VS Code shows a failing test in test_router.py: the mock returns HTTP 500, so "
            "the router retries twice and then falls back to Gemini, but the test expects "
            "Groq. Either make the mock return 200 on the second call, or update the "
            "assertion to expect the fallback.",
            "Gemini",
            "gemini-2.5-flash",
            "looked at screen",
        )
    )
    await shot("04-reply", AppState.SPEAKING, 5.6)
    window.add_entry(ChatEntry(EntryKind.USER, "Open YouTube and play lo-fi coding music"))
    await shot("05-executing", AppState.ACTING, 6.2)
    window.add_entry(ChatEntry(EntryKind.ACTION, "✓ Search youtube for lo-fi coding music."))
    window.add_entry(
        ChatEntry(
            EntryKind.ASSISTANT,
            "Done. YouTube is open with lo-fi coding music.",
            "Groq",
            "qwen/qwen3.8-27b",
            "used tools",
        )
    )
    await shot("06-action-done", AppState.IDLE, 6.8)
    window.close_card()

    future = window.confirm_overlay.ask(
        "Type 34 characters into your last window: “Thanks for today, see you Monday!”",
        Risk.MEDIUM,
    )
    await shot("07-confirm-medium", AppState.THINKING, 7.0)
    window.confirm_overlay.ask(
        "Shutdown the PC in 60 seconds. Unsaved work in open apps may be lost.", Risk.HIGH
    )
    await shot("08-confirm-high", AppState.THINKING, 7.4)
    window.confirm_overlay.hide()
    del future

    window.details.open()
    await shot("09-status", AppState.IDLE, 7.8)
    window.details.hide()
    window.toggle_history()
    await shot("10-history", AppState.IDLE, 8.2)
    window.toggle_history()

    from jarvis.ui.widgets.keys import KeyField

    window._key_fields = lambda: [
        KeyField("Groq", "GROQ_API_KEY", "https://console.groq.com/keys", False),
        KeyField("Gemini", "GEMINI_API_KEY", "https://aistudio.google.com/apikey", False),
    ]
    window.open_keys()
    await shot("11-api-keys", AppState.IDLE, 9.5)
    window.keys_overlay.hide()

    window._router = demo_router(False)
    window.refresh_providers()
    await shot("12-offline", AppState.IDLE, 8.0)

    # A command that couldn't be carried out: everything turns red for a few seconds.
    window._router = demo_router(True)
    window._router.set_preferred("gemini")
    window.refresh_providers()
    window.add_entry(ChatEntry(EntryKind.USER, "Open Spotify and play my liked songs"))
    window.add_entry(ChatEntry(EntryKind.ACTION, "✕ Spotify isn't installed on this PC."))
    window.add_entry(
        ChatEntry(
            EntryKind.ASSISTANT,
            "I couldn't open Spotify: it isn't installed on this PC. Want me to open the "
            "Spotify web player instead?",
            "Gemini",
            "gemini-2.5-flash",
            "used tools",
        )
    )
    window.orb.set_alarm(True)
    await shot("13-failed", AppState.IDLE, 9.0)
    window.clear_failure()
    window.close_card()

    # The corner orb over other apps (drawn on a stand-in desktop).
    overlay = window.overlay
    overlay.orb.set_state(VisualState.SPEAKING)
    overlay.say("JARVIS", "Done. YouTube is open with lo-fi coding music.")
    overlay.orb.freeze(5.6)
    for _ in range(3):
        QApplication.processEvents()
    corner_shot(overlay, out / "14-corner-orb.png")

    # A command that didn't work, seen from another app: the corner turns red too.
    overlay.orb.set_state(VisualState.IDLE)
    overlay.set_alarm(True)
    overlay.say("DIDN'T WORK", "Photoshop isn't installed on this PC.", error=True)
    overlay.orb.freeze(5.6)
    overlay.orb._alarm = 1.0
    corner_shot(overlay, out / "15-corner-failed.png")
    window.quitting = True
    window.close()


def corner_shot(overlay: Any, path: Path) -> None:
    """The corner orb drawn on a stand-in desktop."""
    for _ in range(3):
        QApplication.processEvents()
    desktop = QImage(1280, 720, QImage.Format.Format_ARGB32_Premultiplied)
    p = QPainter(desktop)
    wallpaper = QLinearGradient(0, 0, 1280, 720)
    wallpaper.setColorAt(0.0, QColor(36, 74, 128))
    wallpaper.setColorAt(1.0, QColor(18, 30, 58))
    p.fillRect(desktop.rect(), wallpaper)
    p.fillRect(120, 70, 860, 520, QColor(246, 247, 249))
    p.fillRect(120, 70, 860, 34, QColor(226, 229, 234))
    p.fillRect(0, 672, 1280, 48, QColor(28, 32, 40, 235))
    image = overlay.grab()
    p.drawPixmap(1280 - image.width() // int(image.devicePixelRatio()) - 22, 672 - 22 - 230, image)
    p.end()
    desktop.save(str(path))
    print(path)


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
