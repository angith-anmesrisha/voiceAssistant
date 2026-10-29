"""Filesystem locations for per-user app data."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from jarvis import APP_NAME


def data_dir() -> Path:
    """Return the per-user data directory, creating it if needed.

    ``%LOCALAPPDATA%\\JARVIS`` on Windows; an XDG-style fallback elsewhere so the
    app and tests can run on a dev machine. ``JARVIS_DATA_DIR`` overrides both.
    """
    override = os.environ.get("JARVIS_DATA_DIR")
    if override:
        base = Path(override)
    elif sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        base = Path(local) / APP_NAME
    else:
        xdg = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
        base = Path(xdg) / APP_NAME
    base.mkdir(parents=True, exist_ok=True)
    return base


def logs_dir() -> Path:
    path = data_dir() / "logs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def settings_file() -> Path:
    return data_dir() / "settings.json"


def database_file() -> Path:
    return data_dir() / "jarvis.sqlite3"


def keep_file() -> Path:
    """Facts JARVIS remembers, reminders, watched topics, conversation summaries."""
    return data_dir() / "keep.sqlite3"


def voices_dir() -> Path:
    """The neural voice model, downloaded once."""
    return data_dir() / "voices"


def code_dir() -> Path:
    """The only working folder for programs JARVIS runs (after you confirm each one)."""
    return data_dir() / "code"


def plugins_dir() -> Path:
    """Your own tools as .py files; each new or changed one asks before loading."""
    return data_dir() / "plugins"


def memory_dir(configured: str = "") -> Path:
    """The memory vault: ``memory.vault_path``, else Documents/JARVIS Memory.

    With ``JARVIS_DATA_DIR`` set (tests, portable use) it sits in the data folder instead.
    """
    if configured.strip():
        return Path(os.path.expandvars(configured.strip())).expanduser()
    if os.environ.get("JARVIS_DATA_DIR"):
        return data_dir() / "memory"
    return documents_dir() / "JARVIS Memory"


def documents_dir() -> Path:
    """The user's Documents folder (moved or OneDrive-redirected ones included)."""
    if sys.platform == "win32":
        try:
            from jarvis.tools.files import known_folder

            return known_folder("documents", Path.home())
        except Exception:
            pass
    return Path.home() / "Documents"


def presentations_dir(configured: str = "") -> Path:
    """Where new decks go: ``slides.folder``, else Documents/JARVIS Presentations."""
    if configured.strip():
        return Path(os.path.expandvars(configured.strip())).expanduser()
    if os.environ.get("JARVIS_DATA_DIR"):
        return data_dir() / "presentations"
    return documents_dir() / "JARVIS Presentations"


def writing_dir() -> Path:
    """Where the writing and document assistants save: Documents/JARVIS Writing."""
    if os.environ.get("JARVIS_DATA_DIR"):
        return data_dir() / "writing"
    return documents_dir() / "JARVIS Writing"


def briefs_dir() -> Path:
    """Where morning brief PDFs go: Documents/JARVIS Briefs."""
    if os.environ.get("JARVIS_DATA_DIR"):
        return data_dir() / "briefs"
    return documents_dir() / "JARVIS Briefs"


def images_dir(configured: str = "") -> Path:
    """Where saved pictures and 3D models go: ``images.folder``, else Pictures/JARVIS Images."""
    if configured.strip():
        return Path(os.path.expandvars(configured.strip())).expanduser()
    if os.environ.get("JARVIS_DATA_DIR"):
        return data_dir() / "images"
    home = Path.home()
    if sys.platform == "win32":
        try:
            from jarvis.tools.files import known_folder

            return known_folder("pictures", home) / "JARVIS Images"
        except Exception:
            pass
    return home / "Pictures" / "JARVIS Images"


def research_dir() -> Path:
    """Where research reports go: Documents/JARVIS Research."""
    if os.environ.get("JARVIS_DATA_DIR"):
        return data_dir() / "research"
    return documents_dir() / "JARVIS Research"
