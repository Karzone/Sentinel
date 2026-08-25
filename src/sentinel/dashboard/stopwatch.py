"""The delayed stop-watch: is any open position trading through its stop NOW?

The one place intraday data genuinely changes a decision on an investor's
clock is stop discipline — the EOD mark can sit above the stop all day while
the live price trades through it, and the breach would otherwise wait for
tomorrow's ingest to surface. So this module fetches a DELAYED (~15 min)
quote for the open positions only and compares it to each stop.

Boundaries, all deliberate:
- **Display-only.** A delayed print never reaches a score, an indicator, or
  the database — scores are defined on completed daily bars (the recorded
  EOD rule), and this module has no write path to break that with.
- **Positions only.** A handful of symbols in one request, never the
  watchlist — a live-repainting leaderboard is trader behaviour; a stop
  alert is investor behaviour.
- **Degrades to silence.** No key, a plan without the endpoint, a vendor
  error, a market holiday — every failure returns "no breaches known", and
  the surfaces fall back to the EOD message they showed before this existed.
  A stop-watch that can cry wolf on an error would teach the owner to
  ignore it.
- **Cached for five minutes** per ticker-set, because Streamlit reruns on
  every click and a quote 4 minutes old answers the same question.
"""

from __future__ import annotations

import datetime as dt
import time
from dataclasses import dataclass
from decimal import Decimal
from math import isnan
from typing import Callable, Mapping, Sequence

import pandas as pd

from ..data.base import DelayedQuote, ProviderError
from ..data.eodhd import EodhdProvider
from ..data.finnhub import FinnhubProvider

#: Reruns within this window reuse the cached quotes.
CACHE_TTL_SECONDS = 300.0

_cache: dict[frozenset[str], tuple[float, dict[str, DelayedQuote]]] = {}


@dataclass(frozen=True)
class StopBreach:
    ticker: str
    price: float
    stop: float
    at: dt.datetime | None


def breaches(
    positions: pd.DataFrame, quotes: Mapping[str, DelayedQuote]
) -> list[StopBreach]:
    """Positions whose delayed price is at or below their stop.

    A position with no stop, or no quote in the batch, is skipped — absence
    of evidence is never an alert.
    """
    found: list[StopBreach] = []
    if positions.empty:
        return found
    for row in positions.itertuples():
        stop = getattr(row, "stop", float("nan"))
        quote = quotes.get(row.ticker)
        if quote is None or stop is None or isnan(stop):
            continue
        if quote.price <= Decimal(str(stop)):
            found.append(StopBreach(
                ticker=row.ticker, price=float(quote.price),
                stop=float(stop), at=quote.at,
            ))
    return found


def delayed_quotes(
    tickers: Sequence[str],
    *,
    fetch: Callable[[Sequence[str]], dict[str, DelayedQuote]] | None = None,
    now: Callable[[], float] = time.monotonic,
) -> dict[str, DelayedQuote]:
    """Delayed quotes for the given tickers, cached for CACHE_TTL_SECONDS.

    `fetch`/`now` are injectable for tests; the default fetch is the vendor
    chain below, and any failure — missing keys, a plan without the
    endpoint, network — returns {} so callers show their EOD fallback.
    """
    symbols = frozenset(t for t in tickers if t)
    if not symbols:
        return {}
    cached = _cache.get(symbols)
    if cached is not None and now() - cached[0] < CACHE_TTL_SECONDS:
        return cached[1]
    if fetch is None:
        fetch = _vendor_chain
    try:
        quotes = fetch(sorted(symbols))
    except ProviderError:
        quotes = {}
    _cache[symbols] = (now(), quotes)
    return quotes


def armed() -> bool:
    """Whether any quote vendor is configured. Callers use this to decide if
    auto-refreshing a surface is worth anything — with no keys, a self-
    refreshing tile would just re-render the same EOD text forever."""
    return any(p.available() for p in (EodhdProvider(), FinnhubProvider()))


def _vendor_chain(symbols: Sequence[str]) -> dict[str, DelayedQuote]:
    """First vendor that answers wins. EODHD's real-time endpoint needs a
    paid tier (the owner's plan 403s it — measured 2026-08-25); Finnhub's
    free `/quote` covers US symbols, which is where the positions live. A
    vendor that errors or answers empty just hands over to the next."""
    for provider in (EodhdProvider(), FinnhubProvider()):
        if not provider.available():
            continue
        try:
            quotes = provider.fetch_delayed_quotes(symbols)
        except ProviderError:
            continue
        if quotes:
            return quotes
    return {}


def check(
    positions: pd.DataFrame,
    *,
    fetch: Callable[[Sequence[str]], dict[str, DelayedQuote]] | None = None,
    now: Callable[[], float] = time.monotonic,
) -> list[StopBreach]:
    """The whole stop-watch in one call: quotes for the open positions,
    compared against their stops. Empty on any failure, by design."""
    if positions.empty or "ticker" not in positions.columns:
        return []
    quotes = delayed_quotes(list(positions["ticker"]), fetch=fetch, now=now)
    return breaches(positions, quotes)
