from __future__ import annotations

import array
import math
import wave
from collections.abc import Callable
from io import BytesIO
from pathlib import Path

from jarvis.app.config import Settings, load_settings, save_settings
from jarvis.storage.db import HistoryStore
from jarvis.voice.audio_io import Recorder, pcm_level, to_wav
from jarvis.voice.tts import Speaker, speakable


async def test_history_store_round_trip(tmp_path: Path) -> None:
    store = HistoryStore(tmp_path / "db.sqlite3")
    first = await store.latest_or_new_conversation()
    assert await store.latest_or_new_conversation() == first
    await store.add_message(first, {"role": "user", "content": "hi"}, mode="assist")
    await store.add_message(
        first, {"role": "assistant", "content": "hello"}, provider="Groq", model="m"
    )
    messages = await store.messages(first)
    assert [(m.role, m.content, m.provider) for m in messages] == [
        ("user", "hi", None),
        ("assistant", "hello", "Groq"),
    ]
    assert len(await store.messages(first, limit=1)) == 1
    second = await store.new_conversation()
    assert second != first
    assert await store.messages(second) == []
    store.close()
    reopened = HistoryStore(tmp_path / "db.sqlite3")
    assert len(await reopened.messages(first)) == 2
    reopened.close()


def test_settings_round_trip_and_no_keys_in_file(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    settings = Settings()
    settings.providers.priority = ["ollama", "groq"]
    save_settings(settings, path)
    loaded = load_settings(path)
    assert loaded.providers.priority == ["ollama", "groq"]
    text = path.read_text(encoding="utf-8")
    assert "GROQ_API_KEY" in text  # the variable *name*, never a value
    assert "gsk_" not in text


def sine(seconds: float, amplitude: float, rate: int = 16_000) -> bytes:
    n = int(seconds * rate)
    samples = array.array("h", (int(amplitude * 32767 * math.sin(i / 8)) for i in range(n)))
    return samples.tobytes()


def test_pcm_level_and_wav() -> None:
    assert pcm_level(b"") == 0.0
    assert pcm_level(sine(0.05, 0.0)) == 0.0
    loud, quiet = pcm_level(sine(0.05, 0.8)), pcm_level(sine(0.05, 0.05))
    assert 0 < quiet < loud <= 1.0
    wav = to_wav(sine(0.5, 0.3), 16_000)
    with wave.open(BytesIO(wav)) as reader:
        assert (reader.getnchannels(), reader.getsampwidth(), reader.getframerate()) == (
            1,
            2,
            16_000,
        )
        assert reader.getnframes() == 8000


class FakeMic:
    def __init__(self) -> None:
        self.on_chunk: Callable[[bytes], None] | None = None

    def start(self, on_chunk: Callable[[bytes], None]) -> None:
        self.on_chunk = on_chunk

    def stop(self) -> None:
        self.on_chunk = None


def test_recorder_collects_audio_and_level() -> None:
    mic = FakeMic()
    recorder = Recorder(mic, 16_000)
    recorder.start()
    assert recorder.recording and mic.on_chunk is not None
    for _ in range(10):
        mic.on_chunk(sine(0.05, 0.5))
    assert recorder.level > 0.2
    wav = recorder.stop()
    assert not recorder.recording and recorder.level == 0.0
    assert abs(recorder.duration_s(wav) - 0.5) < 0.01


def test_speakable_strips_markup_and_urls() -> None:
    assert (
        speakable("**Done!** See https://x.com/a?b=1 for `details`") == "Done! See link for details"
    )


async def test_speaker_is_silent_noop_off_windows() -> None:
    speaker = Speaker()
    speaker.available = False
    await speaker.speak("hello")  # returns immediately, no crash
