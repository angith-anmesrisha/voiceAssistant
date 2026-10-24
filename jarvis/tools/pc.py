"""Tools for documents, the desktop, volume, brightness, wallpaper and closing apps.

Every change that can be reversed records its undo step at the moment it is made, from
the value that was really there before.
"""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path
from typing import Any

from jarvis.tools.documents import WRITABLE_TYPES, DocumentError, read_text
from jarvis.tools.files import FileAccessError, FileArea
from jarvis.tools.registry import Risk, ToolRegistry, ToolResult, ToolSpec
from jarvis.tools.system_ctl import SettingUnavailableError, SystemControl
from jarvis.tools.undo import UndoJournal
from jarvis.tools.windows import WindowControl, close_app

IMAGE_TYPES = frozenset({".jpg", ".jpeg", ".png", ".bmp", ".gif", ".webp", ".tif", ".tiff"})

# Desktop tidy-up: file type -> folder on the desktop. Shortcuts and folders stay put.
SORT_FOLDERS = {
    "Images": IMAGE_TYPES | {".heic", ".svg", ".ico"},
    "Documents": {".pdf", ".doc", ".docx", ".odt", ".rtf", ".txt", ".md", ".xls", ".xlsx",
                  ".csv", ".ppt", ".pptx", ".odp", ".ods", ".epub"},
    "Videos": {".mp4", ".mkv", ".mov", ".avi", ".wmv", ".webm", ".m4v"},
    "Music": {".mp3", ".wav", ".flac", ".m4a", ".aac", ".ogg", ".wma"},
    "Archives": {".zip", ".rar", ".7z", ".tar", ".gz", ".iso"},
    "Installers": {".exe", ".msi", ".msix", ".appx"},
    "Code": {".py", ".js", ".ts", ".html", ".css", ".json", ".java", ".c", ".cpp", ".ipynb"},
}  # fmt: skip
_LEAVE = frozenset({".lnk", ".url", ".ini"})


