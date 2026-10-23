"""Wake word / hands-free recording, Alt+` hotkey hook, AI switching, red failure tint."""

from __future__ import annotations

import itertools
import json
from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication

from jarvis.agent.commands import AUTO, provider_command
from jarvis.app.config import load_settings, save_choice
from jarvis.app.events import ChatEntry, EntryKind, WakeWordHeard
from jarvis.app.state import AppState
from jarvis.providers.base import ProviderState, ProviderStatus
from jarvis.ui.widgets.stage import Tween
from jarvis.voice.audio_io import EndOfSpeech
from jarvis.voice.hotkeys import ChordTracker, HookFilter, parse_chord, token_for_vk
from tests.test_assistant import Rig, ScriptedRouter
from tests.test_controller_and_ui import make_controller, qapp, settle
from tests.test_router import FakeProvider, make

__all__ = ["qapp"]  # the fixture, re-used

LOUD = b"\x00\x40" * 512  # 32 ms of speech-level audio at 16 kHz
QUIET = b"\x00\x00" * 512


# --- "use Gemini from now on" ---


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("use gemini from now on", "gemini"),
        ("Use Gemini from now on.", "gemini"),
        ("Jarvis, switch to Groq", "groq"),
        ("switch to grok", "groq"),
        ("use the local model", "ollama"),
        ("go back to automatic", AUTO),
        ("please use ollama instead", "ollama"),
        ("set the AI to gemini", "gemini"),
        ("use google to search for flights", None),
        ("use gemini to summarise this page", None),
        ("what is gemini?", None),
        ("use the force", None),
    ],
)
def test_provider_command(text: str, expected: str | None) -> None:
    assert provider_command(text) == expected


async def test_preferred_provider_goes_first_and_still_falls_back() -> None:
    groq, gemini = FakeProvider("groq"), FakeProvider("gemini")
    router = make(groq, gemini)
    router.set_preferred("gemini")
    assert [p.name for p in router.ordered] == ["gemini", "groq"]
    assert (await router.chat([])).provider == "Gemini"
    router.statuses["gemini"] = ProviderStatus(ProviderState.NO_KEY, "no API key")
    assert (await router.chat([])).provider == "Groq"  # honest fallback
    router.set_preferred(None)
    assert [p.name for p in router.ordered] == ["groq", "gemini"]
    with pytest.raises(KeyError):
        router.set_preferred("openai")


async def test_switch_command_is_handled_without_a_model(tmp_path: Path) -> None:
    rig = Rig(tmp_path, ScriptedRouter())
    router = make(FakeProvider("groq"), FakeProvider("gemini"))
    router.statuses["gemini"] = ProviderStatus(ProviderState.NO_KEY, "no API key")
    chosen: list[str | None] = []
    rig.assistant._router = router
    rig.assistant._on_prefer = chosen.append
    reply = await rig.assistant.handle("use gemini from now on")
    assert reply.ok and reply.provider is None  # JARVIS answered, not a model
    assert "Gemini from now on" in reply.text and "no API key" in reply.text
    assert router.preferred == "gemini" and chosen == ["gemini"]
    assert rig.entries[-1].mode == "settings"
    reply = await rig.assistant.handle("go back to automatic")
    assert router.preferred is None and chosen == ["gemini", None]
    assert "Groq, then Gemini" in reply.text


def test_save_choice_keeps_the_rest_of_the_file(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"log_level": "DEBUG", "providers": {"probe_interval_s": 9}}))
    save_choice("providers", "preferred", "gemini", path)
    data = json.loads(path.read_text())
    assert data == {
        "log_level": "DEBUG",
        "providers": {"probe_interval_s": 9, "preferred": "gemini"},
    }
    settings = load_settings(path)
    assert settings.providers.preferred == "gemini"
    assert settings.hotkeys.push_to_talk == "alt+`"  # still the default


# --- hands-free recording ---


def test_end_of_speech_waits_for_a_pause_after_speech() -> None:
    endpoint = EndOfSpeech(silence_s=0.5)
    chunk = 0.032
    for _ in range(8):  # room noise first
        assert not endpoint.feed(0.05, chunk)
    for _ in range(20):  # talking
        assert not endpoint.feed(0.8, chunk)
    assert endpoint.heard_speech
    stops = [endpoint.feed(0.05, chunk) for _ in range(20)]
    assert True in stops and stops.index(True) >= 14  # ~0.5 s of quiet


def test_end_of_speech_gives_up_when_nobody_talks() -> None:
    endpoint = EndOfSpeech(no_speech_s=1.0)
    stops = [endpoint.feed(0.05, 0.032) for _ in range(40)]
    assert True in stops and not endpoint.heard_speech


