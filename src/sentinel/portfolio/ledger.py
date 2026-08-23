"""The paper-trading ledger.

In-memory and pure Decimal, with persistence as a separate step, so the
backtester and the live paper account run the *same* accounting code. If the
backtest had its own bookkeeping, a discrepancy between backtest and paper
results would be ambiguous — strategy drift or an accounting bug? — and §5.1's
comparison of the two would prove nothing.

Cash is decremented by costs on the way in and on the way out, so NAV is always
net of dealing costs. There is no separate "gross" curve to be tempted by.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Mapping, Sequence

from ..backtest.costs import CostModel, TradeCosts, compute
from ..domain.enums import IdeaClass, PositionStatus
from ..domain.models import Fill, Position
from ..money import GBP, FxRates, dec

LEDGER_VERSION = "ledger-v1"


class InsufficientCash(ValueError):
    pass


@dataclass(slots=True)
class EquityPoint:
    date: dt.date
    nav_gbp: Decimal
    cash_gbp: Decimal
    high_water_gbp: Decimal
    positions: int

    @property
    def drawdown(self) -> Decimal:
        if self.high_water_gbp <= 0:
            return Decimal("0")
        return (self.high_water_gbp - self.nav_gbp) / self.high_water_gbp


@dataclass(slots=True)
class ClosedTrade:
    """One completed round trip, which is the unit §5.1's win rate counts."""

    ticker: str
    idea_id: str
    idea_class: IdeaClass
    opened_on: dt.date
    closed_on: dt.date
    shares: int
    entry: Decimal
    exit: Decimal
    currency: str
    fx_rate: Decimal
    costs_gbp: Decimal
    status: PositionStatus

    @property
    def gross_pnl_gbp(self) -> Decimal:
        return (self.exit - self.entry) * Decimal(self.shares) * self.fx_rate

    @property
    def net_pnl_gbp(self) -> Decimal:
        return self.gross_pnl_gbp - self.costs_gbp

    @property
    def return_pct(self) -> Decimal:
        basis = self.entry * Decimal(self.shares) * self.fx_rate
        return Decimal("0") if basis == 0 else self.net_pnl_gbp / basis

    @property
    def holding_days(self) -> int:
        return (self.closed_on - self.opened_on).days

    @property
    def is_win(self) -> bool:
        return self.net_pnl_gbp > 0


