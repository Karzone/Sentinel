"""UK dealing costs.

A backtest without these is a fiction, and specifically a *flattering* fiction:
every one of these charges is asymmetric against turnover, so omitting them
makes a high-turnover strategy look better than a low-turnover one — which is
exactly the wrong way for a system that has a short-term module it is trying to
judge honestly.

The four charges modelled, and why each is here:

**Stamp duty (0.5%)** is the big one and it is *UK buys only*. It is charged on
purchases of UK-incorporated shares and not on sales, so it is a pure drag on
round trips: a strategy that trades twice as often pays it twice as often. ETFs,
most AIM stocks and non-UK shares are exempt, which is why ``exempt`` exists —
modelling a US name or an ETF as if it paid stamp duty overstates costs and is
just as dishonest in the other direction.

**The PTM levy** is £1 on trades over £10,000, both ways. Small, but it is real
and it is trivially modelled.

**Commission** is flat per trade, which is what a UK retail broker charges. Flat
commission is what makes small positions uneconomic and is the reason
``min_position_gbp`` exists in the risk layer.

**Slippage** is a fraction of notional standing in for the spread and for market
impact. 0.1% is the spec's figure. It is the crudest part of this model: real
spreads widen for small caps and in stressed markets, so a backtest that only
just clears its costs has not really cleared them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal

from ..money import GBP, dec

COSTS_VERSION = "costs-v1"

PENNY = Decimal("0.01")


@dataclass(frozen=True, slots=True)
class CostModel:
    #: Flat per-trade dealing commission, GBP.
    commission_gbp: Decimal = Decimal("5.00")
    #: Fraction of notional lost to spread and impact, each way.
    slippage_pct: Decimal = Decimal("0.001")
    #: UK stamp duty / SDRT on purchases of UK shares.
    stamp_duty_pct: Decimal = Decimal("0.005")
    #: PTM levy, charged on trades above the threshold, both directions.
    ptm_levy_gbp: Decimal = Decimal("1.00")
    ptm_threshold_gbp: Decimal = Decimal("10000")
    #: FX spread charged on the GBP value of a non-GBP trade, each way.
    fx_spread_pct: Decimal = Decimal("0.0025")
    #: Tickers exempt from stamp duty — ETFs, most AIM shares, non-UK lines.
    stamp_duty_exempt: frozenset[str] = field(default_factory=frozenset)

    def is_stampable(self, ticker: str, currency: str) -> bool:
        """Stamp duty applies to UK shares only, and only on the buy side."""
        if currency != GBP:
            return False
        return ticker.upper() not in {t.upper() for t in self.stamp_duty_exempt}


@dataclass(frozen=True, slots=True)
class TradeCosts:
    commission_gbp: Decimal = Decimal("0")
    slippage_gbp: Decimal = Decimal("0")
    stamp_duty_gbp: Decimal = Decimal("0")
    ptm_levy_gbp: Decimal = Decimal("0")
    fx_spread_gbp: Decimal = Decimal("0")

    @property
    def total_gbp(self) -> Decimal:
        return (
            self.commission_gbp + self.slippage_gbp + self.stamp_duty_gbp
            + self.ptm_levy_gbp + self.fx_spread_gbp
        )

    def as_fill_fields(self) -> dict[str, Decimal]:
        """Fill only carries three cost fields, so the two small ones fold into
        commission rather than disappearing."""
        return {
            "commission_gbp": (self.commission_gbp + self.ptm_levy_gbp + self.fx_spread_gbp),
            "stamp_duty_gbp": self.stamp_duty_gbp,
            "slippage_gbp": self.slippage_gbp,
        }


def _q(value: Decimal) -> Decimal:
    return value.quantize(PENNY, rounding=ROUND_HALF_UP)


def compute(
    model: CostModel,
    *,
    ticker: str,
    shares: int,
    price: Decimal,
    currency: str = GBP,
    fx_rate: Decimal = Decimal("1"),
    is_buy: bool,
) -> TradeCosts:
    """Costs for one trade, in GBP."""
    notional_gbp = dec(abs(shares)) * price * fx_rate
    if notional_gbp <= 0:
        return TradeCosts()

    stamp = (
        _q(notional_gbp * model.stamp_duty_pct)
        if is_buy and model.is_stampable(ticker, currency)
        else Decimal("0")
    )
    ptm = model.ptm_levy_gbp if notional_gbp > model.ptm_threshold_gbp else Decimal("0")
    fx_spread = (
        _q(notional_gbp * model.fx_spread_pct) if currency != GBP else Decimal("0")
    )
    return TradeCosts(
        commission_gbp=_q(model.commission_gbp),
        slippage_gbp=_q(notional_gbp * model.slippage_pct),
        stamp_duty_gbp=stamp,
        ptm_levy_gbp=ptm,
        fx_spread_gbp=fx_spread,
    )


def round_trip_drag(
    model: CostModel, *, ticker: str, notional_gbp: Decimal, currency: str = GBP
) -> Decimal:
    """Total cost of a buy and a matching sell, as a fraction of notional.

    Worth printing next to any backtest result: at £1,000 a round trip on a UK
    share costs roughly 1.7% before the strategy has been right about anything,
    which is the number a weekly-rebalance idea has to clear fifty times a year.
    """
    if notional_gbp <= 0:
        return Decimal("0")
    shares = 1
    buy = compute(model, ticker=ticker, shares=shares, price=notional_gbp,
                  currency=currency, is_buy=True)
    sell = compute(model, ticker=ticker, shares=shares, price=notional_gbp,
                   currency=currency, is_buy=False)
    return ((buy.total_gbp + sell.total_gbp) / notional_gbp).quantize(Decimal("0.000001"))
