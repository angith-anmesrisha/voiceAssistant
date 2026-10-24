"""Start with Windows: a shortcut in the user's Startup folder, nothing else.

The shortcut runs ``pythonw -m jarvis --hidden`` (JARVIS starts in the tray) from the
Python JARVIS is running on now. Turning it off deletes that one shortcut. No registry
Run keys, no scheduled tasks, no admin rights.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

SHORTCUT_NAME = "JARVIS.lnk"


class AutostartError(RuntimeError):
    pass


def startup_folder() -> Path | None:
    appdata = os.environ.get("APPDATA")
    if not appdata:
        return None
    return Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"


def shortcut_path() -> Path | None:
    folder = startup_folder()
    return folder / SHORTCUT_NAME if folder is not None else None


def enabled() -> bool:
    path = shortcut_path()
    return path is not None and path.exists()


def windowless_python(executable: str = sys.executable) -> str:
    """pythonw.exe beside python.exe, so no console window opens at sign-in."""
    path = Path(executable)
    candidate = path.with_name("pythonw.exe")
    return str(candidate) if path.name.lower() == "python.exe" and candidate.exists() else str(path)


def set_enabled(on: bool) -> None:
    path = shortcut_path()
    if sys.platform != "win32" or path is None:
        raise AutostartError("starting with Windows only works on Windows")
    if not on:
        path.unlink(missing_ok=True)
        return
    try:
        import win32com.client

        shell = win32com.client.Dispatch("WScript.Shell")
        link = shell.CreateShortcut(str(path))
        link.TargetPath = windowless_python()
        link.Arguments = "-m jarvis --hidden"
        link.WorkingDirectory = str(Path.home())
        link.Description = "Start JARVIS in the tray"
        link.Save()
    except Exception as exc:
        reason = type(exc).__name__
        raise AutostartError(f"couldn't create the startup shortcut ({reason})") from exc
