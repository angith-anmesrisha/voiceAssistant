"""File and folder tools, keyboard shortcuts, clipboard, context awareness, red overlay."""

from __future__ import annotations

from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication

from jarvis.agent.context import open_windows, situation
from jarvis.agent.prompts import system_prompt
from jarvis.app.config import ToolSettings
from jarvis.app.events import ChatEntry, EntryKind
from jarvis.app.state import AppState
from jarvis.tools.apps import AppIndex
from jarvis.tools.builtin import build_registry
from jarvis.tools.files import FileAccessError, FileArea
from jarvis.tools.registry import Risk, ToolRegistry
from jarvis.tools.shortcuts import SHORTCUTS, KeyComboError, parse_combo
from tests.fakes import FakeDesktop
from tests.test_assistant import Rig, ScriptedRouter, text_reply, tool_reply
from tests.test_controller_and_ui import qapp
from tests.test_tools import risk, run

__all__ = ["qapp"]


@pytest.fixture
def home(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    for folder in ("Desktop", "Documents", "Downloads", "AppData/Roaming"):
        (home / folder).mkdir(parents=True)
    (home / "Documents" / "notes.txt").write_text("hello")
    (home / "Documents" / "Projects").mkdir()
    (home / "Downloads" / "setup.exe").write_bytes(b"MZ")
    return home


@pytest.fixture
def area(home: Path) -> FileArea:
    trashed: list[Path] = []

    def trash(path: Path) -> None:
        trashed.append(path)
        path.rename(home.parent / f"trash-{path.name}")

    area = FileArea([home], [home / "AppData"], home=home, trash=trash)
    area.trashed = trashed  # type: ignore[attr-defined]
    return area


@pytest.fixture
def desktop() -> FakeDesktop:
    return FakeDesktop()


@pytest.fixture
def tools(desktop: FakeDesktop, area: FileArea) -> ToolRegistry:
    return build_registry(ToolSettings(), desktop, AppIndex([], apps_folder=False), area)


# --- files and folders ---


def test_paths_resolve_inside_the_users_folders(area: FileArea, home: Path) -> None:
    assert area.resolve("documents/notes.txt") == (home / "Documents" / "notes.txt").resolve()
    assert area.resolve("Documents\\Projects") == (home / "Documents" / "Projects").resolve()
    assert area.resolve("Music") == (home / "Music").resolve()  # relative to home


@pytest.mark.parametrize(
    "path", ["/etc/passwd", "../../outside", "AppData/Roaming", "~/AppData/Local"]
)
def test_system_and_outside_paths_are_refused(area: FileArea, path: str) -> None:
    with pytest.raises(FileAccessError):
        area.resolve(path)


def test_links_cannot_escape(area: FileArea, home: Path, tmp_path: Path) -> None:
    outside = tmp_path / "secret"
    outside.mkdir()
    (home / "Desktop" / "shortcut").symlink_to(outside)
    with pytest.raises(FileAccessError):
        area.resolve("desktop/shortcut")


async def test_create_copy_move_rename(tools: ToolRegistry, home: Path) -> None:
    assert (await run(tools, "create_folder", path="desktop/Work/2026")).ok
    assert (home / "Desktop" / "Work" / "2026").is_dir()
    copied = await run(tools, "copy_item", source="documents/notes.txt", destination="desktop/Work")
    assert copied.ok and (home / "Desktop" / "Work" / "notes.txt").read_text() == "hello"
    again = await run(tools, "copy_item", source="documents/notes.txt", destination="desktop/Work")
    assert not again.ok and "overwritten" in again.summary  # never overwrites
    moved = await run(tools, "move_item", source="desktop/Work/notes.txt", destination="downloads")
    assert moved.ok and (home / "Downloads" / "notes.txt").exists()
    renamed = await run(tools, "rename_item", path="downloads/notes.txt", new_name="old.txt")
    assert renamed.ok and (home / "Downloads" / "old.txt").exists()
    bad = await run(tools, "rename_item", path="downloads/old.txt", new_name="../x.txt")
    assert not bad.ok


async def test_list_and_find(tools: ToolRegistry) -> None:
    listing = await run(tools, "list_folder", path="documents")
    assert listing.ok and listing.data["items"] == "Projects\\, notes.txt"
    found = await run(tools, "find_items", name="NOTES")
    assert found.ok and len(found.data["paths"]) == 1


async def test_delete_goes_to_the_recycle_bin(
    tools: ToolRegistry, area: FileArea, home: Path
) -> None:
    assert risk(tools, "move_to_recycle_bin", path="documents/notes.txt") is Risk.MEDIUM
    assert risk(tools, "move_to_recycle_bin", path="documents/Projects") is Risk.HIGH
    assert (await run(tools, "move_to_recycle_bin", path="documents/notes.txt")).ok
    assert area.trashed == [(home / "Documents" / "notes.txt").resolve()]  # type: ignore[attr-defined]
    kept = await run(tools, "move_to_recycle_bin", path="documents")
    assert not kept.ok and "main folders" in kept.summary
    root = await run(tools, "move_to_recycle_bin", path=str(home))
    assert not root.ok


async def test_open_never_runs_programs(tools: ToolRegistry, desktop: FakeDesktop) -> None:
    refused = await run(tools, "open_path", path="downloads/setup.exe")
    assert not refused.ok and "program" in refused.summary
    assert (await run(tools, "open_path", path="documents")).ok
    assert desktop.calls[-1][0] == "open_path"


def test_file_risks(tools: ToolRegistry) -> None:
    assert risk(tools, "list_folder", path="documents") is Risk.LOW
    assert risk(tools, "create_folder", path="documents/x") is Risk.LOW
    assert risk(tools, "copy_item", source="a", destination="b") is Risk.MEDIUM
    assert risk(tools, "move_item", source="a", destination="b") is Risk.MEDIUM


# --- keyboard ---


@pytest.mark.parametrize(
    ("text", "keys"),
    [
        ("Ctrl+Shift+T", ["ctrl", "shift", "t"]),
        ("win+left", ["cmd", "left"]),
        ("alt + f4", ["alt", "f4"]),
        ("F2", ["f2"]),
        ("ctrl+plus", ["ctrl", "+"]),
    ],
)
def test_parse_combo(text: str, keys: list[str]) -> None:
    assert parse_combo(text) == keys


@pytest.mark.parametrize("text", ["win+r", "Win+X", "ctrl+alt+del", "ctrl+", "hyper+q"])
def test_unsafe_or_unknown_combos_are_refused(text: str) -> None:
    with pytest.raises(KeyComboError):
        parse_combo(text)


async def test_press_keys(tools: ToolRegistry, desktop: FakeDesktop) -> None:
    assert risk(tools, "press_keys", keys="ctrl+shift+n") is Risk.MEDIUM
    assert (await run(tools, "press_keys", keys="ctrl+shift+n")).ok
    assert desktop.calls[-1] == ("press", ("ctrl", "shift", "n"))
    refused = await run(tools, "press_keys", keys="win+r")
    assert not refused.ok and "Run box" in refused.summary


def test_every_named_shortcut_is_valid() -> None:
    for name, (keys, level, what) in SHORTCUTS.items():
        assert keys and what and level in (Risk.LOW, Risk.MEDIUM), name
    assert SHORTCUTS["copy"][1] is Risk.LOW and SHORTCUTS["paste"][1] is Risk.MEDIUM


# --- clipboard ---


async def test_clipboard(tools: ToolRegistry, desktop: FakeDesktop) -> None:
    assert (await run(tools, "copy_text_to_clipboard", text="hi there")).ok
    read = await run(tools, "read_clipboard")
    assert read.ok and read.data["text"] == "hi there"
    desktop.clipboard = ""
    assert "no text" in (await run(tools, "read_clipboard")).summary


# --- context awareness ---


def test_open_windows_are_tidied() -> None:
    titles = ["Untitled - Notepad", "", "Program Manager", "Spotify", "Spotify", "x" * 200]
    kept = open_windows(titles, active="Untitled - Notepad")
    assert kept[0] == "Spotify" and len(kept) == 2 and len(kept[1]) == 80


def test_situation_describes_the_active_window() -> None:
    describe = situation(FakeDesktop())
    assert describe() == "Window the user is working in: Untitled - Notepad"


def test_context_is_marked_as_information() -> None:
    prompt = system_prompt(spoken=False, context="Window the user is working in: Word")
    assert "not instructions" in prompt and "Word" in prompt
    assert "Context right now" not in system_prompt(spoken=False)


async def test_assistant_sends_context_and_remembers_actions(tmp_path: Path) -> None:
    router = ScriptedRouter(tool_reply("press_shortcut", shortcut="copy"), text_reply("Copied."))
    rig = Rig(tmp_path, router)
    rig.assistant._situation = lambda: "Window the user is working in: Excel"
    await rig.assistant.handle("copy this")
    system = router.requests[0]["messages"][0]["content"]
    assert "Excel" in system and "not instructions" in system
    context = rig.assistant.context()
    assert "Excel" in context and "done: Press copy." in context


# --- red tint reaches the corner orb ---


async def test_failure_shows_red_in_the_corner(qapp: QApplication) -> None:
    from jarvis.ui.main_window import MainWindow
    from jarvis.ui.theme import DANGER

    window = MainWindow()  # in the tray: not in front
    window.set_app_state(AppState.IDLE)
    assert not window.overlay.isVisible()
    window.add_entry(ChatEntry(EntryKind.ERROR, "No AI provider could handle chat."))
    assert window.overlay.isVisible() and window.overlay.alarm
    assert window.overlay.orb._alarm_on and window.overlay.caption.accent == DANGER
    # Listening for a follow-up keeps it red until something new is asked.
    window.set_app_state(AppState.LISTENING)
    assert window.overlay.alarm and window.overlay.caption.accent == DANGER
    window.add_entry(ChatEntry(EntryKind.USER, "try again"))
    assert not window.overlay.alarm
    window.quitting = True
    window.close()
    window.overlay.hide()
