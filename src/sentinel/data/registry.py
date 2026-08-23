"""Provider selection.

One place that turns a config string into an adapter, so swapping EODHD for
Polygon is a one-line config edit plus one new module — never a change to the
ingest job or anything above it.
"""

from __future__ import annotations

from typing import Any, Callable

from ..config import Config
from .base import FundamentalsProvider, NewsProvider, PriceProvider
from .chain import FundamentalsChain
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
    """One name, or several comma-separated as a fallback chain.

    Free tiers gate fundamentals per SYMBOL, not per feature, so no single free
    vendor covers a whole universe: `fundamentals_provider = "fmp,eodhd"` tries
    the next vendor for the tickers the first one refuses. A single name still
    builds a single provider, unchanged.
    """
    names = [n.strip() for n in config.data.fundamentals_provider.split(",") if n.strip()]
    if not names:
        raise UnknownProvider(
            "fundamentals provider is empty; known: " + ", ".join(sorted(_FUNDAMENTALS))
        )
    if len(names) == 1:
        return _build(_FUNDAMENTALS, names[0], "fundamentals")
    return FundamentalsChain([_build(_FUNDAMENTALS, n, "fundamentals") for n in names])


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
            # A chain reports one row PER MEMBER. Collapsed to a single
            # "fmp+eodhd: ready" row, a chain with one key missing looks
            # healthy, and health's whole job is to say which key is absent.
            members = getattr(provider, "members", None) or [provider]
            for member in members:
                out.append({
                    "kind": kind, "provider": member.name,
                    "available": member.available(),
                })
        except UnknownProvider as exc:
            out.append({"kind": kind, "provider": "?", "available": False, "error": str(exc)})
    return out
