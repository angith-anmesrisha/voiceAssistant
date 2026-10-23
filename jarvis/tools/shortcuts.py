"""Keyboard shortcuts JARVIS knows, and the rules for pressing any other key combination.

Named shortcuts carry their own risk: harmless ones (media, switching windows, tabs, zoom,
copy) run directly; ones that change or close things ask first. ``press_keys`` accepts any
other combination of ordinary keys, asks first, and refuses the few that would open a
command line (Win+R, Win+X) or the security screen.
"""

from __future__ import annotations

from jarvis.tools.registry import Risk

L, M = Risk.LOW, Risk.MEDIUM

# name -> (keys, risk, what it does). Keys are pynput Key names or single characters.
SHORTCUTS: dict[str, tuple[list[str], Risk, str]] = {
    # media and volume
    "play_pause": (["media_play_pause"], L, "play or pause media"),
    "next_track": (["media_next"], L, "next track"),
    "previous_track": (["media_previous"], L, "previous track"),
    "volume_up": (["media_volume_up"], L, "volume up"),
    "volume_down": (["media_volume_down"], L, "volume down"),
    "mute": (["media_volume_mute"], L, "mute or unmute"),
    # Windows
    "start_menu": (["cmd"], L, "open the Start menu"),
    "show_desktop": (["cmd", "d"], L, "show or hide the desktop"),
    "minimize_all": (["cmd", "m"], L, "minimize all windows"),
    "switch_window": (["alt", "tab"], L, "switch to the previous window"),
    "task_view": (["cmd", "tab"], L, "task view"),
    "file_explorer": (["cmd", "e"], L, "open File Explorer"),
    "windows_settings": (["cmd", "i"], L, "open Settings"),
    "windows_search": (["cmd", "s"], L, "open Windows search"),
    "notifications": (["cmd", "n"], L, "notification centre"),
    "quick_settings": (["cmd", "a"], L, "quick settings (Wi-Fi, Bluetooth, volume)"),
    "emoji_panel": (["cmd", "."], L, "emoji panel"),
    "clipboard_history": (["cmd", "v"], L, "clipboard history"),
    "screenshot_tool": (["cmd", "shift", "s"], L, "snip a screenshot"),
    "screenshot_save": (["cmd", "print_screen"], L, "save a full screenshot"),
    "game_bar": (["cmd", "g"], L, "Xbox Game Bar"),
    "task_manager": (["ctrl", "shift", "esc"], L, "open Task Manager"),
    "snap_left": (["cmd", "left"], L, "snap window left"),
    "snap_right": (["cmd", "right"], L, "snap window right"),
    "maximize_window": (["cmd", "up"], L, "maximize window"),
    "minimize_window": (["cmd", "down"], L, "minimize or restore window"),
    "move_to_other_monitor": (["cmd", "shift", "right"], L, "move window to the next monitor"),
    "new_virtual_desktop": (["cmd", "ctrl", "d"], L, "new virtual desktop"),
    "next_virtual_desktop": (["cmd", "ctrl", "right"], L, "next virtual desktop"),
    "previous_virtual_desktop": (["cmd", "ctrl", "left"], L, "previous virtual desktop"),
    "magnifier_zoom_in": (["cmd", "+"], L, "magnifier zoom in"),
    "magnifier_zoom_out": (["cmd", "-"], L, "magnifier zoom out"),
    "close_window": (["alt", "f4"], M, "close the window"),
    "lock_screen": (["cmd", "l"], M, "lock the PC"),
    # editing (any app)
    "copy": (["ctrl", "c"], L, "copy the selection"),
    "cut": (["ctrl", "x"], M, "cut the selection"),
    "paste": (["ctrl", "v"], M, "paste"),
    "paste_plain_text": (["ctrl", "shift", "v"], M, "paste without formatting"),
    "undo": (["ctrl", "z"], M, "undo"),
    "redo": (["ctrl", "y"], M, "redo"),
    "select_all": (["ctrl", "a"], L, "select all"),
    "save": (["ctrl", "s"], M, "save"),
    "save_as": (["ctrl", "shift", "s"], M, "save as"),
    "print": (["ctrl", "p"], M, "print dialog"),
    "find": (["ctrl", "f"], L, "find on page or in document"),
    "replace": (["ctrl", "h"], M, "find and replace"),
    "bold": (["ctrl", "b"], M, "bold"),
    "italic": (["ctrl", "i"], M, "italic"),
    "underline": (["ctrl", "u"], M, "underline"),
    "new_document": (["ctrl", "n"], L, "new window or document"),
    "open_file_dialog": (["ctrl", "o"], L, "open file dialog"),
    "rename_selected": (["f2"], M, "rename the selected file"),
    "file_properties": (["alt", "enter"], L, "properties of the selected file"),
    "new_folder_in_explorer": (["ctrl", "shift", "n"], M, "new folder in File Explorer"),
    # browser
    "new_tab": (["ctrl", "t"], L, "new tab"),
    "close_tab": (["ctrl", "w"], M, "close tab"),
    "reopen_closed_tab": (["ctrl", "shift", "t"], L, "reopen the last closed tab"),
    "next_tab": (["ctrl", "tab"], L, "next tab"),
    "previous_tab": (["ctrl", "shift", "tab"], L, "previous tab"),
    "refresh": (["f5"], L, "refresh"),
    "hard_refresh": (["ctrl", "f5"], L, "refresh without cache"),
    "back": (["alt", "left"], L, "go back"),
    "forward": (["alt", "right"], L, "go forward"),
    "address_bar": (["ctrl", "l"], L, "focus the address bar"),
    "bookmark_page": (["ctrl", "d"], M, "bookmark the page"),
    "history": (["ctrl", "h"], L, "browser history"),
    "downloads": (["ctrl", "j"], L, "browser downloads"),
    "incognito_window": (["ctrl", "shift", "n"], L, "private window (in a browser)"),
    "zoom_in": (["ctrl", "+"], L, "zoom in"),
    "zoom_out": (["ctrl", "-"], L, "zoom out"),
    "zoom_reset": (["ctrl", "0"], L, "reset zoom"),
    "fullscreen": (["f11"], L, "full screen"),
    "scroll_down": (["page_down"], L, "page down"),
    "scroll_up": (["page_up"], L, "page up"),
    "top_of_page": (["home"], L, "top of the page"),
    "bottom_of_page": (["end"], L, "bottom of the page"),
    "escape": (["esc"], L, "escape / cancel"),
    "enter": (["enter"], M, "press Enter"),
}

