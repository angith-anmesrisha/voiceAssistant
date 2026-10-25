"""What JARVIS says out loud: clean sentences, the neural voice's files and fallback."""

from __future__ import annotations

import hashlib
import threading
from pathlib import Path
from typing import ClassVar

import httpx
import numpy as np
import pytest

from jarvis.agent.prompts import system_prompt
from jarvis.voice import neural
from jarvis.voice.neural import ModelFile, VoiceModelError, sentences, trim
from jarvis.voice.spoken_text import site_name, speakable
from jarvis.voice.tts import Speaker


@pytest.mark.parametrize(
    ("written", "spoken"),
    [
        ("**Done.** It's on [BBC Weather](https://www.bbc.co.uk/weather).",
         "Done. It's on BBC Weather."),
        ("Read more at https://www.theverge.com/2026/10/24/story.", "Read more at theverge.com."),
        ("# Forecast\n- Sunny, 24°C\n- Light wind 🌬️", "Forecast. Sunny, 24 degrees. Light wind."),
        ("Saved to C:\\Users\\Ann\\Desktop\\notes.txt ✓", "Saved to notes.txt."),
        ("Like this:\n```python\nprint('hi')\n```", "Like this: I've put the code on screen."),
        ("| Item | Cost |\n|---|---|\n| Hotel | 120 |", "Item, Cost. Hotel, 120."),
        ("<think>user wants x</think>Certainly. Opening your browser now.",
         "Certainly. Opening your browser now."),
        ("1. Open Spotify\n2. Play something calm", "Open Spotify. Play something calm."),
        ("Set `my_var` → 3", "Set my_var to 3."),
    ],
)  # fmt: skip
def test_replies_are_spoken_without_markup(written: str, spoken: str) -> None:
    assert speakable(written) == spoken


def test_site_names() -> None:
    assert site_name("https://www.bbc.co.uk/news") == "bbc.co.uk"
    assert site_name("www.python.org/doc") == "python.org"


def test_sentences_split_long_ones_at_commas() -> None:
    said = sentences("Good evening. I'm online!  Ready?")
    assert said == ["Good evening.", "I'm online!", "Ready?"]
    opening = "I've checked the forecast for Kochi, and it looks like rain this afternoon."
    assert sentences(opening + " Take an umbrella.") == [
        "I've checked the forecast for Kochi,",
        "and it looks like rain this afternoon.",
        "Take an umbrella.",
    ]
    long = ", ".join(["a fairly long clause about something"] * 12) + "."
    parts = sentences(long, longest=100)
    assert len(parts) > 1 and all(len(p) <= 100 for p in parts)
    assert " ".join(parts) == long


def test_trim_keeps_a_margin() -> None:
    audio = np.concatenate([np.zeros(5000), np.full(100, 0.5), np.zeros(5000)]).astype(np.float32)
    cut = trim(audio, margin=10)
    assert len(cut) == 120 and np.all(trim(np.zeros(50, dtype=np.float32)) == 0)


def fake_files(monkeypatch: pytest.MonkeyPatch, bodies: dict[str, bytes]) -> None:
    files = tuple(
        ModelFile(name, hashlib.sha256(body).hexdigest(), len(body))
        for name, body in bodies.items()
    )
    monkeypatch.setattr(neural, "MODEL_FILES", files)


def test_download_checks_every_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bodies = {"model.onnx": b"m" * 3000, "voices.bin": b"v" * 10}
    fake_files(monkeypatch, bodies)
    served = dict(bodies)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=served[request.url.path.rsplit("/", 1)[-1]])

    client = httpx.Client(transport=httpx.MockTransport(handler))
    seen: list[int] = []
    assert len(neural.missing_files(tmp_path)) == 2
    neural.download(tmp_path, client, lambda done, total: seen.append(done), base_url="https://x/")
    assert neural.missing_files(tmp_path) == [] and seen[-1] == 3010
    assert (tmp_path / "model.onnx").read_bytes() == bodies["model.onnx"]

    (tmp_path / "voices.bin").unlink()
    served["voices.bin"] = b"tampered!!"  # same size, wrong content
    with pytest.raises(VoiceModelError, match="checksum"):
        neural.download(tmp_path, client, base_url="https://x/")
    assert not (tmp_path / "voices.bin").exists() and not (tmp_path / "voices.bin.part").exists()


