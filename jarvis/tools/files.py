"""Files and folders the user can ask about: list, find, create, copy, move, rename, recycle.

Scoped on purpose (the owner asked for file work; the spec forbids touching the system):

- Only paths inside the allowed roots: the user's home folder and any other fixed drive
  that isn't the Windows drive. Windows, Program Files, ProgramData and AppData are off
  limits, wherever a path points (links are resolved first).
- Nothing is ever deleted outright: "delete" moves the item to the Recycle Bin.
- Copy and move never overwrite; they fail if the target exists.
- Opening a file uses its default app, and never runs programs or scripts.
"""

from __future__ import annotations

import os
import shutil
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

# Never opened with "open", whatever the folder: these run code.
RUNNABLE = frozenset(
    [
        ".exe",
        ".com",
        ".bat",
        ".cmd",
        ".ps1",
        ".psm1",
        ".psd1",
        ".vbs",
        ".vbe",
        ".js",
        ".jse",
        ".wsf",
        ".wsh",
        ".msi",
        ".msp",
        ".msc",
        ".scr",
        ".cpl",
        ".hta",
        ".lnk",
        ".url",
        ".reg",
        ".inf",
        ".jar",
        ".py",
        ".pyw",
        ".pif",
        ".application",
        ".appref-ms",
        ".gadget",
        ".sh",
        ".bash",
        ".dll",
        ".sys",
        ".drv",
        ".ocx",
        ".appx",
        ".msix",
        ".appinstaller",
        ".settingcontent-ms",
        ".library-ms",
        ".scf",
        ".xll",
        ".xlam",
        ".ppam",
    ]
)

KNOWN_FOLDERS = ("desktop", "documents", "downloads", "pictures", "music", "videos")


class FileAccessError(Exception):
    """A path outside the allowed folders, or an operation that isn't allowed."""


def _known_folder_windows(name: str) -> Path | None:
    """The real location of Desktop, Documents, ... (OneDrive may have moved them)."""
    import ctypes
    import uuid
    from ctypes import wintypes

    ids = {
        "desktop": "B4BFCC3A-DB2C-424C-B029-7FE99A87C641",
        "documents": "FDD39AD0-238F-46AF-ADB4-6C85480369C7",
        "downloads": "374DE290-123F-4565-9164-39C4925E467B",
        "pictures": "33E28130-4E1E-4676-835A-98395C3BC3BB",
        "music": "4BD8D571-6D19-48D3-BE97-422220080E43",
        "videos": "18989B1D-99B5-455B-841C-AB7C74E4DDFC",
    }
    guid = ids.get(name)
    if guid is None:
        return None

    class GUID(ctypes.Structure):
        _fields_ = [("data", ctypes.c_byte * 16)]

    folder_id = GUID()
    ctypes.memmove(ctypes.byref(folder_id), uuid.UUID(guid).bytes_le, 16)
    out = wintypes.LPWSTR()
    shell32 = ctypes.windll.shell32  # type: ignore[attr-defined]
    if shell32.SHGetKnownFolderPath(ctypes.byref(folder_id), 0, None, ctypes.byref(out)) != 0:
        return None
    try:
        return Path(out.value) if out.value else None
    finally:
        ctypes.windll.ole32.CoTaskMemFree(out)  # type: ignore[attr-defined]


def known_folder(name: str, home: Path) -> Path:
    if sys.platform == "win32":
        found = _known_folder_windows(name)
        if found is not None:
            return found
    return home / name.title()


def default_roots() -> tuple[list[Path], list[Path]]:
    """(allowed roots, blocked folders) for this PC."""
    home = Path.home()
    roots = [home]
    blocked = [home / "AppData"]
    if sys.platform == "win32":
        system_drive = (os.environ.get("SYSTEMDRIVE") or "C:").upper() + "\\"
        windir = os.environ.get("WINDIR") or system_drive + "Windows"
        blocked += [Path(windir)]
        for env in ("PROGRAMFILES", "PROGRAMFILES(X86)", "PROGRAMDATA"):
            if os.environ.get(env):
                blocked.append(Path(os.environ[env]))
        listdrives = getattr(os, "listdrives", None)
        for drive in listdrives() if listdrives else []:
            if drive.upper() != system_drive:
                roots.append(Path(drive))
                blocked += [Path(drive) / "$RECYCLE.BIN", Path(drive) / "System Volume Information"]
    return roots, blocked


