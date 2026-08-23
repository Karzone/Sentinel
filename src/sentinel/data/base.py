"""Vendor adapter protocols.

Rule 7: keep vendor adapters thin and swappable; assume every third-party API
will be replaced eventually. So an adapter's whole job is
*wire format in -> domain object out*. No scoring, no caching policy, no
business rules — those live above and must not have to change when EODHD
becomes Polygon.

Every adapter is allowed to be **dormant**: no key, no crash. ``available()``
is false and the ingest job reports the vendor as not configured rather than
taking the pipeline down. That is what lets the fixture provider drive the
entire system with an empty ``.env``.
"""

from __future__ import annotations

import datetime as dt
from typing import Protocol, runtime_checkable

from ..domain.models import Bar, Fundamentals, NewsItem


class ProviderError(RuntimeError):
    """A vendor failed in a way the caller must see. Never swallowed into an
    empty list — an empty list means 'no data exists', which is a different
    claim from 'the vendor was down', and the two must not be confused by a
    quality check."""


@runtime_checkable
class PriceProvider(Protocol):
    name: str

    def available(self) -> bool: ...

    def fetch_bars(self, ticker: str, start: dt.date, end: dt.date) -> list[Bar]: ...


@runtime_checkable
class FundamentalsProvider(Protocol):
    name: str

    def available(self) -> bool: ...

    def fetch_fundamentals(self, ticker: str) -> Fundamentals | None: ...


@runtime_checkable
class NewsProvider(Protocol):
    name: str

    def available(self) -> bool: ...

    def fetch_news(self, ticker: str, since: dt.datetime) -> list[NewsItem]: ...


@runtime_checkable
class FxProvider(Protocol):
    name: str

    def available(self) -> bool: ...

    def fetch_rates(self, currencies: tuple[str, ...], as_of: dt.date) -> dict[str, object]: ...


def currency_for(ticker: str) -> str:
    """Infer quote currency from the ticker suffix.

    Deliberately crude and deliberately explicit: an unknown suffix returns GBP
    only for bare tickers, and anything else must be mapped. Guessing USD for an
    unrecognised suffix is how a EUR-quoted name silently becomes a GBP
    position size.
    """
    upper = ticker.upper()
    if upper.endswith((".LSE", ".L", ".LON")):
        return "GBP"
    if upper.endswith((".US", ".NYSE", ".NASDAQ", ".O", ".N")):
        return "USD"
    if upper.endswith((".PA", ".DE", ".AS", ".MI", ".MC", ".BR")):
        return "EUR"
    if upper.endswith(".SW"):
        return "CHF"
    if upper.endswith(".TO"):
        return "CAD"
    return "GBP" if "." not in upper else "USD"
