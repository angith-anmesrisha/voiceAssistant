"""Documents, desktop tidy-up, volume/brightness/wallpaper, closing and raising apps."""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from jarvis.app.config import ToolSettings
from jarvis.tools.apps import AppIndex, InstalledApp
from jarvis.tools.builtin import build_registry
from jarvis.tools.documents import DocumentError, read_text
from jarvis.tools.files import FileArea
from jarvis.tools.registry import Risk, ToolRegistry
from jarvis.tools.system_ctl import SettingUnavailableError
from jarvis.tools.undo import UndoJournal
from jarvis.tools.windows import Window, closable, matches
from tests.fakes import FakeDesktop
from tests.test_tools import risk, run


class FakeWindows:
    def __init__(self, *open_windows: Window) -> None:
        self.open = list(open_windows)
        self.raised: list[str] = []
        self.closed: list[str] = []
        self.stubborn: set[int] = set()  # windows that ask "save changes?" and stay
        self.appear_on_launch: Window | None = None

    def windows(self) -> list[Window]:
        return list(self.open)

    def raise_window(self, window: Window) -> bool:
        self.raised.append(window.title)
        return True

    def close(self, window: Window) -> None:
        self.closed.append(window.title)
        if window.handle not in self.stubborn:
            self.open.remove(window)

    def exists(self, window: Window) -> bool:
        return window in self.open


class FakeSystem:
    def __init__(self) -> None:
        self.level = 40
        self.mute = False
        self.light = 70
        self.paper = r"C:\Windows\Web\img0.jpg"
        self.no_brightness = False

    def volume(self) -> int:
        return self.level

    def set_volume(self, percent: int) -> None:
        self.level = percent

    def muted(self) -> bool:
        return self.mute

    def set_muted(self, muted: bool) -> None:
        self.mute = muted

    def brightness(self) -> int:
        if self.no_brightness:
            raise SettingUnavailableError("this screen's brightness can't be set by Windows")
        return self.light

    def set_brightness(self, percent: int) -> None:
        self.light = percent

    def wallpaper(self) -> str:
        return self.paper

    def set_wallpaper(self, path: Path) -> None:
        self.paper = str(path)


