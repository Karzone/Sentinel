"""Recording trades that already happened at a real broker.

One implementation shared by `sentinel paper buy/sell` and the dashboard's
Record-a-trade form, because the rules are the point and rules enforced twice
drift apart:

- Nothing here places an order anywhere. Sentinel has no broker connection.
- Recording reality is never refused for breaking a risk limit — a limit you
  are already past is exactly what the risk layer exists to surface — so a
  breach WARNS and records. Input that can only be a mistake (a long's stop
  above its entry, a duplicate of a fill already recorded) is refused, because
  refusing a typo protects the book and refusing the truth corrupts it.
- Every entry is audited with ``manual: true``.

The duplicate guard doubles as double-submit protection for the web form: the
position row itself upserts idempotently on (ticker, date, "manual"), but the
cash deduction does not, so the second submit of the same fill must be refused
rather than absorbed.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal

from ..config import Config
from ..domain.enums import IdeaClass, PositionStatus
from ..domain.models import Position
from ..data.base import currency_for
from ..risk import sector_allocation
from ..storage import audit, repo

MANUAL_IDEA_ID = "manual"


class ManualEntryError(ValueError):
    """Input that can only be a mistake. Refused before anything is written."""


@dataclass(slots=True)
class RecordResult:
    position: Position
    cash: Decimal
    nav: Decimal
    gbp_amount: Decimal            # cost on a buy, proceeds on a sell
    pnl: Decimal | None = None     # sells only
    warnings: list[str] = field(default_factory=list)


def allowed_in(*, dashboard_local: bool, demo: bool) -> bool:
    """Whether a dashboard session may record trades at all.

    Two independent gates, both required: the hosted deploy is never local
    (streamlit_app.py pops the flag), and a demo database must stay fabricated
    end to end — a real fill written into invented history would be the one
    real-looking number on a page that promises there are none.
    """
    return dashboard_local and not demo


def record_buy(
    conn,
    config: Config,
    *,
    ticker: str,
    shares: int,
    price: Decimal,
    opened_on: dt.date | None = None,
    stop: Decimal | None = None,
    idea_class: IdeaClass = IdeaClass.LONG_TERM,
    note: str = "",
) -> RecordResult:
    from .. import pipeline

    ticker = ticker.strip().upper()
    opened = opened_on or dt.date.today()
    if not ticker:
        raise ManualEntryError("a ticker is required")
    if shares <= 0:
        raise ManualEntryError("shares must be positive")
    if price <= 0:
        raise ManualEntryError("price must be positive")
    if stop is not None and stop >= price:
        raise ManualEntryError(
            f"stop {stop} is at or above entry {price} — for a long position the "
            f"stop sits below the entry"
        )
    if repo.get_position(conn, repo.position_id(ticker, opened, MANUAL_IDEA_ID)):
        raise ManualEntryError(
            f"a manual fill in {ticker} dated {opened} is already recorded — "
            f"this is the same fill submitted twice, or a second lot that needs "
            f"its own date"
        )

    warnings: list[str] = []
    state = pipeline.portfolio_state(conn, config, as_of=opened)
    currency = currency_for(ticker)
    fx = _fx_rate(state.fx, currency, warnings)

    position = Position(
        ticker=ticker, idea_id=MANUAL_IDEA_ID, idea_class=idea_class,
        sector=config.sector_of(ticker), opened_on=opened, shares=shares,
        entry=price, currency=currency, fx_rate_at_entry=fx,
        stop=stop, invalidation=note,
    )
    pid = repo.save_position(conn, position)
    cost = position.gbp_cost_basis()
    cash = state.cash - cost
    nav = state.nav  # cash became stock at the same value; NAV is unchanged at the fill
    repo.save_equity_point(conn, opened, nav, cash, max(state.high_water_mark, nav))
    audit.record(conn, audit.AuditEvent.POSITION_OPENED, payload={
        "id": pid, "ticker": ticker, "shares": shares, "entry": str(price),
        "stop": None if stop is None else str(stop), "manual": True,
        "gbp_cost": str(cost),
    })
    conn.commit()

    if cash < 0:
        warnings.append(
            f"cash is negative — satellite capital in sentinel.toml is "
            f"£{config.satellite_capital_gbp:,.0f}; raise it to match the account "
            f"this book mirrors"
        )
    _limit_warnings(conn, config, position, opened, warnings)
    return RecordResult(position=position, cash=cash, nav=nav, gbp_amount=cost,
                        warnings=warnings)


def record_sell(
    conn,
    config: Config,
    *,
    ticker: str,
    price: Decimal,
    closed_on: dt.date | None = None,
) -> RecordResult:
    from .. import pipeline

    ticker = ticker.strip().upper()
    closed = closed_on or dt.date.today()
    if price <= 0:
        raise ManualEntryError("price must be positive")
    open_here = [p for p in repo.get_open_positions(conn) if p.ticker == ticker]
    if not open_here:
        raise ManualEntryError(f"no open position in {ticker}")
    position = open_here[0]

    updated = position.model_copy(update={
        "status": PositionStatus.CLOSED_MANUAL, "closed_on": closed,
        "exit_price": price,
    })
    repo.save_position(conn, updated)
    state = pipeline.portfolio_state(conn, config, as_of=closed)
    proceeds = price * Decimal(position.shares) * position.fx_rate_at_entry
    cash = state.cash + proceeds
    repo.save_equity_point(conn, closed, state.nav, cash,
                           max(state.high_water_mark, state.nav))
    audit.record(conn, audit.AuditEvent.POSITION_CLOSED, payload={
        "ticker": ticker, "exit": str(price), "manual": True,
        "gbp_proceeds": str(proceeds),
    })
    conn.commit()
    pnl = (price - position.entry) * Decimal(position.shares) * position.fx_rate_at_entry
    return RecordResult(position=updated, cash=cash, nav=state.nav,
                        gbp_amount=proceeds, pnl=pnl)


def _fx_rate(fx, currency: str, warnings: list[str]) -> Decimal:
    """GBP per unit of `currency`, from the archived rate set.

    Falls back to 1 with a warning when no rate was ever ingested: refusing to
    record a real holding over a missing FX row would leave the book lying by
    omission, which is worse than a mark that is off by the exchange rate and
    says so.
    """
    if currency == "GBP":
        return Decimal("1")
    rate = fx.rates.get(currency)
    if rate is None:
        warnings.append(
            f"no {currency}/GBP rate ingested — recording at 1:1; GBP values for "
            f"this position are wrong until FX is ingested"
        )
        return Decimal("1")
    return Decimal(str(rate))


def _limit_warnings(conn, config: Config, position: Position, as_of: dt.date,
                    warnings: list[str]) -> None:
    from .. import pipeline

    fresh = pipeline.portfolio_state(conn, config, as_of=as_of)
    exposure = fresh.exposure_gbp(position)
    single_cap = config.risk.max_single_position_pct / Decimal("100")
    if fresh.satellite_capital and exposure / fresh.satellite_capital > single_cap:
        warnings.append(
            f"over the single-position limit: "
            f"{exposure / fresh.satellite_capital:.1%} of satellite vs "
            f"{single_cap:.0%} cap"
        )
    cap = config.risk.max_sector_pct / Decimal("100")
    for sector, weight in sector_allocation(fresh).items():
        if weight >= cap:
            warnings.append(f"sector {sector} is at {weight:.1%} of a {cap:.0%} cap")
