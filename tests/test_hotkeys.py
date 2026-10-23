from __future__ import annotations

from types import SimpleNamespace

import pytest

from jarvis.voice.hotkeys import ChordTracker, _token_for, normalize_token, parse_chord


def test_parse_chord_normalizes() -> None:
    assert parse_chord("Ctrl+Alt+Space") == {"ctrl", "alt", "space"}
    assert parse_chord("control + cmd") == {"ctrl", "win"}
    with pytest.raises(ValueError):
        parse_chord(" + ")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("ctrl_l", "ctrl"),
        ("ctrl_r", "ctrl"),
        ("alt_gr", "alt"),
        ("cmd_l", "win"),
        ("shift", "shift"),
    ],
)
def test_normalize_token(raw: str, expected: str) -> None:
    assert normalize_token(raw) == expected


def test_token_for_pynput_like_keys() -> None:
    assert _token_for(SimpleNamespace(name="ctrl_l")) == "ctrl"
    assert _token_for(SimpleNamespace(name=None, vk=0x41, char="\x01")) == "a"
    assert _token_for(SimpleNamespace(name=None, vk=None, char="/")) == "/"
    assert _token_for(SimpleNamespace(name=None, vk=None, char=None)) is None


def make_tracker() -> tuple[ChordTracker, list[str]]:
    log: list[str] = []
    tracker = ChordTracker(
        parse_chord("ctrl+alt+space"),
        on_press=lambda: log.append("press"),
        on_release=lambda: log.append("release"),
    )
    return tracker, log


def test_fires_once_per_hold_despite_key_repeat() -> None:
    tracker, log = make_tracker()
    for token in ("ctrl", "alt", "space", "space", "space"):
        tracker.key_down(token)
    tracker.key_up("space")
    tracker.key_up("alt")
    tracker.key_up("ctrl")
    assert log == ["press", "release"]


def test_any_order_and_release_of_modifier_ends_hold() -> None:
    tracker, log = make_tracker()
    for token in ("space", "alt", "ctrl"):
        tracker.key_down(token)
    tracker.key_up("ctrl")
    assert log == ["press", "release"]
    tracker.key_down("ctrl")  # still holding alt+space
    assert log == ["press", "release", "press"]


def test_partial_chord_does_nothing() -> None:
    tracker, log = make_tracker()
    tracker.key_down("ctrl")
    tracker.key_down("space")
    tracker.key_up("space")
    assert log == []


def test_unrelated_key_release_does_not_end_hold() -> None:
    tracker, log = make_tracker()
    for token in ("ctrl", "alt", "space", "x"):
        tracker.key_down(token)
    tracker.key_up("x")
    assert log == ["press"]
