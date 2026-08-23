"""Money, currency and FX.

Rule 5 of the build instructions: *all* financial calculations in ``Decimal``,
GBP base, explicit FX for USD names. This module is what makes that mechanical
rather than a convention — a ``Money`` refuses to add itself to a different
currency, so a USD price can never silently land in a GBP position size.

Floats are still fine for *indicator* maths (RSI, SMA, correlation) where the
output is a dimensionless score and never becomes a pound. The boundary is:
anything that could end up on a contract note is ``Decimal``.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP, localcontext
from typing import Mapping

GBP = "GBP"

# Money rounds to the penny for display and ledger entries. Intermediate maths
# keeps full precision; only `.quantize()` at the edges.
PENNY = Decimal("0.01")
# Prices need more than pennies — LSE quotes in pence to 2dp, FX to 6dp.
PRICE_DP = Decimal("0.000001")


class CurrencyMismatch(ValueError):
    """Raised when two amounts in different currencies meet."""


def dec(value: object) -> Decimal:
    """Coerce to Decimal without ever going through binary float.

    ``Decimal(0.1)`` is 0.1000000000000000055511151231257827; ``dec(0.1)`` is
    ``Decimal("0.1")``. Vendor JSON hands us floats, so this is the front door.

    NumPy scalars need the same treatment but cannot take the same route:
    ``repr(numpy.float64(1.0))`` is the string ``"np.float64(1.0)"`` on NumPy 2,
    which ``Decimal`` rejects outright. Anything that is not already a Decimal,
    an int or a string but *is* float-like goes through ``float()`` first — which
    is exact, since a float64 already is a Python float — and then through the
    same shortest-round-trip ``repr``.
    """
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):
        raise TypeError("refusing to treat a bool as a monetary amount")
    if isinstance(value, float):
        # `float(value)` is what makes this safe for numpy: np.float64 SUBCLASSES
        # float, so it lands here, and repr() on the subclass yields
        # "np.float64(1.0)". Converting first is exact — a float64 already is a
        # Python float — and restores the shortest round-tripping repr.
        return Decimal(repr(float(value)))
    if isinstance(value, int):
        return Decimal(int(value))
    if isinstance(value, str):
        return Decimal(value)
    if hasattr(value, "__index__"):
        return Decimal(int(value))          # numpy integer types
    if hasattr(value, "__float__"):
        return Decimal(repr(float(value)))
    return Decimal(str(value))


def quantize_money(value: Decimal) -> Decimal:
    return value.quantize(PENNY, rounding=ROUND_HALF_UP)


def quantize_price(value: Decimal) -> Decimal:
    return value.quantize(PRICE_DP, rounding=ROUND_HALF_UP)


@dataclass(frozen=True, slots=True, order=False)
class Money:
    """An amount in a named currency. Immutable; arithmetic is currency-checked."""

    amount: Decimal
    currency: str = GBP

    def __post_init__(self) -> None:
        object.__setattr__(self, "amount", dec(self.amount))
        object.__setattr__(self, "currency", self.currency.upper())

    # -- construction -----------------------------------------------------
    @classmethod
    def gbp(cls, amount: object) -> "Money":
        return cls(dec(amount), GBP)

    @classmethod
    def zero(cls, currency: str = GBP) -> "Money":
        return cls(Decimal("0"), currency)

    # -- arithmetic -------------------------------------------------------
    def _check(self, other: "Money") -> None:
        if self.currency != other.currency:
            raise CurrencyMismatch(
                f"cannot combine {self.currency} and {other.currency} without an FX rate"
            )

    def __add__(self, other: "Money") -> "Money":
        self._check(other)
        return Money(self.amount + other.amount, self.currency)

    def __sub__(self, other: "Money") -> "Money":
        self._check(other)
        return Money(self.amount - other.amount, self.currency)

    def __mul__(self, factor: object) -> "Money":
        return Money(self.amount * dec(factor), self.currency)

    __rmul__ = __mul__

    def __truediv__(self, divisor: object) -> "Money":
        return Money(self.amount / dec(divisor), self.currency)

    def __neg__(self) -> "Money":
        return Money(-self.amount, self.currency)

    def __lt__(self, other: "Money") -> bool:
        self._check(other)
        return self.amount < other.amount

    def __le__(self, other: "Money") -> bool:
        self._check(other)
        return self.amount <= other.amount

    def __gt__(self, other: "Money") -> bool:
        self._check(other)
        return self.amount > other.amount

    def __ge__(self, other: "Money") -> bool:
        self._check(other)
        return self.amount >= other.amount

    # -- presentation -----------------------------------------------------
    @property
    def rounded(self) -> "Money":
        return Money(quantize_money(self.amount), self.currency)

    def is_zero(self) -> bool:
        return self.amount == 0

    def __str__(self) -> str:
        symbol = {"GBP": "£", "USD": "$", "EUR": "€"}.get(self.currency, "")
        q = quantize_money(self.amount)
        return f"{symbol}{q:,}" if symbol else f"{q:,} {self.currency}"


@dataclass(frozen=True, slots=True)
class FxRates:
    """Units of GBP per one unit of the foreign currency.

    Deliberately *not* a live lookup inside the maths: a brief is reproducible
    only if the FX rates it used are archived with it, so a rate set is a value
    object that gets stored in the audit row alongside the idea.
    """

    as_of: str                      # ISO date the rates were observed
    rates: Mapping[str, Decimal]    # e.g. {"USD": Decimal("0.79")}

    def to_gbp(self, amount: Money) -> Money:
        if amount.currency == GBP:
            return amount
        try:
            rate = dec(self.rates[amount.currency])
        except KeyError as exc:  # fail loudly — never guess at parity
            raise CurrencyMismatch(
                f"no {amount.currency}/GBP rate in the {self.as_of} rate set"
            ) from exc
        with localcontext() as ctx:
            ctx.prec = 28
            return Money(amount.amount * rate, GBP)

    @classmethod
    def identity(cls, as_of: str = "1970-01-01") -> "FxRates":
        """A rate set for GBP-only universes and tests."""
        return cls(as_of=as_of, rates={GBP: Decimal("1")})


def pct(value: object) -> Decimal:
    """Percent literal -> fraction. ``pct(1)`` is ``Decimal('0.01')``."""
    return dec(value) / Decimal("100")
