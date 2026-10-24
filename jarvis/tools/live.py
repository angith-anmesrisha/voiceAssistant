"""Live information from free, keyless public APIs (picked from the public-apis list, MIT).

Finance: stock and index prices (Yahoo Finance chart data), crypto (CoinGecko, then the
fawazahmed0 currency API), gold and silver (fawazahmed0: international spot price per
troy ounce, converted per gram; Indian retail prices add duty and GST), currency
conversion (Frankfurter, the European Central Bank's rates, then fawazahmed0) and Indian
mutual fund NAVs (mfapi.in). Information: Wikipedia summaries, dictionary definitions
(dictionaryapi.dev), air quality (Open-Meteo), earthquakes (USGS), public holidays
(Nager.Date) and jokes (JokeAPI, safe mode).

Everything here is read-only and needs no key. Prices are as the source last reported
them (stock exchanges' data is often delayed by 15 minutes or so); replies say the source.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

import httpx

from jarvis.storage.keep import Keep
from jarvis.tools.registry import Risk, ToolRegistry, ToolResult, ToolSpec
from jarvis.tools.web import WebError, get, locate

TROY_OUNCE_G = 31.1034768

# Spoken names → Yahoo Finance symbols.
INDICES = {
    "nifty": "^NSEI",
    "nifty 50": "^NSEI",
    "nifty fifty": "^NSEI",
    "sensex": "^BSESN",
    "bank nifty": "^NSEBANK",
    "nifty bank": "^NSEBANK",
    "nifty it": "^CNXIT",
    "dow": "^DJI",
    "dow jones": "^DJI",
    "nasdaq": "^IXIC",
    "s&p": "^GSPC",
    "s&p 500": "^GSPC",
    "s and p 500": "^GSPC",
    "ftse": "^FTSE",
    "ftse 100": "^FTSE",
    "nikkei": "^N225",
    "dax": "^GDAXI",
    "hang seng": "^HSI",
}

# Spoken names → (CoinGecko id, ticker).
COINS = {
    "bitcoin": ("bitcoin", "btc"),
    "btc": ("bitcoin", "btc"),
    "ethereum": ("ethereum", "eth"),
    "ether": ("ethereum", "eth"),
    "eth": ("ethereum", "eth"),
    "solana": ("solana", "sol"),
    "sol": ("solana", "sol"),
    "dogecoin": ("dogecoin", "doge"),
    "doge": ("dogecoin", "doge"),
    "xrp": ("ripple", "xrp"),
    "ripple": ("ripple", "xrp"),
    "cardano": ("cardano", "ada"),
    "bnb": ("binancecoin", "bnb"),
    "litecoin": ("litecoin", "ltc"),
    "tether": ("tether", "usdt"),
    "usdt": ("tether", "usdt"),
    "polkadot": ("polkadot", "dot"),
    "toncoin": ("the-open-network", "ton"),
}

METALS = {"gold": "xau", "silver": "xag", "platinum": "xpt", "palladium": "xpd"}

CURRENCY_WORDS = {
    "rupee": "INR",
    "rupees": "INR",
    "dollar": "USD",
    "dollars": "USD",
    "euro": "EUR",
    "euros": "EUR",
    "pound": "GBP",
    "pounds": "GBP",
    "sterling": "GBP",
    "yen": "JPY",
    "dirham": "AED",
    "dirhams": "AED",
    "riyal": "SAR",
    "riyals": "SAR",
    "yuan": "CNY",
    "franc": "CHF",
    "francs": "CHF",
}

_COUNTRY_CURRENCY = {
    "IN": "INR",
    "US": "USD",
    "GB": "GBP",
    "AE": "AED",
    "SA": "SAR",
    "AU": "AUD",
    "CA": "CAD",
    "SG": "SGD",
    "JP": "JPY",
    "DE": "EUR",
    "FR": "EUR",
    "IE": "EUR",
}

_SYMBOLS = {"INR": "₹", "USD": "$", "GBP": "£", "EUR": "€", "JPY": "¥"}
_FUND_WORDS = re.compile(r"\b(mutual fund|fund|nav|direct plan|regular plan|growth)\b", re.I)
_TICKER = re.compile(r"^[\^A-Z0-9][A-Z0-9.\-=^]{0,14}$")
_FAWAZ = "https://cdn.jsdelivr.net/npm/@fawazahmed0/currency-api@latest/v1/currencies"


@dataclass(frozen=True, slots=True)
class Quote:
    name: str
    price: float
    currency: str
    change_pct: float | None = None  # since the previous close / over 24 h
    source: str = ""
    unit: str = ""  # "a gram", "per unit"
    when: str = ""

    def text(self) -> str:
        line = f"{self.name}: {money(self.price, self.currency)}"
        if self.unit:
            line += f" {self.unit}"
        if self.change_pct is not None:
            line += f" ({self.change_pct:+.2f}% today)"
        if self.when:
            line += f", as of {self.when}"
        return line + (f" (source: {self.source})" if self.source else "")


def money(amount: float, currency: str) -> str:
    sign = _SYMBOLS.get(currency.upper(), "")
    digits = 2 if abs(amount) < 1000 else 0
    if abs(amount) < 1:
        digits = 4
    figure = f"{amount:,.{digits}f}"
    return f"{sign}{figure}" if sign else f"{figure} {currency.upper()}"


def home_currency(locale: str) -> str:
    """INR for "en-IN", GBP for "en-GB"...; USD otherwise."""
    country = locale.rpartition("-")[2].upper()
    return _COUNTRY_CURRENCY.get(country, "USD")


def currency_code(word: str) -> str:
    word = word.strip()
    return CURRENCY_WORDS.get(word.lower(), word.upper())


def _clean(name: str) -> str:
    key = re.sub(r"\b(price|rate|today|now|current|of|the|share|stock|value)\b", " ", name.lower())
    return " ".join(key.split())


def classify(name: str) -> str:
    """Which source a price question goes to: metal, crypto, index, fund or stock."""
    key = _clean(name)
    if key in METALS or any(m in key.split() for m in METALS):
        return "metal"
    if key in COINS:
        return "crypto"
    if key in INDICES:
        return "index"
    if _FUND_WORDS.search(name):
        return "fund"
    return "stock"


# --- finance ---


async def metal_price(client: httpx.AsyncClient, metal: str, currency: str) -> Quote:
    code = next((c for m, c in METALS.items() if m in metal.lower()), "xau")
    data = (await get(client, f"{_FAWAZ}/{code}.json")).json()
    per_ounce = float((data.get(code) or {}).get(currency.lower()) or 0)
    if per_ounce <= 0:
        raise WebError(f"no {metal} price in {currency}")
    name = next(m for m, c in METALS.items() if c == code).title()
    return Quote(
        f"{name} (international spot)",
        per_ounce / TROY_OUNCE_G,
        currency,
        source="fawazahmed0 currency API",
        unit="a gram",
        when=str(data.get("date", "")),
    )


async def crypto_price(client: httpx.AsyncClient, coin: str, currency: str) -> Quote:
    gecko, ticker = COINS.get(_clean(coin), (_clean(coin), _clean(coin)))
    cur = currency.lower()
    try:
        data = (
            await get(
                client,
                "https://api.coingecko.com/api/v3/simple/price",
                ids=gecko,
                vs_currencies=cur,
                include_24hr_change="true",
            )
        ).json()
        row = data.get(gecko) or {}
        if row.get(cur) is not None:
            change = row.get(f"{cur}_24h_change")
            return Quote(
                gecko.replace("-", " ").title(),
                float(row[cur]),
                currency,
                round(float(change), 2) if change is not None else None,
                "CoinGecko",
            )
    except (WebError, ValueError, TypeError):
        pass
    data = (await get(client, f"{_FAWAZ}/{ticker}.json")).json()
    price = (data.get(ticker) or {}).get(cur)
    if price is None:
        raise WebError(f"I couldn't find a price for {coin}")
    return Quote(
        ticker.upper(),
        float(price),
        currency,
        source="fawazahmed0 currency API",
        when=str(data.get("date", "")),
    )


async def find_symbol(client: httpx.AsyncClient, name: str, region: str = "") -> str:
    """A Yahoo Finance symbol for a company or index name ("Reliance" → RELIANCE.NS)."""
    key = _clean(name)
    if key in INDICES:
        return INDICES[key]
    if _TICKER.match(name.strip()) and name.strip().upper() == name.strip():
        return name.strip()
    data = (
        await get(
            client,
            "https://query2.finance.yahoo.com/v1/finance/search",
            q=key or name,
            quotesCount=6,
            newsCount=0,
        )
    ).json()
    quotes = [
        q
        for q in data.get("quotes") or []
        if isinstance(q, dict)
        and q.get("symbol")
        and q.get("quoteType") in ("EQUITY", "INDEX", "ETF", "MUTUALFUND", None)
    ]
    if not quotes:
        raise WebError(f"I couldn't find a share called {name}")
    if region.upper() == "IN":
        for suffix in (".NS", ".BO"):
            for q in quotes:
                if str(q["symbol"]).endswith(suffix):
                    return str(q["symbol"])
    return str(quotes[0]["symbol"])


async def stock_quote(client: httpx.AsyncClient, name: str, region: str = "") -> Quote:
    symbol = await find_symbol(client, name, region)
    data = (
        await get(
            client,
            f"https://query1.finance.yahoo.com/v8/finance/chart/{quote(symbol, safe='')}",
            range="1d",
            interval="1d",
        )
    ).json()
    try:
        meta = data["chart"]["result"][0]["meta"]
        price = float(meta["regularMarketPrice"])
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise WebError(f"no price for {symbol}") from exc
    previous = meta.get("chartPreviousClose") or meta.get("previousClose")
    change = (price / float(previous) - 1) * 100 if previous else None
    when = ""
    if isinstance(meta.get("regularMarketTime"), int | float):
        stamp = datetime.fromtimestamp(meta["regularMarketTime"], UTC).astimezone()
        when = stamp.strftime("%d %b %H:%M")
    label = str(meta.get("longName") or meta.get("shortName") or symbol)
    return Quote(
        f"{label} ({symbol})",
        price,
        str(meta.get("currency") or ""),
        round(change, 2) if change is not None else None,
        "Yahoo Finance",
        when=when,
    )


async def fund_nav(client: httpx.AsyncClient, name: str) -> Quote:
    """The latest NAV of an Indian mutual fund scheme (AMFI data via mfapi.in)."""
    query = _FUND_WORDS.sub(" ", name).strip() or name
    found = (await get(client, "https://api.mfapi.in/mf/search", q=query)).json()
    if not isinstance(found, list) or not found:
        raise WebError(f"I couldn't find a mutual fund called {name}")
    words = set(re.findall(r"\w+", name.lower()))
    # Prefer direct-plan growth schemes when the name doesn't say otherwise.
    best = max(
        (f for f in found if isinstance(f, dict) and f.get("schemeCode")),
        key=lambda f: (
            len(words & set(re.findall(r"\w+", str(f.get("schemeName", "")).lower()))),
            "direct" in str(f.get("schemeName", "")).lower(),
            "growth" in str(f.get("schemeName", "")).lower(),
        ),
    )
    data = (await get(client, f"https://api.mfapi.in/mf/{best['schemeCode']}/latest")).json()
    try:
        latest = data["data"][0]
        nav = float(latest["nav"])
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise WebError("the fund's NAV wasn't in the reply") from exc
    scheme = str((data.get("meta") or {}).get("scheme_name") or best.get("schemeName"))
    return Quote(scheme, nav, "INR", source="mfapi.in (AMFI)", unit="per unit", when=latest["date"])


async def price_of(client: httpx.AsyncClient, name: str, locale: str = "en-US") -> Quote:
    currency = home_currency(locale)
    region = locale.rpartition("-")[2]
    kind = classify(name)
    if kind == "metal":
        return await metal_price(client, name, currency)
    if kind == "crypto":
        return await crypto_price(client, name, currency)
    if kind == "fund":
        return await fund_nav(client, name)
    return await stock_quote(client, name, region)


async def convert(client: httpx.AsyncClient, amount: float, source: str, target: str) -> str:
    src, dst = currency_code(source), currency_code(target)
    try:
        data = (
            await get(client, "https://api.frankfurter.dev/v1/latest", base=src, symbols=dst)
        ).json()
        rate = float((data.get("rates") or {})[dst])
        origin, day = "Frankfurter (European Central Bank rates)", str(data.get("date", ""))
    except (WebError, KeyError, ValueError, TypeError):
        data = (await get(client, f"{_FAWAZ}/{src.lower()}.json")).json()
        found = (data.get(src.lower()) or {}).get(dst.lower())
        if found is None:
            raise WebError(f"no exchange rate from {src} to {dst}") from None
        rate = float(found)
        origin, day = "fawazahmed0 currency API", str(data.get("date", ""))
    return (
        f"{money(amount, src)} is {money(amount * rate, dst)} "
        f"(1 {src} = {rate:,.4f} {dst}, {day}; source: {origin})."
    )


# --- information ---


async def wiki_summary(client: httpx.AsyncClient, topic: str, language: str = "en") -> str:
    data = (
        await get(
            client,
            f"https://{language}.wikipedia.org/w/api.php",
            action="query",
            generator="search",
            gsrsearch=topic,
            gsrlimit=1,
            prop="extracts",
            exintro=1,
            explaintext=1,
            redirects=1,
            format="json",
        )
    ).json()
    pages = ((data.get("query") or {}).get("pages") or {}).values()
    page = next(iter(pages), None)
    if not isinstance(page, dict) or not str(page.get("extract", "")).strip():
        raise WebError(f"Wikipedia has no article on {topic}")
    extract = " ".join(str(page["extract"]).split())
    if len(extract) > 1500:
        extract = extract[:1500].rsplit(". ", 1)[0] + "."
    return f"{page.get('title', topic)} (Wikipedia): {extract}"


async def define(client: httpx.AsyncClient, word: str) -> str:
    try:
        response = await get(
            client, f"https://api.dictionaryapi.dev/api/v2/entries/en/{quote(word.strip())}"
        )
    except WebError as exc:
        if "404" in str(exc):
            raise WebError(f"the dictionary doesn't have “{word}”") from exc
        raise
    entries = response.json()
    if not isinstance(entries, list) or not entries:
        raise WebError(f"the dictionary doesn't have “{word}”")
    entry = entries[0]
    lines = [f"{entry.get('word', word)} {entry.get('phonetic', '')}".strip() + ":"]
    for meaning in (entry.get("meanings") or [])[:3]:
        definitions = meaning.get("definitions") or []
        if not definitions:
            continue
        first = definitions[0]
        line = f"({meaning.get('partOfSpeech', '')}) {first.get('definition', '')}"
        if first.get("example"):
            line += f" e.g. “{first['example']}”"
        lines.append(line)
    return " ".join(lines) + " (source: dictionaryapi.dev)"


def _aqi_band(aqi: float) -> str:
    for limit, band in (
        (50, "good"),
        (100, "moderate"),
        (150, "unhealthy for sensitive groups"),
        (200, "unhealthy"),
        (300, "very unhealthy"),
    ):
        if aqi <= limit:
            return band
    return "hazardous"


async def air_quality(client: httpx.AsyncClient, place: str) -> str:
    name, latitude, longitude = await locate(client, place)
    data = (
        await get(
            client,
            "https://air-quality-api.open-meteo.com/v1/air-quality",
            latitude=str(latitude),
            longitude=str(longitude),
            current="us_aqi,pm2_5,pm10",
        )
    ).json()
    now = data.get("current") or {}
    aqi = now.get("us_aqi")
    if aqi is None:
        raise WebError(f"no air quality reading for {name}")
    return (
        f"Air quality in {name}: AQI {round(aqi)} ({_aqi_band(float(aqi))}), "
        f"PM2.5 {now.get('pm2_5', '?')} µg/m³, PM10 {now.get('pm10', '?')} µg/m³ "
        "(US AQI scale; source: Open-Meteo)."
    )


async def earthquakes(client: httpx.AsyncClient, limit: int = 5) -> list[str]:
    """Magnitude 4.5+ earthquakes in the last day, strongest first (USGS)."""
    data = (
        await get(
            client, "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/4.5_day.geojson"
        )
    ).json()
    quakes = []
    for feature in data.get("features") or []:
        props = feature.get("properties") or {}
        if props.get("mag") is None:
            continue
        when = datetime.fromtimestamp(float(props.get("time", 0)) / 1000, UTC).astimezone()
        quakes.append((float(props["mag"]), str(props.get("place", "")), when))
    quakes.sort(reverse=True)
    return [f"M{mag:.1f} {place}, {when:%d %b %H:%M}" for mag, place, when in quakes[:limit]]


async def holidays(client: httpx.AsyncClient, country: str, limit: int = 5) -> list[str]:
    code = country.strip().upper()[:2] or "IN"
    data = (
        await get(client, f"https://date.nager.at/api/v3/NextPublicHolidays/{quote(code)}")
    ).json()
    if not isinstance(data, list):
        raise WebError(f"no holiday list for {code}")
    out = []
    for item in data[:limit]:
        try:
            day = datetime.fromisoformat(str(item["date"])).strftime("%a %d %b %Y")
        except (KeyError, ValueError):
            continue
        local, english = str(item.get("localName", "")), str(item.get("name", ""))
        out.append(f"{day}: {english}" + (f" ({local})" if local and local != english else ""))
    return out


async def joke(client: httpx.AsyncClient) -> str:
    data = (
        await get(
            client,
            "https://v2.jokeapi.dev/joke/Programming,Misc,Pun?safe-mode"
            "&blacklistFlags=nsfw,religious,political,racist,sexist,explicit",
        )
    ).json()
    if data.get("error"):
        raise WebError("the joke service is having a bad day")
    if data.get("type") == "twopart":
        return f"{data.get('setup', '')} … {data.get('delivery', '')}"
    return str(data.get("joke", ""))


# --- tools ---

WATCH_KEY = "market_watch"


async def watched(keep: Keep) -> dict[str, dict[str, float | None]]:
    """{name: {"move": percent, "last": price told last}} from the keep's state."""
    try:
        data = json.loads(await keep.get_state(WATCH_KEY) or "{}")
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


