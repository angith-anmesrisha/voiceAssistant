"""Live data tools (prices, currency, Wikipedia, dictionary, air, quakes, holidays, jokes)
and the market watch, against canned replies shaped like each API's."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from jarvis.agent.routines import Briefing, MarketWatch
from jarvis.agent.toolbudget import groups_for
from jarvis.storage.keep import Keep
from jarvis.tools import live
from jarvis.tools.registry import ToolRegistry
from jarvis.tools.web import WebError
from jarvis.voice.spoken_text import speakable

Handler = Callable[[httpx.Request], httpx.Response]

YAHOO_SEARCH = {
    "quotes": [
        {"symbol": "RELIANCE.BO", "quoteType": "EQUITY", "shortname": "RELIANCE INDS"},
        {"symbol": "RELIANCE.NS", "quoteType": "EQUITY", "shortname": "RELIANCE INDS"},
    ]
}


def chart(price: float, previous: float, name: str = "NIFTY 50", currency: str = "INR") -> Any:
    return {
        "chart": {
            "result": [
                {
                    "meta": {
                        "currency": currency,
                        "symbol": "^NSEI",
                        "regularMarketPrice": price,
                        "chartPreviousClose": previous,
                        "shortName": name,
                        "regularMarketTime": 1_792_838_400,
                    }
                }
            ]
        }
    }


def routes(table: dict[str, Any]) -> Handler:
    """Answer by the first key found in the URL; a value that is an int is a status."""

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        for key, value in table.items():
            if key in url:
                if isinstance(value, int):
                    return httpx.Response(value, json={})
                return httpx.Response(200, json=value)
        return httpx.Response(404, json={})

    return handler


def client(table: dict[str, Any]) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(routes(table)))


def test_names_are_classified() -> None:
    assert live.classify("gold price today") == "metal"
    assert live.classify("Bitcoin") == "crypto"
    assert live.classify("the Nifty 50") == "index"
    assert live.classify("Parag Parikh Flexi Cap fund") == "fund"
    assert live.classify("Reliance share price") == "stock"
    assert live.home_currency("en-IN") == "INR" and live.home_currency("xx") == "USD"
    assert live.currency_code("dollars") == "USD" and live.currency_code("eur") == "EUR"


def test_money_reads_well() -> None:
    assert live.money(24310.55, "INR") == "₹24,311"
    assert live.money(12.5, "USD") == "$12.50"
    assert live.money(0.12345, "XYZ") == "0.1235 XYZ"
    assert speakable("Gold is ₹12,220 a gram.") == "Gold is 12,220 rupees a gram."


async def test_index_price_comes_from_yahoo() -> None:
    async with client({"/chart/%5ENSEI": chart(24310.5, 24200.0)}) as http:
        quote = await live.price_of(http, "nifty", "en-IN")
    assert quote.name == "NIFTY 50 (^NSEI)" and quote.currency == "INR"
    assert quote.change_pct == pytest.approx(0.46, abs=0.01)
    assert "₹24,310" in quote.text() and "Yahoo Finance" in quote.text()


async def test_indian_shares_prefer_the_nse() -> None:
    async with client(
        {"finance/search": YAHOO_SEARCH, "/chart/RELIANCE.NS": chart(1400, 1390, "Reliance")}
    ) as http:
        quote = await live.price_of(http, "Reliance", "en-IN")
    assert quote.name == "Reliance (RELIANCE.NS)"


async def test_crypto_falls_back_when_coingecko_is_down() -> None:
    async with client(
        {
            "coingecko": {"bitcoin": {"inr": 9_500_000, "inr_24h_change": -1.234}},
        }
    ) as http:
        quote = await live.price_of(http, "bitcoin", "en-IN")
    assert (quote.price, quote.change_pct, quote.source) == (9_500_000, -1.23, "CoinGecko")
    async with client(
        {"coingecko": 429, "currencies/btc.json": {"date": "2026-10-24", "btc": {"inr": 9.4e6}}}
    ) as http:
        quote = await live.price_of(http, "BTC", "en-IN")
    assert quote.price == 9.4e6 and "fawazahmed0" in quote.source


async def test_gold_is_per_gram() -> None:
    reply = {"date": "2026-10-24", "xau": {"inr": 380105.28, "usd": 4010.30}}
    async with client({"currencies/xau.json": reply}) as http:
        quote = await live.price_of(http, "gold", "en-IN")
    assert quote.price == pytest.approx(380105.28 / live.TROY_OUNCE_G)
    assert quote.unit == "a gram" and "spot" in quote.name


async def test_mutual_fund_nav() -> None:
    async with client(
        {
            "mf/search": [
                {"schemeCode": 1, "schemeName": "Parag Parikh Flexi Cap Fund - Regular Plan"},
                {"schemeCode": 2, "schemeName": "Parag Parikh Flexi Cap Fund - Direct Growth"},
            ],
            "mf/2/latest": {
                "meta": {"scheme_name": "Parag Parikh Flexi Cap Fund - Direct Growth"},
                "data": [{"date": "22-10-2026", "nav": "89.40880"}],
            },
        }
    ) as http:
        quote = await live.price_of(http, "parag parikh flexi cap fund", "en-IN")
    assert quote.price == pytest.approx(89.4088) and quote.when == "22-10-2026"


async def test_currency_conversion_and_fallback() -> None:
    async with client(
        {
            "frankfurter": {
                "amount": 1.0,
                "base": "USD",
                "date": "2026-10-23",
                "rates": {"INR": 94.94},
            }
        }
    ) as http:
        text = await live.convert(http, 100, "dollars", "inr")
    assert text.startswith("$100.00 is ₹9,494") and "Frankfurter" in text
    async with client(
        {"frankfurter": 404, "currencies/aed.json": {"date": "d", "aed": {"inr": 25.8}}}
    ) as http:
        assert "₹2,580" in await live.convert(http, 100, "dirhams", "INR")


async def test_information_sources() -> None:
    async with client(
        {
            "wikipedia.org": {
                "query": {"pages": {"1": {"title": "Kochi", "extract": "Kochi is a city."}}}
            },
            "dictionaryapi": [
                {
                    "word": "serendipity",
                    "phonetic": "/serendipity/",
                    "meanings": [
                        {
                            "partOfSpeech": "noun",
                            "definitions": [{"definition": "A happy accident."}],
                        }
                    ],
                }
            ],
            "air-quality": {"current": {"us_aqi": 112, "pm2_5": 40.2, "pm10": 60}},
            "geocoding": {
                "results": [
                    {"name": "Delhi", "country": "India", "latitude": 28.6, "longitude": 77.2}
                ]
            },
            "earthquake.usgs": {
                "features": [
                    {"properties": {"mag": 4.6, "place": "Fiji", "time": 1_792_838_400_000}},
                    {"properties": {"mag": 6.1, "place": "Chile", "time": 1_792_838_400_000}},
                ]
            },
            "nager.at": [{"date": "2026-11-08", "localName": "Diwali", "name": "Diwali"}],
            "jokeapi": {"type": "twopart", "setup": "Why?", "delivery": "Because."},
        }
    ) as http:
        assert await live.wiki_summary(http, "kochi") == "Kochi (Wikipedia): Kochi is a city."
        assert "(noun) A happy accident." in await live.define(http, "serendipity")
        assert "AQI 112 (unhealthy for sensitive groups)" in await live.air_quality(http, "Delhi")
        quakes = await live.earthquakes(http)
        assert quakes[0].startswith("M6.1 Chile")
        assert await live.holidays(http, "in") == ["Sun 08 Nov 2026: Diwali"]
        assert await live.joke(http) == "Why? … Because."


async def test_missing_word_is_a_clear_error() -> None:
    async with client({}) as http:
        with pytest.raises(WebError, match="doesn't have"):
            await live.define(http, "qwertyuiop")


async def test_tools_register_and_watch_market(tmp_path: Path) -> None:
    keep = Keep(tmp_path / "keep.sqlite3")
    registry = ToolRegistry()
    async with client({"/chart/%5ENSEI": chart(24000, 24000)}) as http:
        live.register_live_tools(registry, http, keep, locale="en-IN")
        names = set(registry.names())
        assert {"get_price", "convert_currency", "wikipedia", "watch_market"} <= names
        spec = registry.get("watch_market")
        assert spec is not None
        result = await registry.run(spec, {"name": "Nifty", "move_percent": 1})
        assert result.ok
    assert await live.watched(keep) == {"nifty": {"move": 1.0, "last": 24000.0}}

    prices = iter([24100.0, 24300.0, 24310.0])

    async def price(name: str) -> live.Quote:
        return live.Quote("NIFTY 50", next(prices), "INR")

    watch = MarketWatch(keep, price, quiet_hours=(0, 0), every=timedelta(0))
    noon = datetime(2026, 10, 24, 12, 0).astimezone()
    assert await watch.tick(noon) == []  # +0.4%: not enough
    said = await watch.tick(noon)
    assert [s.text for s in said] == ["Nifty is up 1.2% since I last mentioned it: ₹24,300."]
    assert await watch.tick(noon) == []  # measured from the new mark now
    keep.close()


async def test_briefing_reads_markets(tmp_path: Path) -> None:
    keep = Keep(tmp_path / "keep.sqlite3")

    async def nothing() -> Any:
        return None

    async def no_news() -> list[Any]:
        return []

    async def markets() -> list[live.Quote]:
        return [
            live.Quote("NIFTY 50 (^NSEI)", 24310, "INR", 0.46),
            live.Quote("Gold (international spot)", 12220.4, "INR", unit="a gram"),
        ]

    text = await Briefing(keep, nothing, no_news, markets=markets).compose(
        datetime(2026, 10, 24, 9, 0).astimezone()
    )
    assert "Markets: NIFTY 50 ₹24,310, up 0.5%; Gold ₹12,220 a gram." in text
    keep.close()


@pytest.mark.parametrize(
    ("text", "group"),
    [
        ("what's the nifty at", "finance"),
        ("convert 50 dollars to rupees", "finance"),
        ("gold rate today", "finance"),
        ("tell me about the Eiffel Tower", "info"),
        ("what's the air quality like", "info"),
        ("tell me a joke", "info"),
    ],
)
def test_requests_get_the_right_tools(text: str, group: str) -> None:
    assert group in groups_for(text)