def _schema(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": required or []}


def sort_folder(path: Path) -> str | None:
    suffix = path.suffix.lower()
    if suffix in _LEAVE or path.name.lower() == "desktop.ini":
        return None
    for folder, types in SORT_FOLDERS.items():
        if suffix in types:
            return folder
    return "Other"


def register_pc_tools(
    registry: ToolRegistry,
    area: FileArea,
    journal: UndoJournal,
    windows: WindowControl | None,
    system: SystemControl | None,
) -> None:
    _document_tools(registry, area, journal)
    if windows is not None:
        _window_tools(registry, windows)
    if system is not None:
        _setting_tools(registry, area, journal, system)


def _document_tools(registry: ToolRegistry, area: FileArea, journal: UndoJournal) -> None:
    async def read_document(path: str) -> ToolResult:
        try:
            target = area.resolve(path)
            text, cut = await asyncio.to_thread(read_text, target)
        except (FileAccessError, DocumentError) as exc:
            return ToolResult(False, f"Couldn't read it: {exc}.")
        except OSError as exc:
            return ToolResult(False, f"Couldn't read it: {exc.strerror or type(exc).__name__}.")
        note = " (only the start; it's long)" if cut else ""
        return ToolResult(True, f"Read {target.name}{note}.", {"text": text or "(empty)"})

    def save_text(path: str, text: str) -> ToolResult:
        try:
            target = area.resolve(path)
            if target.suffix.lower() not in WRITABLE_TYPES:
                allowed = ", ".join(sorted(WRITABLE_TYPES))
                return ToolResult(False, f"JARVIS only creates text files ({allowed}).")
            if target.exists():
                return ToolResult(False, f"{target.name} already exists; nothing was overwritten.")
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("x", encoding="utf-8") as handle:
                handle.write(text)
        except FileAccessError as exc:
            return ToolResult(False, f"Couldn't create it: {exc}.")
        except OSError as exc:
            return ToolResult(False, f"Couldn't create it: {exc.strerror or type(exc).__name__}.")

        def revert() -> str:
            area.recycle(str(target))
            return f"Sent {target.name} to the Recycle Bin."

        journal.record(f"Created {target.name}", revert)
        return ToolResult(True, f"Created {target} ({len(text)} characters).")

    def organize_desktop() -> ToolResult:
        try:
            desktop = area.resolve("desktop")
        except FileAccessError as exc:
            return ToolResult(False, f"Couldn't tidy the desktop: {exc}.")
        moves: list[tuple[Path, Path]] = []
        made: list[Path] = []
        for item in sorted(desktop.iterdir()):
            folder = sort_folder(item) if item.is_file() else None
            if folder is None:
                continue
            target_dir = desktop / folder
            if not target_dir.exists():
                target_dir.mkdir()
                made.append(target_dir)
            target = target_dir / item.name
            if target.exists():
                continue  # never overwrite
            try:
                shutil.move(str(item), str(target))
            except OSError:
                continue  # open in another app; leave it
            moves.append((item, target))
        if not moves:
            for empty in made:
                empty.rmdir()
            return ToolResult(True, "The desktop was already tidy.")

        def revert() -> str:
            back = 0
            for original, moved in reversed(moves):
                if moved.exists() and not original.exists():
                    shutil.move(str(moved), str(original))
                    back += 1
            for folder in made:
                if folder.exists() and not any(folder.iterdir()):
                    folder.rmdir()
            return f"Put {back} files back where they were on the desktop."

        journal.record(f"Tidied {len(moves)} desktop files into folders", revert)
        counts: dict[str, int] = {}
        for _original, moved in moves:
            counts[moved.parent.name] = counts.get(moved.parent.name, 0) + 1
        detail = ", ".join(f"{n} to {name}" for name, n in sorted(counts.items()))
        return ToolResult(True, f"Moved {len(moves)} files: {detail}.")

    path_arg = {"type": "string", "maxLength": 500}
    registry.register(
        ToolSpec(
            "read_document",
            "Read a document in the user's folders (text, Markdown, CSV, JSON, Word, Excel, "
            "PowerPoint, PDF) to summarise it or answer questions about it.",
            _schema({"path": path_arg}, ["path"]),
            read_document,
            Risk.LOW,
            lambda a: f"Read {a.get('path')}",
            group="files",
        )
    )
    registry.register(
        ToolSpec(
            "save_text",
            "Create a NEW text file (.txt, .md, .csv, .json...) with the given text, e.g. to "
            "save notes or a summary. Never overwrites.",
            _schema({"path": path_arg, "text": {"type": "string", "maxLength": 50_000}},
                    ["path", "text"]),
            save_text,
            Risk.LOW,
            lambda a: f"Create {a.get('path')}",
            group="files",
        )
    )  # fmt: skip
    registry.register(
        ToolSpec(
            "organize_desktop",
            "Tidy the desktop: move loose files into folders by type (Images, Documents, "
            "Videos...). Shortcuts and folders stay. Can be undone in one go.",
            _schema({}),
            organize_desktop,
            Risk.MEDIUM,
            lambda a: "Move the files on your desktop into folders by type",
            group="files",
        )
    )


def _window_tools(registry: ToolRegistry, windows: WindowControl) -> None:
    async def close_app_tool(name: str) -> ToolResult:
        asked, still_open = await asyncio.to_thread(close_app, windows, name)
        if not asked:
            return ToolResult(
                False, f"No open window of {name} found (JARVIS never closes itself)."
            )
        if still_open:
            return ToolResult(
                False,
                f"Asked {name} to close, but {still_open} window(s) are still open; it may be "
                "asking about unsaved work.",
            )
        return ToolResult(True, f"Closed {name}.")

    registry.register(
        ToolSpec(
            "close_app",
            "Close an app by name (politely, like clicking its X, so it can ask to save). "
            "Checks that it closed.",
            _schema({"name": {"type": "string", "maxLength": 100}}, ["name"]),
            close_app_tool,
            Risk.LOW,
            lambda a: f"Close {a.get('name')}",
            group="system",
        )
    )


def _setting_tools(
    registry: ToolRegistry, area: FileArea, journal: UndoJournal, system: SystemControl
) -> None:
    async def change(what: str, read: Any, write: Any, value: Any, show: Any) -> ToolResult:
        try:
            before = await asyncio.to_thread(read)
            await asyncio.to_thread(write, value)
        except SettingUnavailableError as exc:
            return ToolResult(False, f"Couldn't change the {what}: {exc}.")
        except Exception as exc:
            return ToolResult(False, f"Couldn't change the {what} ({type(exc).__name__}).")

        def revert() -> str:
            write(before)
            return f"Put the {what} back to {show(before)}."

        journal.record(f"Set the {what} to {show(value)}", revert)
        return ToolResult(True, f"Set the {what} to {show(value)} (it was {show(before)}).")

    async def set_volume(percent: int | None = None, mute: bool | None = None) -> ToolResult:
        if mute is not None:
            return await change(
                "sound", system.muted, system.set_muted, mute, lambda m: "muted" if m else "on"
            )
        if percent is None:
            return ToolResult(False, "Give a volume level from 0 to 100, or mute.")
        return await change(
            "volume", system.volume, system.set_volume, max(0, min(100, percent)),
            lambda v: f"{v}%",
        )  # fmt: skip

    async def set_brightness(percent: int) -> ToolResult:
        return await change(
            "brightness", system.brightness, system.set_brightness, max(0, min(100, percent)),
            lambda v: f"{v}%",
        )  # fmt: skip

    async def set_wallpaper(path: str) -> ToolResult:
        try:
            picture = area.resolve(path)
        except FileAccessError as exc:
            return ToolResult(False, f"Couldn't use it: {exc}.")
        if picture.suffix.lower() not in IMAGE_TYPES or not picture.is_file():
            return ToolResult(False, f"{picture.name} isn't a picture JARVIS can use.")
        return await change(
            "wallpaper", system.wallpaper, lambda p: system.set_wallpaper(Path(p)), str(picture),
            lambda p: Path(p).name or "none",
        )  # fmt: skip

    async def set_dark_mode(on: bool) -> ToolResult:
        return await change(
            "theme", system.dark_mode, system.set_dark_mode, on,
            lambda dark: "dark mode" if dark else "light mode",
        )  # fmt: skip

    async def set_wifi(on: bool) -> ToolResult:
        result = await change(
            "Wi-Fi", system.wifi, system.set_wifi, on, lambda live: "on" if live else "off"
        )
        if result.ok and not on:
            result = ToolResult(
                True,
                result.summary + " Online AIs can't answer until it's back on; JARVIS's local "
                "model (if installed) still can, or flip it back in the taskbar.",
            )
        return result

    async def get_volume_and_brightness() -> ToolResult:
        parts = []
        for label, read in (("volume", system.volume), ("brightness", system.brightness)):
            try:
                parts.append(f"{label} {await asyncio.to_thread(read)}%")
            except Exception:
                parts.append(f"{label} unknown")
        return ToolResult(True, ", ".join(parts).capitalize() + ".")

    level = {"type": "integer", "description": "0 to 100"}
    registry.register(
        ToolSpec(
            "set_volume",
            "Set the speaker volume to a level, or mute/unmute. For small steps the volume "
            "keys (press_shortcut) are fine. Undoable.",
            _schema({"percent": level, "mute": {"type": "boolean"}}),
            set_volume,
            Risk.LOW,
            lambda a: "Mute" if a.get("mute") else f"Set the volume to {a.get('percent')}%",
            group="system",
        )
    )
    registry.register(
        ToolSpec(
            "set_brightness",
            "Set the screen brightness (built-in screens). Undoable.",
            _schema({"percent": level}, ["percent"]),
            set_brightness,
            Risk.LOW,
            lambda a: f"Set the brightness to {a.get('percent')}%",
            group="system",
        )
    )
    registry.register(
        ToolSpec(
            "set_wallpaper",
            "Set the desktop wallpaper to a picture in the user's folders. Undoable.",
            _schema({"path": {"type": "string", "maxLength": 500}}, ["path"]),
            set_wallpaper,
            Risk.LOW,
            lambda a: f"Set the wallpaper to {a.get('path')}",
            group="system",
        )
    )
    registry.register(
        ToolSpec(
            "set_dark_mode",
            "Switch Windows and apps to dark mode (on=true) or light mode. Undoable.",
            _schema({"on": {"type": "boolean"}}, ["on"]),
            set_dark_mode,
            Risk.MEDIUM,
            lambda a: (
                "Switch Windows to dark mode" if a.get("on") else "Switch Windows to light mode"
            ),
            group="system",
        )
    )
    registry.register(
        ToolSpec(
            "set_wifi",
            "Turn the Wi-Fi radio on or off. Turning it off cuts the internet, so online AIs "
            "stop answering. Undoable.",
            _schema({"on": {"type": "boolean"}}, ["on"]),
            set_wifi,
            Risk.MEDIUM,
            lambda a: (
                "Turn Wi-Fi on"
                if a.get("on")
                else "Turn Wi-Fi off (online AIs stop answering until it's back on)"
            ),
            group="system",
        )
    )
    registry.register(
        ToolSpec(
            "get_volume_and_brightness",
            "Read the current volume and screen brightness.",
            _schema({}),
            get_volume_and_brightness,
            Risk.LOW,
            lambda a: "Read volume and brightness",
            group="system",
        )
    )