async def save_watched(keep: Keep, items: dict[str, dict[str, float | None]]) -> None:
    await keep.set_state(WATCH_KEY, json.dumps(items))


def register_live_tools(
    registry: ToolRegistry,
    http: httpx.AsyncClient,
    keep: Keep | None,
    *,
    locale: str = "en-US",
    home_place: str = "",
) -> None:
    async def get_price(name: str) -> ToolResult:
        try:
            found = await price_of(http, name, locale)
        except WebError as exc:
            return ToolResult(False, f"Couldn't get the price: {exc}.")
        return ToolResult(True, found.text())

    async def convert_currency(
        amount: float, from_currency: str, to_currency: str = ""
    ) -> ToolResult:
        try:
            text = await convert(http, amount, from_currency, to_currency or home_currency(locale))
        except WebError as exc:
            return ToolResult(False, f"Couldn't convert: {exc}.")
        return ToolResult(True, text)

    async def wiki(topic: str) -> ToolResult:
        try:
            return ToolResult(True, await wiki_summary(http, topic))
        except WebError as exc:
            return ToolResult(False, f"Couldn't read Wikipedia: {exc}.")

    async def define_word(word: str) -> ToolResult:
        try:
            return ToolResult(True, await define(http, word))
        except WebError as exc:
            return ToolResult(False, f"Couldn't look it up: {exc}.")

    async def get_air_quality(place: str = "") -> ToolResult:
        from jarvis.tools.personal import home_city

        where = place.strip() or await home_city(keep, home_place)
        if not where:
            return ToolResult(False, "Which city? (Tell me once and I'll remember it.)")
        try:
            return ToolResult(True, await air_quality(http, where))
        except WebError as exc:
            return ToolResult(False, f"Couldn't get the air quality: {exc}.")

    async def recent_earthquakes() -> ToolResult:
        try:
            found = await earthquakes(http)
        except WebError as exc:
            return ToolResult(False, f"Couldn't reach USGS: {exc}.")
        if not found:
            return ToolResult(True, "No earthquakes of magnitude 4.5 or more in the last day.")
        return ToolResult(
            True, f"{len(found)} strongest in the last day (USGS).", {"quakes": found}
        )

    async def public_holidays(country: str = "") -> ToolResult:
        code = country.strip() or locale.rpartition("-")[2] or "IN"
        try:
            found = await holidays(http, code)
        except WebError as exc:
            return ToolResult(False, f"Couldn't get the holidays: {exc}.")
        return ToolResult(True, f"Next public holidays ({code.upper()}).", {"holidays": found})

    async def tell_joke() -> ToolResult:
        try:
            return ToolResult(True, await joke(http))
        except WebError as exc:
            return ToolResult(False, f"No joke today: {exc}.")

    def spec(
        name: str,
        text: str,
        props: dict[str, Any],
        required: list[str],
        func: Any,
        what: Callable[[dict[str, Any]], str],
        group: str,
    ) -> ToolSpec:
        schema = {"type": "object", "properties": props, "required": required}
        return ToolSpec(name, text, schema, func, Risk.LOW, what, group=group)

    text = {"type": "string", "maxLength": 120}
    registry.register(
        spec(
            "get_price",
            "Live price of a share, stock index (Nifty, Sensex, Nasdaq...), cryptocurrency, "
            "gold or silver (per gram), or an Indian mutual fund's NAV. Give the name as said.",
            {"name": text},
            ["name"],
            get_price,
            lambda a: f"Check the price of {a.get('name', '')}",
            "finance",
        )
    )
    registry.register(
        spec(
            "convert_currency",
            "Convert money between currencies at today's rate (codes like USD or words like "
            "dollars; empty to = the user's currency).",
            {
                "amount": {"type": "number"},
                "from_currency": {"type": "string", "maxLength": 20},
                "to_currency": {"type": "string", "maxLength": 20},
            },
            ["amount", "from_currency"],
            convert_currency,
            lambda a: f"Convert {a.get('amount', '')} {a.get('from_currency', '')}",
            "finance",
        )
    )
    registry.register(
        spec(
            "wikipedia",
            "Read the opening of the Wikipedia article on a person, place or thing.",
            {"topic": text},
            ["topic"],
            wiki,
            lambda a: f"Read Wikipedia on {a.get('topic', '')}",
            "info",
        )
    )
    registry.register(
        spec(
            "define_word",
            "Dictionary meaning, part of speech and example of an English word.",
            {"word": {"type": "string", "maxLength": 60}},
            ["word"],
            define_word,
            lambda a: f"Define {a.get('word', '')}",
            "info",
        )
    )
    registry.register(
        spec(
            "air_quality",
            "Current air quality (AQI, PM2.5) for a place (empty = the user's home city).",
            {"place": {"type": "string", "maxLength": 100}},
            [],
            get_air_quality,
            lambda a: f"Check the air in {a.get('place') or 'your city'}",
            "info",
        )
    )
    registry.register(
        spec(
            "earthquakes",
            "The strongest earthquakes worldwide in the last day (USGS).",
            {},
            [],
            recent_earthquakes,
            lambda a: "Check recent earthquakes",
            "info",
        )
    )
    registry.register(
        spec(
            "public_holidays",
            "Upcoming public holidays for a country (two-letter code; empty = the user's).",
            {"country": {"type": "string", "maxLength": 2}},
            [],
            public_holidays,
            lambda a: "Check public holidays",
            "info",
        )
    )
    registry.register(
        spec(
            "tell_joke",
            "Fetch a clean joke when the user asks for one.",
            {},
            [],
            tell_joke,
            lambda a: "Find a joke",
            "info",
        )
    )
    if keep is None:
        return

    async def watch_market(name: str, move_percent: float = 2.0, stop: bool = False) -> ToolResult:
        items = await watched(keep)
        key = name.strip().lower()
        if stop:
            if items.pop(key, None) is None:
                return ToolResult(False, f"I wasn't watching {name}.")
            await save_watched(keep, items)
            return ToolResult(True, f"Stopped watching {name}.")
        try:
            found = await price_of(http, name, locale)
        except WebError as exc:
            return ToolResult(False, f"Couldn't find a price for {name}: {exc}.")
        move = max(0.2, min(abs(move_percent or 2.0), 50.0))
        items[key] = {"move": move, "last": found.price}
        await save_watched(keep, items)
        return ToolResult(
            True, f"Watching {name}: now {found.text()}. I'll tell you if it moves {move:g}%."
        )

    registry.register(
        spec(
            "watch_market",
            "Keep an eye on a price (share, index, crypto, gold) and tell the user when it "
            "moves by move_percent from the last price mentioned; or stop watching it.",
            {"name": text, "move_percent": {"type": "number"}, "stop": {"type": "boolean"}},
            ["name"],
            watch_market,
            lambda a: f"Watch the price of {a.get('name', '')}",
            "finance",
        )
    )
