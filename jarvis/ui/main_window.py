"""The JARVIS window: header, status column, orb, command bar and conversation."""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from PySide6.QtCore import QBuffer, QIODevice, Qt, QTimer
from PySide6.QtGui import QCloseEvent, QGuiApplication, QImage
from PySide6.QtWidgets import QHBoxLayout, QVBoxLayout, QWidget

from jarvis.agent.assistant import ConfirmRequest
from jarvis.agent.prompts import Mode
from jarvis.app.events import (
    ChatEntry,
    EntryKind,
    EventBus,
    NoticeLevel,
    ProvidersChanged,
    UserNotice,
)
from jarvis.app.state import AppState, StateChanged
from jarvis.providers.base import Capability, ProviderState
from jarvis.providers.router import ProviderRouter
from jarvis.ui import theme
from jarvis.ui.theme import AMBER, CYAN, DANGER, ICE, STEEL
from jarvis.ui.widgets.chat import ChatView
from jarvis.ui.widgets.glass import (
    GlassPanel,
    HudButton,
    body_label,
    icon_key,
    icon_mic,
    icon_new_chat,
    icon_send,
)
from jarvis.ui.widgets.keys import KeyField, KeysOverlay
from jarvis.ui.widgets.orb import OrbHud, VisualState
from jarvis.ui.widgets.panels import (
    Backdrop,
    CommandBar,
    ConfirmOverlay,
    Header,
    ModeSwitch,
    NetworkPanel,
    SafetyPanel,
    StateCaption,
    SystemPanel,
)

_CAPTIONS: dict[VisualState, tuple[str, str]] = {
    VisualState.IDLE: ("STANDING BY", "Hold {hotkey} or tap the mic to speak"),
    VisualState.LISTENING: ("LISTENING", "Release the hotkey or tap the mic again to send"),
    VisualState.THINKING: ("THINKING", "Working on it…"),
    VisualState.SPEAKING: ("SPEAKING", "Tap the mic to interrupt"),
    VisualState.ACTING: ("EXECUTING", "Running an approved action"),
    VisualState.OFFLINE: (
        "OFFLINE",
        "No AI available. Add a Groq or Gemini key with the key button, or start Ollama.",
    ),
}

_PLACEHOLDERS = {
    Mode.ANALYZE: "Ask about your screen, or press Enter to analyze it…",
    Mode.ASSIST: "Ask JARVIS anything…",
    Mode.EXECUTE: "Tell JARVIS what to do, e.g. “open notepad and type hello”…",
}


