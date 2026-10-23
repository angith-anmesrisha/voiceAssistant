"""API keys, kept in environment variables only.

A key is read from this process's environment first. On Windows it is then read from
the user's saved environment variables (``HKCU\\Environment``, where ``setx`` writes), so
a key set with ``setx`` works without opening a new terminal. Saving a key from the app
writes the same place ``setx`` does. Keys are never logged or written anywhere else.
"""

from __future__ import annotations

import contextlib
import os
import sys

from loguru import logger

# Tests turn this off so a developer's real saved keys never leak into them.
read_user_environment = sys.platform == "win32"

_ENV_KEY = "Environment"


def get_key(name: str) -> str | None:
    value = os.environ.get(name, "").strip()
    if not value and read_user_environment:
        value = _read_user_variable(name).strip()
    return value or None


def save_key(name: str, value: str) -> bool:
    """Save (or with an empty ``value``, remove) a key for this app and future sessions.

    Returns True if it was saved for future sessions too (Windows user environment),
    False if only for this run.
    """
    value = value.strip()
    if value:
        os.environ[name] = value
    else:
        os.environ.pop(name, None)
    if sys.platform != "win32":
        return False
    try:
        _write_user_variable(name, value)
    except OSError as exc:
        logger.warning("Couldn't save {} to the user environment: {}", name, type(exc).__name__)
        return False
    _broadcast_environment_change()
    return True


def _read_user_variable(name: str) -> str:
    if sys.platform != "win32":
        return ""
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _ENV_KEY) as key:
            value, _kind = winreg.QueryValueEx(key, name)
    except OSError:
        return ""
    return value if isinstance(value, str) else ""


def _write_user_variable(name: str, value: str) -> None:
    if sys.platform != "win32":
        return
    import winreg

    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _ENV_KEY, 0, winreg.KEY_SET_VALUE) as key:
        if value:
            winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)
        else:
            with contextlib.suppress(FileNotFoundError):
                winreg.DeleteValue(key, name)


def _broadcast_environment_change() -> None:
    """Tell Explorer the user environment changed, as ``setx`` does, so new terminals see it."""
    if sys.platform != "win32":
        return
    import ctypes
    from ctypes import wintypes

    hwnd_broadcast, wm_settingchange, smto_abortifhung = 0xFFFF, 0x001A, 0x0002
    result = wintypes.DWORD()
    with contextlib.suppress(OSError):
        ctypes.windll.user32.SendMessageTimeoutW(
            hwnd_broadcast,
            wm_settingchange,
            0,
            "Environment",
            smto_abortifhung,
            1000,
            ctypes.byref(result),
        )
