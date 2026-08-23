"""Performance metrics.

§5.1 is explicit that risk-adjusted return is the headline number, not raw
return, so ``summarise`` puts Sharpe and Sortino first and the total return
after them. That ordering is not cosmetic — a 40% year at triple the volatility
of the benchmark is not a better year, and a report that leads with the 40%
invites exactly the conclusion the kill criteria in §5.5 exist to prevent.

Metrics are computed in float. These are dimensionless ratios that never become
a pound; the Decimal boundary is money (see money.py), and returns arrive here
as Decimal and are converted once at the door.
"""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass
from decimal import Decimal
from typing import Sequence

METRICS_VERSION = "metrics-v1"

TRADING_DAYS = 252
#: B3, the cash floor: a money-market yield the strategy must beat to justify
#: any risk at all.
DEFAULT_RISK_FREE_ANNUAL = 0.045


def _floats(values: Sequence[Decimal | float]) -> list[float]:
    return [float(v) for v in values]


def annualisation_factor(periods_per_year: int) -> float:
    return math.sqrt(periods_per_year)


def mean(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def stdev(values: Sequence[float]) -> float:
    """Sample standard deviation (ddof=1).

    ddof=1 rather than 0 because a backtest is a *sample* of the return
    distribution, not the population. With 24 monthly observations the
    difference is about 2% on the Sharpe — small, but it flatters, and every
    small flattering choice compounds into an unjustified deployment.
    """
    if len(values) < 2:
        return 0.0
    m = mean(values)
    return math.sqrt(sum((v - m) ** 2 for v in values) / (len(values) - 1))


def total_return(returns: Sequence[Decimal | float]) -> float:
    """Compounded, not summed."""
    total = 1.0
    for r in _floats(returns):
        total *= 1.0 + r
    return total - 1.0


def cagr(returns: Sequence[Decimal | float], *, periods_per_year: int = TRADING_DAYS) -> float:
    values = _floats(returns)
    if not values:
        return 0.0
    years = len(values) / periods_per_year
    if years <= 0:
        return 0.0
    growth = 1.0 + total_return(values)
    if growth <= 0:
        return -1.0
    return growth ** (1.0 / years) - 1.0


def sharpe(
    returns: Sequence[Decimal | float],
    *,
    periods_per_year: int = TRADING_DAYS,
    risk_free_annual: float = DEFAULT_RISK_FREE_ANNUAL,
) -> float | None:
    """None when there is not enough data, never 0.0.

    A Sharpe of 0.0 and "we cannot compute a Sharpe from four observations" mean
    completely different things to someone deciding whether to fund a strategy.
    """
    values = _floats(returns)
    if len(values) < 2:
        return None
    rf_per_period = risk_free_annual / periods_per_year
    excess = [v - rf_per_period for v in values]
    sd = stdev(excess)
    if sd == 0:
        return None
    return (mean(excess) / sd) * annualisation_factor(periods_per_year)


def sortino(
    returns: Sequence[Decimal | float],
    *,
    periods_per_year: int = TRADING_DAYS,
    risk_free_annual: float = DEFAULT_RISK_FREE_ANNUAL,
) -> float | None:
    """Sortino penalises downside deviation only.

    The downside deviation divides by the *full* sample count, not by the count
    of negative periods. Dividing by the negative count is a common
    implementation error and inflates the ratio for strategies that rarely lose
    — precisely the strategies where an inflated number is most dangerous.
    """
    values = _floats(returns)
    if len(values) < 2:
        return None
    rf_per_period = risk_free_annual / periods_per_year
    excess = [v - rf_per_period for v in values]
    downside = [min(0.0, v) for v in excess]
    dd = math.sqrt(sum(v * v for v in downside) / len(excess))
    if dd == 0:
        return None
    return (mean(excess) / dd) * annualisation_factor(periods_per_year)


@dataclass(frozen=True, slots=True)
class Drawdown:
    max_drawdown: float
    peak_index: int
    trough_index: int
    #: Periods from peak to trough.
    depth_periods: int
    #: Periods from peak back to a new high; None if never recovered.
    recovery_periods: int | None

    @property
    def duration_periods(self) -> int:
        """Peak to recovery, or peak to the end of the sample if still under."""
        return self.depth_periods + (self.recovery_periods or 0)


def max_drawdown(equity: Sequence[Decimal | float]) -> Drawdown:
    """Deepest peak-to-trough fall, with how long it lasted.

    Duration matters as much as depth: a 20% drawdown recovered in a month and a
    20% drawdown that takes three years are the same number and completely
    different experiences, and only one of them gets abandoned at the bottom.
    """
    values = _floats(equity)
    if not values:
        return Drawdown(0.0, 0, 0, 0, None)

    peak = values[0]
    peak_index = 0
    worst = 0.0
    worst_peak = 0
    worst_trough = 0
    for i, value in enumerate(values):
        if value > peak:
            peak, peak_index = value, i
        drop = 0.0 if peak <= 0 else (value / peak) - 1.0
        if drop < worst:
            worst, worst_peak, worst_trough = drop, peak_index, i

    recovery: int | None = None
    if worst < 0:
        target = values[worst_peak]
        for i in range(worst_trough + 1, len(values)):
            if values[i] >= target:
                recovery = i - worst_trough
                break
    return Drawdown(
        max_drawdown=worst, peak_index=worst_peak, trough_index=worst_trough,
        depth_periods=worst_trough - worst_peak, recovery_periods=recovery,
    )


def volatility(returns: Sequence[Decimal | float], *, periods_per_year: int = TRADING_DAYS) -> float:
    return stdev(_floats(returns)) * annualisation_factor(periods_per_year)


def beta(returns: Sequence[Decimal | float], benchmark: Sequence[Decimal | float]) -> float | None:
    a, b = _floats(returns), _floats(benchmark)
    n = min(len(a), len(b))
    if n < 2:
        return None
    a, b = a[-n:], b[-n:]
    mb = mean(b)
    variance = sum((v - mb) ** 2 for v in b) / (n - 1)
    if variance == 0:
        return None
    ma = mean(a)
    covariance = sum((a[i] - ma) * (b[i] - mb) for i in range(n)) / (n - 1)
    return covariance / variance


def alpha(
    returns: Sequence[Decimal | float],
    benchmark: Sequence[Decimal | float],
    *,
    periods_per_year: int = TRADING_DAYS,
    risk_free_annual: float = DEFAULT_RISK_FREE_ANNUAL,
) -> float | None:
    """Annualised Jensen's alpha — §5.1's "signal or just being long?" question.

    A strategy that is 60% invested in a rising market will beat a cash-heavy
    benchmark on raw return while adding nothing. Alpha is what is left after
    paying for the beta it took.
    """
    b = beta(returns, benchmark)
    if b is None:
        return None
    n = min(len(returns), len(benchmark))
    r = mean(_floats(returns)[-n:])
    m = mean(_floats(benchmark)[-n:])
    rf = risk_free_annual / periods_per_year
    return ((r - rf) - b * (m - rf)) * periods_per_year


@dataclass(frozen=True, slots=True)
class TradeStats:
    trades: int
    wins: int
    losses: int
    win_rate: float | None
    average_win: float
    average_loss: float
    win_loss_ratio: float | None
    profit_factor: float | None
    average_holding_days: float | None

    @property
    def expectancy(self) -> float | None:
        """Expected P&L per trade, in units of the average loss.

        The number that makes a win rate interpretable: 40% winners with a
        3:1 win/loss ratio is a good system, and 70% winners with a 1:4 ratio is
        not, and only expectancy says so in one figure.
        """
        if self.win_rate is None or self.win_loss_ratio is None:
            return None
        return self.win_rate * self.win_loss_ratio - (1 - self.win_rate)


def trade_stats(trades: Sequence[object]) -> TradeStats:
    """Accepts anything with ``net_pnl_gbp``, ``is_win`` and ``holding_days``."""
    if not trades:
        return TradeStats(0, 0, 0, None, 0.0, 0.0, None, None, None)

    pnls = [float(getattr(t, "net_pnl_gbp")) for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    average_win = mean(wins) if wins else 0.0
    average_loss = abs(mean(losses)) if losses else 0.0
    holding = [float(getattr(t, "holding_days")) for t in trades if getattr(t, "holding_days", None) is not None]

    return TradeStats(
        trades=len(trades), wins=len(wins), losses=len(losses),
        win_rate=len(wins) / len(pnls),
        average_win=average_win, average_loss=average_loss,
        win_loss_ratio=(average_win / average_loss) if average_loss > 0 else None,
        # No losing trades at all makes profit factor infinite, which is a
        # small-sample artefact rather than a result. None says so.
        profit_factor=(gross_profit / gross_loss) if gross_loss > 0 else None,
        average_holding_days=mean(holding) if holding else None,
    )


@dataclass(frozen=True, slots=True)
class PerformanceSummary:
    periods: int
    total_return: float
    cagr: float
    sharpe: float | None
    sortino: float | None
    volatility: float
    max_drawdown: float
    drawdown_periods: int
    recovered: bool
    alpha_vs_benchmark: float | None
    beta_vs_benchmark: float | None
    average_exposure: float | None
    trades: TradeStats

    def headline(self) -> str:
        def fmt(value: float | None, suffix: str = "") -> str:
            return "n/a" if value is None else f"{value:.2f}{suffix}"

        return (
            f"Sharpe {fmt(self.sharpe)} · Sortino {fmt(self.sortino)} · "
            f"return {self.total_return:+.1%} · max drawdown {self.max_drawdown:.1%} · "
            f"{self.periods} periods"
        )


def summarise(
    returns: Sequence[Decimal | float],
    equity: Sequence[Decimal | float],
    *,
    benchmark_returns: Sequence[Decimal | float] | None = None,
    trades: Sequence[object] = (),
    exposures: Sequence[Decimal | float] | None = None,
    periods_per_year: int = TRADING_DAYS,
    risk_free_annual: float = DEFAULT_RISK_FREE_ANNUAL,
) -> PerformanceSummary:
    dd = max_drawdown(equity)
    return PerformanceSummary(
        periods=len(returns),
        total_return=total_return(returns),
        cagr=cagr(returns, periods_per_year=periods_per_year),
        sharpe=sharpe(returns, periods_per_year=periods_per_year, risk_free_annual=risk_free_annual),
        sortino=sortino(returns, periods_per_year=periods_per_year, risk_free_annual=risk_free_annual),
        volatility=volatility(returns, periods_per_year=periods_per_year),
        max_drawdown=dd.max_drawdown,
        drawdown_periods=dd.duration_periods,
        recovered=dd.recovery_periods is not None or dd.max_drawdown == 0,
        alpha_vs_benchmark=(
            alpha(returns, benchmark_returns, periods_per_year=periods_per_year,
                  risk_free_annual=risk_free_annual) if benchmark_returns else None
        ),
        beta_vs_benchmark=beta(returns, benchmark_returns) if benchmark_returns else None,
        average_exposure=mean(_floats(exposures)) if exposures else None,
        trades=trade_stats(trades),
    )
