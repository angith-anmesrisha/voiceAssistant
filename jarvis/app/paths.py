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
