"""EODHD adapter — prices (and optionally fundamentals).

Chosen as the Phase 1 default for prices because LSE coverage is the binding
constraint for a UK investor and EODHD's is good at the ~$20/mo tier.

The HTTP call and the parse are separate on purpose: ``parse_eod`` is a pure
function over decoded JSON, so the mapping is unit-tested against recorded
payloads with no key and no network, which is the only part of a vendor
adapter that can actually harbour a bug.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Any, Sequence

import httpx

from ..config import api_key
from ..domain.enums import Wrapper
from ..domain.models import Bar, Fundamentals
from ..money import dec
from .base import ProviderError, currency_for, redact

BASE_URL = "https://eodhd.com/api"
ADAPTER_VERSION = "eodhd-v1"


def parse_eod(ticker: str, payload: Sequence[dict[str, Any]], *, currency: str | None = None) -> list[Bar]:
    """EODHD `/eod` rows -> Bars.

    Rows with a null OHLC field are dropped rather than zero-filled: a zero
    close would sail past the price-sanity check as a -100% move and poison
    every indicator. A missing bar is honest; a fabricated one is not.
    """
    ccy = currency or currency_for(ticker)
    bars: list[Bar] = []
    for row in payload:
        try:
            if any(row.get(k) is None for k in ("open", "high", "low", "close")):
                continue
            close = dec(row["close"])
            bars.append(
                Bar(
                    ticker=ticker,
                    date=dt.date.fromisoformat(str(row["date"])),
                    open=dec(row["open"]), high=dec(row["high"]), low=dec(row["low"]),
                    close=close,
                    # EODHD omits adjusted_close on some endpoints; falling back
                    # to close makes the factor 1.0, which the integrity check
                    # reads as "no adjustment", not as corruption.
                    adjusted_close=dec(row.get("adjusted_close", close)),
                    volume=int(row.get("volume") or 0),
                    currency=ccy,
                )
            )
        except (KeyError, ValueError, TypeError) as exc:
            raise ProviderError(f"EODHD returned an unparseable bar for {ticker}: {row!r}") from exc
    bars.sort(key=lambda b: b.date)
    return bars


def parse_fundamentals(ticker: str, payload: dict[str, Any]) -> Fundamentals | None:
    """EODHD `/fundamentals` -> Fundamentals.

    EODHD nests everything; only the fields Phase 2 actually scores are mapped.
    Anything absent stays ``None`` and the fundamental module simply scores that
    factor as unavailable rather than as zero.
    """
    general = payload.get("General") or {}
    highlights = payload.get("Highlights") or {}
    valuation = payload.get("Valuation") or {}
    if not highlights and not general:
        return None

    def num(source: dict[str, Any], key: str) -> Decimal | None:
        value = source.get(key)
        return None if value in (None, "", "NA") else dec(value)

    # `as_of` is the PERIOD the numbers describe, not when the vendor last
    # touched the row. `General.UpdatedAt` is EODHD's record-update timestamp,
    # and using it here broke two things at once.
    #
    # It is stamped in EODHD's timezone, so it runs a day ahead of a UK clock —
    # every snapshot landed dated tomorrow, and `repo.get_fundamentals` reads
    # point-in-time (`as_of <= ?`), so all 25 rows were written and then
    # filtered out. The brief reported "no fundamentals snapshot" for a database
    # that had them.
    #
    # And even with the dates in range it defeated the staleness check: a
    # snapshot "filed" today is never stale, so two-year-old financials would
    # have scored as current.
    #
    # MostRecentQuarter is the period end, which is what both of those want.
    period = _maybe_date(highlights.get("MostRecentQuarter"))
    if period is None:
        as_of_raw = general.get("UpdatedAt") or dt.date.today().isoformat()
        try:
            period = dt.date.fromisoformat(str(as_of_raw)[:10])
        except ValueError:
            period = dt.date.today()
    as_of = period

    return Fundamentals(
        ticker=ticker,
        as_of=as_of,
        currency=general.get("CurrencyCode") or currency_for(ticker),
        sector=(general.get("Sector") or "").lower() or None,
        market_cap=num(highlights, "MarketCapitalization"),
        revenue_ttm=num(highlights, "RevenueTTM"),
        eps_ttm=num(highlights, "EarningsShare"),
        gross_margin=num(highlights, "GrossProfitTTM"),
        operating_margin=num(highlights, "OperatingMarginTTM"),
        net_margin=num(highlights, "ProfitMargin"),
        total_debt=num(highlights, "TotalDebt"),
        pe_ratio=num(highlights, "PERatio"),
        ev_ebitda=num(valuation, "EnterpriseValueEbitda"),
        # NOT MostRecentQuarter — that is the quarter that just ENDED, so using
        # it here asserted the next earnings date was in the past. Nothing reads
        # this field today; None is the honest value until a real upcoming-
        # earnings field is confirmed against the live API.
        next_earnings_date=None,
        wrapper=Wrapper.UNKNOWN,
    )


def _maybe_date(value: Any) -> dt.date | None:
    try:
        return dt.date.fromisoformat(str(value)[:10])
    except (ValueError, TypeError):
        return None


class EodhdProvider:
    name = "eodhd"

    def __init__(self, token: str | None = None, *, client: httpx.Client | None = None) -> None:
        self._token = token or api_key("EODHD_API_KEY")
        self._client = client

    def available(self) -> bool:
        return bool(self._token)

    def _get(self, path: str, params: dict[str, Any]) -> Any:
        if not self.available():
            raise ProviderError("EODHD_API_KEY is not set")
        client = self._client or httpx.Client(timeout=30)
        try:
            response = client.get(
                f"{BASE_URL}/{path}", params={**params, "api_token": self._token, "fmt": "json"}
            )
            response.raise_for_status()
            return response.json()
        except httpx.HTTPError as exc:
            # NEVER let httpx's message through unredacted. EODHD authenticates
            # by query parameter, and httpx puts the full URL in the exception —
            # so the raw API token was reaching the terminal, the log files, the
            # brief's data-warnings section, and anything a user pastes when
            # asking for help. A credential that appears in an error message is
            # a credential you have to rotate.
            raise ProviderError(f"EODHD request failed: {redact(exc, self._token)}") from exc
        finally:
            if self._client is None:
                client.close()

    def fetch_bars(self, ticker: str, start: dt.date, end: dt.date) -> list[Bar]:
        payload = self._get(
            f"eod/{ticker}",
            {"from": start.isoformat(), "to": end.isoformat(), "period": "d", "order": "a"},
        )
        return parse_eod(ticker, payload)

    def fetch_fundamentals(self, ticker: str) -> Fundamentals | None:
        return parse_fundamentals(ticker, self._get(f"fundamentals/{ticker}", {}))