class Ledger:
    def __init__(
        self,
        starting_cash: Decimal,
        *,
        costs: CostModel | None = None,
        fx: FxRates | None = None,
    ) -> None:
        self.starting_cash = dec(starting_cash)
        self.cash = dec(starting_cash)
        self.costs_model = costs or CostModel()
        self.fx = fx or FxRates.identity()
        self.positions: dict[str, Position] = {}
        self.closed: list[ClosedTrade] = []
        self.fills: list[Fill] = []
        self.equity: list[EquityPoint] = []
        self.high_water = dec(starting_cash)
        self.total_costs = Decimal("0")

    # ---------------------------------------------------------------- helpers
    def _fx_for(self, currency: str) -> Decimal:
        if currency == GBP:
            return Decimal("1")
        return dec(self.fx.rates.get(currency, Decimal("1")))

    @property
    def open_positions(self) -> list[Position]:
        return [p for p in self.positions.values() if p.is_open]

    # ---------------------------------------------------------------- trading
    def open(
        self,
        *,
        ticker: str,
        idea_id: str,
        idea_class: IdeaClass,
        sector: str,
        shares: int,
        price: Decimal,
        date: dt.date,
        stop: Decimal | None = None,
        invalidation: str = "",
        currency: str = GBP,
    ) -> Position:
        if shares <= 0:
            raise ValueError("cannot open a position of zero shares")
        if ticker in self.positions and self.positions[ticker].is_open:
            raise ValueError(f"already holding {ticker}")

        fx_rate = self._fx_for(currency)
        costs = compute(self.costs_model, ticker=ticker, shares=shares, price=price,
                        currency=currency, fx_rate=fx_rate, is_buy=True)
        outlay = dec(shares) * price * fx_rate + costs.total_gbp
        if outlay > self.cash:
            raise InsufficientCash(
                f"{ticker}: {outlay} needed including {costs.total_gbp} of costs, "
                f"{self.cash} available"
            )

        self.cash -= outlay
        self.total_costs += costs.total_gbp
        position = Position(
            ticker=ticker, idea_id=idea_id, idea_class=idea_class, sector=sector,
            opened_on=date, shares=shares, entry=price, currency=currency,
            fx_rate_at_entry=fx_rate, stop=stop, invalidation=invalidation,
            status=PositionStatus.OPEN,
        )
        self.positions[ticker] = position
        self._record_fill(position, date, shares, price, currency, fx_rate, costs)
        return position

    def close(
        self, ticker: str, *, price: Decimal, date: dt.date,
        status: PositionStatus = PositionStatus.CLOSED_MANUAL,
    ) -> ClosedTrade:
        position = self.positions.get(ticker)
        if position is None or not position.is_open:
            raise ValueError(f"no open position in {ticker}")

        fx_rate = self._fx_for(position.currency)
        costs = compute(self.costs_model, ticker=ticker, shares=position.shares, price=price,
                        currency=position.currency, fx_rate=fx_rate, is_buy=False)
        proceeds = dec(position.shares) * price * fx_rate - costs.total_gbp
        self.cash += proceeds
        self.total_costs += costs.total_gbp

        # Entry costs are attributed to the round trip so a ClosedTrade's net
        # P&L is the whole truth about that trade, not the sell half of it.
        entry_costs = compute(
            self.costs_model, ticker=ticker, shares=position.shares, price=position.entry,
            currency=position.currency, fx_rate=position.fx_rate_at_entry, is_buy=True,
        )
        trade = ClosedTrade(
            ticker=ticker, idea_id=position.idea_id, idea_class=position.idea_class,
            opened_on=position.opened_on, closed_on=date, shares=position.shares,
            entry=position.entry, exit=price, currency=position.currency,
            fx_rate=position.fx_rate_at_entry,
            costs_gbp=costs.total_gbp + entry_costs.total_gbp, status=status,
        )
        self.closed.append(trade)
        self.positions[ticker] = position.model_copy(
            update={"status": status, "closed_on": date, "exit_price": price}
        )
        self._record_fill(position, date, -position.shares, price, position.currency,
                          fx_rate, costs)
        return trade

    def _record_fill(
        self, position: Position, date: dt.date, shares: int, price: Decimal,
        currency: str, fx_rate: Decimal, costs: TradeCosts,
    ) -> None:
        self.fills.append(Fill(
            ticker=position.ticker, date=date, shares=shares, price=price,
            currency=currency, fx_rate=fx_rate, **costs.as_fill_fields(),
        ))

    # ---------------------------------------------------------------- valuation
    def market_value(self, marks: Mapping[str, Decimal]) -> Decimal:
        """Open positions at the supplied marks.

        A position with no mark is held at its entry price rather than dropped.
        Dropping it would silently shrink NAV and manufacture a drawdown out of
        a missing data point.
        """
        total = Decimal("0")
        for position in self.open_positions:
            mark = marks.get(position.ticker, position.entry)
            total += dec(mark) * Decimal(position.shares) * self._fx_for(position.currency)
        return total

    def nav(self, marks: Mapping[str, Decimal]) -> Decimal:
        return self.cash + self.market_value(marks)

    def mark_to_market(self, date: dt.date, marks: Mapping[str, Decimal]) -> EquityPoint:
        nav = self.nav(marks)
        self.high_water = max(self.high_water, nav)
        point = EquityPoint(
            date=date, nav_gbp=nav.quantize(Decimal("0.01")),
            cash_gbp=self.cash.quantize(Decimal("0.01")),
            high_water_gbp=self.high_water.quantize(Decimal("0.01")),
            positions=len(self.open_positions),
        )
        self.equity.append(point)
        return point

    def drawdown(self, marks: Mapping[str, Decimal] | None = None) -> Decimal:
        nav = self.nav(marks or {})
        if self.high_water <= 0:
            return Decimal("0")
        return (self.high_water - nav) / self.high_water

    # ---------------------------------------------------------------- reporting
    def returns(self) -> list[Decimal]:
        """Period returns from the equity curve, for the metrics module."""
        out: list[Decimal] = []
        for prev, curr in zip(self.equity, self.equity[1:]):
            if prev.nav_gbp > 0:
                out.append(curr.nav_gbp / prev.nav_gbp - Decimal("1"))
        return out

    def exposure_fraction(self, marks: Mapping[str, Decimal] | None = None) -> Decimal:
        """How much of NAV is actually invested.

        §5.1 asks whether performance came from signal or from simply being long
        in an up market. This is the series that makes that answerable.
        """
        nav = self.nav(marks or {})
        if nav <= 0:
            return Decimal("0")
        return (self.market_value(marks or {}) / nav).quantize(Decimal("0.0001"))

    def summary(self) -> dict[str, object]:
        nav = self.equity[-1].nav_gbp if self.equity else self.cash
        return {
            "starting_cash": self.starting_cash,
            "cash": self.cash.quantize(Decimal("0.01")),
            "nav": nav,
            "total_return": ((nav / self.starting_cash - 1) if self.starting_cash else Decimal("0")),
            "open_positions": len(self.open_positions),
            "closed_trades": len(self.closed),
            "total_costs": self.total_costs.quantize(Decimal("0.01")),
            "high_water": self.high_water.quantize(Decimal("0.01")),
        }


def apply_exits(
    ledger: Ledger, marks: Mapping[str, Decimal], date: dt.date,
    *, invalidated: Sequence[str] = (),
) -> list[ClosedTrade]:
    """Close everything whose stop was hit or whose invalidation fired.

    Stops fill *at the stop level*, not at the mark. That is deliberately
    optimistic in one direction and honest about it: a real stop can gap through
    and fill far worse, so a backtest using this is reporting a best case for its
    exits. It never fills *better* than the stop, which would be fantasy.
    """
    closed: list[ClosedTrade] = []
    for position in list(ledger.open_positions):
        mark = marks.get(position.ticker)
        if position.ticker in invalidated:
            price = mark if mark is not None else position.entry
            closed.append(ledger.close(position.ticker, price=dec(price), date=date,
                                       status=PositionStatus.CLOSED_INVALIDATED))
        elif mark is not None and position.stop is not None and dec(mark) <= position.stop:
            closed.append(ledger.close(position.ticker, price=position.stop, date=date,
                                       status=PositionStatus.CLOSED_STOP))
    return closed
