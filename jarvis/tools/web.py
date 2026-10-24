"""Reading the web for answers: weather (Open-Meteo, no key), news headlines (Google News
RSS) and search results (DuckDuckGo's HTML page). Everything returned is outside text:
the assistant screens it with the prompt-injection guard and treats it as information.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from urllib.parse import parse_qs, unquote, urlparse
from xml.etree import ElementTree

import httpx

_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) JARVIS/1.0"}
_TIMEOUT = 12.0

# WMO weather interpretation codes (open-meteo.com/en/docs).
_WEATHER = {
    0: "clear sky",
    1: "mainly clear",
    2: "partly cloudy",
    3: "overcast",
    45: "fog",
    48: "freezing fog",
    51: "light drizzle",
    53: "drizzle",
    55: "heavy drizzle",
    56: "freezing drizzle",
    57: "freezing drizzle",
    61: "light rain",
    63: "rain",
    65: "heavy rain",
    66: "freezing rain",
    67: "freezing rain",
    71: "light snow",
    73: "snow",
    75: "heavy snow",
    77: "snow grains",
    80: "light showers",
    81: "showers",
    82: "violent showers",
    85: "snow showers",
    86: "heavy snow showers",
    95: "thunderstorm",
    96: "thunderstorm with hail",
    99: "thunderstorm with heavy hail",
}


class WebError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class Headline:
    title: str
    source: str
    published: str
    link: str


@dataclass(frozen=True, slots=True)
class SearchHit:
    title: str
    url: str
    snippet: str


def describe_code(code: int) -> str:
    return _WEATHER.get(code, "mixed weather")


async def get(client: httpx.AsyncClient, url: str, **params: str | int) -> httpx.Response:
    try:
        response = await client.get(url, params=params or None, headers=_UA, timeout=_TIMEOUT)
    except httpx.TimeoutException as exc:
        raise WebError("the site didn't answer in time") from exc
    except httpx.HTTPError as exc:
        raise WebError("no connection to the site") from exc
    if response.status_code >= 400:
        raise WebError(f"the site answered with error {response.status_code}")
    return response


@dataclass(frozen=True, slots=True)
class DayForecast:
    label: str  # Today, Tomorrow, The day after
    sky: str
    low: int
    high: int
    rain: int | None  # chance of rain, %


@dataclass(frozen=True, slots=True)
class Forecast:
    place: str  # "Kochi, India"
    sky: str
    temperature: int
    feels_like: int
    humidity: int
    wind: int  # km/h
    days: tuple[DayForecast, ...]

    def text(self) -> str:
        line = (
            f"{self.place}: {self.sky}, {self.temperature}°C (feels like {self.feels_like}°C), "
            f"humidity {self.humidity}%, wind {self.wind} km/h."
        )
        for day in self.days:
            chance = f", {day.rain}% chance of rain" if day.rain is not None else ""
            line += f" {day.label}: {day.sky}, {day.low} to {day.high}°C{chance}."
        return line


async def locate(client: httpx.AsyncClient, place: str) -> tuple[str, float, float]:
    """("Kochi, India", latitude, longitude) for a place name (Open-Meteo geocoding)."""
    found = await get(
        client,
        "https://geocoding-api.open-meteo.com/v1/search",
        name=place,
        count=1,
        language="en",
        format="json",
    )
    results = found.json().get("results") or []
    if not results:
        raise WebError(f"I couldn't find a place called {place}")
    spot = results[0]
    name = ", ".join(str(p) for p in (spot.get("name"), spot.get("country")) if p)
    return name, float(spot["latitude"]), float(spot["longitude"])


async def forecast(client: httpx.AsyncClient, place: str) -> Forecast:
    """Current weather and the next two days for a place."""
    name, latitude, longitude = await locate(client, place)
    response = await get(
        client,
        "https://api.open-meteo.com/v1/forecast",
        latitude=str(latitude),
        longitude=str(longitude),
        current="temperature_2m,apparent_temperature,relative_humidity_2m,weather_code,"
        "wind_speed_10m",
        daily="weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max",
        timezone="auto",
        forecast_days=3,
    )
    data = response.json()
    now = data.get("current") or {}
    daily = data.get("daily") or {}
    days = []
    for i, label in enumerate(["Today", "Tomorrow", "The day after"]):
        try:
            high = round(daily["temperature_2m_max"][i])
            low = round(daily["temperature_2m_min"][i])
            code = int(daily["weather_code"][i])
            rain = daily.get("precipitation_probability_max", [None] * 3)[i]
        except (KeyError, IndexError, TypeError):
            break
        days.append(DayForecast(label, describe_code(code), low, high, rain))
    return Forecast(
        name,
        describe_code(int(now.get("weather_code", -1))),
        round(now.get("temperature_2m", 0)),
        round(now.get("apparent_temperature", 0)),
        round(now.get("relative_humidity_2m", 0)),
        round(now.get("wind_speed_10m", 0)),
        tuple(days),
    )


async def weather(client: httpx.AsyncClient, place: str) -> str:
    """Current weather and the next two days for a place, as one short paragraph."""
    return (await forecast(client, place)).text()


async def news(
    client: httpx.AsyncClient, topic: str, locale: str = "en-US", limit: int = 6
) -> list[Headline]:
    """Latest headlines on a topic (or top stories when ``topic`` is empty)."""
    language, _, country = locale.partition("-")
    country = country or "US"
    params: dict[str, str | int] = {"hl": locale, "gl": country, "ceid": f"{country}:{language}"}
    url = "https://news.google.com/rss"
    if topic.strip():
        url += "/search"
        params["q"] = topic.strip()  # (httpx replaces a query written into the url)
    response = await get(client, url, **params)
    try:
        root = ElementTree.fromstring(response.content)
    except ElementTree.ParseError as exc:
        raise WebError("the news feed couldn't be read") from exc
    headlines = []
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        source = (item.findtext("source") or "").strip()
        if source and title.endswith(f" - {source}"):
            title = title[: -len(source) - 3]
        headlines.append(
            Headline(
                title,
                source,
                (item.findtext("pubDate") or "").strip(),
                (item.findtext("link") or "").strip(),
            )
        )
        if len(headlines) >= limit:
            break
    return headlines


_RESULT = re.compile(
    r'<a[^>]+class="result__a"[^>]+href="(?P<url>[^"]+)"[^>]*>(?P<title>.*?)</a>'
    r'(?:.*?<a[^>]+class="result__snippet"[^>]*>(?P<snippet>.*?)</a>)?',
    re.DOTALL,
)
_TAGS = re.compile(r"<[^>]+>")


def _text(fragment: str) -> str:
    return " ".join(html.unescape(_TAGS.sub("", fragment or "")).split())


def _real_url(href: str) -> str:
    """DuckDuckGo wraps results as /l/?uddg=<url>; unwrap them."""
    href = html.unescape(href)
    if href.startswith("//"):
        href = "https:" + href
    parsed = urlparse(href)
    if parsed.path.startswith("/l/"):
        target = parse_qs(parsed.query).get("uddg", [""])[0]
        return unquote(target) or href
    return href


def parse_results(page: str, limit: int = 6) -> list[SearchHit]:
    hits = []
    for match in _RESULT.finditer(page):
        url = _real_url(match.group("url"))
        if "duckduckgo.com/y.js" in url:  # ads
            continue
        hits.append(SearchHit(_text(match.group("title")), url, _text(match.group("snippet"))))
        if len(hits) >= limit:
            break
    return hits


async def search(client: httpx.AsyncClient, query: str, limit: int = 6) -> list[SearchHit]:
    response = await get(client, "https://html.duckduckgo.com/html/", q=query)
    hits = parse_results(response.text, limit)
    if not hits:
        raise WebError("the search found nothing (or the search page changed)")
    return hits
