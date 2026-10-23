"""The assistant's tools. Each one is small, allowlisted, and has a risk level."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from urllib.parse import quote_plus, urlparse

from jarvis.app.config import ToolSettings
from jarvis.tools.desktop import Desktop, DesktopUnsupportedError
from jarvis.tools.registry import Risk, ToolRegistry, ToolResult, ToolSpec

# name -> (keys, risk). Keys are pynput Key names or single characters.
SHORTCUTS: dict[str, tuple[list[str], Risk]] = {
    "play_pause": (["media_play_pause"], Risk.LOW),
    "next_track": (["media_next"], Risk.LOW),
    "previous_track": (["media_previous"], Risk.LOW),
    "volume_up": (["media_volume_up"], Risk.LOW),
    "volume_down": (["media_volume_down"], Risk.LOW),
    "mute": (["media_volume_mute"], Risk.LOW),
    "screenshot_tool": (["cmd", "shift", "s"], Risk.LOW),
    "show_desktop": (["cmd", "d"], Risk.MEDIUM),
    "switch_window": (["alt", "tab"], Risk.MEDIUM),
    "copy": (["ctrl", "c"], Risk.MEDIUM),
    "paste": (["ctrl", "v"], Risk.MEDIUM),
    "undo": (["ctrl", "z"], Risk.MEDIUM),
    "select_all": (["ctrl", "a"], Risk.MEDIUM),
    "new_tab": (["ctrl", "t"], Risk.MEDIUM),
    "close_tab": (["ctrl", "w"], Risk.MEDIUM),
}

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


def build_registry(settings: ToolSettings, desktop: Desktop) -> ToolRegistry:
    registry = ToolRegistry()
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
        target = apps.get(name.strip().lower())
        if target is None:
            return ToolResult(
                False,
                f"'{name}' isn't an approved app. Approved: {', '.join(sorted(apps))}.",
            )
        return _safe(f"open {name}", lambda: desktop.launch(target))

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
            "Open an approved desktop app by name.",
            _schema(
                {"name": {"type": "string", "enum": sorted(apps), "description": "App name"}},
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

    def press_shortcut(shortcut: str) -> ToolResult:
        keys, _risk = SHORTCUTS[shortcut]

        def go() -> None:
            if keys[0].startswith("media_"):
                desktop.press(keys)  # media keys work regardless of focus
                return
            desktop.focus_previous_window()
            desktop.press(keys)

        return _safe(f"press {shortcut.replace('_', ' ')}", go)

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
            "Press a named keyboard shortcut (media keys, volume, copy/paste, tabs, etc.).",
            _schema({"shortcut": {"type": "string", "enum": list(SHORTCUTS)}}, ["shortcut"]),
            press_shortcut,
            lambda a: SHORTCUTS.get(str(a.get("shortcut")), ([], Risk.MEDIUM))[1],
            lambda a: f"Press {str(a.get('shortcut', '')).replace('_', ' ')}",
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
