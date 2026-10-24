"""YouTube, flight searches, Steam games and the webcam.

YouTube and Google Flights open in the browser (both are trusted sites). Steam games
start through Steam's own ``steam://`` links. The webcam takes one photo, only after
the user agrees, and a vision model describes it; the photo is never saved.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import quote, urlencode

import httpx

from jarvis.tools.desktop import Desktop
from jarvis.tools.registry import Risk, ToolRegistry, ToolResult, ToolSpec
from jarvis.tools.steam import SteamGame, find_game

_VIDEO = re.compile(r'"videoRenderer":\{"videoId":"(?P<id>[\w-]{11})"')
_TITLE = re.compile(r'"title":\{"runs":\[\{"text":"(?P<title>(?:[^"\\]|\\.)*)"')
_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)", "Accept-Language": "en"}

CameraFn = Callable[[], Awaitable[bytes | None]]
DescribeFn = Callable[[bytes, str], Awaitable[str]]


def _schema(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": required or []}


def first_video(page: str) -> tuple[str, str] | None:
    """(video id, title) of the first proper video on a YouTube results page."""
    found = _VIDEO.search(page)
    if found is None:
        return None
    title = _TITLE.search(page, found.end())
    try:
        name = json.loads(f'"{title.group("title")}"') if title else ""
    except ValueError:
        name = ""
    return found.group("id"), name


def flights_url(origin: str, destination: str, date: str = "", return_date: str = "") -> str:
    query = f"Flights to {destination} from {origin}"
    if date:
        query += f" on {date}"
    query += f" returning {return_date}" if return_date else (" one way" if date else "")
    return "https://www.google.com/travel/flights?q=" + quote(query)


def register_media_tools(
    registry: ToolRegistry,
    desktop: Desktop,
    http: httpx.AsyncClient | None,
    steam_games: Callable[[], list[SteamGame]] | None,
    camera: CameraFn | None,
    describe_image: DescribeFn | None,
) -> None:
    def open_page(url: str, done: str) -> ToolResult:
        try:
            desktop.open_url(url)
        except Exception as exc:
            return ToolResult(False, f"Couldn't open the browser ({type(exc).__name__}).")
        return ToolResult(True, done)

    async def play_youtube(query: str) -> ToolResult:
        search = "https://www.youtube.com/results?" + urlencode({"search_query": query})
        video = None
        if http is not None:
            try:
                page = await http.get(search, headers=_UA, timeout=10.0, follow_redirects=True)
                video = first_video(page.text) if page.status_code == 200 else None
            except httpx.HTTPError:
                video = None
        if video is None:
            return open_page(search, f"Opened YouTube's results for {query}; pick one to play.")
        video_id, title = video
        shown = f"“{title}”" if title else "the top result"
        return open_page(
            f"https://www.youtube.com/watch?v={video_id}", f"Playing {shown} on YouTube."
        )

    def find_flights(
        origin: str, destination: str, date: str = "", return_date: str = ""
    ) -> ToolResult:
        url = flights_url(origin, destination, date, return_date)
        return open_page(
            url,
            f"Opened Google Flights for {origin} to {destination}. The prices are on the page; "
            "JARVIS doesn't read or book them.",
        )

    registry.register(
        ToolSpec(
            "play_youtube",
            "Find a video or song on YouTube and play the top result in the browser.",
            _schema({"query": {"type": "string", "maxLength": 200}}, ["query"]),
            play_youtube,
            Risk.LOW,
            lambda a: f"Play {a.get('query')} on YouTube",
            group="media",
        )
    )
    registry.register(
        ToolSpec(
            "find_flights",
            "Open Google Flights for a route (cities or airport codes; dates as the user said "
            "them, e.g. '12 March'). Leave return_date empty for one way.",
            _schema(
                {
                    "origin": {"type": "string", "maxLength": 80},
                    "destination": {"type": "string", "maxLength": 80},
                    "date": {"type": "string", "maxLength": 40},
                    "return_date": {"type": "string", "maxLength": 40},
                },
                ["origin", "destination"],
            ),
            find_flights,
            Risk.LOW,
            lambda a: f"Look up flights from {a.get('origin')} to {a.get('destination')}",
            group="media",
        )
    )
    if steam_games is not None:
        _steam_tools(registry, desktop, steam_games)
    if camera is not None and describe_image is not None:
        _camera_tool(registry, camera, describe_image)


def _steam_tools(
    registry: ToolRegistry, desktop: Desktop, steam_games: Callable[[], list[SteamGame]]
) -> None:
    async def list_steam_games() -> ToolResult:
        games = await asyncio.to_thread(steam_games)
        if not games:
            return ToolResult(False, "No Steam games found on this PC.")
        waiting = [g.name for g in games if g.needs_update]
        text = f"{len(games)} Steam games: " + ", ".join(g.name for g in games[:60]) + "."
        if waiting:
            text += " Waiting for an update or download: " + ", ".join(waiting) + "."
        return ToolResult(True, text)

    async def play_steam_game(name: str) -> ToolResult:
        game = find_game(name, await asyncio.to_thread(steam_games))
        if game is None:
            return ToolResult(False, f"No installed Steam game called {name}.")
        try:
            desktop.launch(f"steam://rungameid/{game.app_id}")
        except Exception as exc:
            return ToolResult(False, f"Steam didn't respond ({type(exc).__name__}).")
        note = " Steam may update it first." if game.needs_update else ""
        return ToolResult(True, f"Asked Steam to start {game.name}.{note}")

    def steam_downloads() -> ToolResult:
        try:
            desktop.launch("steam://open/downloads")
        except Exception as exc:
            return ToolResult(False, f"Steam didn't respond ({type(exc).__name__}).")
        return ToolResult(True, "Opened Steam's downloads, where updates are queued and run.")

    registry.register(
        ToolSpec(
            "list_steam_games",
            "List the Steam games installed on this PC, and which are waiting for updates.",
            _schema({}),
            list_steam_games,
            Risk.LOW,
            lambda a: "List your Steam games",
            group="media",
        )
    )
    registry.register(
        ToolSpec(
            "play_steam_game",
            "Start an installed Steam game by name, through Steam.",
            _schema({"name": {"type": "string", "maxLength": 100}}, ["name"]),
            play_steam_game,
            Risk.LOW,
            lambda a: f"Start {a.get('name')} on Steam",
            group="media",
        )
    )
    registry.register(
        ToolSpec(
            "steam_downloads",
            "Open Steam's downloads page to update games.",
            _schema({}),
            steam_downloads,
            Risk.LOW,
            lambda a: "Open Steam's downloads",
            group="media",
        )
    )


def _camera_tool(registry: ToolRegistry, camera: CameraFn, describe_image: DescribeFn) -> None:
    async def look_through_webcam(question: str = "") -> ToolResult:
        try:
            photo = await camera()
        except Exception as exc:
            return ToolResult(False, f"The webcam couldn't take a photo ({exc}).")
        if photo is None:
            return ToolResult(False, "No webcam found, or Windows didn't allow access to it.")
        ask = question.strip() or "Describe what you see."
        return ToolResult(True, await describe_image(photo, ask))

    registry.register(
        ToolSpec(
            "look_through_webcam",
            "Take one photo with the webcam and describe it or answer a question about it "
            "(e.g. 'how do I look?', 'what am I holding?'). Asks the user first.",
            _schema({"question": {"type": "string", "maxLength": 300}}),
            look_through_webcam,
            Risk.MEDIUM,
            lambda a: "Take one photo with your webcam and have the AI look at it (not saved)",
            group="camera",
        )
    )
