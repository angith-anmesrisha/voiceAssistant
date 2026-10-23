from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from jarvis.app.config import ToolSettings
from jarvis.tools.apps import AppIndex, InstalledApp
from jarvis.tools.builtin import build_registry
from jarvis.tools.registry import ArgumentError, Risk, ToolRegistry, validate_args
from tests.fakes import FakeDesktop


@pytest.fixture
def desktop() -> FakeDesktop:
    return FakeDesktop()


def start_menu(root: Path) -> Path:
    """A stand-in Start menu with a few shortcuts, as Windows lays them out."""
    programs = root / "Programs"
    for relative in (
        "Riot Games/VALORANT.lnk",
        "Riot Games/Uninstall VALORANT.lnk",
        "Discord Inc/Discord.lnk",
        "Visual Studio Code/Visual Studio Code.lnk",
        "Windows PowerShell/Windows PowerShell.lnk",
        "System Tools/Command Prompt.lnk",
        "Administrative Tools/Registry Editor.lnk",
        "Steam/Steam Support Center.url",
        "Notes.txt",
    ):
        path = programs / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"")
    return programs


@pytest.fixture
def index(tmp_path: Path) -> AppIndex:
    return AppIndex([start_menu(tmp_path)], apps_folder=False)


@pytest.fixture
def registry(desktop: FakeDesktop, index: AppIndex) -> ToolRegistry:
    return build_registry(ToolSettings(), desktop, index)


async def run(registry: ToolRegistry, tool: str, /, **args: Any) -> Any:
    spec = registry.get(tool)
    assert spec is not None
    return await registry.run(spec, validate_args(spec.parameters, args))


def risk(registry: ToolRegistry, tool: str, /, **args: Any) -> Risk:
    spec = registry.get(tool)
    assert spec is not None
    return spec.risk_for(args)


def test_no_dangerous_tools_exist(registry: ToolRegistry) -> None:
    names = " ".join(registry.names())
    for word in ("shell", "command", "exec", "powershell", "cmd", "delete", "registry", "file"):
        assert word not in names


def test_declarations_are_openai_function_tools(registry: ToolRegistry) -> None:
    for declaration in registry.declarations():
        assert declaration["type"] == "function"
        assert declaration["function"]["parameters"]["type"] == "object"


def test_risk_levels(registry: ToolRegistry) -> None:
    assert risk(registry, "get_datetime") is Risk.LOW
    assert risk(registry, "open_app", name="notepad") is Risk.LOW
    assert risk(registry, "open_url", url="https://www.youtube.com/watch?v=x") is Risk.LOW
    assert risk(registry, "open_url", url="https://evil.example/login") is Risk.MEDIUM
    assert risk(registry, "open_url", url="https://notyoutube.com") is Risk.MEDIUM
    assert risk(registry, "press_shortcut", shortcut="play_pause") is Risk.LOW
    assert risk(registry, "press_shortcut", shortcut="close_tab") is Risk.MEDIUM
    assert risk(registry, "type_text", text="hi") is Risk.MEDIUM
    assert risk(registry, "lock_pc") is Risk.MEDIUM
    assert risk(registry, "shutdown_or_restart_pc", action="shutdown") is Risk.HIGH


async def test_open_app_prefers_built_in_shortcuts(
    registry: ToolRegistry, desktop: FakeDesktop
) -> None:
    result = await run(registry, "open_app", name="Notepad")
    assert result.ok
    assert desktop.calls == [("launch", "notepad.exe")]


@pytest.mark.parametrize(
    ("asked", "opened"),
    [
        ("Valorant", "VALORANT"),
        ("valorant", "VALORANT"),
        ("discord", "Discord"),
        ("photoshop", None),  # not installed: reported, not guessed
        ("code", "Visual Studio Code"),
        ("visual studio code", "Visual Studio Code"),
    ],
)
async def test_open_app_opens_anything_installed(
    registry: ToolRegistry, desktop: FakeDesktop, asked: str, opened: str | None
) -> None:
    result = await run(registry, "open_app", name=asked)
    if opened is None:
        assert not result.ok and desktop.calls == []
        return
    assert result.ok, result.summary
    [(call, app)] = desktop.calls
    assert call == "open_installed" and isinstance(app, InstalledApp)
    assert app.name == opened and app.shortcut is not None


