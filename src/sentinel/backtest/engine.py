"""The backtester.

Two properties matter more than anything else in this file, and both are
tested rather than asserted in a comment.

**No lookahead.** A ranker is handed bars up to and including the decision date
and nothing after it, and the resulting orders fill at the *next* session's
open. Computing a signal on Monday's close and filling at Monday's close is the
single most common way a backtest invents returns that cannot be earned.

**The same risk layer runs.** Sizing, the 10% cap, sector concentration, the
swing sub-allocation and the kill switch are the live ``RiskEngine``, not a
simplified copy. A backtest of a strategy that ignores the risk limits is a
backtest of a strategy you are not allowed to run.

Costs come from the live ``CostModel`` and the ledger is the live ``Ledger``,
for the same reason: if backtest and paper trading disagree, that has to mean
the market disagreed, not that two sets of books disagreed.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Callable, Mapping, Sequence

from ..config import RiskLimits
from ..domain.enums import IdeaClass
from ..domain.models import Bar
from ..money import FxRates, dec
from ..portfolio.ledger import ClosedTrade, EquityPoint, InsufficientCash, Ledger, apply_exits
from ..risk import PortfolioState, RiskEngine
from ..risk.sizing import SizingCap, size
from ..costs import CostModel

BACKTEST_VERSION = "backtest-v1"

#: (decision_date, history) -> [(ticker, score)], best first.
Ranker = Callable[[dt.date, Mapping[str, list[Bar]]], Sequence[tuple[str, Decimal]]]


@dataclass(slots=True)
class BacktestConfig:
    starting_cash: Decimal = Decimal("10000")
    rebalance_days: int = 21
    max_positions: int = 8
    #: Stop distance as a fraction of the entry price, when the ranker gives none.
    stop_pct: Decimal = Decimal("0.10")
    idea_class: IdeaClass = IdeaClass.LONG_TERM
    min_score: Decimal = Decimal("60")
    warmup_bars: int = 250


@dataclass(slots=True)
class BacktestResult:
    dates: list[dt.date] = field(default_factory=list)
    equity: list[EquityPoint] = field(default_factory=list)
    trades: list[ClosedTrade] = field(default_factory=list)
    ledger: Ledger | None = None
    rejections: dict[str, int] = field(default_factory=dict)
    exposures: list[Decimal] = field(default_factory=list)

    @property
    def returns(self) -> list[Decimal]:
        out: list[Decimal] = []
        for prev, curr in zip(self.equity, self.equity[1:]):
            if prev.nav_gbp > 0:
                out.append(curr.nav_gbp / prev.nav_gbp - Decimal("1"))
        return out

    @property
    def nav_series(self) -> list[Decimal]:
        return [p.nav_gbp for p in self.equity]


def trading_dates(bars: Mapping[str, Sequence[Bar]]) -> list[dt.date]:
    dates: set[dt.date] = set()
    for series in bars.values():
        dates.update(b.date for b in series)
    return sorted(dates)


def _history_upto(bars: Mapping[str, Sequence[Bar]], date: dt.date) -> dict[str, list[Bar]]:
    """Bars up to and including ``date``. The one place lookahead could enter."""
    return {t: [b for b in series if b.date <= date] for t, series in bars.items()}


def _marks_on(bars: Mapping[str, Sequence[Bar]], date: dt.date) -> dict[str, Decimal]:
    """Last known adjusted close at or before ``date``, per ticker."""
    marks: dict[str, Decimal] = {}
    for ticker, series in bars.items():
        latest = None
        for bar in series:
            if bar.date > date:
                break
            latest = bar
        if latest is not None:
            marks[ticker] = latest.adjusted_close
    return marks


def _open_on(bars: Mapping[str, Sequence[Bar]], ticker: str, date: dt.date) -> Decimal | None:
    for bar in bars.get(ticker, ()):
        if bar.date == date:
            factor = bar.adjusted_close / bar.close if bar.close else Decimal("1")
            return (bar.open * factor).quantize(Decimal("0.0001"))
    return None


def run(
    bars: Mapping[str, Sequence[Bar]],
    ranker: Ranker,
    config: BacktestConfig | None = None,
    *,
    limits: RiskLimits | None = None,
    sectors: Mapping[str, str] | None = None,
    costs: CostModel | None = None,
    fx: FxRates | None = None,
    start: dt.date | None = None,
    end: dt.date | None = None,
) -> BacktestResult:
    config = config or BacktestConfig()
    limits = limits or RiskLimits()
    engine = RiskEngine(limits, sectors=sectors)
    ledger = Ledger(config.starting_cash, costs=costs or CostModel(), fx=fx)
    result = BacktestResult(ledger=ledger)

    all_dates = [d for d in trading_dates(bars) if (not start or d >= start) and (not end or d <= end)]
    if len(all_dates) <= config.warmup_bars:
        return result

    pending: list[tuple[str, Decimal, Decimal]] = []   # (ticker, score, stop_pct)
    since_rebalance = config.rebalance_days

    for index, date in enumerate(all_dates):
        marks = _marks_on(bars, date)

        # 1. Fill yesterday's decisions at today's open. Orders never fill at
        #    the price that generated them.
        for ticker, _score, stop_pct in pending:
            fill = _open_on(bars, ticker, date)
            if fill is None or fill <= 0:
                continue
            if any(p.ticker == ticker for p in ledger.open_positions):
                continue
            stop = (fill * (Decimal("1") - stop_pct)).quantize(Decimal("0.0001"))
            state = PortfolioState(
                satellite_capital=config.starting_cash, cash=ledger.cash,
                positions=list(ledger.positions.values()),
                nav=ledger.nav(marks), high_water_mark=ledger.high_water,
                fx=fx or FxRates.identity(), marks=marks,
            )
            plan_shares = _shares_for(engine, state, ticker, fill, stop, config, limits, fx)
            if plan_shares <= 0:
                result.rejections["risk_layer"] = result.rejections.get("risk_layer", 0) + 1
                continue
            try:
                ledger.open(
                    ticker=ticker, idea_id=f"bt-{ticker}-{date.isoformat()}",
                    idea_class=config.idea_class, sector=engine.sector_of(ticker),
                    shares=plan_shares, price=fill, date=date, stop=stop,
                    currency=bars[ticker][0].currency,
                )
            except InsufficientCash:
                result.rejections["cash"] = result.rejections.get("cash", 0) + 1
        pending = []

        # 2. Exits, at the stop level.
        result.trades.extend(apply_exits(ledger, marks, date))

        # 3. Decide, using history that stops at today.
        since_rebalance += 1
        if index >= config.warmup_bars and since_rebalance >= config.rebalance_days:
            since_rebalance = 0
            history = _history_upto(bars, date)
            held = {p.ticker for p in ledger.open_positions}
            room = config.max_positions - len(held)
            if room > 0:
                for ticker, score in ranker(date, history):
                    if room <= 0:
                        break
                    if ticker in held or score < config.min_score:
                        continue
                    pending.append((ticker, dec(score), config.stop_pct))
                    room -= 1

        # 4. Mark.
        result.equity.append(ledger.mark_to_market(date, marks))
        result.exposures.append(ledger.exposure_fraction(marks))
        result.dates.append(date)

    # Close anything still open at the final mark, so the trade statistics
    # describe the whole run rather than only the positions that happened to exit.
    final_marks = _marks_on(bars, all_dates[-1])
    for position in list(ledger.open_positions):
        price = final_marks.get(position.ticker, position.entry)
        result.trades.append(ledger.close(position.ticker, price=price, date=all_dates[-1]))
    return result


def _shares_for(
    engine: RiskEngine, state: PortfolioState, ticker: str, entry: Decimal,
    stop: Decimal, config: BacktestConfig, limits: RiskLimits, fx: FxRates | None,
) -> int:
    """Size through the same caps the live engine applies.

    A synthetic Idea is not built here: the engine's ``evaluate`` needs a memo
    and an invalidation string that a backtest has no business inventing, so
    the caps are assembled directly from the same limits. The sizing arithmetic
    itself is the shared ``risk.sizing.size``.
    """
    if engine.kill_switch_active(state) and config.idea_class is IdeaClass.SWING:
        return 0
    capital = state.satellite_capital
    sector = engine.sector_of(ticker)
    caps = [
        SizingCap("max_single_position", capital * limits.max_single_position_pct / Decimal("100")),
        SizingCap("max_sector_concentration",
                  capital * limits.max_sector_pct / Decimal("100") - state.sector_exposure_gbp(sector)),
        SizingCap("cash", state.cash),
    ]
    if config.idea_class is IdeaClass.SWING:
        caps.append(SizingCap(
            "swing_sub_allocation",
            capital * limits.swing_max_pct / Decimal("100")
            - state.class_exposure_gbp(IdeaClass.SWING),
        ))
    if len(state.open_positions) >= limits.max_open_positions:
        return 0
    sizing = size(
        entry=entry, stop=stop, satellite_capital=capital,
        risk_per_trade_pct=limits.risk_per_trade_pct, caps=tuple(caps),
    )
    if sizing.gbp_exposure < limits.min_position_gbp:
        return 0
    return sizing.shares