def test_speaker_uses_windows_voice_until_the_model_is_there(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_files(monkeypatch, {"model.onnx": b"m", "voices.bin": b"v"})
    notes: list[tuple[str, str]] = []

    speaker = Speaker(engine="kokoro", model_folder=tmp_path)
    speaker.available = True
    speaker.prepare_neural(download=False, status=lambda *n: notes.append(n))
    assert speaker.voice_name == "Windows voice" and notes == []

    class FakeVoice:
        def __init__(self, folder: Path, voice: str, speed: float) -> None:
            self.voice = voice

    def fetch(folder: Path, client: object, progress: object = None) -> None:
        for name in ("model.onnx", "voices.bin"):
            (folder / name).write_bytes(name[0].encode())

    monkeypatch.setattr(neural, "download", fetch)
    monkeypatch.setattr(neural, "KokoroVoice", FakeVoice)
    speaker.prepare_neural(download=True, status=lambda *n: notes.append(n))
    assert speaker.voice_name == "Kokoro bm_george"
    assert [title for title, _ in notes] == ["Getting JARVIS's voice", "JARVIS's voice is ready"]

    sapi_only = Speaker(engine="sapi", model_folder=tmp_path)
    sapi_only.available = True
    sapi_only.prepare_neural(download=True)
    assert sapi_only.voice_name == "Windows voice"


def test_a_broken_model_falls_back(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_files(monkeypatch, {"model.onnx": b"m"})
    (tmp_path / "model.onnx").write_bytes(b"m")

    def broken(*_args: object) -> None:
        raise VoiceModelError("the voice model has no phoneme table")

    monkeypatch.setattr(neural, "KokoroVoice", broken)
    notes: list[tuple[str, str]] = []
    speaker = Speaker(engine="kokoro", model_folder=tmp_path)
    speaker.available = True
    speaker.prepare_neural(download=True, status=lambda *n: notes.append(n))
    assert speaker.voice_name == "Windows voice" and notes[0][0] == "Using the Windows voice"


def test_personality_is_in_every_prompt() -> None:
    for prompt in (system_prompt(spoken=False), system_prompt(spoken=True, screen=True)):
        assert "British English" in prompt and "Lead with the result" in prompt
        assert "Never invent results" in prompt
    assert "measured British voice" in system_prompt(spoken=True)


class FakeStream:
    made: ClassVar[list[FakeStream]] = []

    def __init__(self, **_kwargs: object) -> None:
        self.frames = 0
        self.aborted = False
        self.on_block: object = None
        FakeStream.made.append(self)

    def start(self) -> None: ...
    def stop(self) -> None: ...
    def close(self) -> None: ...

    def abort(self) -> None:
        self.aborted = True

    def write(self, block: np.ndarray) -> None:
        self.frames += len(block)
        if callable(self.on_block):
            self.on_block()


def play(speaker: Speaker, text: str) -> None:
    from jarvis.voice.feed import SpeechFeed

    feed = SpeechFeed(text, closed=True)
    first = feed.take(60, single=True)
    assert first
    speaker._play_neural(feed, first)


class OneSecondVoice:
    voice = "bm_george"

    def __init__(self) -> None:
        self.made: list[str] = []

    def synthesize(self, text: str) -> np.ndarray:
        self.made.append(text)
        return np.full(24_000, 0.1, dtype=np.float32)  # one second per piece


def test_neural_playback_speaks_pieces_and_stops(monkeypatch: pytest.MonkeyPatch) -> None:
    import sys
    import types

    monkeypatch.setitem(sys.modules, "sounddevice", types.SimpleNamespace(OutputStream=FakeStream))
    FakeStream.made.clear()
    speaker = Speaker(engine="kokoro")
    speaker._neural = voice = OneSecondVoice()
    long_reply = "Certainly. " + "The weather is warm today, with showers after four. " * 6
    play(speaker, long_reply)
    played = FakeStream.made[0]
    pieces = len(voice.made)
    assert voice.made[0] == "Certainly." and pieces >= 3
    assert played.frames == pieces * 24_000 + (pieces - 1) * int(0.22 * 24_000)
    assert not played.aborted

    original = FakeStream.__init__

    def stopping(self: FakeStream, **kwargs: object) -> None:
        original(self, **kwargs)
        self.on_block = speaker.stop

    monkeypatch.setattr(FakeStream, "__init__", stopping)
    play(speaker, "First. Second. Third.")
    barged = FakeStream.made[1]
    assert barged.aborted and barged.frames == 1200  # one 50 ms block, then silence


def test_pieces_grow_with_how_fast_speech_is_made() -> None:
    speaker = Speaker(engine="kokoro")
    speaker._seconds_per_char = 0.02
    slow = speaker._budget(1.0)
    speaker._learn("x" * 100, 0.5)  # this PC makes speech faster than assumed
    assert speaker._budget(1.0) > slow
    assert speaker._budget(30.0) == 280 and speaker._budget(0.0) >= 40


def test_a_reply_is_spoken_while_it_is_written(monkeypatch: pytest.MonkeyPatch) -> None:
    import sys
    import threading
    import types

    from jarvis.voice.feed import SpeechFeed

    monkeypatch.setitem(sys.modules, "sounddevice", types.SimpleNamespace(OutputStream=FakeStream))
    speaker = Speaker(engine="kokoro")
    speaker._neural = voice = OneSecondVoice()
    feed = SpeechFeed()
    feed.put("Of course. The first ")
    first = feed.take(60, single=True)
    assert first == "Of course."
    assert feed.take(60) == ""  # "The first" isn't a whole clause yet

    def write_rest() -> None:
        feed.put("part is done, and ")
        feed.put("the rest follows")
        feed.close()

    threading.Timer(0.05, write_rest).start()
    speaker._play_neural(feed, first)
    assert voice.made == ["Of course.", "The first part is done, and the rest follows."]


def test_speech_feed_hands_out_whole_clauses() -> None:
    from jarvis.voice.feed import SpeechFeed

    feed = SpeechFeed("One, two. Three four five six seven, eight.", closed=True)
    assert feed.take(5) == "One,"
    assert feed.take(200) == "two. Three four five six seven, eight."
    assert feed.take(10) is None and feed.finished
    open_feed = SpeechFeed("no boundary yet")
    assert open_feed.take(50) == ""
    stop = threading.Event()
    stop.set()
    assert open_feed.take(50, wait_s=5, stop=stop) is None


def test_echo_of_own_reply_is_recognised() -> None:
    from jarvis.voice.confirm import is_echo

    said = "Certainly. I've opened Notepad and typed your shopping list into it."
    assert is_echo("opened notepad and typed your shopping list", said)
    assert is_echo("I've opened Notepad, and typed your shopping list into it", said)
    assert not is_echo("now save it to my desktop please", said)
    assert not is_echo("send it", "Shall I send it?")  # short answers are real
    assert not is_echo("open notepad", "")


def test_reasoning_is_never_shown() -> None:
    from jarvis.voice.spoken_text import without_thinking

    assert without_thinking("<think>plan it</think>\nOpening Spotify now.") == (
        "Opening Spotify now."
    )
    assert without_thinking("<think>never closed") == ""


@pytest.mark.parametrize(
    ("text", "language"),
    [
        ("Opening your browser now.", None),
        ("Abriendo el navegador.", None),
        ("नमस्ते, मैं तैयार हूँ।", "hi"),
        ("ശരി, ഞാൻ തയ്യാറാണ്.", "ml"),
        ("Привет! Готово.", "ru"),
        ("今日はいい天気ですね。", "ja"),
        ("Done: नमस्ते", "hi"),
        ("Saved notes.txt (नोट)", None),
    ],
)
def test_reply_language_by_script(text: str, language: str | None) -> None:
    from jarvis.voice.spoken_text import script_language

    found = script_language(text)
    assert (found[0] if found else None) == language
