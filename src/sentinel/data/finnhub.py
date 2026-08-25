"""Finnhub adapter — company-tagged news.

Company tagging is the whole reason this vendor is here: a generic market feed
cannot tell the catalyst module *which* company an event belongs to, and an
untagged headline scored against a ticker is a fabricated input.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Any, Sequence

import httpx

from ..config import api_key
from ..domain.models import NewsItem
from .base import DelayedQuote, ProviderError, describe_http_error, redact

BASE_URL = "https://finnhub.io/api/v1"
ADAPTER_VERSION = "finnhub-v1"


def parse_news(ticker: str, payload: Sequence[dict[str, Any]], *, since: dt.datetime | None = None) -> list[NewsItem]:
    items: list[NewsItem] = []
    for row in payload:
        headline = (row.get("headline") or "").strip()
        if not headline:
            continue
        try:
            published = dt.datetime.fromtimestamp(int(row["datetime"]), dt.UTC)
        except (KeyError, ValueError, TypeError, OSError):
            continue
        if since and published < since:
            continue
        items.append(
            NewsItem(
                ticker=ticker,
                published_at=published,
                headline=headline,
                summary=(row.get("summary") or "").strip(),
                source=(row.get("source") or "").strip(),
                url=(row.get("url") or "").strip(),
            )
        )
    items.sort(key=lambda i: i.published_at, reverse=True)
    return items


class FinnhubProvider:
    name = "finnhub"

    def __init__(self, token: str | None = None, *, client: httpx.Client | None = None) -> None:
        self._token = token or api_key("FINNHUB_API_KEY")
        self._client = client

    def available(self) -> bool:
        return bool(self._token)

    def fetch_news(self, ticker: str, since: dt.datetime) -> list[NewsItem]:
        if not self.available():
            raise ProviderError("FINNHUB_API_KEY is not set")
        client = self._client or httpx.Client(timeout=30)
        try:
            response = client.get(
                f"{BASE_URL}/company-news",
                params={
                    "symbol": ticker.split(".")[0],
                    "from": since.date().isoformat(),
                    "to": dt.date.today().isoformat(),
                    "token": self._token,
                },
            )
            response.raise_for_status()
            return parse_news(ticker, response.json(), since=since)
        except httpx.HTTPError as exc:
            raise ProviderError(
                f"Finnhub request failed: {describe_http_error(exc, self._token)}"
            ) from exc
        finally:
            if self._client is None:
                client.close()

    def fetch_delayed_quotes(self, tickers: Sequence[str]) -> dict[str, DelayedQuote]:
        """Latest US quote per ticker from the free `/quote` endpoint.

        One small request per symbol — the stop-watch only ever asks for the
        open positions, and its five-minute cache keeps this far inside the
        free rate limit. Finnhub keys US symbols bare ("PLTR"), so the
        exchange suffix is stripped on the way out and the result is keyed by
        the ORIGINAL ticker so callers can match their positions. An unknown
        symbol answers c=0, t=0 and is skipped — absence, not a zero price.
        """
        if not self.available():
            raise ProviderError("FINNHUB_API_KEY is not set")
        client = self._client or httpx.Client(timeout=30)
        quotes: dict[str, DelayedQuote] = {}
        try:
            for ticker in dict.fromkeys(t for t in tickers if t):
                response = client.get(
                    f"{BASE_URL}/quote",
                    params={"symbol": ticker.split(".")[0], "token": self._token},
                )
                response.raise_for_status()
                row = response.json()
                price = row.get("c")
                stamp = row.get("t")
                if not price:  # 0 or None = symbol unknown to the vendor
                    continue
                quotes[ticker] = DelayedQuote(
                    ticker=ticker, price=Decimal(str(price)),
                    at=(dt.datetime.fromtimestamp(int(stamp), tz=dt.timezone.utc)
                        if stamp else None),
                )
            return quotes
        except httpx.HTTPError as exc:
            raise ProviderError(
                f"Finnhub request failed: {describe_http_error(exc, self._token)}"
            ) from exc
        finally:
            if self._client is None:
                client.close()