@pytest.fixture
def home(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    for folder in ("Desktop", "Documents", "Pictures"):
        (home / folder).mkdir(parents=True)
    return home


def tools(
    home: Path,
    windows: FakeWindows | None = None,
    system: FakeSystem | None = None,
    journal: UndoJournal | None = None,
    installed: AppIndex | None = None,
    desktop: FakeDesktop | None = None,
) -> ToolRegistry:
    def trash(path: Path) -> None:
        path.unlink()

    return build_registry(
        ToolSettings(approved_apps={}),
        desktop or FakeDesktop(),
        installed or AppIndex([], apps_folder=False),
        FileArea([home], home=home, trash=trash),
        undo=journal,
        windows=windows,
        system=system,
    )


# --- documents ---


def write_zip(path: Path, files: dict[str, str]) -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        for name, body in files.items():
            archive.writestr(name, body)
    return path


W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
S = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
A = 'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"'


def test_office_documents_are_read(home: Path) -> None:
    docx = write_zip(
        home / "Documents" / "plan.docx",
        {
            "word/document.xml": f"<w:document {W}><w:body>"
            "<w:p><w:r><w:t>Trip </w:t></w:r><w:r><w:t>plan</w:t></w:r></w:p>"
            "<w:p><w:r><w:t>Day 1: Kochi</w:t></w:r></w:p></w:body></w:document>"
        },
    )
    assert read_text(docx) == ("Trip plan\nDay 1: Kochi", False)
    xlsx = write_zip(
        home / "Documents" / "budget.xlsx",
        {
            "xl/sharedStrings.xml": f"<sst {S}><si><t>Item</t></si><si><t>Hotel</t></si></sst>",
            "xl/worksheets/sheet1.xml": f'<worksheet {S}><sheetData><row><c t="s"><v>0</v></c>'
            '<c><v>Cost</v></c></row><row><c t="s"><v>1</v></c><c><v>120</v></c></row>'
            "</sheetData></worksheet>",
        },
    )
    assert read_text(xlsx)[0] == "[sheet1]\nItem | Cost\nHotel | 120"
    pptx = write_zip(
        home / "Documents" / "deck.pptx",
        {
            "ppt/slides/slide2.xml": f"<p {A}><a:t>Second</a:t></p>",
            "ppt/slides/slide1.xml": f"<p {A}><a:t>First</a:t><a:t>slide</a:t></p>",
        },
    )
    assert read_text(pptx)[0] == "Slide 1: First slide\nSlide 2: Second"


def test_long_unknown_and_damaged_documents(home: Path) -> None:
    long = home / "Documents" / "log.txt"
    long.write_text("x" * 9000)
    text, cut = read_text(long)
    assert cut and len(text) == 8000
    with pytest.raises(DocumentError, match=r"can't read \.bin"):
        (home / "Documents" / "a.bin").write_bytes(b"\0")
        read_text(home / "Documents" / "a.bin")
    (home / "Documents" / "bad.docx").write_bytes(b"not a zip")
    with pytest.raises(DocumentError, match="damaged"):
        read_text(home / "Documents" / "bad.docx")


async def test_read_and_save_text(home: Path) -> None:
    journal = UndoJournal()
    registry = tools(home, journal=journal)
    (home / "Documents" / "notes.md").write_text("# Ideas\nBuild JARVIS")
    read = await run(registry, "read_document", path="documents/notes.md")
    assert read.ok and read.data["text"] == "# Ideas\nBuild JARVIS"
    saved = await run(registry, "save_text", path="desktop/summary.txt", text="Short.")
    assert saved.ok and (home / "Desktop" / "summary.txt").read_text() == "Short."
    again = await run(registry, "save_text", path="desktop/summary.txt", text="Other")
    assert not again.ok and "nothing was overwritten" in again.summary
    script = await run(registry, "save_text", path="desktop/run.bat", text="echo hi")
    assert not script.ok and not (home / "Desktop" / "run.bat").exists()
    assert (await run(registry, "undo_last")).ok
    assert not (home / "Desktop" / "summary.txt").exists()


async def test_desktop_is_tidied_and_put_back(home: Path) -> None:
    desk = home / "Desktop"
    for name in ("photo.JPG", "cv.pdf", "song.mp3", "setup.exe", "Game.lnk", "odd.xyz"):
        (desk / name).write_text(name)
    (desk / "Projects").mkdir()
    registry = tools(home)
    assert risk(registry, "organize_desktop") is Risk.MEDIUM
    tidy = await run(registry, "organize_desktop")
    assert tidy.ok and tidy.summary.startswith("Moved 5 files:")
    assert (desk / "Images" / "photo.JPG").exists() and (desk / "Installers" / "setup.exe").exists()
    assert (desk / "Other" / "odd.xyz").exists() and (desk / "Game.lnk").exists()
    undone = await run(registry, "undo_last")
    assert undone.summary == "Put 5 files back where they were on the desktop."
    assert sorted(p.name for p in desk.iterdir()) == sorted(
        ["photo.JPG", "cv.pdf", "song.mp3", "setup.exe", "Game.lnk", "odd.xyz", "Projects"]
    )
    assert (await run(registry, "organize_desktop")).ok


# --- settings ---


async def test_volume_brightness_and_wallpaper_are_undoable(home: Path) -> None:
    system = FakeSystem()
    registry = tools(home, system=system)
    set_to = await run(registry, "set_volume", percent=150)
    assert set_to.summary == "Set the volume to 100% (it was 40%)."
    assert (await run(registry, "set_volume", mute=True)).ok and system.mute
    assert (await run(registry, "set_brightness", percent=30)).ok and system.light == 30
    picture = home / "Pictures" / "space.png"
    picture.write_bytes(b"png")
    assert (await run(registry, "set_wallpaper", path="pictures/space.png")).ok
    assert system.paper == str(picture.resolve())
    (home / "Pictures" / "notes.txt").write_text("x")
    assert not (await run(registry, "set_wallpaper", path="pictures/notes.txt")).ok
    for _ in range(4):
        assert (await run(registry, "undo_last")).ok
    assert (system.level, system.mute, system.light) == (40, False, 70)
    assert system.paper.endswith("img0.jpg")
    reading = await run(registry, "get_volume_and_brightness")
    assert reading.summary == "Volume 40%, brightness 70%."


async def test_a_screen_without_brightness_control(home: Path) -> None:
    system = FakeSystem()
    system.no_brightness = True
    failed = await run(tools(home, system=system), "set_brightness", percent=50)
    assert not failed.ok and "can't be set" in failed.summary


# --- apps ---


def seeded(*apps: InstalledApp) -> AppIndex:
    index = AppIndex([], apps_folder=False)
    index.refresh()
    index._apps = list(apps)
    return index


SPOTIFY = Window(1, "Spotify Premium", "Spotify.exe", 100)
WORD = Window(2, "Report.docx - Word", "WINWORD.EXE", 101)
NOTEPAD = Window(3, "notes.txt - Notepad", "Notepad.exe", 102)


@pytest.mark.parametrize(
    ("name", "window", "expected"),
    [
        ("spotify", SPOTIFY, True),
        ("Spotify", Window(1, "Song - Artist", "Spotify.exe", 1), True),
        ("word", WORD, True),
        ("notepad", NOTEPAD, True),
        ("note", NOTEPAD, True),
        ("chrome", NOTEPAD, False),
        ("", NOTEPAD, False),
    ],
)
def test_window_matching(name: str, window: Window, expected: bool) -> None:
    assert matches(name, window) is expected


def test_jarvis_and_the_shell_are_never_closed() -> None:
    assert not closable(Window(9, "JARVIS", "python.exe", 42), own_pid=42)
    assert not closable(Window(8, "Program Manager", "explorer.exe", 7), own_pid=42)
    assert closable(SPOTIFY, own_pid=42)


async def test_close_app_checks_it_closed(home: Path) -> None:
    windows = FakeWindows(SPOTIFY, WORD)
    registry = tools(home, windows=windows)
    assert (await run(registry, "close_app", name="spotify")).summary == "Closed spotify."
    windows.stubborn.add(WORD.handle)
    stuck = await run(registry, "close_app", name="word")
    assert not stuck.ok and "unsaved work" in stuck.summary
    missing = await run(registry, "close_app", name="discord")
    assert not missing.ok


async def test_open_app_raises_a_running_one(home: Path) -> None:
    windows = FakeWindows(SPOTIFY)
    desktop = FakeDesktop()
    installed = seeded(InstalledApp("Spotify", app_id="Spotify.App"))
    registry = tools(home, windows=windows, installed=installed, desktop=desktop)
    result = await run(registry, "open_app", name="spotify")
    assert result.summary == "spotify was already open; brought it to the front."
    assert windows.raised == ["Spotify Premium"] and desktop.calls == []


async def test_open_app_reports_what_it_saw(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    real_sleep = asyncio.sleep
    monkeypatch.setattr(asyncio, "sleep", lambda _s: real_sleep(0))
    windows = FakeWindows()
    desktop = FakeDesktop()
    installed = seeded(InstalledApp("Discord", app_id="Discord.App"))
    registry = tools(home, windows=windows, installed=installed, desktop=desktop)
    unseen = await run(registry, "open_app", name="discord")
    assert unseen.ok and "hasn't appeared yet" in unseen.summary

    original_launch = desktop.open_installed

    def launch(app: object) -> None:
        original_launch(app)
        windows.open.append(Window(5, "Discord", "Discord.exe", 200))

    desktop.open_installed = launch  # type: ignore[method-assign]
    windows.open.clear()
    seen = await run(registry, "open_app", name="discord")
    assert seen.summary == "Opened Discord; its window is up."
