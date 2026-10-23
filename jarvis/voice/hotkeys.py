"""Global press-and-hold hotkeys.

``ChordTracker`` is pure logic (tested); ``GlobalHotkeys`` feeds it from a pynput
low-level keyboard hook on a background thread and publishes events on the loop.
Keys are only compared against the configured chords, never stored or logged.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from loguru import logger

from jarvis.app.events import Event, EventBus

_ALIASES = {
    "control": "ctrl",
    "ctl": "ctrl",
    "option": "alt",
    "altgr": "alt",
    "alt_gr": "alt",
    "cmd": "win",
    "super": "win",
    "meta": "win",
    "windows": "win",
    "return": "enter",
    "esc": "escape",
}


def normalize_token(token: str) -> str:
    token = token.strip().lower()
    for suffix in ("_l", "_r", "_gr"):
        if token.endswith(suffix) and len(token) > len(suffix):
            token = token[: -len(suffix)]
            break
    return _ALIASES.get(token, token)


def parse_chord(spec: str) -> frozenset[str]:
    """``"Ctrl+Alt+Space"`` -> ``{"ctrl", "alt", "space"}``."""
    keys = frozenset(normalize_token(part) for part in spec.split("+") if part.strip())
    if not keys:
        raise ValueError(f"Empty hotkey: {spec!r}")
    return keys


class ChordTracker:
    """Turns a stream of key down/up tokens into chord pressed/released edges.

    Fires ``on_press`` once when every key of the chord is down (ignoring key
    repeat) and ``on_release`` once when any of them goes up.
    """

    def __init__(
        self,
        chord: frozenset[str],
        on_press: Callable[[], None],
        on_release: Callable[[], None],
    ) -> None:
        self.chord = chord
        self._on_press = on_press
        self._on_release = on_release
        self._down: set[str] = set()
        self._active = False

    def key_down(self, token: str) -> None:
        self._down.add(token)
        if not self._active and self.chord <= self._down:
            self._active = True
            self._on_press()

    def key_up(self, token: str) -> None:
        self._down.discard(token)
        if self._active and token in self.chord:
            self._active = False
            self._on_release()


def _token_for(key: Any) -> str | None:
    """Map a pynput key object to our token vocabulary."""
    name = getattr(key, "name", None)
    if name:  # pynput.keyboard.Key member
        return normalize_token(name)
    vk = getattr(key, "vk", None)
    # A-Z and 0-9 by Windows virtual-key code, since Ctrl turns chars into control codes.
    if isinstance(vk, int) and (0x41 <= vk <= 0x5A or 0x30 <= vk <= 0x39):
        return chr(vk).lower()
    char = getattr(key, "char", None)
    if isinstance(char, str) and char.isprintable():
        return char.lower()
    return None


class GlobalHotkeys:
    """Watches the keyboard for hold chords and publishes events on the bus."""

    def __init__(self, bus: EventBus, loop: asyncio.AbstractEventLoop) -> None:
        self._bus = bus
        self._loop = loop
        self._trackers: list[ChordTracker] = []
        self._listener: Any = None

    def add_hold(self, spec: str, pressed: Event, released: Event) -> None:
        self._trackers.append(
            ChordTracker(
                parse_chord(spec),
                on_press=lambda: self._emit(pressed),
                on_release=lambda: self._emit(released),
            )
        )

    def _emit(self, event: Event) -> None:
        self._loop.call_soon_threadsafe(lambda: asyncio.ensure_future(self._bus.publish(event)))

    def start(self) -> bool:
        try:
            from pynput import keyboard
        except Exception as exc:  # no display / unsupported platform
            logger.error("Global hotkeys unavailable: {}", type(exc).__name__)
            return False

        def on_press(key: Any) -> None:
            token = _token_for(key)
            if token:
                for tracker in self._trackers:
                    tracker.key_down(token)

        def on_release(key: Any) -> None:
            token = _token_for(key)
            if token:
                for tracker in self._trackers:
                    tracker.key_up(token)

        self._listener = keyboard.Listener(on_press=on_press, on_release=on_release)
        self._listener.daemon = True
        self._listener.start()
        return True

    def stop(self) -> None:
        if self._listener is not None:
            self._listener.stop()
            self._listener = None
