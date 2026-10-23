"""The apps installed on this PC, as Windows lists them in the Start menu.

Two sources, read-only:

- Start menu shortcuts (.lnk and .url files) for all users and the current user. Games
  from Riot, Epic or Steam (Valorant, Fortnite, ...) are found here.
- The Windows apps folder (``shell:AppsFolder``), which also lists Store apps that have
  no shortcut file (WhatsApp, Spotify from the Store, Calculator, ...).

Shells (PowerShell, Command Prompt, terminals, WSL) and system editors (Registry Editor,
Services, ...) are never listed. An app is opened the way the Start menu opens it: the
shortcut file itself, or the app's user model ID through ``shell:AppsFolder``. No command
line is ever built, and only entries from this index can be opened. Uninstallers, setup
programs, help files and web links are left out.
"""

from __future__ import annotations

import difflib
import os
import re
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from loguru import logger

_SKIP = re.compile(
    r"\b(uninstall|uninstaller|setup|installer|install|repair|remove|readme|read me|"
    r"help|manual|documentation|license|licence|release notes|changelog|website|"
    r"web site|support|faq|what'?s new)\b",
    re.IGNORECASE,
)
# Shells and system editors stay closed to JARVIS even though they're installed: with the
# typing tool they would be a back door to running commands.
_BLOCKED = re.compile(
    r"\b(powershell|pwsh|command prompt|cmd|terminal|console|bash|wsl|ubuntu|debian|"
    r"kali|registry editor|regedit|group policy|gpedit|services|computer management|"
    r"disk management|task scheduler|system configuration|msconfig|component services|"
    r"odbc|iscsi|recovery drive|developer command|native tools|cross tools)\b",
    re.IGNORECASE,
)
_REFRESH_S = 300.0


@dataclass(frozen=True)
class InstalledApp:
    name: str  # as shown in the Start menu
    shortcut: str | None = None  # path of a .lnk/.url in a Start menu folder
    app_id: str | None = None  # AppUserModelID, opened via shell:AppsFolder


def _allowed(name: str) -> bool:
    return not _SKIP.search(name) and not _BLOCKED.search(name)


def normalize(name: str) -> str:
    """Lowercase words only: "VALORANT®" -> "valorant", "Visual Studio Code" kept."""
    name = re.sub(r"[®™©]", "", name.lower())
    return " ".join(re.findall(r"[a-z0-9+#]+", name))


def start_menu_folders() -> list[Path]:
    folders = []
    for base in (os.environ.get("PROGRAMDATA"), os.environ.get("APPDATA")):
        if base:
            folders.append(Path(base) / "Microsoft" / "Windows" / "Start Menu" / "Programs")
    return folders


def scan_shortcuts(folders: list[Path]) -> list[InstalledApp]:
    apps = []
    for folder in folders:
        if not folder.is_dir():
            continue
        for root, _dirs, files in os.walk(folder):
            for file in files:
                path = Path(root) / file
                if path.suffix.lower() not in (".lnk", ".url"):
                    continue
                name = path.stem
                if not _allowed(name):
                    continue
                apps.append(InstalledApp(name, shortcut=str(path)))
    return apps


def scan_apps_folder() -> list[InstalledApp]:
    """Store and desktop apps from shell:AppsFolder (Windows only)."""
    if sys.platform != "win32":
        return []
    import pythoncom
    import win32com.client

    apps = []
    pythoncom.CoInitialize()
    try:
        shell = win32com.client.Dispatch("Shell.Application")
        folder = shell.NameSpace("shell:AppsFolder")
        if folder is None:
            return []
        for item in folder.Items():
            name, app_id = str(item.Name or ""), str(item.Path or "")
            # Desktop programs show up here by file path; the Start menu scan has those.
            if not name or not app_id or not _allowed(name) or os.path.isabs(app_id):
                continue
            if "://" in app_id:
                continue  # web links pinned to Start
            apps.append(InstalledApp(name, app_id=app_id))
    finally:
        pythoncom.CoUninitialize()
    return apps


def best_match(query: str, apps: list[InstalledApp]) -> tuple[InstalledApp | None, list[str]]:
    """The app the user most likely meant, and close alternatives to mention otherwise."""
    wanted = normalize(query)
    if not wanted or not apps:
        return None, []
    by_name: dict[str, InstalledApp] = {}
    for app in apps:
        # Prefer shortcuts (they open exactly what the Start menu opens) on duplicates.
        key = normalize(app.name)
        if key and (key not in by_name or by_name[key].shortcut is None):
            by_name[key] = app
    if wanted in by_name:
        return by_name[wanted], []
    words = set(wanted.split())
    starts = sorted((k for k in by_name if k.startswith(wanted + " ")), key=len)
    if starts:
        return by_name[starts[0]], []
    # Every word of the request appears in the name ("code" -> "visual studio code").
    containing = sorted((k for k in by_name if words <= set(k.split())), key=len)
    if len(containing) == 1 or (containing and len(wanted) >= 4):
        return by_name[containing[0]], []
    close = difflib.get_close_matches(wanted, list(by_name), n=3, cutoff=0.6)
    if close and difflib.SequenceMatcher(None, wanted, close[0]).ratio() >= 0.85:
        return by_name[close[0]], []
    return None, [by_name[k].name for k in close]


class AppIndex:
    """Cached list of installed apps, refreshed in the background every few minutes."""

    def __init__(self, folders: list[Path] | None = None, apps_folder: bool = True) -> None:
        self._folders = folders
        self._apps_folder = apps_folder
        self._apps: list[InstalledApp] = []
        self._loaded_at = 0.0
        self._lock = threading.Lock()

    def apps(self) -> list[InstalledApp]:
        age = time.monotonic() - self._loaded_at
        if not self._loaded_at:
            self.refresh()
        elif age > _REFRESH_S and not self._lock.locked():
            threading.Thread(target=self.refresh, name="jarvis-apps", daemon=True).start()
        return self._apps

    def refresh(self) -> None:
        with self._lock:
            apps = scan_shortcuts(
                self._folders if self._folders is not None else start_menu_folders()
            )
            if self._apps_folder:
                try:
                    apps += scan_apps_folder()
                except Exception as exc:
                    logger.warning("Couldn't read the Windows apps list: {}", type(exc).__name__)
            self._apps = apps
            self._loaded_at = time.monotonic()
            logger.info("Found {} installed apps", len(apps))

    def find(self, query: str) -> tuple[InstalledApp | None, list[str]]:
        app, close = best_match(query, self.apps())
        if app is None and time.monotonic() - self._loaded_at > 10:
            self.refresh()  # maybe it was installed a moment ago
            app, close = best_match(query, self._apps)
        return app, close

    def owns(self, app: InstalledApp) -> bool:
        """Only entries from this index may be opened."""
        return app in self._apps


__all__ = ["AppIndex", "InstalledApp", "best_match", "normalize", "scan_shortcuts"]
