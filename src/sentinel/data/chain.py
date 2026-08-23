"""A fallback chain for fundamentals.

Free vendor tiers do not gate a *feature*, they gate *rows*. FMP's free plan
answered nine of the twenty-five AI tickers and returned `402 Payment Required
— Special Endpoint: This value set for 'symbol' is not available under your
current subscription` for the other sixteen. Nothing is wrong with the adapter
in that case and no retry will help: those symbols are simply not in that plan.

So the vendor is chosen per TICKER rather than per install. Each vendor is
tried in configured order and the first one with an answer wins, which turns
two partial free tiers into one wider one. Only when every vendor has refused
the same ticker is that a failure — and then the error carries what *each* of
them said, because "402 not in your plan" and "403 endpoint not in your plan"
are differently actionable and collapsing them to "no fundamentals" is the
mistake this file exists to avoid.

Which vendor actually answered is recorded per snapshot (`answered_by`), so the
audit trail says `fmp` for one row and `eodhd` for the next instead of naming
the chain — provenance has to survive the fallback or it is not provenance.
"""

from __future__ import annotations

import datetime as dt

from ..domain.models import Fundamentals
from .base import FundamentalsProvider, ProviderError


class FundamentalsChain:
    """Tries each provider in order; first non-None answer wins."""

    def __init__(self, providers: list[FundamentalsProvider]) -> None:
        if not providers:
            raise ValueError("a fundamentals chain needs at least one provider")
        self._providers = providers
        #: Set to the vendor that answered the most recent successful call, so
        #: ingest can record the true source rather than the chain's name. None
        #: until something answers, and reset on every call so a stale name can
        #: never be attributed to a later row.
        self.answered_by: str | None = None

    @property
    def members(self) -> list[FundamentalsProvider]:
        """So `sentinel health` can report each vendor's key separately."""
        return list(self._providers)

    @property
    def name(self) -> str:
        return "+".join(p.name for p in self._providers)

    def available(self) -> bool:
        return any(p.available() for p in self._providers)

    def fetch_fundamentals(self, ticker: str) -> Fundamentals | None:
        self.answered_by = None
        refusals: list[str] = []
        for provider in self._providers:
            if not provider.available():
                continue
            try:
                snapshot = provider.fetch_fundamentals(ticker)
            except ProviderError as exc:
                refusals.append(f"{provider.name}: {exc}")
                continue
            if snapshot is not None:
                self.answered_by = provider.name
                return snapshot
            # A vendor that is configured, reachable, and simply has no row for
            # this ticker is still a refusal worth naming: otherwise a chain
            # where every member returns None fails silently.
            refusals.append(f"{provider.name}: no data for {ticker}")
        if refusals:
            raise ProviderError("; ".join(refusals))
        return None

    # `fetch_prices` / `fetch_news` are deliberately absent: bars and headlines
    # are not gated per symbol the way fundamentals rows are, and a half-filled
    # price series stitched from two vendors is a backtest bug, not coverage.

