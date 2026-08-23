"""Walk-forward evaluation.

The spec's line is "walk-forward splits, not one in-sample fit", and the reason
is worth stating plainly: any ranker with a tunable parameter can be made to
look excellent on a period you already have the answers for. A single
in-sample backtest measures how well you fitted, not how well the strategy
works.

Here, each fold hands the factory only the *training* window's bars and then
evaluates on a period the factory never saw. The out-of-sample equity curves are
chained — each fold starts with the cash the previous one ended with — so the
concatenated result is a real, compounding, tradeable sequence rather than an
average of independent experiments.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Callable, Mapping, Sequence

from ..config import RiskLimits
from ..domain.models import Bar
from ..money import FxRates
from ..costs import CostModel
from .engine import BacktestConfig, BacktestResult, Ranker, run, trading_dates

WALKFORWARD_VERSION = "walkforward-v1"

#: (train_bars, train_start, train_end) -> a ranker for the following test window.
RankerFactory = Callable[[Mapping[str, list[Bar]], dt.date, dt.date], Ranker]


@dataclass(slots=True)
class Fold:
    index: int
    train_start: dt.date
    train_end: dt.date
    test_start: dt.date
    test_end: dt.date
    result: BacktestResult | None = None


@dataclass(slots=True)
class WalkForwardResult:
    folds: list[Fold] = field(default_factory=list)
    equity: list[Decimal] = field(default_factory=list)
    returns: list[Decimal] = field(default_factory=list)
    trades: list[object] = field(default_factory=list)
    exposures: list[Decimal] = field(default_factory=list)
    dates: list[dt.date] = field(default_factory=list)

    @property
    def completed_folds(self) -> int:
        return sum(1 for f in self.folds if f.result is not None)


def make_folds(
    dates: Sequence[dt.date], *, folds: int, train_periods: int, test_periods: int
) -> list[Fold]:
    """Rolling windows: train on N periods, test on the M that follow, step by M.

    Rolling rather than expanding, so every fold trains on the same amount of
    history. An expanding window makes late folds look better simply because
    they had more data, which confounds "the strategy works" with "the strategy
    needs ten years of history".
    """
    out: list[Fold] = []
    cursor = 0
    for index in range(folds):
        train_end_index = cursor + train_periods
        test_end_index = train_end_index + test_periods
        if test_end_index > len(dates):
            break
        out.append(Fold(
            index=index,
            train_start=dates[cursor], train_end=dates[train_end_index - 1],
            test_start=dates[train_end_index], test_end=dates[test_end_index - 1],
        ))
        cursor += test_periods
    return out


def run_walk_forward(
    bars: Mapping[str, Sequence[Bar]],
    factory: RankerFactory,
    *,
    folds: int = 4,
    train_periods: int = 504,
    test_periods: int = 252,
    config: BacktestConfig | None = None,
    limits: RiskLimits | None = None,
    sectors: Mapping[str, str] | None = None,
    costs: CostModel | None = None,
    fx: FxRates | None = None,
) -> WalkForwardResult:
    config = config or BacktestConfig()
    dates = trading_dates(bars)
    plan = make_folds(dates, folds=folds, train_periods=train_periods, test_periods=test_periods)
    out = WalkForwardResult(folds=plan)

    cash = config.starting_cash
    for fold in plan:
        train_bars = {
            t: [b for b in series if fold.train_start <= b.date <= fold.train_end]
            for t, series in bars.items()
        }
        ranker = factory(train_bars, fold.train_start, fold.train_end)

        # The test window is fed the training history too, because indicators
        # need a warm-up — but the *decisions* start at test_start, which is
        # what `warmup_bars` enforces below.
        window = {
            t: [b for b in series if fold.train_start <= b.date <= fold.test_end]
            for t, series in bars.items()
        }
        warmup = len([d for d in trading_dates(window) if d < fold.test_start])
        fold_config = BacktestConfig(
            starting_cash=cash, rebalance_days=config.rebalance_days,
            max_positions=config.max_positions, stop_pct=config.stop_pct,
            idea_class=config.idea_class, min_score=config.min_score,
            warmup_bars=warmup,
        )
        result = run(window, ranker, fold_config, limits=limits, sectors=sectors,
                     costs=costs, fx=fx)
        fold.result = result

        oos = [p for p in result.equity if p.date >= fold.test_start]
        out.equity.extend(p.nav_gbp for p in oos)
        out.dates.extend(p.date for p in oos)
        for prev, curr in zip(oos, oos[1:]):
            if prev.nav_gbp > 0:
                out.returns.append(curr.nav_gbp / prev.nav_gbp - Decimal("1"))
        out.trades.extend(t for t in result.trades if t.closed_on >= fold.test_start)
        out.exposures.extend(
            e for e, d in zip(result.exposures, result.dates) if d >= fold.test_start
        )
        if oos:
            cash = oos[-1].nav_gbp
    return out