@dataclass
class FileArea:
    """Resolves the user's words to paths, and refuses anything outside the allowed area."""

    roots: list[Path]
    blocked: list[Path] = field(default_factory=list)
    home: Path = field(default_factory=Path.home)
    trash: Callable[[Path], None] | None = None  # moves a path to the Recycle Bin

    def resolve(self, raw: str) -> Path:
        text = raw.strip().strip('"').strip()
        if not text:
            raise FileAccessError("no path given")
        text = os.path.expandvars(os.path.expanduser(text))
        first, _, rest = text.replace("\\", "/").partition("/")
        if first.lower() in KNOWN_FOLDERS:
            path = known_folder(first.lower(), self.home) / rest
        else:
            path = Path(text)
            if not path.is_absolute():
                path = self.home / path
        path = path.resolve()
        self.check(path)
        return path

    def check(self, path: Path) -> None:
        resolved = path.resolve()
        if any(_inside(resolved, b.resolve()) for b in self.blocked):
            raise FileAccessError(f"{resolved} is a system folder JARVIS doesn't touch")
        if not any(_inside(resolved, r.resolve()) for r in self.roots):
            raise FileAccessError(f"{resolved} is outside your folders")

    # --- read ---

    def list_folder(self, raw: str, limit: int = 80) -> tuple[Path, list[str]]:
        folder = self.resolve(raw)
        if not folder.is_dir():
            raise FileAccessError(f"{folder} isn't a folder")
        entries = sorted(folder.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
        names = [p.name + ("\\" if p.is_dir() else "") for p in entries]
        return folder, names[:limit]

    def find(self, name: str, raw_folder: str = "", limit: int = 30) -> list[Path]:
        start = self.resolve(raw_folder) if raw_folder.strip() else self.home
        wanted = name.lower().strip()
        if not wanted:
            raise FileAccessError("nothing to search for")
        found: list[Path] = []
        for visited, (root, dirs, files) in enumerate(os.walk(start)):
            here = Path(root)
            dirs[:] = [
                d
                for d in dirs
                if not d.startswith(".") and not any(_inside(here / d, b) for b in self.blocked)
            ]
            for entry in [*dirs, *files]:
                if wanted in entry.lower():
                    found.append(here / entry)
                    if len(found) >= limit:
                        return found
            if visited > 4000:  # keep a search quick
                break
        return found

    # --- change ---

    def create_folder(self, raw: str) -> Path:
        folder = self.resolve(raw)
        self._refuse_root(folder)
        if folder.exists() and not folder.is_dir():
            raise FileAccessError(f"{folder} already exists as a file")
        folder.mkdir(parents=True, exist_ok=True)
        return folder

    def copy(self, raw_source: str, raw_target: str) -> Path:
        source, target = self._source_and_target(raw_source, raw_target)
        if source.is_dir():
            shutil.copytree(source, target)
        else:
            shutil.copy2(source, target)
        return target

    def move(self, raw_source: str, raw_target: str) -> Path:
        source, target = self._source_and_target(raw_source, raw_target)
        self._refuse_root(source)
        shutil.move(str(source), str(target))
        return target

    def rename(self, raw: str, new_name: str) -> Path:
        source = self.resolve(raw)
        self._refuse_root(source)
        if not source.exists():
            raise FileAccessError(f"{source} doesn't exist")
        clean = new_name.strip()
        if not clean or any(c in clean for c in '\\/:*?"<>|'):
            raise FileAccessError("a new name can't contain \\ / : * ? \" < > |")
        target = source.with_name(clean)
        self.check(target)
        if target.exists():
            raise FileAccessError(f"{target.name} already exists there")
        source.rename(target)
        return target

    def recycle(self, raw: str) -> Path:
        path = self.resolve(raw)
        self._refuse_root(path)
        main_folders = {known_folder(name, self.home).resolve() for name in KNOWN_FOLDERS}
        main_folders.add((self.home / "OneDrive").resolve())
        if path in main_folders:
            raise FileAccessError(f"{path.name} is one of your main folders and stays")
        if not path.exists():
            raise FileAccessError(f"{path} doesn't exist")
        if self.trash is None:
            raise FileAccessError("the Recycle Bin is only available on Windows")
        self.trash(path)
        return path

    def openable(self, raw: str) -> Path:
        path = self.resolve(raw)
        if not path.exists():
            raise FileAccessError(f"{path} doesn't exist")
        if path.is_file() and path.suffix.lower() in RUNNABLE:
            raise FileAccessError(f"{path.name} is a program or script; JARVIS doesn't run those")
        return path

    def remove_empty_folder(self, path: Path) -> None:
        """Undo for a new folder: only while it is still empty (never anything inside)."""
        self.check(path)
        self._refuse_root(path)
        if path.exists():
            if any(path.iterdir()):
                raise FileAccessError(f"{path.name} isn't empty any more, so it stays")
            path.rmdir()

    def is_folder(self, raw: str) -> bool:
        try:
            return self.resolve(raw).is_dir()
        except FileAccessError:
            return False

    # --- helpers ---

    def _source_and_target(self, raw_source: str, raw_target: str) -> tuple[Path, Path]:
        source = self.resolve(raw_source)
        if not source.exists():
            raise FileAccessError(f"{source} doesn't exist")
        target = self.resolve(raw_target)
        if target.is_dir():
            target = target / source.name  # "copy X to Documents" puts it inside
        self.check(target)
        self._refuse_root(target)
        if target.exists():
            raise FileAccessError(f"{target} already exists; nothing was overwritten")
        if source.is_dir() and _inside(target, source):
            raise FileAccessError("can't put a folder inside itself")
        target.parent.mkdir(parents=True, exist_ok=True)
        return source, target

    def _refuse_root(self, path: Path) -> None:
        if any(path == r.resolve() for r in self.roots):
            raise FileAccessError(f"{path} itself can't be changed")


def _inside(path: Path, folder: Path) -> bool:
    return path == folder or path.is_relative_to(folder)


def recycle_windows(path: Path) -> None:
    """Move to the Recycle Bin (undoable), with no Explorer dialogs."""
    import ctypes
    from ctypes import wintypes

    class SHFILEOPSTRUCTW(ctypes.Structure):
        _fields_ = [
            ("hwnd", wintypes.HWND),
            ("wFunc", ctypes.c_uint),
            ("pFrom", wintypes.LPCWSTR),
            ("pTo", wintypes.LPCWSTR),
            ("fFlags", ctypes.c_ushort),
            ("fAnyOperationsAborted", wintypes.BOOL),
            ("hNameMappings", ctypes.c_void_p),
            ("lpszProgressTitle", wintypes.LPCWSTR),
        ]

    fo_delete = 3
    flags = 0x0040 | 0x0010 | 0x0400 | 0x0004  # ALLOWUNDO | NOCONFIRMATION | NOERRORUI | SILENT
    op = SHFILEOPSTRUCTW(None, fo_delete, str(path) + "\0", None, flags, False, None, None)
    result = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))  # type: ignore[attr-defined]
    if result != 0 or op.fAnyOperationsAborted:
        raise OSError(f"Recycle Bin refused it (code {result})")


def default_area() -> FileArea:
    roots, blocked = default_roots()
    return FileArea(roots, blocked, trash=recycle_windows if sys.platform == "win32" else None)


__all__ = ["FileAccessError", "FileArea", "default_area", "known_folder"]
