"""Position sizing. Pure arithmetic, all of it Decimal.

The formula from Phase 3:

    shares = (satellite_capital x risk_per_trade) / (entry - stop)

capped by the maximum single position, then by whatever headroom the sector and
sub-allocation limits leave, then by cash.

Two properties this file has to hold and the tests pin:

**Rounding is always down.** Rounding a fractional share up puts more than the
risk budget at risk, every time, on every position. Down is the only direction
that cannot breach the limit it exists to enforce.

**The FX rate is applied to the stop distance, not just the exposure.** A USD
name sized off dollar figures with a GBP risk budget puts roughly 25% more money
at risk than intended at a 0.80 rate — the error is invisible because every
number involved looks right on its own.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal


@dataclass(frozen=True, slots=True)
class SizingCap:
    """One ceiling on the position, in GBP, with the name of what imposed it."""

    name: str
    gbp_limit: Decimal


@dataclass(frozen=True, slots=True)
class Sizing:
    shares: int
    gbp_exposure: Decimal
    gbp_risk: Decimal
    binding_cap: str | None
    #: The size the risk formula alone would have bought, before any cap.
    unconstrained_shares: int


def gbp_per_share(entry: Decimal, fx_rate: Decimal) -> Decimal:
    return entry * fx_rate


def size(
    *,
    entry: Decimal,
    stop: Decimal,
    satellite_capital: Decimal,
    risk_per_trade_pct: Decimal,
    fx_rate: Decimal = Decimal("1"),
    caps: tuple[SizingCap, ...] = (),
) -> Sizing:
    """Shares to buy, or zero. ``entry`` and ``stop`` are in the instrument's own
    currency; every output is GBP."""
    if entry <= 0 or stop >= entry or stop < 0:
        return Sizing(0, Decimal("0"), Decimal("0"), "invalid_stop", 0)

    risk_budget = satellite_capital * risk_per_trade_pct / Decimal("100")
    gbp_stop_distance = (entry - stop) * fx_rate
    if gbp_stop_distance <= 0:
        return Sizing(0, Decimal("0"), Decimal("0"), "invalid_stop", 0)

    unconstrained = int((risk_budget / gbp_stop_distance).to_integral_value(rounding=ROUND_DOWN))
    shares = unconstrained
    binding: str | None = None

    per_share = gbp_per_share(entry, fx_rate)
    for cap in caps:
        allowed = 0 if cap.gbp_limit <= 0 else int(
            (cap.gbp_limit / per_share).to_integral_value(rounding=ROUND_DOWN)
        )
        if allowed < shares:
            shares = allowed
            binding = cap.name

    shares = max(shares, 0)
    exposure = (per_share * Decimal(shares)).quantize(Decimal("0.01"))
    risk = (gbp_stop_distance * Decimal(shares)).quantize(Decimal("0.01"))
    return Sizing(shares, exposure, risk, binding, unconstrained)