async def test_wake_word_listens_and_sends_when_you_go_quiet() -> None:
    _controller, rig = make_controller()
    await rig.bus.publish(WakeWordHeard())
    assert rig.state.state is AppState.LISTENING
    for chunk in [QUIET] * 6 + [LOUD] * 30 + [QUIET] * 40:
        if rig.mic.on_chunk is not None:
            rig.mic.on_chunk(chunk)
    await settle()
    assert rig.assistant.calls == [("open notepad", True)]
    final: AppState = rig.state.state
    assert final is AppState.IDLE


async def test_wake_word_without_speech_is_dropped_quietly() -> None:
    _controller, rig = make_controller()
    await rig.bus.publish(WakeWordHeard())
    for _ in range(400):  # well past the no-speech limit
        if rig.mic.on_chunk is not None:
            rig.mic.on_chunk(QUIET)
    await settle()
    assert rig.assistant.calls == []
    assert rig.entries == []
    final: AppState = rig.state.state
    assert final is AppState.IDLE


# --- Alt+` ---


def test_backtick_key_codes() -> None:
    assert token_for_vk(0xC0) == "`"
    assert token_for_vk(0xA4) == "alt"
    assert parse_chord("alt+`") == {"alt", "`"}


def test_hook_swallows_only_the_hotkeys_main_key() -> None:
    log: list[str] = []
    tracker = ChordTracker(
        parse_chord("alt+`"), lambda: log.append("on"), lambda: log.append("off")
    )
    hook = HookFilter([tracker])
    assert hook.key("`", True) is False  # a plain backtick types normally
    assert hook.key("`", False) is False
    assert hook.key("alt", True) is False  # modifiers always pass through
    assert hook.key("`", True) is True  # Alt+`: swallowed, recording starts
    assert hook.key("`", True) is True  # key repeat while held
    assert hook.key("`", False) is True  # its release is swallowed too
    assert hook.key("alt", False) is False
    assert log == ["on", "off"]


# --- animation and UI ---


def test_spring_tween_eases_in_without_overshoot() -> None:
    tween = Tween(0.0, 9.0)
    tween.target = 1.0
    values = []
    while tween.step(1 / 60):
        values.append(tween.value)
    assert values[0] < 0.02  # starts gently
    assert all(b >= a for a, b in itertools.pairwise(values))
    assert max(values) <= 1.0 and tween.value == 1.0
    assert values[40] > 0.98  # visually there in about 0.7 s
    assert len(values) < 90


async def test_failed_command_tints_the_ui_red(qapp: QApplication) -> None:
    from jarvis.ui.main_window import MainWindow

    window = MainWindow()
    window.add_entry(ChatEntry(EntryKind.USER, "open spotify"))
    window.add_entry(ChatEntry(EntryKind.ACTION, "✕ Spotify isn't installed."))
    window.settle_animations()
    assert window.alarm == 1.0 and window.orb._alarm_on
    window.add_entry(ChatEntry(EntryKind.USER, "open notepad"))  # next request: back to blue
    window.settle_animations()
    assert window.alarm == 0.0 and not window.orb._alarm_on
    window.add_entry(ChatEntry(EntryKind.ERROR, "No AI provider could handle chat."))
    window.settle_animations()
    assert window.alarm == 1.0
    window.quitting = True
    window.close()


async def test_corner_orb_shows_when_the_window_is_not_in_front(qapp: QApplication) -> None:
    from jarvis.ui.main_window import MainWindow
    from jarvis.ui.widgets.orb import VisualState

    window = MainWindow()  # never shown, as when JARVIS sits in the tray
    window.set_app_state(AppState.LISTENING)
    assert window.overlay.isVisible()
    assert window.overlay.orb.state is VisualState.LISTENING
    window.add_entry(ChatEntry(EntryKind.ASSISTANT, "Opened Notepad.", "Groq", "m"))
    assert window.overlay.caption.body.text() == "Opened Notepad."
    window.set_app_state(AppState.IDLE)
    assert window._overlay_hide.isActive()  # fades away after a moment
    window.quitting = True
    window.close()
    assert not window.overlay.isVisible()


async def test_provider_switch_reports_the_choice(qapp: QApplication) -> None:
    from jarvis.app.events import EventBus
    from jarvis.ui.main_window import MainWindow

    router = make(FakeProvider("groq"), FakeProvider("gemini"), FakeProvider("ollama"))
    for name in router.names:
        router.statuses[name] = ProviderStatus(ProviderState.READY)
    picked: list[str | None] = []
    window = MainWindow()
    window.bind(
        EventBus(),
        router,
        level=lambda: 0.0,
        on_submit=lambda _t: True,
        on_mic=lambda: None,
        on_new_chat=lambda: None,
        on_prefer=picked.append,
    )
    window.provider_switch.chosen.emit("ollama")
    assert picked == ["ollama"]
    router.set_preferred("ollama")
    window.refresh_providers()
    assert window.provider_switch._current == "ollama"
    assert window.header._status_text.endswith("OLLAMA")
    window.quitting = True
    window.close()
