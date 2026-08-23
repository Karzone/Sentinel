"""Financial Modeling Prep adapter — fundamentals.

FMP splits what Phase 2 needs across four endpoints (income statement, balance
sheet, cash flow, TTM ratios), so ``assemble`` takes all four decoded payloads
and produces one point-in-time snapshot. Keeping it a pure function means the
Piotroski inputs — which need *prior period* balance-sheet lines and are the
easiest thing in the file to get subtly wrong — are testable directly.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Any, Sequence

import httpx

from ..config import api_key
from ..domain.models import Fundamentals
from ..money import dec
from .base import ProviderError, currency_for

BASE_URL = "https://financialmodelingprep.com/api/v3"
ADAPTER_VERSION = "fmp-v1"


def _num(source: dict[str, Any] | None, key: str) -> Decimal | None:
    if not source:
        return None
    value = source.get(key)
    return None if value in (None, "", "NA") else dec(value)


def _at(rows: Sequence[dict[str, Any]], index: int) -> dict[str, Any] | None:
    return rows[index] if len(rows) > index else None


def assemble(
    ticker: str,
    income: Sequence[dict[str, Any]],
    balance: Sequence[dict[str, Any]],
    cashflow: Sequence[dict[str, Any]],
    ratios_ttm: Sequence[dict[str, Any]],
    *,
    profile: Sequence[dict[str, Any]] = (),
) -> Fundamentals | None:
    """FMP payloads (newest first, as FMP returns them) -> one snapshot."""
    if not income:
        return None
    latest_income, prior_income = _at(income, 0), _at(income, 1)
    latest_balance, prior_balance = _at(balance, 0), _at(balance, 1)
    latest_cash = _at(cashflow, 0)
    ratios = _at(ratios_ttm, 0) or {}
    prof = _at(profile, 0) or {}

    # `date` on an FMP statement is the period end. `fillingDate` (their
    # spelling) is when it became public — the only one of the two that is safe
    # to use as `as_of`, because scoring a period the day it ended is lookahead.
    as_of_raw = (latest_income or {}).get("fillingDate") or (latest_income or {}).get("date")
    try:
        as_of = dt.date.fromisoformat(str(as_of_raw)[:10])
    except (ValueError, TypeError):
        as_of = dt.date.today()

    revenue = _num(latest_income, "revenue")
    net_income = _num(latest_income, "netIncome")
    equity = _num(latest_balance, "totalStockholdersEquity")

    return Fundamentals(
        ticker=ticker,
        as_of=as_of,
        currency=(prof.get("currency") or currency_for(ticker)),
        sector=(prof.get("sector") or "").lower() or None,
        market_cap=_num(prof, "mktCap"),
        revenue_ttm=revenue,
        revenue_prior_ttm=_num(prior_income, "revenue"),
        eps_ttm=_num(latest_income, "epsdiluted") or _num(latest_income, "eps"),
        eps_prior_ttm=_num(prior_income, "epsdiluted") or _num(prior_income, "eps"),
        gross_margin=_safe_ratio(_num(latest_income, "grossProfit"), revenue),
        operating_margin=_safe_ratio(_num(latest_income, "operatingIncome"), revenue),
        net_margin=_safe_ratio(net_income, revenue),
        free_cash_flow_ttm=_num(latest_cash, "freeCashFlow"),
        operating_cash_flow_ttm=_num(latest_cash, "operatingCashFlow"),
        total_debt=_num(latest_balance, "totalDebt"),
        total_equity=equity,
        total_assets=_num(latest_balance, "totalAssets"),
        total_assets_prior=_num(prior_balance, "totalAssets"),
        current_assets=_num(latest_balance, "totalCurrentAssets"),
        current_liabilities=_num(latest_balance, "totalCurrentLiabilities"),
        current_assets_prior=_num(prior_balance, "totalCurrentAssets"),
        current_liabilities_prior=_num(prior_balance, "totalCurrentLiabilities"),
        shares_outstanding=_num(latest_income, "weightedAverageShsOutDil"),
        shares_outstanding_prior=_num(prior_income, "weightedAverageShsOutDil"),
        net_income_ttm=net_income,
        net_income_prior_ttm=_num(prior_income, "netIncome"),
        pe_ratio=_num(ratios, "peRatioTTM"),
        ev_ebitda=_num(ratios, "enterpriseValueMultipleTTM"),
    )


def _safe_ratio(numerator: Decimal | None, denominator: Decimal | None) -> Decimal | None:
    if numerator is None or denominator is None or denominator == 0:
        return None
    return (numerator / denominator).quantize(Decimal("0.0001"))


class FmpProvider:
    name = "fmp"

    def __init__(self, token: str | None = None, *, client: httpx.Client | None = None) -> None:
        self._token = token or api_key("FMP_API_KEY")
        self._client = client

    def available(self) -> bool:
        return bool(self._token)

    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        if not self.available():
            raise ProviderError("FMP_API_KEY is not set")
        client = self._client or httpx.Client(timeout=30)
        try:
            response = client.get(f"{BASE_URL}/{path}", params={**(params or {}), "apikey": self._token})
            response.raise_for_status()
            return response.json()
        except httpx.HTTPError as exc:
            raise ProviderError(f"FMP request failed: {exc}") from exc
        finally:
            if self._client is None:
                client.close()

    def fetch_fundamentals(self, ticker: str) -> Fundamentals | None:
        symbol = ticker.split(".")[0]
        limit = {"limit": 2}
        return assemble(
            ticker,
            self._get(f"income-statement/{symbol}", limit),
            self._get(f"balance-sheet-statement/{symbol}", limit),
            self._get(f"cash-flow-statement/{symbol}", limit),
            self._get(f"ratios-ttm/{symbol}"),
            profile=self._get(f"profile/{symbol}"),
        )
