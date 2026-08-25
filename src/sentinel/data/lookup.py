"""Name -> symbol resolution for the search box.

Two vendors, first answer wins: EODHD's `/search` (suffixed symbols across
every exchange it covers — but 403 on the owner's current plan, measured
2026-08-25), then Finnhub's free `/search` (US listings only: Finnhub keys
US symbols bare, and inventing exchange suffixes for its non-US results
would produce tickers the ingest cannot fetch — so dotted symbols are
dropped rather than guessed at).

"Rocket Lab" is not a ticker, and forcing a beginner to know RKLB before the
app will talk to them is the ticker tail wagging the dog. This is the one
seam that turns a company name into fetchable symbols.

Same shape as every other adapter: ``parse_search`` is a pure function over
decoded JSON (unit-tested with no key and no network), the HTTP call is thin,
and errors are redacted — EODHD authenticates by query parameter, so the raw
exception would otherwise carry the API token.

One request per submitted query, never per keystroke: the dashboard calls
this only after the user commits text that is not a known stock, so it stays
a lookup, not a streaming autocomplete against a metered vendor.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Sequence

import httpx

from ..config import api_key
from .base import ProviderError, describe_http_error

# Env-overridable for the same reason FMP_API_BASE is: a stub server is how
# the dashboard's lookup path gets exercised end-to-end without a key.
SEARCH_URL = os.environ.get("EODHD_SEARCH_BASE", "https://eodhd.com/api/search")
LOOKUP_VERSION = "lookup-v1"


@dataclass(frozen=True, slots=True)
class SymbolMatch:
    ticker: str      # already suffixed: "RKLB.US"
    name: str
    exchange: str
    currency: str


def parse_search(payload: Sequence[dict[str, Any]], *, limit: int = 6) -> list[SymbolMatch]:
    """EODHD `/search` rows -> matches, best first as the API ranks them.

    A row without a code or exchange cannot be fetched, so it is dropped
    rather than surfaced as a button that would start a doomed ingest.
    """
    matches: list[SymbolMatch] = []
    for row in payload:
        code = str(row.get("Code") or "").strip().upper()
        exchange = str(row.get("Exchange") or "").strip().upper()
        if not code or not exchange:
            continue
        matches.append(SymbolMatch(
            ticker=f"{code}.{exchange}",
            name=str(row.get("Name") or code),
            exchange=exchange,
            currency=str(row.get("Currency") or ""),
        ))
        if len(matches) >= limit:
            break
    return matches


def parse_finnhub_search(payload: dict[str, Any], *, limit: int = 6) -> list[SymbolMatch]:
    """Finnhub `/search` rows -> matches. US listings only: a bare symbol
    ("TSLA") becomes "TSLA.US"; a dotted one ("VOD.L") is another exchange's
    scheme and is dropped rather than mis-suffixed."""
    matches: list[SymbolMatch] = []
    for row in payload.get("result") or []:
        if not isinstance(row, dict):
            continue
        symbol = str(row.get("symbol") or "").strip().upper()
        if not symbol or "." in symbol:
            continue
        matches.append(SymbolMatch(
            ticker=f"{symbol}.US",
            name=str(row.get("description") or symbol).title(),
            exchange="US",
            currency="USD",
        ))
        if len(matches) >= limit:
            break
    return matches


def _finnhub_search(
    text: str, *, token: str, client: httpx.Client | None = None, limit: int = 6,
) -> list[SymbolMatch]:
    http = client or httpx.Client(timeout=15)
    try:
        response = http.get(
            "https://finnhub.io/api/v1/search",
            params={"q": text, "token": token},
        )
        response.raise_for_status()
        payload = response.json()
    except httpx.HTTPError as exc:
        raise ProviderError(
            f"Finnhub symbol search failed: {describe_http_error(exc, token)}"
        ) from exc
    finally:
        if client is None:
            http.close()
    if not isinstance(payload, dict):
        raise ProviderError(f"Finnhub symbol search returned a non-dict payload for {text!r}")
    return parse_finnhub_search(payload, limit=limit)


def search_symbols(
    query: str, *, token: str | None = None, client: httpx.Client | None = None,
    limit: int = 6,
) -> list[SymbolMatch]:
    text = query.strip()
    if not text:
        return []
    eodhd_token = token or api_key("EODHD_API_KEY")
    finnhub_token = api_key("FINNHUB_API_KEY")
    if not eodhd_token and not finnhub_token:
        raise ProviderError(
            "no search vendor configured — set EODHD_API_KEY or FINNHUB_API_KEY")

    failures: list[str] = []
    if eodhd_token:
        http = client or httpx.Client(timeout=15)
        try:
            response = http.get(
                f"{SEARCH_URL}/{text}",
                params={"api_token": eodhd_token, "fmt": "json", "limit": limit},
            )
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, list):
                raise ProviderError(
                    f"EODHD symbol search returned a non-list payload for {text!r}")
            matches = parse_search(payload, limit=limit)
            if matches:
                return matches
        except httpx.HTTPError as exc:
            failures.append(
                f"EODHD symbol search failed: {describe_http_error(exc, eodhd_token)}")
        finally:
            if client is None:
                http.close()

    if finnhub_token:
        try:
            # `client` is not forwarded on the fallback leg: an injected test
            # client stubs ONE vendor's wire format, and this is a different
            # vendor's.
            return _finnhub_search(text, token=finnhub_token, limit=limit)
        except ProviderError as exc:
            failures.append(str(exc))

    if failures:
        raise ProviderError("; ".join(failures))
    return []
