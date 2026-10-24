"""YouTube, flights, Steam games, the webcam, start with Windows, and the tool groups."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from jarvis.agent.toolbudget import groups_for
from jarvis.app import autostart
from jarvis.app.config import ToolSettings
from jarvis.tools.apps import AppIndex
from jarvis.tools.builtin import build_registry
from jarvis.tools.media import CameraFn, DescribeFn, first_video, flights_url
from jarvis.tools.registry import Risk, ToolRegistry
from jarvis.tools.steam import SteamGame, find_game, installed_games
from tests.fakes import FakeDesktop
from tests.test_tools import risk, run

RESULTS_PAGE = (
    'var ytInitialData = {"contents":[{"adSlotRenderer":{}},'
    '{"videoRenderer":{"videoId":"dQw4w9WgXcQ","thumbnail":{},'
    '"title":{"runs":[{"text":"Lo-fi beats \\u0026 rain"}]}}},'
    '{"videoRenderer":{"videoId":"aaaaaaaaaaa","title":{"runs":[{"text":"Other"}]}}}]};'
)


def tools(
    desktop: FakeDesktop,
    page: str | None = RESULTS_PAGE,
    games: list[SteamGame] | None = None,
    camera: CameraFn | None = None,
    describe: DescribeFn | None = None,
) -> ToolRegistry:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["search_query"] == "lofi rain"
        return httpx.Response(200, text=page) if page else httpx.Response(503)

    return build_registry(
        ToolSettings(),
        desktop,
        AppIndex([], apps_folder=False),
        http=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        steam_games=(lambda: games) if games is not None else None,
        camera=camera,
        describe_image=describe,
    )


def test_first_video_on_a_results_page() -> None:
    assert first_video(RESULTS_PAGE) == ("dQw4w9WgXcQ", "Lo-fi beats & rain")
    assert first_video("<html>consent</html>") is None


async def test_youtube_plays_the_top_result_or_shows_results() -> None:
    desktop = FakeDesktop()
    played = await run(tools(desktop), "play_youtube", query="lofi rain")
    assert played.summary == "Playing “Lo-fi beats & rain” on YouTube."
    assert desktop.calls == [("open_url", "https://www.youtube.com/watch?v=dQw4w9WgXcQ")]
    desktop.calls.clear()
    fallback = await run(tools(desktop, page=None), "play_youtube", query="lofi rain")
    assert fallback.ok and "results" in fallback.summary
    assert desktop.calls[0][1] == "https://www.youtube.com/results?search_query=lofi+rain"


async def test_flights_open_google_flights() -> None:
    assert flights_url("Kochi", "Dubai", "12 March") == (
        "https://www.google.com/travel/flights?q=Flights%20to%20Dubai%20from%20Kochi"
        "%20on%2012%20March%20one%20way"
    )
    assert flights_url("COK", "LHR", "1 May", "9 May").endswith("returning%209%20May")
    desktop = FakeDesktop()
    found = await run(tools(desktop), "find_flights", origin="Kochi", destination="Dubai")
    assert found.ok and risk(tools(desktop), "find_flights") is Risk.LOW
    assert "doesn't read or book" in found.summary


def write_manifest(library: Path, app_id: int, name: str, flags: int = 4) -> None:
    apps = library / "steamapps"
    apps.mkdir(parents=True, exist_ok=True)
    (apps / f"appmanifest_{app_id}.acf").write_text(
        f'"AppState"\n{{\n\t"appid"\t\t"{app_id}"\n\t"name"\t\t"{name}"\n'
        f'\t"StateFlags"\t\t"{flags}"\n}}\n'
    )


def test_steam_library_is_read(tmp_path: Path) -> None:
    steam = tmp_path / "Steam"
    other = tmp_path / "Games"
    write_manifest(steam, 730, "Counter-Strike 2")
    write_manifest(other, 1091500, "Cyberpunk 2077", flags=6)
    write_manifest(other, 228980, "Steamworks Common Redistributables")
    vdf = steam / "steamapps" / "libraryfolders.vdf"
    vdf.write_text(
        '"libraryfolders"\n{\n\t"0"\n\t{\n\t\t"path"\t\t"'
        + str(steam).replace("\\", "\\\\")
        + '"\n\t}\n\t"1"\n\t{\n\t\t"path"\t\t"'
        + str(other).replace("\\", "\\\\")
        + '"\n\t}\n}\n'
    )
    games = installed_games(steam)
    assert games == [
        SteamGame(730, "Counter-Strike 2", False),
        SteamGame(1091500, "Cyberpunk 2077", True),
    ]
    assert find_game("cyberpunk", games) == games[1]
    assert find_game("counter strike 2", games) == games[0]
    assert find_game("valorant", games) is None
    assert installed_games(None) == []


async def test_steam_tools() -> None:
    desktop = FakeDesktop()
    games = [SteamGame(730, "Counter-Strike 2", False), SteamGame(1, "Cyberpunk 2077", True)]
    registry = tools(desktop, games=games)
    listed = await run(registry, "list_steam_games")
    assert "Waiting for an update or download: Cyberpunk 2077." in listed.summary
    started = await run(registry, "play_steam_game", name="cyberpunk")
    assert started.summary == "Asked Steam to start Cyberpunk 2077. Steam may update it first."
    assert desktop.calls == [("launch", "steam://rungameid/1")]
    assert not (await run(registry, "play_steam_game", name="halo")).ok
    assert (await run(registry, "steam_downloads")).ok
    assert tools(FakeDesktop()).get("play_steam_game") is None  # no Steam: no tools


async def test_webcam_asks_first_and_describes() -> None:
    photos: list[bytes] = []

    async def camera() -> bytes | None:
        return b"jpeg"

    async def describe(photo: bytes, question: str) -> str:
        photos.append(photo)
        return f"Looked: {question}"

    registry = tools(FakeDesktop(), camera=camera, describe=describe)
    assert risk(registry, "look_through_webcam") is Risk.MEDIUM
    seen = await run(registry, "look_through_webcam", question="how do I look?")
    assert seen.summary == "Looked: how do I look?" and photos == [b"jpeg"]

    async def no_camera() -> bytes | None:
        return None

    missing = await run(tools(FakeDesktop(), camera=no_camera, describe=describe),
                        "look_through_webcam")  # fmt: skip
    assert not missing.ok and "No webcam" in missing.summary


@pytest.mark.parametrize(
    ("text", "group"),
    [
        ("play some lofi on youtube", "media"),
        ("find flights from kochi to dubai", "media"),
        ("start cyberpunk on steam", "media"),
        ("how do I look today?", "camera"),
        ("what am I holding", "camera"),
    ],
)
def test_new_groups_are_offered(text: str, group: str) -> None:
    assert group in groups_for(text)


def test_autostart_uses_the_startup_folder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APPDATA", str(tmp_path))
    path = autostart.shortcut_path()
    assert path is not None and path.parent.name == "Startup" and not autostart.enabled()
    python = tmp_path / "python.exe"
    python.write_text("")
    assert autostart.windowless_python(str(python)) == str(python)
    (tmp_path / "pythonw.exe").write_text("")
    assert autostart.windowless_python(str(python)) == str(tmp_path / "pythonw.exe")
    with pytest.raises(autostart.AutostartError):
        autostart.set_enabled(True)  # not Windows here
