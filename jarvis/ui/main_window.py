"""The JARVIS window.

A full-screen HUD in the style of the reference video: a charcoal grid, the orb in the
middle with a clock under it, and a command bar at the bottom. When JARVIS answers, the
reply opens as a card in the centre and the orb glides to the bottom-right corner. Status
details, history and API keys open on demand, so the main view stays clean.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable

from loguru import logger
from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QRect, Qt, QTimer
from PySide6.QtGui import (
    QCloseEvent,
    QColor,
    QGuiApplication,
    QImage,
    QImageWriter,
    QMouseEvent,
    QPainter,
)
from PySide6.QtWidgets import QGraphicsOpacityEffect, QHBoxLayout, QWidget

from jarvis.agent.assistant import ConfirmRequest, plan_lines
from jarvis.app.events import (
    ChatEntry,
    ConversationChanged,
    EntryKind,
    EventBus,
    NoticeLevel,
    PlanUpdated,
    ProvidersChanged,
    UserNotice,
)
from jarvis.app.state import AppState, StateChanged
from jarvis.providers.base import Capability, ProviderState
from jarvis.providers.router import ProviderRouter
from jarvis.ui import theme
from jarvis.ui.frame_clock import FrameClock
from jarvis.ui.theme import AMBER, CYAN, DANGER, ELECTRIC, ICE, STEEL
from jarvis.ui.widgets.chat import ChatView
from jarvis.ui.widgets.glass import (
    GlassPanel,
    HudButton,
    body_label,
    icon_assist,
    icon_key,
    icon_mic,
    icon_new_chat,
    icon_send,
)
from jarvis.ui.widgets.keys import KeyField, KeysOverlay
from jarvis.ui.widgets.orb import OrbHud, VisualState
from jarvis.ui.widgets.overlay import OrbOverlay
from jarvis.ui.widgets.panels import (
    STATE_COLORS,
    CommandBar,
    ConfirmOverlay,
    NetworkPanel,
    SafetyPanel,
    SystemPanel,
)
from jarvis.ui.widgets.stage import (
    CenterCaption,
    CornerText,
    HudBackdrop,
    ProviderSwitch,
    ReplyCard,
    StatusStrip,
    TopBar,
    Tween,
)

_FOLLOW_UP = ("STILL LISTENING", "Ask a follow-up, no wake word needed. Stay quiet to end.")
_CAPTIONS: dict[VisualState, tuple[str, str]] = {
    VisualState.IDLE: ("STANDING BY", "Say “Jarvis”, hold {hotkey} or tap the mic, then just ask"),
    VisualState.LISTENING: (
        "LISTENING",
        "Go ahead: it sends when you pause, or when you let go of the hotkey",
    ),
    VisualState.THINKING: ("THINKING", "Working on it…"),
    VisualState.SPEAKING: ("SPEAKING", "Tap the mic to interrupt"),
    VisualState.ACTING: ("EXECUTING", "Running an approved action"),
    VisualState.OFFLINE: (
        "OFFLINE",
        "No AI available. Add a Groq or Gemini key (key icon, top right), or start Ollama.",
    ),
}

_GLOW = {
    VisualState.LISTENING: 1.0,
    VisualState.SPEAKING: 0.8,
    VisualState.THINKING: 0.45,
    VisualState.ACTING: 0.7,
}

_PLACEHOLDER = "Ask JARVIS anything, or tell it what to do…"

_DOCK_SIZE = 170


class _DetailsOverlay(QWidget):
    """Providers, network/system and permissions, shown on demand over a dim layer."""

    def __init__(self, parent: QWidget, panels: list[QWidget]):
        super().__init__(parent)
        self.hide()
        self._row = QWidget(self)
        layout = QHBoxLayout(self._row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(18)
        for panel in panels:
            panel.setFixedWidth(330)
            layout.addWidget(panel, 0, Qt.AlignmentFlag.AlignTop)

    def open(self) -> None:
        parent = self.parentWidget()
        if parent is not None:
            self.setGeometry(parent.rect())
        self._place()
        self.raise_()
        self.show()

    def _place(self) -> None:
        hint = self._row.sizeHint()
        x = (self.width() - hint.width()) // 2
        y = max(80, (self.height() - hint.height()) // 2 - 40)
        self._row.setGeometry(x, y, hint.width(), hint.height())

    def resizeEvent(self, event: object) -> None:  # noqa: N802
        self._place()

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if not self._row.geometry().contains(event.position().toPoint()):
            self.hide()

    def paintEvent(self, _event: object) -> None:  # noqa: N802
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(4, 6, 9, 185))
        p.end()


class MainWindow(HudBackdrop):
    def __init__(self, hotkey_label: str = "Alt+`") -> None:
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
        self._listening_again = False
        self._asked = ""
        self._actions: list[str] = []
        # While a long request runs as a list of tasks: ("STEP 2/4", "Open Spotify").
        self._plan_step: tuple[str, str] | None = None
        self.quitting = False

        # Eased layout values, advanced by one animation timer.
        # Springs (stiffness ~ 4.7 / seconds to settle), advanced on the display-synced
        # frame clock only while something is moving.
        self._dock = Tween(0.0, 8.5)  # 0 = orb centred, 1 = docked bottom-right
        self._drawer = Tween(0.0, 12.0)  # history drawer
        self._glow = Tween(0.0, 7.0)  # screen-edge light
        self._alarm = Tween(0.0, 9.0)  # red tint after a failed command
        self._alarm_timer = QTimer(self)
        self._alarm_timer.setSingleShot(True)
        self._alarm_timer.timeout.connect(self.clear_failure)
        self._anim: int | None = None
        self._last_anim = time.perf_counter()

        self.header = TopBar(self)
        self.provider_switch = ProviderSwitch(self)
        self._on_prefer: Callable[[str | None], None] = lambda _name: None
        self.provider_switch.chosen.connect(lambda name: self._on_prefer(name))
        self.history_button = HudButton(icon_assist, "Conversation history", 34)
        self.history_button.setParent(self)
        self.keys_button = HudButton(icon_key, "API keys: add or change Groq and Gemini keys", 34)
        self.keys_button.setParent(self)

        self.orb = OrbHud(lambda: self._level(), self)
        self.caption = CenterCaption(self)
        self.card = ReplyCard(self)
        self.card.hide()
        self.card.closed.connect(self.close_card)

        self.mic = HudButton(icon_mic, f"Voice: tap to talk (or hold {hotkey_label})", 46)
        self.send = HudButton(icon_send, "Send (Enter)", 46)
        self.command = CommandBar(self.mic, self.send, self)
        self.command.input.setPlaceholderText(_PLACEHOLDER)
        self.notice = body_label("", 9, theme.TEXT_DIM)
        self.notice.setParent(self)
        self.notice.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.status_strip = StatusStrip(self)
        self.corner_left = CornerText(Qt.AlignmentFlag.AlignLeft, self)
        self.corner_right = CornerText(Qt.AlignmentFlag.AlignRight, self)

        # On-demand panels.
        self.providers = SystemPanel()
        self.network = NetworkPanel()
        self.safety = SafetyPanel()
        self.details = _DetailsOverlay(self, [self.providers, self.network, self.safety])
        self.status_strip.clicked.connect(self.details.open)

        self.conversation = GlassPanel("HISTORY", self)
        self.chat = ChatView()
        self.new_chat = HudButton(icon_new_chat, "New conversation", 30)
        self.conversation.set_corner_button(self.new_chat)
        self.conversation.body.addWidget(self.chat)
        self.conversation.hide()
        self.history_button.clicked.connect(self.toggle_history)
        self.keys_button.clicked.connect(self.open_keys)

        # The corner orb shown over other apps while JARVIS works in the background.
        self.overlay = OrbOverlay(lambda: self._level())
        self.overlay.clicked.connect(self._open_from_overlay)
        self._overlay_hide = QTimer(self)
        self._overlay_hide.setSingleShot(True)
        self._overlay_hide.timeout.connect(self.overlay.hide)

        self.confirm_overlay = ConfirmOverlay(self)
        self.keys_overlay = KeysOverlay(self, on_save=lambda _values: None)

        self._pulse = QTimer(self)
        self._pulse.timeout.connect(self._update_mic_button)
        self._stats = QTimer(self)
        self._stats.timeout.connect(self.refresh_system_stats)
        self._apply_state()
        self._layout()

    # --- wiring ---

    def bind(
        self,
        bus: EventBus,
        router: ProviderRouter,
        level: Callable[[], float],
        on_submit: Callable[[str], bool],
        on_mic: Callable[[], None],
        on_new_chat: Callable[[], None],
        key_fields: Callable[[], list[KeyField]] = list,
        on_save_keys: Callable[[dict[str, str]], None] | None = None,
        on_prefer: Callable[[str | None], None] | None = None,
    ) -> None:
        self._router = router
        if on_prefer is not None:
            self._on_prefer = on_prefer
        self._key_fields = key_fields
        if on_save_keys is not None:
            self.keys_overlay._on_save = on_save_keys
        self._level = level
        bus.subscribe(StateChanged, self._on_state_event)
        bus.subscribe(ChatEntry, self._on_chat_event)
        bus.subscribe(ProvidersChanged, self._on_providers_event)
        bus.subscribe(UserNotice, self._on_notice_event)
        bus.subscribe(ConversationChanged, self._on_conversation_event)
        bus.subscribe(PlanUpdated, self._on_plan_event)
        self.command.submitted.connect(lambda text: self._submit(text, on_submit))
        self.provider_switch.setToolTip(
            "Which AI to ask first. If it can't answer, the others are tried in turn, and the "
            "answer says which one replied."
        )
        self.providers.test.clicked.connect(
            lambda: self._submit("test the AI providers", on_submit)
        )
        self.mic.clicked.connect(on_mic)

        def new_chat() -> None:
            self.chat.clear()
            self._last_error = ""
            self.close_card()
            on_new_chat()

        self.new_chat.clicked.connect(new_chat)
        self._stats.start(2000)
        self.refresh_system_stats()
        self.refresh_providers()

    def _submit(self, text: str, on_submit: Callable[[str], bool]) -> None:
        if not text.strip():
            return
        if on_submit(text):
            self.command.input.clear()
            self.notice.setText("")
        else:
            self.show_notice("JARVIS is busy. Wait for the current request to finish.")

    async def _on_conversation_event(self, event: ConversationChanged) -> None:
        self._listening_again = event.active and event.listening_again

    async def _on_state_event(self, event: StateChanged) -> None:
        self._state = event.current
        self._apply_state()

    async def _on_chat_event(self, event: ChatEntry) -> None:
        self.add_entry(event)

    async def _on_providers_event(self, _event: ProvidersChanged) -> None:
        self.refresh_providers()
        router = self._router
        if self.visual_state() is VisualState.THINKING and router is not None and router.activity:
            title, _hint = _CAPTIONS[VisualState.THINKING]
            self.caption.show_state(title, router.activity, CYAN)
            if self.overlay.isVisible():
                self.overlay.say(title, router.activity)
        self._apply_state()

    async def _on_plan_event(self, event: PlanUpdated) -> None:
        self.show_plan(event)

    async def _on_notice_event(self, event: UserNotice) -> None:
        self.show_notice(f"{event.title}: {event.message}", event.level)

    # --- display ---

    def open_keys(self) -> None:
        self.show_and_raise()
        self.details.hide()
        self.keys_overlay.open(self._key_fields())

    def toggle_history(self) -> None:
        self._drawer.target = 0.0 if self._drawer.target > 0.5 else 1.0
        self.conversation.raise_()
        self._start_animation()

    def close_card(self) -> None:
        self._dock.target = 0.0
        self._start_animation()

    def flash_failure(self) -> None:
        """Tint the whole UI red for a while: the last command didn't work."""
        self._alarm.target = 1.0
        self.orb.set_alarm(True)
        self.overlay.set_alarm(True)
        self._alarm_timer.start(6000)
        if not self._in_front():
            # Show the failure in the corner too, even if the overlay had already gone.
            if not self.overlay.isVisible():
                self.overlay.say("DIDN'T WORK", "", error=True)
                self.overlay.place()
                self.overlay.show()
            if self.visual_state() in (VisualState.IDLE, VisualState.OFFLINE):
                self._overlay_hide.start(7000)
        self._start_animation()

    def clear_failure(self) -> None:
        self._alarm_timer.stop()
        if self._alarm.target == 0.0:
            return
        self._alarm.target = 0.0
        self.orb.set_alarm(False)
        self.overlay.set_alarm(False)
        self._start_animation()

    def add_entry(self, entry: ChatEntry) -> None:
        if (
            entry.kind is EntryKind.ERROR
            or entry.failed
            or (entry.kind is EntryKind.ACTION and entry.text.startswith(("✕", "Declined")))
        ):
            self.flash_failure()
        elif entry.kind is EntryKind.USER:
            self.clear_failure()
        self._overlay_entry(entry)
        if entry.kind is EntryKind.ERROR:
            if entry.text == self._last_error:
                self.show_notice("Same problem again; see the conversation.", NoticeLevel.WARNING)
                return  # don't stack identical error cards
            self._last_error = entry.text
        elif entry.kind is EntryKind.USER:
            self._last_error = ""
        meta = ""
        if entry.kind is EntryKind.USER:
            meta = "YOU"
            self._asked, self._actions = entry.text, []
            self._plan_step = None
            self.close_card()
        elif entry.kind is EntryKind.ASSISTANT:
            meta = "JARVIS"
            if entry.provider:
                meta += f" · {entry.provider.upper()} · {entry.model}"
                skipped = self._router.skipped if self._router is not None else []
                if skipped:
                    why = "; ".join(f"{a.provider}: {_short(a.error)}" for a in skipped)
                    self.show_notice(
                        f"{entry.provider} answered because {why}", NoticeLevel.WARNING
                    )
            if entry.mode:
                meta += f" · {entry.mode.upper()}"
            # Red if something in this request just failed.
            self._open_card(meta, entry.text, error=self._alarm.target > 0)
        elif entry.kind is EntryKind.ACTION:
            self._actions.append(entry.text)
        elif entry.kind is EntryKind.ERROR:
            meta = "PROBLEM"
            self._open_card(meta, entry.text, error=True)
        self.chat.add(entry.kind, entry.text, meta)

    def _open_card(self, meta: str, text: str, *, error: bool) -> None:
        self.card.show_exchange(self._asked, meta, text, self._actions, error=error)
        self._dock.target = 1.0
        self._layout()
        self._start_animation()

    def show_plan(self, plan: PlanUpdated) -> None:
        """Show the task list in the reply card, ticking tasks off as they finish."""
        total = len(plan.steps)
        current = plan.current
        if plan.finished or current < 0:
            self._plan_step = None
        else:
            self._plan_step = (f"STEP {current + 1}/{total}", plan.steps[current].title)
        if not plan.finished:
            meta = f"PLAN · {total} TASKS"
            if self._plan_step is not None:
                meta += f" · {self._plan_step[0]}"
            self._open_card(meta, plan_lines(plan.steps), error=self._alarm.target > 0)
        self._apply_state()

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
        hint = hint.format(hotkey=self._hotkey)
        again = self._listening_again and visual is VisualState.LISTENING
        if again:
            title, hint = _FOLLOW_UP
        elif visual is not VisualState.LISTENING:
            self._listening_again = False
        if visual is VisualState.THINKING and self._router is not None:
            # What the router is really doing (it updates on every attempt and fallback).
            first = self._router.first_usable()
            hint = self._router.activity or (f"Asking {first.display_name}…" if first else hint)
        if self._plan_step is not None and visual in (VisualState.THINKING, VisualState.ACTING):
            title, hint = self._plan_step
        if visual is VisualState.IDLE:
            self.caption.show_clock(hint)
        else:
            color = {VisualState.OFFLINE: STEEL, VisualState.ACTING: ICE}.get(visual, CYAN)
            self.caption.show_state(title, hint, color)
        if visual is VisualState.LISTENING and not again:
            # A new request brings the orb back to the centre; a follow-up keeps the
            # last reply on screen until something is said.
            self._dock.target = 0.0
        self._glow.target = _GLOW.get(visual, 0.0)
        self.glow_color = ELECTRIC if visual is VisualState.ACTING else CYAN
        listening = visual is VisualState.LISTENING
        if listening:
            self._pulse.start(50)
        else:
            self._pulse.stop()
        self.mic.set_active(listening, 0.4)
        self._update_overlay(visual, title, hint)
        self._start_animation()

    # --- corner overlay ---

    def _in_front(self) -> bool:
        return self.isVisible() and not self.isMinimized() and self.isActiveWindow()

    def _update_overlay(self, visual: VisualState, title: str, hint: str) -> None:
        overlay = self.overlay
        overlay.orb.set_state(visual)
        if visual in (VisualState.IDLE, VisualState.OFFLINE):
            if overlay.isVisible():
                self._overlay_hide.start(7000 if self._alarm.target > 0 else 4500)
            return
        self._overlay_hide.stop()
        if self._in_front():
            overlay.hide()
            return
        if visual is VisualState.LISTENING:
            overlay.say(title, hint if self._listening_again else "Go ahead, I'm listening.")
        elif visual is VisualState.THINKING or visual is VisualState.ACTING:
            overlay.say(title, hint if self._plan_step is not None else self._asked)
        if not overlay.isVisible():
            overlay.place()
            overlay.show()

    def _overlay_entry(self, entry: ChatEntry) -> None:
        if not self.overlay.isVisible():
            return
        if entry.kind is EntryKind.ASSISTANT:
            if entry.failed:
                self.overlay.say("DIDN'T WORK", entry.text, error=True)
            else:
                self.overlay.say("JARVIS", entry.text)
        elif entry.kind is EntryKind.ERROR:
            self.overlay.say("DIDN'T WORK", entry.text, error=True)
        elif entry.kind is EntryKind.ACTION and entry.text.startswith(("✕", "Declined")):
            self.overlay.say("DIDN'T WORK", entry.text.lstrip("✕ "), error=True)

    def _open_from_overlay(self) -> None:
        self.overlay.hide()
        self.show_and_raise()

    def _update_mic_button(self) -> None:
        self.mic.set_active(True, min(1.0, 0.3 + self._level() * 1.4))

    def refresh_providers(self) -> None:
        router = self._router
        if router is None:
            return
        last = router.last_used
        strip: list[tuple[str, QColor]] = []
        for provider in router.ordered:
            status = router.statuses[provider.name]
            strip.append((provider.display_name.upper(), STATE_COLORS[status.state]))
            row = self.providers.rows.get(provider.name)
            if row is None:
                continue
            active = last is not None and last[0] == provider.display_name
            model = provider.model_for(Capability.CHAT)
            vision = provider.model_for(Capability.VISION)
            if vision and vision != model:
                model = f"{model} · sees with {vision}"
            row.set_status(status, model, active)
        if last is not None:
            self.providers.active.setText(f"Last answer from {last[0]} · {last[1]}")
        self.provider_switch.set_items(
            [
                (p.name, p.display_name.upper(), STATE_COLORS[router.statuses[p.name].state])
                for p in sorted(router.ordered, key=lambda p: router.names.index(p.name))
            ],
            router.preferred,
        )
        net = router.network
        self.network.set_network(net.online, net.latency_ms)
        strip.append(("ONLINE" if net.online else "OFFLINE", CYAN if net.online else DANGER))
        self.status_strip.set_items(strip)
        usable = [p for p in router.ordered if router.statuses[p.name].usable]
        # The header names the provider that will be asked first (the same order the router
        # uses), or, once something answered, the one that really did.
        answered = router.provider_named(last[0]) if last is not None else None
        first = answered or (usable[0] if usable else None)
        if first is not None and (
            answered or router.statuses[first.name].state is ProviderState.READY
        ):
            where = "LOCAL AI" if first.name == "ollama" else "AI ONLINE"
            self.header.set_status(f"{where} · {first.display_name.upper()}", CYAN)
            model = last[1] if answered and last else first.model_for(Capability.CHAT)
            self.corner_left.set_text(f"{first.display_name.upper()}  ·  {model}")
        elif usable:
            self.header.set_status("CHECKING", STEEL)
        else:
            self.header.set_status("NO AI AVAILABLE", DANGER)
            self.corner_left.set_text("NO AI AVAILABLE")
        self._layout()

    def refresh_system_stats(self) -> None:
        try:
            import psutil
        except ImportError:
            return
        cpu = psutil.cpu_percent(interval=None)
        ram = psutil.virtual_memory().percent
        self.network.cpu.set_value(cpu)
        self.network.ram.set_value(ram)
        battery = psutil.sensors_battery() if hasattr(psutil, "sensors_battery") else None
        parts = [f"CPU {cpu:.0f}%", f"MEM {ram:.0f}%"]
        if battery is None:
            self.network.battery.set_value(None, "AC")
        else:
            self.network.battery.set_value(
                battery.percent,
                label="CHARGING" if battery.power_plugged else "BATTERY",
                higher_is_better=True,
            )
            parts.append(
                f"BAT {battery.percent:.0f}%" + (" CHARGING" if battery.power_plugged else "")
            )
        self.corner_right.set_text("  ·  ".join(parts))

    # --- animation and layout ---

    def _start_animation(self) -> None:
        if self._anim is None:
            self._last_anim = time.perf_counter()
            self._anim = FrameClock.shared().subscribe(self._animate)

    def _animate(self, now: float) -> None:
        dt = min(0.1, now - self._last_anim)
        self._last_anim = now
        before = (self.glow, self.alarm)
        moving = [t.step(dt) for t in (self._dock, self._drawer, self._glow, self._alarm)]
        self.glow = self._glow.value
        self.alarm = max(0.0, min(1.0, self._alarm.value))
        self._layout()
        if abs(self.glow - before[0]) > 0.002 or abs(self.alarm - before[1]) > 0.002:
            self.update()  # the edge light and red tint live on the backdrop
        if not any(moving) and self._anim is not None:
            FrameClock.shared().unsubscribe(self._anim)
            self._anim = None

    def settle_animations(self) -> None:
        """Jump every transition to its end (used for renders and tests)."""
        for tween in (self._dock, self._drawer, self._glow, self._alarm):
            tween.settle()
        self.glow = self._glow.value
        self.alarm = self._alarm.value
        self._layout()
        self.update()

    def _layout(self) -> None:
        w, h = self.width(), self.height()
        self.header.setGeometry(0, 0, w, 64)
        self.keys_button.move(w - 58, 18)
        self.history_button.move(w - 100, 18)
        pill = self.header.status_rect()
        self.provider_switch.move(int(pill.left()) - self.provider_switch.width() - 12, 22)

        bar_w = min(720, int(w * 0.5))
        bar_h = self.command.sizeHint().height()
        bar_y = h - 64 - bar_h
        self.command.setGeometry((w - bar_w) // 2, bar_y, bar_w, bar_h)
        self.notice.setGeometry((w - bar_w) // 2, bar_y - 26, bar_w, 22)
        self.status_strip.setGeometry((w - 520) // 2, h - 54, 520, 26)
        self.corner_left.setGeometry(28, h - 40, 420, 20)
        self.corner_right.setGeometry(w - 448, h - 40, 420, 20)

        # Orb: centred (with the caption underneath) or docked bottom-right.
        d = _smooth(self._dock.value)
        free_h = bar_y - 26 - 72
        size = int(min(w * 0.4, free_h - 110, 520))
        cx0, cy0 = w / 2, 72 + (free_h - 110) / 2
        dock_cx, dock_cy = w - 40 - _DOCK_SIZE / 2, bar_y - 20 - _DOCK_SIZE / 2
        s = size + (_DOCK_SIZE - size) * d
        cx = cx0 + (dock_cx - cx0) * d
        cy = cy0 + (dock_cy - cy0) * d
        self.orb.setGeometry(QRect(int(cx - s / 2), int(cy - s / 2), int(s), int(s)))
        self.caption.setGeometry(int(cx0 - 300), int(cy0 + size / 2 - 6), 600, 96)
        fade = 1.0 - _smooth(d / 0.35)
        if abs(fade - self.caption.opacity) > 0.001:
            self.caption.opacity = fade
            self.caption.update()
        self.caption.setVisible(fade > 0.0)

        # Reply card rises into place as the orb leaves the centre.
        card_w = min(860, w - 2 * (_DOCK_SIZE + 60))
        room = bar_y - 46 - 92
        card_h = max(160, min(room, self.card.fit_height(card_w)))
        offset = int((1 - d) * 40)
        card_y = 92 + (room - card_h) // 2
        self.card.setGeometry((w - card_w) // 2, card_y + offset, card_w, card_h)
        self._fade(self.card, _smooth((d - 0.3) / 0.7))

        # History drawer slides in from the right.
        dw = 400
        x = int(w - (dw + 24) * _smooth(self._drawer.value))
        self.conversation.setGeometry(x, 72, dw, bar_y - 14 - 72)
        self.conversation.setVisible(self._drawer.value > 0.01)

        for overlay in (self.details, self.confirm_overlay, self.keys_overlay):
            if overlay.isVisible():
                overlay.setGeometry(self.rect())

    @staticmethod
    def _fade(widget: QWidget, opacity: float) -> None:
        """Fade a widget in or out; the opacity effect is only attached mid-fade."""
        widget.setVisible(opacity > 0.0)
        if 0.0 < opacity < 1.0:
            effect = widget.graphicsEffect()
            if not isinstance(effect, QGraphicsOpacityEffect):
                effect = QGraphicsOpacityEffect(widget)
                widget.setGraphicsEffect(effect)
            effect.setOpacity(opacity)
        elif widget.graphicsEffect() is not None:
            widget.setGraphicsEffect(None)  # type: ignore[arg-type]

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
        hidden = [
            w for w in (self, self.overlay) if w.isVisible() and not w.isMinimized()
        ]  # keep JARVIS itself out of the picture
        for widget in hidden:
            widget.setWindowOpacity(0.0)
        if hidden:
            await asyncio.sleep(0.25)
        try:
            image: QImage = screen.grabWindow(0).toImage()
        finally:
            for widget in hidden:
                widget.setWindowOpacity(1.0)
        if image.isNull():
            return None
        if image.width() > 1600:
            image = image.scaledToWidth(1600, Qt.TransformationMode.SmoothTransformation)
        return encode_jpeg(image)

    # --- window behaviour ---

    def resizeEvent(self, event: object) -> None:  # noqa: N802
        super().resizeEvent(event)  # type: ignore[arg-type]
        self._layout()
        self.confirm_overlay.setGeometry(self.rect())
        self.keys_overlay.setGeometry(self.rect())
        self.details.setGeometry(self.rect())

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802
        if self.quitting:
            self.overlay.close()
            event.accept()
            return
        event.ignore()
        self.hide()  # keep running in the tray


def encode_jpeg(image: QImage, quality: int = 80) -> bytes | None:
    """JPEG bytes for the vision request, or None if Qt can't encode it."""
    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    # QImageWriter, not QImage.save: PySide6 6.12's save() rejects the bytes format its
    # own type hints ask for, and older versions reject str.
    writer = QImageWriter(buffer, QByteArray(b"jpeg"))
    writer.setQuality(quality)
    if not writer.write(image):
        logger.warning("Couldn't encode the screenshot: {}", writer.errorString())
        return None
    return bytes(buffer.data().data())


def _smooth(t: float) -> float:
    """Smoothstep: eases in and out of a transition."""
    t = max(0.0, min(1.0, t))
    return t * t * (3 - 2 * t)


def _short(error: str, limit: int = 90) -> str:
    error = " ".join(error.split())
    return error if len(error) <= limit else error[: limit - 1] + "…"


__all__ = ["MainWindow"]
