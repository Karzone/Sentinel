"""B1-B4.

B4 is the one that earns its place. Beating a global index over one period can
be luck; beating the *median of a thousand random portfolios drawn from the same
universe with the same cadence* is the test that separates a signal from a
coin that landed well. If Sentinel cannot clear that bar it has no demonstrated
skill, whatever it did against B1, and §5.5's kill criteria should bite.

Everything is reported in GBP total-return terms, net of the same cost model the
strategy pays. Comparing a net-of-costs strategy against a gross benchmark is
a quiet way to lose the comparison and call it a win.
"""

from __future__ import annotations

import datetime as dt
import random
from dataclasses import dataclass
from decimal import Decimal
from typing import Mapping, Sequence

from ..domain.models import Bar
from ..money import dec
from .costs import CostModel, compute
from .engine import trading_dates

BENCHMARKS_VERSION = "benchmarks-v1"


@dataclass(frozen=True, slots=True)
class BenchmarkSeries:
    name: str
    label: str
    returns: list[Decimal]
    equity: list[Decimal]

    @property
    def total_return(self) -> Decimal:
        if not self.equity or self.equity[0] == 0:
            return Decimal("0")
        return self.equity[-1] / self.equity[0] - Decimal("1")


def buy_and_hold(
    bars: Sequence[Bar], *, name: str, label: str, starting: Decimal = Decimal("10000"),
    fx_rate: Decimal = Decimal("1"),
) -> BenchmarkSeries:
    """B1/B2: hold the index, in GBP, total return.

    Adjusted closes, so dividends are reinvested — comparing a total-return
    strategy against a price-only index would hand the strategy roughly 2-4% a
    year it did not earn.
    """
    if not bars:
        return BenchmarkSeries(name, label, [], [])
    equity: list[Decimal] = []
    returns: list[Decimal] = []
    base = bars[0].adjusted_close
    for bar in bars:
        equity.append((starting * bar.adjusted_close / base * fx_rate).quantize(Decimal("0.01")))
    for prev, curr in zip(equity, equity[1:]):
        returns.append(curr / prev - Decimal("1") if prev > 0 else Decimal("0"))
    return BenchmarkSeries(name, label, returns, equity)


def cash(
    periods: int, *, annual_rate: Decimal = Decimal("0.045"),
    starting: Decimal = Decimal("10000"), periods_per_year: int = 252,
) -> BenchmarkSeries:
    """B3: the absolute floor. If the strategy cannot beat this it is a hobby."""
    per_period = annual_rate / Decimal(periods_per_year)
    equity = [starting]
    returns: list[Decimal] = []
    for _ in range(max(periods - 1, 0)):
        equity.append((equity[-1] * (Decimal("1") + per_period)).quantize(Decimal("0.01")))
        returns.append(per_period)
    return BenchmarkSeries("B3", f"Cash at {annual_rate:.1%}", returns, equity)


@dataclass(frozen=True, slots=True)
class MonteCarloResult:
    portfolios: int
    median_return: Decimal
    percentiles: dict[int, Decimal]
    strategy_return: Decimal | None = None
    strategy_percentile: float | None = None

    @property
    def beats_median(self) -> bool | None:
        if self.strategy_return is None:
            return None
        return self.strategy_return > self.median_return

    def verdict(self) -> str:
        if self.strategy_return is None:
            return f"{self.portfolios} random portfolios, median {self.median_return:+.1%}"
        if self.strategy_percentile is None:
            return "no verdict"
        standing = f"{self.strategy_percentile:.0f}th percentile of {self.portfolios} random portfolios"
        if self.strategy_percentile < 50:
            return (f"{standing} — worse than picking at random. On this evidence the "
                    "selection adds nothing.")
        if self.strategy_percentile < 80:
            return (f"{standing} — ahead of the median, but inside the range luck alone "
                    "produces. Not yet evidence of skill.")
        return f"{standing} — outside what random selection produced on this sample."


def random_portfolios(
    bars: Mapping[str, Sequence[Bar]],
    *,
    portfolios: int = 1000,
    holdings: int = 8,
    rebalance_days: int = 21,
    starting: Decimal = Decimal("10000"),
    costs: CostModel | None = None,
    seed: int = 20260101,
    start: dt.date | None = None,
    end: dt.date | None = None,
) -> MonteCarloResult:
    """B4: equal-weight portfolios drawn at random from the same universe.

    Seeded, so the same universe always produces the same distribution. An
    unseeded Monte Carlo would let a rerun quietly move the bar the strategy is
    being measured against.
    """
    costs = costs or CostModel()
    dates = [d for d in trading_dates(bars) if (not start or d >= start) and (not end or d <= end)]
    tickers = [t for t, series in bars.items() if len(series) > 1]
    if len(dates) < 2 or len(tickers) < holdings:
        return MonteCarloResult(0, Decimal("0"), {})

    prices: dict[str, dict[dt.date, Decimal]] = {
        t: {b.date: b.adjusted_close for b in bars[t]} for t in tickers
    }
    rebalance_dates = dates[::rebalance_days] or [dates[0]]
    # One-way cost as a fraction, applied at each rebalance to the whole book —
    # a random portfolio pays to trade exactly as the strategy does.
    sample = compute(costs, ticker="X.LSE", shares=1,
                     price=starting / Decimal(holdings), is_buy=True)
    turnover_drag = sample.total_gbp / (starting / Decimal(holdings))

    rng = random.Random(seed)
    finals: list[Decimal] = []
    for _ in range(portfolios):
        value = starting
        for i, rebalance_date in enumerate(rebalance_dates):
            picks = rng.sample(tickers, holdings)
            next_date = rebalance_dates[i + 1] if i + 1 < len(rebalance_dates) else dates[-1]
            growth = Decimal("0")
            counted = 0
            for ticker in picks:
                begin = prices[ticker].get(rebalance_date)
                finish = prices[ticker].get(next_date)
                if begin and finish and begin > 0:
                    growth += finish / begin
                    counted += 1
            if counted:
                value = value * (growth / Decimal(counted)) * (Decimal("1") - turnover_drag)
        finals.append(value / starting - Decimal("1"))

    finals.sort()
    def percentile(p: int) -> Decimal:
        index = min(len(finals) - 1, max(0, int(round((p / 100) * (len(finals) - 1)))))
        return finals[index].quantize(Decimal("0.0001"))

    return MonteCarloResult(
        portfolios=len(finals),
        median_return=percentile(50),
        percentiles={p: percentile(p) for p in (5, 25, 50, 75, 95)},
    )


def place_strategy(result: MonteCarloResult, strategy_return: Decimal,
                   distribution: Sequence[Decimal]) -> MonteCarloResult:
    """Where the strategy's return falls in the random distribution."""
    if not distribution:
        return result
    below = sum(1 for value in distribution if value < strategy_return)
    return MonteCarloResult(
        portfolios=result.portfolios, median_return=result.median_return,
        percentiles=result.percentiles, strategy_return=dec(strategy_return),
        strategy_percentile=100.0 * below / len(distribution),
    )