class MainWindow(Backdrop):
    def __init__(self, hotkey_label: str = "Ctrl+Alt+Space") -> None:
        super().__init__()
        self.setWindowTitle("JARVIS")
        self.setMinimumSize(1100, 700)
        self.resize(1360, 840)
        self._hotkey = hotkey_label
        self._level: Callable[[], float] = lambda: 0.0
        self._state = AppState.IDLE
        self._router: ProviderRouter | None = None
        self._key_fields: Callable[[], list[KeyField]] = list
        self._last_error = ""
        self.quitting = False

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        self.header = Header()
        root.addWidget(self.header)

        body = QHBoxLayout()
        body.setContentsMargins(22, 18, 22, 20)
        body.setSpacing(20)
        root.addLayout(body, 1)

        # Left: status column.
        left = QVBoxLayout()
        left.setSpacing(16)
        self.providers = SystemPanel()
        self.network = NetworkPanel()
        left.addWidget(self.providers)
        left.addWidget(self.network)
        self.safety = SafetyPanel()
        left.addWidget(self.safety)
        left.addStretch(1)
        left_box = QWidget()
        left_box.setLayout(left)
        left_box.setFixedWidth(318)
        body.addWidget(left_box)

        # Centre: orb, caption, modes, command bar.
        center = QVBoxLayout()
        center.setSpacing(12)
        self.orb = OrbHud(lambda: self._level())
        self.caption = StateCaption()
        self.modes = ModeSwitch()
        self.mic = HudButton(icon_mic, f"Voice: tap to talk (or hold {hotkey_label})", 50)
        self.send = HudButton(icon_send, "Send (Enter)", 50)
        self.command = CommandBar(self.mic, self.send)
        self.notice = body_label("", 9, theme.TEXT_DIM)
        self.notice.setAlignment(Qt.AlignmentFlag.AlignCenter)
        center.addWidget(self.orb, 1)
        center.addWidget(self.caption)
        mode_row = QHBoxLayout()
        mode_row.addStretch(1)
        mode_row.addWidget(self.modes, 4)
        mode_row.addStretch(1)
        center.addLayout(mode_row)
        center.addWidget(self.command)
        center.addWidget(self.notice)
        body.addLayout(center, 1)

        # Right: conversation.
        self.conversation = GlassPanel("CONVERSATION")
        self.chat = ChatView()
        self.new_chat = HudButton(icon_new_chat, "New conversation", 30)
        self.conversation.set_corner_button(self.new_chat)
        self.conversation.body.addWidget(self.chat)
        self.conversation.setFixedWidth(410)
        body.addWidget(self.conversation)

        self.keys_button = HudButton(
            icon_key, "API keys: add or change your Groq and Gemini keys", 30
        )
        self.providers.set_corner_button(self.keys_button)
        self.keys_button.clicked.connect(self.open_keys)

        self.confirm_overlay = ConfirmOverlay(self)
        self.keys_overlay = KeysOverlay(self, on_save=lambda _values: None)

        self._pulse = QTimer(self)
        self._pulse.timeout.connect(self._update_mic_button)
        self._stats = QTimer(self)
        self._stats.timeout.connect(self.refresh_system_stats)
        self.modes.changed.connect(self._on_mode_changed)
        self._on_mode_changed(self.modes.mode)
        self._apply_state()

    # --- wiring ---

    def bind(
        self,
        bus: EventBus,
        router: ProviderRouter,
        level: Callable[[], float],
        on_submit: Callable[[str, Mode], bool],
        on_mic: Callable[[], None],
        on_mode: Callable[[Mode], None],
        on_new_chat: Callable[[], None],
        key_fields: Callable[[], list[KeyField]] = list,
        on_save_keys: Callable[[dict[str, str]], None] | None = None,
    ) -> None:
        self._router = router
        self._key_fields = key_fields
        if on_save_keys is not None:
            self.keys_overlay._on_save = on_save_keys
        self._level = level
        bus.subscribe(StateChanged, self._on_state_event)
        bus.subscribe(ChatEntry, self._on_chat_event)
        bus.subscribe(ProvidersChanged, self._on_providers_event)
        bus.subscribe(UserNotice, self._on_notice_event)
        self.command.submitted.connect(lambda text: self._submit(text, on_submit))
        self.mic.clicked.connect(on_mic)
        self.modes.changed.connect(on_mode)

        def new_chat() -> None:
            self.chat.clear()
            self._last_error = ""
            on_new_chat()

        self.new_chat.clicked.connect(new_chat)
        self._stats.start(2000)
        self.refresh_system_stats()
        self.refresh_providers()

    def _submit(self, text: str, on_submit: Callable[[str, Mode], bool]) -> None:
        mode = self.modes.mode
        if not text.strip() and mode is not Mode.ANALYZE:
            return
        if on_submit(text, mode):
            self.command.input.clear()
            self.notice.setText("")
        else:
            self.show_notice("JARVIS is busy. Wait for the current request to finish.")

    async def _on_state_event(self, event: StateChanged) -> None:
        self._state = event.current
        self._apply_state()

    async def _on_chat_event(self, event: ChatEntry) -> None:
        self.add_entry(event)

    async def _on_providers_event(self, _event: ProvidersChanged) -> None:
        self.refresh_providers()
        self._apply_state()

    async def _on_notice_event(self, event: UserNotice) -> None:
        self.show_notice(f"{event.title}: {event.message}", event.level)

    # --- display ---

    def open_keys(self) -> None:
        self.show_and_raise()
        self.keys_overlay.open(self._key_fields())

    def add_entry(self, entry: ChatEntry) -> None:
        if entry.kind is EntryKind.ERROR:
            if entry.text == self._last_error:
                self.show_notice("Same problem again; see the conversation.", NoticeLevel.WARNING)
                return  # don't stack identical error cards
            self._last_error = entry.text
        elif entry.kind is EntryKind.USER:
            self._last_error = ""
        meta = ""
        if entry.kind is EntryKind.USER:
            meta = f"YOU · {entry.mode.upper()}" if entry.mode else "YOU"
        elif entry.kind is EntryKind.ASSISTANT:
            meta = "JARVIS"
            if entry.provider:
                meta += f" · {entry.provider.upper()} · {entry.model}"
        elif entry.kind is EntryKind.ERROR:
            meta = "PROBLEM"
        self.chat.add(entry.kind, entry.text, meta)

    def show_notice(self, text: str, level: NoticeLevel = NoticeLevel.INFO) -> None:
        color = {NoticeLevel.ERROR: DANGER, NoticeLevel.WARNING: AMBER}.get(level, theme.TEXT_DIM)
        self.notice.setStyleSheet(f"color: {theme.css(color)}; background: transparent;")
        self.notice.setText(text)

    def visual_state(self) -> VisualState:
        mapping = {
            AppState.LISTENING: VisualState.LISTENING,
            AppState.THINKING: VisualState.THINKING,
            AppState.SPEAKING: VisualState.SPEAKING,
            AppState.ACTING: VisualState.ACTING,
        }
        if self._state in mapping:
            return mapping[self._state]
        if self._router is not None and not self._router.any_usable():
            return VisualState.OFFLINE
        return VisualState.IDLE

    def set_app_state(self, state: AppState) -> None:
        self._state = state
        self._apply_state()

    def _apply_state(self) -> None:
        visual = self.visual_state()
        self.orb.set_state(visual)
        title, hint = _CAPTIONS[visual]
        if visual is VisualState.THINKING and self._router is not None:
            ready = [
                p.display_name for p in self._router.ordered if self._router.statuses[p.name].usable
            ]
            if ready:
                hint = f"Asking {ready[0]}…"
        color = {VisualState.OFFLINE: STEEL, VisualState.ACTING: ICE}.get(visual, CYAN)
        self.caption.set_text(title, hint.format(hotkey=self._hotkey), color)
        listening = visual is VisualState.LISTENING
        if listening:
            self._pulse.start(80)
        else:
            self._pulse.stop()
        self.mic.set_active(listening, 0.4)

    def _update_mic_button(self) -> None:
        self.mic.set_active(True, min(1.0, 0.3 + self._level() * 1.4))

    def refresh_providers(self) -> None:
        router = self._router
        if router is None:
            return
        last = router.last_used
        for provider in router.ordered:
            row = self.providers.rows.get(provider.name)
            if row is None:
                continue
            status = router.statuses[provider.name]
            active = last is not None and last[0] == provider.display_name
            model = provider.model_for(Capability.CHAT)
            vision = provider.model_for(Capability.VISION)
            if vision and vision != model:
                model = f"{model} · sees with {vision}"
            row.set_status(status, model, active)
        if last is not None:
            self.providers.active.setText(f"Last answer from {last[0]} · {last[1]}")
        net = router.network
        self.network.set_network(net.online, net.latency_ms)
        usable = [p for p in router.ordered if router.statuses[p.name].usable]
        ready = [p for p in usable if router.statuses[p.name].state is ProviderState.READY]
        if ready:
            first = ready[0]
            where = "LOCAL AI" if first.name == "ollama" else "AI ONLINE"
            self.header.set_status(f"{where} · {first.display_name.upper()}", CYAN)
        elif usable:
            self.header.set_status("CHECKING", STEEL)
        else:
            self.header.set_status("NO AI AVAILABLE", DANGER)

    def refresh_system_stats(self) -> None:
        try:
            import psutil
        except ImportError:
            return
        self.network.cpu.set_value(psutil.cpu_percent(interval=None))
        self.network.ram.set_value(psutil.virtual_memory().percent)
        battery = psutil.sensors_battery() if hasattr(psutil, "sensors_battery") else None
        if battery is None:
            self.network.battery.set_value(None, "AC")
        else:
            self.network.battery.set_value(
                battery.percent,
                label="CHARGING" if battery.power_plugged else "BATTERY",
                higher_is_better=True,
            )

    def _on_mode_changed(self, mode: object) -> None:
        if isinstance(mode, Mode):
            self.command.input.setPlaceholderText(_PLACEHOLDERS[mode])

    # --- confirmations and screen capture ---

    async def confirm(self, request: ConfirmRequest) -> bool:
        self.show_and_raise()
        return await self.confirm_overlay.ask(request.description, request.risk)

    def show_and_raise(self) -> None:
        if self.isMinimized():
            self.showNormal()
        self.show()
        self.raise_()
        self.activateWindow()

    async def capture_screen(self) -> bytes | None:
        """Screenshot of the screen JARVIS is on, without JARVIS in it, as JPEG."""
        screen = self.screen() or QGuiApplication.primaryScreen()
        if screen is None:
            return None
        was_visible = self.isVisible() and not self.isMinimized()
        if was_visible:
            self.setWindowOpacity(0.0)
            await asyncio.sleep(0.25)
        try:
            image: QImage = screen.grabWindow(0).toImage()
        finally:
            if was_visible:
                self.setWindowOpacity(1.0)
        if image.isNull():
            return None
        if image.width() > 1600:
            image = image.scaledToWidth(1600, Qt.TransformationMode.SmoothTransformation)
        buffer = QBuffer()
        buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        image.save(buffer, b"JPEG", 80)
        return bytes(buffer.data().data())

    # --- window behaviour ---

    def resizeEvent(self, event: object) -> None:  # noqa: N802
        super().resizeEvent(event)  # type: ignore[arg-type]
        self.confirm_overlay.setGeometry(self.rect())
        self.keys_overlay.setGeometry(self.rect())

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802
        if self.quitting:
            event.accept()
            return
        event.ignore()
        self.hide()  # keep running in the tray


__all__ = ["MainWindow"]
