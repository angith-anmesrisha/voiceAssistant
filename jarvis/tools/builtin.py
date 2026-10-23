"""The assistant's tools. Each one is small, allowlisted, and has a risk level."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from urllib.parse import quote_plus, urlparse

from jarvis.app.config import ToolSettings
from jarvis.tools.apps import AppIndex
from jarvis.tools.desktop import Desktop, DesktopUnsupportedError
from jarvis.tools.files import FileAccessError, FileArea, default_area
from jarvis.tools.registry import Risk, ToolRegistry, ToolResult, ToolSpec
from jarvis.tools.shortcuts import SHORTCUTS, KeyComboError, catalogue, parse_combo

SEARCH_ENGINES = {
    "google": "https://www.google.com/search?q={}",
    "youtube": "https://www.youtube.com/results?search_query={}",
    "wikipedia": "https://en.wikipedia.org/w/index.php?search={}",
    "amazon": "https://www.amazon.com/s?k={}",
    "github": "https://github.com/search?q={}",
    "maps": "https://www.google.com/maps/search/{}",
}

POWER_DELAY_S = 60


def _schema(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": required or []}


def _host_trusted(url: str, trusted: list[str]) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return any(host == site or host.endswith("." + site) for site in trusted)


def _safe(action: str, fn: Any) -> ToolResult:
    try:
        fn()
    except DesktopUnsupportedError as exc:
        return ToolResult(False, f"Couldn't {action}: {exc}.")
    except Exception as exc:
        return ToolResult(False, f"Couldn't {action}: {type(exc).__name__}.")
    return ToolResult(True, action[0].upper() + action[1:] + ".")


def build_registry(
    settings: ToolSettings,
    desktop: Desktop,
    installed: AppIndex | None = None,
    files: FileArea | None = None,
) -> ToolRegistry:
    registry = ToolRegistry()
    installed = installed if installed is not None else AppIndex()
    area = files if files is not None else default_area()
    apps = {name.lower(): target for name, target in settings.approved_apps.items()}

    # --- read-only (LOW) ---

    def get_datetime() -> ToolResult:
        now = datetime.now().astimezone()
        return ToolResult(True, now.strftime("It's %A %d %B %Y, %H:%M %Z."))

    def get_system_status() -> ToolResult:
        import psutil

        data: dict[str, Any] = {
            "cpu_percent": psutil.cpu_percent(interval=0.2),
            "memory_percent": psutil.virtual_memory().percent,
        }
        battery = psutil.sensors_battery() if hasattr(psutil, "sensors_battery") else None
        if battery is not None:
            data["battery_percent"] = round(battery.percent)
            data["plugged_in"] = bool(battery.power_plugged)
        summary = f"CPU {data['cpu_percent']:.0f}%, memory {data['memory_percent']:.0f}%"
        if "battery_percent" in data:
            summary += f", battery {data['battery_percent']}%"
        return ToolResult(True, summary + ".", data)

    def list_open_windows() -> ToolResult:
        try:
            titles = desktop.window_titles()
        except DesktopUnsupportedError as exc:
            return ToolResult(False, str(exc))
        return ToolResult(True, f"{len(titles)} windows open.", {"titles": titles[:40]})

    registry.register(
        ToolSpec(
            "get_datetime",
            "Get the current local date and time.",
            _schema({}),
            get_datetime,
            Risk.LOW,
            lambda a: "Read the date and time",
        )
    )
    registry.register(
        ToolSpec(
            "get_system_status",
            "Get CPU, memory and battery usage of this PC.",
            _schema({}),
            get_system_status,
            Risk.LOW,
            lambda a: "Read system status",
        )
    )
    registry.register(
        ToolSpec(
            "list_open_windows",
            "List the titles of open windows.",
            _schema({}),
            list_open_windows,
            Risk.LOW,
            lambda a: "List open windows",
        )
    )

    # --- opening things ---

    def open_app(name: str) -> ToolResult:
        name = name.strip()
        target = apps.get(name.lower())
        if target is not None:
            return _safe(f"open {name}", lambda: desktop.launch(target))
        # Otherwise anything installed, as listed in the Start menu.
        app, close = installed.find(name)
        if app is None:
            hint = f" Closest installed: {', '.join(close)}." if close else ""
            return ToolResult(False, f"{name} isn't installed on this PC.{hint}")
        found = app
        return _safe(f"open {found.name}", lambda: desktop.open_installed(found))

    def open_url(url: str) -> ToolResult:
        parsed = urlparse(url.strip())
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            return ToolResult(False, "Only http and https web addresses can be opened.")
        return _safe(f"open {parsed.hostname}", lambda: desktop.open_url(url.strip()))

    def web_search(query: str, site: str = "google") -> ToolResult:
        template = SEARCH_ENGINES.get(site, SEARCH_ENGINES["google"])
        url = template.format(quote_plus(query))
        return _safe(f"search {site} for {query}", lambda: desktop.open_url(url))

    registry.register(
        ToolSpec(
            "open_app",
            "Open any app or game installed on this PC (anything in the Start menu, e.g. "
            "Valorant, Discord, Steam, Word, WhatsApp) by its name. Always try this when the "
            "user asks to open or launch an app; it reports if the app isn't installed. "
            f"Built-in shortcuts: {', '.join(sorted(apps))}.",
            _schema(
                {"name": {"type": "string", "maxLength": 100, "description": "App name"}},
                ["name"],
            ),
            open_app,
            Risk.LOW,
            lambda a: f"Open {a.get('name', 'an app')}",
        )
    )
    registry.register(
        ToolSpec(
            "open_url",
            "Open a web page (http or https) in the default browser.",
            _schema({"url": {"type": "string", "maxLength": 2000}}, ["url"]),
            open_url,
            lambda a: (
                Risk.LOW
                if _host_trusted(str(a.get("url", "")), settings.trusted_sites)
                else Risk.MEDIUM
            ),
            lambda a: f"Open {a.get('url', 'a web page')}",
        )
    )
    registry.register(
        ToolSpec(
            "web_search",
            "Search the web (Google, YouTube, Wikipedia, Amazon, GitHub or Maps) in the browser.",
            _schema(
                {
                    "query": {"type": "string", "maxLength": 300},
                    "site": {"type": "string", "enum": sorted(SEARCH_ENGINES)},
                },
                ["query"],
            ),
            web_search,
            Risk.LOW,
            lambda a: f"Search {a.get('site', 'google')} for “{a.get('query', '')}”",
        )
    )

    # --- keyboard (acts on whatever window has focus) ---

    def press(keys: list[str], label: str) -> ToolResult:
        def go() -> None:
            if keys[0].startswith("media_"):
                desktop.press(keys)  # media keys work regardless of focus
                return
            desktop.focus_previous_window()
            desktop.press(keys)

        return _safe(f"press {label}", go)

    def press_shortcut(shortcut: str) -> ToolResult:
        keys, _risk, _what = SHORTCUTS[shortcut]
        return press(keys, shortcut.replace("_", " "))

    def press_keys(keys: str) -> ToolResult:
        try:
            parsed = parse_combo(keys)
        except KeyComboError as exc:
            return ToolResult(False, f"Couldn't press {keys}: {exc}.")
        return press(parsed, keys)

    def type_text(text: str) -> ToolResult:
        if not text:
            return ToolResult(False, "Nothing to type.")

        def go() -> None:
            if not desktop.focus_previous_window():
                raise RuntimeError("no target window")
            desktop.type_text(text)

        result = _safe("type the text", go)
        if not result.ok and "RuntimeError" in result.summary:
            return ToolResult(False, "Couldn't find the window to type into. Click it first.")
        return ToolResult(
            result.ok, f"Typed {len(text)} characters." if result.ok else result.summary
        )

    registry.register(
        ToolSpec(
            "press_shortcut",
            "Press a named keyboard shortcut in the window the user was using. Known: "
            + catalogue(),
            _schema({"shortcut": {"type": "string", "enum": list(SHORTCUTS)}}, ["shortcut"]),
            press_shortcut,
            lambda a: SHORTCUTS.get(str(a.get("shortcut")), ([], Risk.MEDIUM, ""))[1],
            lambda a: f"Press {str(a.get('shortcut', '')).replace('_', ' ')}",
        )
    )
    registry.register(
        ToolSpec(
            "press_keys",
            "Press any other key combination in the window the user was using, written "
            "like 'ctrl+shift+t', 'alt+f4', 'win+left' or 'f2'. Use the app's own shortcut "
            "when you know it (e.g. ctrl+shift+n for a new folder in Explorer).",
            _schema({"keys": {"type": "string", "maxLength": 60}}, ["keys"]),
            press_keys,
            Risk.MEDIUM,
            lambda a: f"Press {a.get('keys', 'keys')}",
        )
    )
    registry.register(
        ToolSpec(
            "type_text",
            "Type text into the window the user was last using (dictation).",
            _schema({"text": {"type": "string", "maxLength": settings.max_typed_chars}}, ["text"]),
            type_text,
            Risk.MEDIUM,
            lambda a: (
                f"Type {len(str(a.get('text', '')))} characters into your last window: "
                f"“{str(a.get('text', ''))[:80]}”"
            ),
        )
    )

    # --- files and folders (only in the user's folders; nothing deleted outright) ---

    def file_op(action: str, fn: Any) -> ToolResult:
        try:
            done = fn()
        except FileAccessError as exc:
            return ToolResult(False, f"Couldn't {action}: {exc}.")
        except OSError as exc:
            return ToolResult(False, f"Couldn't {action}: {exc.strerror or type(exc).__name__}.")
        return done if isinstance(done, ToolResult) else ToolResult(True, str(done))

    def list_folder(path: str) -> ToolResult:
        def go() -> ToolResult:
            folder, names = area.list_folder(path)
            listing = ", ".join(names) or "(empty)"
            return ToolResult(True, f"{folder}: {len(names)} items.", {"items": listing})

        return file_op("list the folder", go)

    def find_items(name: str, folder: str = "") -> ToolResult:
        def go() -> ToolResult:
            found = area.find(name, folder)
            paths = [str(p) for p in found]
            return ToolResult(True, f"Found {len(paths)} matching items.", {"paths": paths})

        return file_op(f"search for {name}", go)

    def create_folder(path: str) -> ToolResult:
        return file_op("create the folder", lambda: f"Created folder {area.create_folder(path)}.")

    def copy_item(source: str, destination: str) -> ToolResult:
        return file_op("copy it", lambda: f"Copied to {area.copy(source, destination)}.")

    def move_item(source: str, destination: str) -> ToolResult:
        return file_op("move it", lambda: f"Moved to {area.move(source, destination)}.")

    def rename_item(path: str, new_name: str) -> ToolResult:
        return file_op("rename it", lambda: f"Renamed to {area.rename(path, new_name).name}.")

    def move_to_recycle_bin(path: str) -> ToolResult:
        return file_op(
            "move it to the Recycle Bin",
            lambda: f"Moved {area.recycle(path).name} to the Recycle Bin.",
        )

    def open_path(path: str) -> ToolResult:
        def go() -> ToolResult:
            target = area.openable(path)
            return _safe(f"open {target.name or target}", lambda: desktop.open_path(target))

        return file_op("open it", go)

    path_help = (
        "A full path, or one starting with desktop, documents, downloads, pictures, music "
        "or videos (e.g. 'documents/Projects/notes.txt')."
    )
    path_arg = {"type": "string", "maxLength": 500, "description": path_help}
    file_tools: list[tuple[str, str, dict[str, Any], list[str], Any, Any, Any]] = [
        ("list_folder", "List what is in a folder.", {"path": path_arg}, ["path"],
         list_folder, Risk.LOW, lambda a: f"List {a.get('path')}"),
        ("find_items", "Find files or folders by part of their name (in the user's folders, "
         "or inside 'folder').", {"name": {"type": "string", "maxLength": 100},
         "folder": path_arg}, ["name"], find_items, Risk.LOW,
         lambda a: f"Search for {a.get('name')}"),
        ("create_folder", "Create a folder (and any missing parent folders).",
         {"path": path_arg}, ["path"], create_folder, Risk.LOW,
         lambda a: f"Create folder {a.get('path')}"),
        ("copy_item", "Copy a file or folder. If the destination is a folder, the copy goes "
         "inside it. Never overwrites.", {"source": path_arg, "destination": path_arg},
         ["source", "destination"], copy_item, Risk.MEDIUM,
         lambda a: f"Copy {a.get('source')} to {a.get('destination')}"),
        ("move_item", "Move a file or folder. If the destination is a folder, it goes inside "
         "it. Never overwrites.", {"source": path_arg, "destination": path_arg},
         ["source", "destination"], move_item, Risk.MEDIUM,
         lambda a: f"Move {a.get('source')} to {a.get('destination')}"),
        ("rename_item", "Rename a file or folder (new name only, not a path).",
         {"path": path_arg, "new_name": {"type": "string", "maxLength": 200}},
         ["path", "new_name"], rename_item, Risk.MEDIUM,
         lambda a: f"Rename {a.get('path')} to {a.get('new_name')}"),
        ("move_to_recycle_bin", "Delete a file or folder by moving it to the Recycle Bin "
         "(it can be restored).", {"path": path_arg}, ["path"], move_to_recycle_bin,
         lambda a: Risk.HIGH if area.is_folder(str(a.get("path", ""))) else Risk.MEDIUM,
         lambda a: f"Move {a.get('path')} to the Recycle Bin"),
        ("open_path", "Open a folder in File Explorer, or a document, picture, video or song "
         "in its default app (never programs or scripts).", {"path": path_arg}, ["path"],
         open_path, Risk.LOW, lambda a: f"Open {a.get('path')}"),
    ]  # fmt: skip
    for name, description, props, required, func, risk_rule, describe in file_tools:
        registry.register(
            ToolSpec(name, description, _schema(props, required), func, risk_rule, describe)
        )

    # --- clipboard ---

    def copy_text_to_clipboard(text: str) -> ToolResult:
        return _safe("copy the text to the clipboard", lambda: desktop.set_clipboard_text(text))

    def read_clipboard() -> ToolResult:
        try:
            text = desktop.get_clipboard_text()
        except DesktopUnsupportedError as exc:
            return ToolResult(False, str(exc))
        except Exception as exc:
            return ToolResult(False, f"Couldn't read the clipboard: {type(exc).__name__}.")
        if not text:
            return ToolResult(True, "The clipboard has no text.")
        # Information for the model, never instructions (see the safety prompt).
        return ToolResult(True, f"Clipboard has {len(text)} characters.", {"text": text[:4000]})

    registry.register(
        ToolSpec(
            "copy_text_to_clipboard",
            "Put text on the clipboard so the user can paste it.",
            _schema({"text": {"type": "string", "maxLength": 20_000}}, ["text"]),
            copy_text_to_clipboard,
            Risk.LOW,
            lambda a: "Copy text to the clipboard",
        )
    )
    registry.register(
        ToolSpec(
            "read_clipboard",
            "Read the text currently on the clipboard (e.g. to summarise or rewrite it).",
            _schema({}),
            read_clipboard,
            Risk.LOW,
            lambda a: "Read the clipboard",
        )
    )

    # --- power ---

    registry.register(
        ToolSpec(
            "lock_pc",
            "Lock the PC (Windows lock screen).",
            _schema({}),
            lambda: _safe("lock the PC", desktop.lock),
            Risk.MEDIUM,
            lambda a: "Lock the PC",
        )
    )
    registry.register(
        ToolSpec(
            "sleep_pc",
            "Put the PC to sleep.",
            _schema({}),
            lambda: _safe("put the PC to sleep", desktop.sleep),
            Risk.MEDIUM,
            lambda a: "Put the PC to sleep",
        )
    )
    registry.register(
        ToolSpec(
            "shutdown_or_restart_pc",
            f"Shut down or restart the PC after a {POWER_DELAY_S} second warning.",
            _schema({"action": {"type": "string", "enum": ["shutdown", "restart"]}}, ["action"]),
            lambda action: _safe(
                f"schedule a {action} in {POWER_DELAY_S} seconds",
                lambda: desktop.schedule_power(action, POWER_DELAY_S),
            ),
            Risk.HIGH,
            lambda a: (
                f"{str(a.get('action', 'shutdown')).title()} the PC in {POWER_DELAY_S} "
                "seconds. Unsaved work in open apps may be lost."
            ),
        )
    )
    registry.register(
        ToolSpec(
            "cancel_shutdown",
            "Cancel a scheduled shutdown or restart.",
            _schema({}),
            lambda: _safe("cancel the scheduled shutdown", desktop.cancel_power),
            Risk.LOW,
            lambda a: "Cancel the scheduled shutdown",
        )
    )
    return registry