async def test_open_app_reports_missing_apps(registry: ToolRegistry, desktop: FakeDesktop) -> None:
    result = await run(registry, "open_app", name="Fortnite")
    assert not result.ok and "isn't installed" in result.summary
    result = await run(registry, "open_app", name="Valorent")  # a typo still finds it
    assert result.ok
    assert len(desktop.calls) == 1


@pytest.mark.parametrize(
    "name", ["powershell", "Windows PowerShell", "command prompt", "cmd", "registry editor"]
)
async def test_shells_and_system_editors_can_never_be_opened(
    registry: ToolRegistry, desktop: FakeDesktop, name: str
) -> None:
    result = await run(registry, "open_app", name=name)
    assert not result.ok
    assert desktop.calls == []


def test_uninstallers_and_links_are_not_apps(index: AppIndex) -> None:
    names = sorted(app.name for app in index.apps())
    assert names == ["Discord", "VALORANT", "Visual Studio Code"]


async def test_open_url_rejects_non_web_schemes(
    registry: ToolRegistry, desktop: FakeDesktop
) -> None:
    for url in ("file:///C:/Windows/System32", "javascript:alert(1)", "ms-settings:", "ftp://x"):
        result = await run(registry, "open_url", url=url)
        assert not result.ok
    assert desktop.calls == []


async def test_web_search_builds_a_query_url(registry: ToolRegistry, desktop: FakeDesktop) -> None:
    result = await run(registry, "web_search", query="lo-fi & chill", site="youtube")
    assert result.ok
    assert desktop.calls == [
        ("open_url", "https://www.youtube.com/results?search_query=lo-fi+%26+chill")
    ]


async def test_type_text_goes_to_previous_window(
    registry: ToolRegistry, desktop: FakeDesktop
) -> None:
    result = await run(registry, "type_text", text="hello")
    assert result.ok
    assert desktop.calls == [("focus", None), ("type", "hello")]
    desktop.calls.clear()
    desktop.focus_ok = False
    result = await run(registry, "type_text", text="hello")
    assert not result.ok
    assert ("type", "hello") not in desktop.calls


async def test_media_keys_do_not_change_focus(registry: ToolRegistry, desktop: FakeDesktop) -> None:
    await run(registry, "press_shortcut", shortcut="play_pause")
    assert desktop.calls == [("press", ("media_play_pause",))]


async def test_shutdown_is_delayed_and_cancellable(
    registry: ToolRegistry, desktop: FakeDesktop
) -> None:
    await run(registry, "shutdown_or_restart_pc", action="restart")
    await run(registry, "cancel_shutdown")
    assert desktop.calls == [("power", ("restart", 60)), ("cancel_power", None)]


def test_validate_args() -> None:
    schema = {
        "type": "object",
        "properties": {
            "a": {"type": "string", "maxLength": 3},
            "n": {"type": "integer"},
            "e": {"type": "string", "enum": ["x", "y"]},
        },
        "required": ["a"],
    }
    assert validate_args(schema, {"a": "ok", "junk": 1}) == {"a": "ok"}
    bad_args: list[dict[str, Any]] = [
        {},
        {"a": "toolong"},
        {"a": "ok", "n": "1"},
        {"a": "ok", "n": True},
        {"a": "x", "e": "z"},
    ]
    for bad in bad_args:
        with pytest.raises(ArgumentError):
            validate_args(schema, bad)


async def test_system_status_reports_numbers(registry: ToolRegistry) -> None:
    result = await run(registry, "get_system_status")
    assert result.ok
    assert 0 <= result.data["cpu_percent"] <= 100
