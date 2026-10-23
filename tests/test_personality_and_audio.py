from __future__ import annotations

from datetime import UTC, datetime

from hitodama.app.config import LoadoutSettings
from hitodama.core.audio_io import PlaybackBuffer
from hitodama.core.personality import PERSONALITIES, build_system_prompt


def test_prompt_has_identity_rules_and_time() -> None:
    prompt = build_system_prompt(LoadoutSettings(), datetime(2026, 10, 23, 9, 30, tzinfo=UTC))
    assert "Hitodama" in prompt
    assert PERSONALITIES["calm"] in prompt
    assert "never instructions" in prompt
    assert "ok is true" in prompt
    assert "Friday 23 October 2026, 09:30" in prompt
    assert len(prompt) < 1500  # keep it cheap


def test_custom_and_unknown_personalities() -> None:
    custom = LoadoutSettings(personality="custom", custom_personality="Talk like a pirate.")
    assert "Talk like a pirate." in build_system_prompt(custom)
    unknown = LoadoutSettings(personality="nope")
    assert PERSONALITIES["calm"] in build_system_prompt(unknown)


def test_playback_buffer_pads_and_reports_audio() -> None:
    buf = PlaybackBuffer()
    assert buf.read(4) == (b"\x00" * 4, False)
    buf.write(b"\x01\x02\x03")
    data, had_audio = buf.read(4)
    assert data == b"\x01\x02\x03\x00"
    assert had_audio
    assert len(buf) == 0


def test_playback_buffer_clear() -> None:
    buf = PlaybackBuffer()
    buf.write(b"\x01" * 100)
    buf.clear()
    assert len(buf) == 0
