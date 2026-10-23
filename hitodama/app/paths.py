"""Filesystem locations for per-user app data."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from hitodama import APP_NAME


def data_dir() -> Path:
    """Return the per-user data directory, creating it if needed.

    ``%LOCALAPPDATA%\\Hitodama`` on Windows; an XDG-style fallback elsewhere so the
    app and tests can run on a dev machine. ``HITODAMA_DATA_DIR`` overrides both.
    """
    override = os.environ.get("HITODAMA_DATA_DIR")
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
