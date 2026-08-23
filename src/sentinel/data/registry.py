"""Provider selection.

One place that turns a config string into an adapter, so swapping EODHD for
Polygon is a one-line config edit plus one new module — never a change to the
ingest job or anything above it.
"""

from __future__ import annotations

from typing import Any, Callable

from ..config import Config
from .base import FundamentalsProvider, NewsProvider, PriceProvider
from .eodhd import EodhdProvider
from .finnhub import FinnhubProvider
from .fixtures import FixtureProvider
from .fmp import FmpProvider

_PRICE: dict[str, Callable[[], Any]] = {
    "fixture": FixtureProvider,
    "eodhd": EodhdProvider,
}
_FUNDAMENTALS: dict[str, Callable[[], Any]] = {
    "fixture": FixtureProvider,
    "eodhd": EodhdProvider,
    "fmp": FmpProvider,
}
_NEWS: dict[str, Callable[[], Any]] = {
    "fixture": FixtureProvider,
    "finnhub": FinnhubProvider,
}


class UnknownProvider(KeyError):
    pass


def _build(table: dict[str, Callable[[], Any]], name: str, kind: str) -> Any:
    try:
        return table[name]()
    except KeyError as exc:
        raise UnknownProvider(
            f"unknown {kind} provider {name!r}; known: {', '.join(sorted(table))}"
        ) from exc


def price_provider(config: Config) -> PriceProvider:
    return _build(_PRICE, config.data.price_provider, "price")


def fundamentals_provider(config: Config) -> FundamentalsProvider:
    return _build(_FUNDAMENTALS, config.data.fundamentals_provider, "fundamentals")


def news_provider(config: Config) -> NewsProvider:
    return _build(_NEWS, config.data.news_provider, "news")


def describe(config: Config) -> list[dict[str, Any]]:
    """What `sentinel health` prints: which vendor is wired to what, and whether
    it is dormant for want of a key."""
    out: list[dict[str, Any]] = []
    for kind, builder in (
        ("prices", price_provider), ("fundamentals", fundamentals_provider), ("news", news_provider)
    ):
        try:
            provider = builder(config)
            out.append({"kind": kind, "provider": provider.name, "available": provider.available()})
        except UnknownProvider as exc:
            out.append({"kind": kind, "provider": "?", "available": False, "error": str(exc)})
    return out
