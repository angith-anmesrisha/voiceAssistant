"""Global press-and-hold hotkeys.

``ChordTracker`` and ``HookFilter`` are pure logic (tested); ``GlobalHotkeys`` feeds them
from a pynput low-level keyboard hook on a background thread and publishes events on the
loop. Keys are only compared against the configured chords, never stored or logged.

On Windows the hook also swallows the chord's main key (the ` of Alt+`), so it doesn't
reach the focused app, and taps a harmless unassigned key so releasing Alt doesn't open
that app's menu bar.
"""

from __future__ import annotations

import asyncio
import sys
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
    """``"Alt+`"`` -> ``{"alt", "`"}``."""
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

    @property
    def active(self) -> bool:
        return self._active

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


_MODIFIERS = frozenset({"ctrl", "alt", "shift", "win"})

# Windows virtual-key codes for the keys a chord can use.
_VK_TOKENS = {
    0x10: "shift",
    0xA0: "shift",
    0xA1: "shift",
    0x11: "ctrl",
    0xA2: "ctrl",
    0xA3: "ctrl",
    0x12: "alt",
    0xA4: "alt",
    0xA5: "alt",
    0x5B: "win",
    0x5C: "win",
    0x20: "space",
    0x0D: "enter",
    0x1B: "escape",
    0x09: "tab",
    0xC0: "`",  # the key left of 1 on US/Indian layouts
    0xDF: "`",  # the same key on UK layouts
}
_VK_TOKENS.update({0x70 + i: f"f{i + 1}" for i in range(12)})
_VK_TOKENS.update({vk: chr(vk).lower() for vk in range(0x41, 0x5B)})
_VK_TOKENS.update({vk: chr(vk) for vk in range(0x30, 0x3A)})

_WM_KEYDOWN, _WM_KEYUP, _WM_SYSKEYDOWN, _WM_SYSKEYUP = 0x100, 0x101, 0x104, 0x105
_LLKHF_INJECTED = 0x10
_VK_MASK = 0xE8  # unassigned; tapped so a lone Alt release doesn't focus a menu bar


def token_for_vk(vk: int) -> str | None:
    return _VK_TOKENS.get(vk)


class HookFilter:
    """Decides, per raw key event, whether the chord's main key must be swallowed.

    Updates the trackers as it goes. Modifiers always pass through; only the chord's
    other key (e.g. the backtick) is held back, for as long as the chord is active.
    """

    def __init__(self, trackers: list[ChordTracker]) -> None:
        self._trackers = trackers

    def key(self, token: str, down: bool) -> bool:
        """Returns True if this event should be swallowed."""
        swallow = False
        for tracker in self._trackers:
            if down:
                tracker.key_down(token)
                swallow |= tracker.active and token in tracker.chord
            else:
                swallow |= tracker.active and token in tracker.chord
                tracker.key_up(token)
        return swallow and token not in _MODIFIERS


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
    return token_for_vk(vk) if isinstance(vk, int) else None


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

        if sys.platform == "win32":
            self._listener = self._windows_listener(keyboard)
        else:

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

    def _windows_listener(self, keyboard: Any) -> Any:
        """Handle keys in the raw hook so the chord's main key can be swallowed."""
        import ctypes

        hook = HookFilter(self._trackers)
        listener: Any = None

        def tap_mask_key() -> None:
            user32 = ctypes.windll.user32  # type: ignore[attr-defined]
            user32.keybd_event(_VK_MASK, 0, 0, 0)
            user32.keybd_event(_VK_MASK, 0, 2, 0)  # KEYEVENTF_KEYUP

        def event_filter(msg: int, data: Any) -> bool:
            if data.flags & _LLKHF_INJECTED:
                return True  # our own mask key, or another program's typing
            token = token_for_vk(int(data.vkCode))
            if token is None or msg not in (_WM_KEYDOWN, _WM_KEYUP, _WM_SYSKEYDOWN, _WM_SYSKEYUP):
                return True
            down = msg in (_WM_KEYDOWN, _WM_SYSKEYDOWN)
            if hook.key(token, down):
                if down:
                    tap_mask_key()
                listener.suppress_event()
            return True

        listener = keyboard.Listener(win32_event_filter=event_filter)
        return listener

    def stop(self) -> None:
        if self._listener is not None:
            self._listener.stop()
            self._listener = None
