"""The offline provider.

Every number this emits is generated from a hash of the ticker, so the same
ticker always produces the same series on any machine, forever. That property is
what the whole test suite and the reproducibility eval (§5.4) stand on — a
fixture that drifted between runs would make "rerunning a brief for a past date
produces identical deterministic outputs" untestable.

It is **not** a market simulator and must never be used to justify a strategy.
It exists so the pipeline can be exercised end-to-end with an empty ``.env``.
The regimes below (a 2020-style crash, a 2022-style grind down) are scripted in
purely so that backtest and drawdown code meets a real drawdown in tests.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import math
import random
from decimal import Decimal
from typing import Iterable

from ..domain.enums import Wrapper
from ..domain.models import Bar, Fundamentals, NewsItem
from ..money import dec
from .base import currency_for

FIXTURE_VERSION = "fixture-v1"

# Scripted regimes, so a 10-year fixture backtest actually contains the two
# episodes the spec insists on covering.
_CRASH = (dt.date(2020, 2, 20), dt.date(2020, 3, 23), -0.0180)
_RECOVERY = (dt.date(2020, 3, 24), dt.date(2020, 8, 31), 0.0075)
_BEAR = (dt.date(2022, 1, 4), dt.date(2022, 10, 12), -0.0022)


def _seed(*parts: str) -> int:
    return int(hashlib.sha256("|".join(parts).encode()).hexdigest()[:12], 16)


def _regime_drift(day: dt.date) -> float:
    for start, end, drift in (_CRASH, _RECOVERY, _BEAR):
        if start <= day <= end:
            return drift
    return 0.0


def weekdays(start: dt.date, end: dt.date) -> Iterable[dt.date]:
    day = start
    while day <= end:
        if day.weekday() < 5:
            yield day
        day += dt.timedelta(days=1)


class FixtureProvider:
    """Prices, fundamentals and news, all deterministic, all offline."""

    name = "fixture"

    def __init__(self, *, base_price: Decimal = Decimal("100")) -> None:
        self.base_price = base_price

    def available(self) -> bool:
        return True

    # -- prices -----------------------------------------------------------
    def fetch_bars(self, ticker: str, start: dt.date, end: dt.date) -> list[Bar]:
        rng = random.Random(_seed(ticker, "prices"))
        currency = currency_for(ticker)
        # Per-ticker personality: some names drift up, some sideways, some down,
        # with different volatilities. Fixed by the hash, not by the clock.
        drift = (rng.random() - 0.42) * 0.0012
        vol = 0.008 + rng.random() * 0.016
        price = float(self.base_price) * (0.5 + rng.random() * 1.5)
        bars: list[Bar] = []
        for day in weekdays(start, end):
            shock = rng.gauss(0, 1) * vol
            price *= math.exp(drift + _regime_drift(day) + shock)
            price = max(price, 0.5)
            intraday = abs(rng.gauss(0, 1)) * vol * price
            open_ = price * (1 + rng.gauss(0, 1) * vol * 0.3)
            high = max(open_, price) + intraday * 0.6
            low = min(open_, price) - intraday * 0.6
            low = max(low, 0.01)
            open_ = min(max(open_, low), high)
            volume = int(500_000 * (1 + abs(rng.gauss(0, 1))))
            bars.append(
                Bar(
                    ticker=ticker, date=day,
                    open=_r(open_), high=_r(high), low=_r(low), close=_r(price),
                    adjusted_close=_r(price), volume=volume, currency=currency,
                )
            )
        return bars

    # -- fundamentals -----------------------------------------------------
    def fetch_fundamentals(self, ticker: str) -> Fundamentals | None:
        rng = random.Random(_seed(ticker, "fundamentals"))
        sector = ("technology", "consumer", "healthcare", "industrials", "financials")[
            _seed(ticker, "sector") % 5
        ]
        revenue = Decimal(str(round(2_000_000 * (1 + rng.random() * 40), 2)))
        growth = dec(round(rng.uniform(-0.12, 0.35), 4))
        prior_revenue = (revenue / (1 + growth)).quantize(Decimal("0.01"))
        net_margin = dec(round(rng.uniform(-0.05, 0.28), 4))
        net_income = (revenue * net_margin).quantize(Decimal("0.01"))
        prior_net_income = (prior_revenue * net_margin * dec(round(rng.uniform(0.7, 1.2), 4))).quantize(Decimal("0.01"))
        equity = (revenue * dec(round(rng.uniform(0.4, 1.6), 3))).quantize(Decimal("0.01"))
        shares = Decimal(str(rng.randrange(20_000_000, 400_000_000)))
        return Fundamentals(
            ticker=ticker,
            as_of=dt.date(2024, 6, 30),
            currency=currency_for(ticker),
            sector=sector,
            market_cap=(revenue * dec(round(rng.uniform(0.8, 6.0), 3))).quantize(Decimal("0.01")),
            revenue_ttm=revenue, revenue_prior_ttm=prior_revenue,
            eps_ttm=(net_income / shares).quantize(Decimal("0.0001")),
            eps_prior_ttm=(prior_net_income / shares).quantize(Decimal("0.0001")),
            gross_margin=dec(round(rng.uniform(0.15, 0.75), 4)),
            operating_margin=dec(round(rng.uniform(-0.05, 0.35), 4)),
            net_margin=net_margin,
            free_cash_flow_ttm=(net_income * dec(round(rng.uniform(0.4, 1.6), 3))).quantize(Decimal("0.01")),
            operating_cash_flow_ttm=(net_income * dec(round(rng.uniform(0.8, 2.0), 3))).quantize(Decimal("0.01")),
            total_debt=(equity * dec(round(rng.uniform(0.0, 1.8), 3))).quantize(Decimal("0.01")),
            total_equity=equity,
            total_assets=(equity * dec("2.1")).quantize(Decimal("0.01")),
            total_assets_prior=(equity * dec("2.0")).quantize(Decimal("0.01")),
            current_assets=(equity * dec("0.8")).quantize(Decimal("0.01")),
            current_liabilities=(equity * dec(round(rng.uniform(0.2, 0.9), 3))).quantize(Decimal("0.01")),
            current_assets_prior=(equity * dec("0.75")).quantize(Decimal("0.01")),
            current_liabilities_prior=(equity * dec("0.45")).quantize(Decimal("0.01")),
            shares_outstanding=shares,
            shares_outstanding_prior=shares * dec(round(rng.uniform(0.97, 1.05), 4)),
            net_income_ttm=net_income, net_income_prior_ttm=prior_net_income,
            pe_ratio=dec(round(rng.uniform(6, 45), 2)),
            pe_5y_median=dec(round(rng.uniform(8, 35), 2)),
            pe_sector_median=dec(round(rng.uniform(9, 30), 2)),
            ev_ebitda=dec(round(rng.uniform(4, 25), 2)),
            ev_ebitda_sector_median=dec(round(rng.uniform(6, 20), 2)),
            next_earnings_date=dt.date(2024, 8, 1) + dt.timedelta(days=_seed(ticker, "earnings") % 90),
            wrapper=Wrapper.ISA_ELIGIBLE,
        )

    # -- news -------------------------------------------------------------
    _TEMPLATES = (
        ("{t} reports Q{q} revenue ahead of consensus", "Revenue up {pct}% year on year; management raised full-year guidance."),
        ("{t} shares slide after cautious outlook", "The board flagged softening demand into the second half."),
        ("{t} announces £{amt}m share buyback", "Funded from free cash flow; no change to the dividend policy."),
        ("Regulator opens review into {t} pricing", "No fine proposed at this stage; the company says it will cooperate."),
        ("{t} names new chief financial officer", "The incoming CFO joins from a larger listed peer."),
        ("Broker upgrades {t} to buy", "Price target raised on improving margin trajectory."),
    )

    def fetch_news(self, ticker: str, since: dt.datetime) -> list[NewsItem]:
        rng = random.Random(_seed(ticker, "news", since.date().isoformat()))
        now = dt.datetime.now(dt.UTC)
        base = ticker.split(".")[0].title()
        items: list[NewsItem] = []
        for i in range(rng.randrange(2, 6)):
            headline, summary = self._TEMPLATES[rng.randrange(len(self._TEMPLATES))]
            published = now - dt.timedelta(hours=rng.randrange(1, 24 * 12))
            if published < since:
                continue
            fields = {
                "t": base, "q": rng.randrange(1, 5), "pct": rng.randrange(2, 30),
                "amt": rng.randrange(20, 400),
            }
            items.append(
                NewsItem(
                    ticker=ticker,
                    published_at=published,
                    headline=headline.format(**fields),
                    summary=summary.format(**fields),
                    source="fixture-wire",
                    url=f"https://fixtures.invalid/{ticker}/{i}",
                )
            )
        return items


def _r(value: float) -> Decimal:
    return dec(round(value, 4))
