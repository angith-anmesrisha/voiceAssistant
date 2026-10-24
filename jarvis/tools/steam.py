"""The Steam games installed on this PC, read from Steam's own library files.

Steam keeps a list of library folders (``libraryfolders.vdf``) and, in each, one
``appmanifest_<id>.acf`` per installed game. Games are started and checked through
Steam's own ``steam://`` links, the same ones the Steam client uses; nothing is run
directly.
"""

from __future__ import annotations

import difflib
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

_FIELD = re.compile(r'"(\w+)"\s+"((?:[^"\\]|\\.)*)"')
_FULLY_INSTALLED = 4  # StateFlags: anything else means an update or download is pending


@dataclass(frozen=True, slots=True)
class SteamGame:
    app_id: int
    name: str
    needs_update: bool


def _fields(text: str) -> list[tuple[str, str]]:
    return [(key, value.replace("\\\\", "\\")) for key, value in _FIELD.findall(text)]


def steam_folder() -> Path | None:
    """Where Steam is installed: its registry entry, else the usual place."""
    if sys.platform == "win32":
        try:
            import winreg

            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as key:
                path = Path(str(winreg.QueryValueEx(key, "SteamPath")[0]))
                if path.is_dir():
                    return path
        except OSError:
            pass
    default = Path(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")) / "Steam"
    return default if default.is_dir() else None


def library_folders(steam: Path) -> list[Path]:
    folders = [steam]
    vdf = steam / "steamapps" / "libraryfolders.vdf"
    try:
        text = vdf.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return folders
    for key, value in _fields(text):
        if key == "path":
            path = Path(value)
            if path not in folders:
                folders.append(path)
    return folders


def installed_games(steam: Path | None) -> list[SteamGame]:
    if steam is None:
        return []
    games: dict[int, SteamGame] = {}
    for library in library_folders(steam):
        for manifest in sorted((library / "steamapps").glob("appmanifest_*.acf")):
            try:
                fields = dict(_fields(manifest.read_text(encoding="utf-8", errors="replace")))
                app_id = int(fields["appid"])
                flags = int(fields.get("StateFlags", _FULLY_INSTALLED))
            except (OSError, KeyError, ValueError):
                continue
            name = fields.get("name", "").strip()
            if not name or "redistributable" in name.lower() or name.startswith("Steamworks"):
                continue
            games[app_id] = SteamGame(app_id, name, flags != _FULLY_INSTALLED)
    return sorted(games.values(), key=lambda g: g.name.lower())


def _plain(text: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", text.lower().replace("®", "").replace("™", "")))


def find_game(name: str, games: list[SteamGame]) -> SteamGame | None:
    wanted = _plain(name)
    if not wanted:
        return None
    for game in games:
        if _plain(game.name) == wanted:
            return game
    for game in games:
        if _plain(game.name).startswith(wanted) or f" {wanted} " in f" {_plain(game.name)} ":
            return game
    close = difflib.get_close_matches(wanted, [_plain(g.name) for g in games], n=1, cutoff=0.75)
    return next((g for g in games if close and _plain(g.name) == close[0]), None)
