"""Dictation (hold, speak, release: words typed where the cursor is), the speech-to-text
vocabulary, and hotkeys that differ only by a modifier."""

from __future__ import annotations

import time
from typing import Any

import httpx

from jarvis.app.config import ProviderSettings
from jarvis.app.events import DictationPressed, DictationReleased
from jarvis.app.state import AppState
from jarvis.providers.backends import GeminiProvider, GroqProvider
from jarvis.providers.router import ProviderRouter
from jarvis.storage.keep import Fact
from jarvis.voice.dictation import Dictation, Foreground, tidy
from jarvis.voice.hotkeys import ChordTracker, parse_chord
from jarvis.voice.vocabulary import vocabulary
from tests.fakes import FakeDesktop
from tests.test_controller_and_ui import FakeRouter, make_controller, settle


async def no_wait(_: float) -> None:
    return None


def dictation(window: Foreground | None) -> tuple[Dictation, FakeDesktop]:
    desktop = FakeDesktop()
    return Dictation(desktop, lambda: window, sleep=no_wait), desktop


async def test_words_are_pasted_and_the_clipboard_put_back() -> None:
    paste, desktop = dictation(Foreground("notepad.exe", "Untitled - Notepad"))
    desktop.clipboard = "what was there"
    assert await paste.paste("  hello   there\n") is None
    assert ("press", ("ctrl", "v")) in desktop.calls
    assert ("clipboard", "hello there ") in desktop.calls
    assert desktop.clipboard == "what was there"
    assert ("press", ("enter",)) not in desktop.calls
    assert tidy("") == ""


async def test_shells_and_jarvis_never_get_dictation() -> None:
    for window in (
        Foreground("cmd.exe", "Command Prompt"),
        Foreground("powershell.exe", "Windows PowerShell"),
        Foreground("python.exe", "JARVIS", own=True),
        None,
    ):
        paste, desktop = dictation(window)
        problem = await paste.paste("rm everything")
        assert problem and not desktop.calls


async def test_dictation_hotkey_types_without_asking_the_ai() -> None:
    controller, rig = make_controller(FakeRouter(text="Dear Priya, see you at eight."))
    typed: list[str] = []

    async def paste(text: str) -> str | None:
        typed.append(text)
        return None

    controller.set_dictation(paste)
    await rig.bus.publish(DictationPressed())
    assert rig.state.state is AppState.LISTENING
    rig.mic.on_chunk(b"\x10\x00" * 16_000)
    await rig.bus.publish(DictationReleased())
    await settle()
    assert typed == ["Dear Priya, see you at eight."]
    assert rig.assistant.calls == [] and rig.speaker.spoken == []
    final: AppState = rig.state.state
    assert final is AppState.IDLE


def test_vocabulary_from_custom_words_and_remembered_names() -> None:
    now = time.time()
    facts = [
        Fact("sister_name", "My sister is Priya Menon", "people", now),
        Fact("home_city", "Kochi", "places", now),
        Fact("favourite_food", "Biryani on Fridays", "preferences", now),  # not names
    ]
    words = vocabulary(["Valorant", "valorant", "OBS"], facts)
    assert words == ["Valorant", "OBS", "Priya", "Menon", "Kochi"]


async def test_vocabulary_reaches_whisper_and_gemini() -> None:
    sent: list[Any] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request.content)
        if "audio/transcriptions" in str(request.url):
            return httpx.Response(200, json={"text": "Priya"})
        return httpx.Response(200, json={"choices": [{"message": {"content": "Priya"}}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        groq = GroqProvider(
            "groq",
            "Groq",
            ProviderSettings(base_url="http://g/v1", chat_model="m", stt_model="whisper"),
            client,
        )
        gemini = GeminiProvider(
            "gemini",
            "Gemini",
            ProviderSettings(base_url="http://m/v1", chat_model="m", stt_model="flash"),
            client,
        )
        router = ProviderRouter(
            {"groq": groq, "gemini": gemini}, ["groq", "gemini"], {"groq": 0, "gemini": 0}
        )
        router.set_vocabulary(["Priya", "Kochi"])
        await groq.transcribe(b"RIFF")
        await gemini.transcribe(b"RIFF")
    assert b'name="prompt"\r\n\r\nPriya, Kochi' in sent[0]
    assert b"Names and words that may come up: Priya, Kochi" in sent[1]


def test_a_chord_with_an_extra_modifier_is_a_different_hotkey() -> None:
    fired: list[str] = []
    talk = ChordTracker(parse_chord("alt+`"), lambda: fired.append("talk"), lambda: None)
    dictate = ChordTracker(
        parse_chord("alt+shift+`"), lambda: fired.append("dictate"), lambda: None
    )
    for token in ("alt", "shift", "`"):
        talk.key_down(token)
        dictate.key_down(token)
    assert fired == ["dictate"]


async def test_whistle_is_used_offline_with_the_vocabulary() -> None:
    import io
    import wave

    from jarvis.voice.whistle import OfflineStt, WhistleStt

    heard: list[Any] = []

    class Model:
        def transcribe(self, audio: Any, keywords: Any = None) -> dict[str, str]:
            heard.append((len(audio), float(abs(audio).max()), keywords))
            return {"text": " call  Priya ", "language": "en"}

    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(16_000)
        writer.writeframes(b"\x00\x40" * 1600)
    whistle = WhistleStt(lambda: ["Priya"], loader=Model)

    class Broken:
        available = True
        name = "Windows speech (offline)"

        async def transcribe(self, wav: bytes) -> str:
            raise RuntimeError("no recognizer")

    chain = OfflineStt([Broken(), whistle])
    assert chain.available
    assert await chain.transcribe(buffer.getvalue()) == "call Priya"
    assert chain.name == "Whistle (offline)"
    assert heard == [(1600, 0.5, ["Priya"])]
