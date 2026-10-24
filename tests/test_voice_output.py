"""What JARVIS says out loud: clean sentences, the neural voice's files and fallback."""

from __future__ import annotations

import hashlib
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
        ("# Forecast\n- Sunny, 24°C\n- Light wind 🌬️", "Forecast. Sunny, 24 degrees C. Light wind."),
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


def test_neural_playback_streams_sentences_and_stops(monkeypatch: pytest.MonkeyPatch) -> None:
    import sys
    import types

    monkeypatch.setitem(sys.modules, "sounddevice", types.SimpleNamespace(OutputStream=FakeStream))
    FakeStream.made.clear()

    class Voice:
        voice = "bm_george"

        def synthesize(self, text: str) -> np.ndarray:
            return np.full(24_000, 0.1, dtype=np.float32)  # one second per sentence

    speaker = Speaker(engine="kokoro")
    speaker._neural = Voice()
    speaker._play_neural("First. Second.")
    played = FakeStream.made[0]
    assert played.frames == 2 * 24_000 + int(0.32 * 24_000) and not played.aborted

    original = FakeStream.__init__

    def stopping(self: FakeStream, **kwargs: object) -> None:
        original(self, **kwargs)
        self.on_block = speaker.stop

    monkeypatch.setattr(FakeStream, "__init__", stopping)
    speaker._play_neural("First. Second. Third.")
    barged = FakeStream.made[1]
    assert barged.aborted and barged.frames == 1200  # one 50 ms block, then silence