_MODIFIERS = {"ctrl": "ctrl", "control": "ctrl", "alt": "alt", "shift": "shift", "win": "cmd"}
_NAMED = {
    "enter": "enter", "return": "enter", "esc": "esc", "escape": "esc", "tab": "tab",
    "space": "space", "backspace": "backspace", "delete": "delete", "del": "delete",
    "insert": "insert", "home": "home", "end": "end", "pageup": "page_up",
    "page_up": "page_up", "pagedown": "page_down", "page_down": "page_down", "up": "up",
    "down": "down", "left": "left", "right": "right", "printscreen": "print_screen",
    "print_screen": "print_screen", "prtsc": "print_screen", "plus": "+", "minus": "-",
}  # fmt: skip
_NAMED.update({f"f{i}": f"f{i}" for i in range(1, 21)})
_NAMED.update({"cmd": "cmd", "windows": "cmd", "meta": "cmd", "super": "cmd"})

# Combinations that open a place to type commands, or the security screen.
_REFUSED = {
    frozenset({"cmd", "r"}): "Win+R opens the Run box (a command line)",
    frozenset({"cmd", "x"}): "Win+X opens the admin menu with Terminal",
    frozenset({"ctrl", "alt", "delete"}): "Ctrl+Alt+Delete is the security screen",
}


class KeyComboError(ValueError):
    pass


def parse_combo(text: str) -> list[str]:
    """``"Ctrl+Shift+T"`` -> ``["ctrl", "shift", "t"]`` in pynput names; refuses unsafe ones."""
    parts = [p.strip().lower() for p in text.replace(" ", "").split("+")]
    if not parts or any(not p for p in parts):
        raise KeyComboError(f"not a key combination: {text!r}")
    keys: list[str] = []
    for part in parts:
        if part in _MODIFIERS:
            key = _MODIFIERS[part]
        elif part in _NAMED:
            key = _NAMED[part]
        elif len(part) == 1 and (part.isalnum() or part in "`-=[];',./\\"):
            key = part
        else:
            raise KeyComboError(f"unknown key {part!r}")
        if key not in keys:
            keys.append(key)
    if len(keys) > 4:
        raise KeyComboError("too many keys at once")
    if reason := _REFUSED.get(frozenset(keys)):
        raise KeyComboError(f"refused: {reason}")
    return keys


def catalogue() -> str:
    """One line per shortcut for the model: "name (keys): what it does"."""
    return "; ".join(
        f"{name} ({'+'.join(k.replace('cmd', 'win') for k in keys)}): {what}"
        for name, (keys, _risk, what) in SHORTCUTS.items()
    )


__all__ = ["SHORTCUTS", "KeyComboError", "catalogue", "parse_combo"]
