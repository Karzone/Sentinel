"""The technical module — deterministic, no LLM, no randomness.

Same input always produces the same score on any machine, which is what makes
the golden-dataset regression suite in §5.2 possible at all.

What this module deliberately does *not* do is decide anything. It emits a
0-100 score with the evidence that produced it. Whether a 78 becomes an idea is
the synthesis module's business, and whether an idea becomes a position is the
risk layer's — and neither of those is allowed to reach back in here.
"""

from __future__ import annotations

import datetime as dt
import math
from decimal import Decimal
from typing import Sequence

import pandas as pd

from ..domain.enums import ModuleName
from ..domain.models import Bar, Evidence, Signal
from ..money import dec
from . import indicators

TECHNICAL_VERSION = "technical-v1"

#: Below this the module refuses to speak rather than guessing a trend.
MIN_BARS = 60
#: A full read needs a 200-day SMA and a 12-1 momentum window.
FULL_BARS = 273

WEIGHTS = {
    "trend": Decimal("0.35"),
    "momentum": Decimal("0.30"),
    "rsi": Decimal("0.15"),
    "volume": Decimal("0.10"),
    "location": Decimal("0.10"),
}


class InsufficientHistory(ValueError):
    pass


def frames(bars: Sequence[Bar]) -> tuple[pd.Series, pd.Series, pd.Series, pd.Series]:
    """Adjusted close, high, low, volume as float Series.

    Highs and lows are scaled by the same adjustment factor as the close, so ATR
    is measured on the adjusted series too. Mixing a raw high with an adjusted
    close is a classic way to get an ATR that is quietly wrong across a split.
    """
    index = [b.date for b in bars]
    closes, highs, lows, volumes = [], [], [], []
    for bar in bars:
        factor = float(bar.adjusted_close / bar.close) if bar.close else 1.0
        closes.append(float(bar.adjusted_close))
        highs.append(float(bar.high) * factor)
        lows.append(float(bar.low) * factor)
        volumes.append(float(bar.volume))
    return (
        pd.Series(closes, index=index, dtype="float64"),
        pd.Series(highs, index=index, dtype="float64"),
        pd.Series(lows, index=index, dtype="float64"),
        pd.Series(volumes, index=index, dtype="float64"),
    )


def _trend_score(close: pd.Series) -> tuple[Decimal, str]:
    last = float(close.iloc[-1])
    sma50 = indicators.sma(close, 50)
    sma200 = indicators.sma(close, 200)
    fast = float(sma50.iloc[-1]) if not pd.isna(sma50.iloc[-1]) else None
    slow = float(sma200.iloc[-1]) if not pd.isna(sma200.iloc[-1]) else None

    if fast is None:
        return Decimal("50"), "not enough history for a 50-day average"
    if slow is None:
        base, label = (Decimal("65"), "above the 50-day (no 200-day yet)") if last > fast \
            else (Decimal("35"), "below the 50-day (no 200-day yet)")
        return base, label

    if last > fast > slow:
        base, label = Decimal("90"), "price above a rising 50-day above the 200-day"
    elif last > slow and fast <= slow:
        base, label = Decimal("65"), "above the 200-day but the 50-day has not crossed back up"
    elif last > fast and last <= slow:
        base, label = Decimal("45"), "above the 50-day but still under the 200-day"
    else:
        base, label = Decimal("15"), "below both the 50-day and the 200-day"

    # Slope of the 50-day over the last month, as a tie-breaker within a regime.
    if len(sma50.dropna()) > 20:
        prior = float(sma50.dropna().iloc[-21])
        if prior > 0:
            slope = fast / prior - 1.0
            adjust = Decimal("5") if slope > 0.01 else (Decimal("-5") if slope < -0.01 else Decimal("0"))
            base = max(Decimal("0"), min(Decimal("100"), base + adjust))
            if adjust:
                label += "; 50-day " + ("rising" if adjust > 0 else "falling")
    return base, label


def _momentum_score(close: pd.Series) -> tuple[Decimal, str, float | None]:
    mom = indicators.momentum_12_1(close)
    if mom is None:
        return Decimal("50"), "not enough history for 12-1 momentum", None
    # Bounded map: +/-30% saturates towards the ends without ever clipping, so
    # a 200% mover and a 40% mover are distinguishable but neither runs away.
    score = dec(round(50 + 50 * math.tanh(mom / 0.30), 4))
    return score, f"12-1 momentum {mom:+.1%}", mom


def _rsi_score(close: pd.Series) -> tuple[Decimal, str, float | None]:
    values = indicators.rsi(close).dropna()
    if values.empty:
        return Decimal("50"), "no RSI yet", None
    value = float(values.iloc[-1])
    # Scored as an *entry-timing* read, which is what the philosophy says
    # technicals are for on a long-term idea: buying something already vertical
    # is a worse entry than buying the same thesis mid-range.
    if value >= 80:
        score, label = Decimal("10"), "deeply overbought"
    elif value >= 70:
        score, label = Decimal("30"), "overbought"
    elif value >= 55:
        score, label = Decimal("75"), "firm but not stretched"
    elif value >= 45:
        score, label = Decimal("65"), "neutral"
    elif value >= 30:
        score, label = Decimal("55"), "soft"
    elif value >= 20:
        score, label = Decimal("40"), "oversold"
    else:
        score, label = Decimal("25"), "extremely oversold — often still falling"
    return score, f"RSI {value:.1f} ({label})", value


def _volume_score(volume: pd.Series, trend: Decimal) -> tuple[Decimal, str, float | None]:
    z = indicators.volume_zscore(volume)
    if z is None:
        return Decimal("50"), "no volume baseline", None
    if z > 1.5:
        # Heavy volume is only confirmation if it is confirming something. In a
        # downtrend the same three-sigma print is distribution.
        score = Decimal("80") if trend >= Decimal("60") else Decimal("35")
        label = "heavy volume " + ("confirming the trend" if trend >= Decimal("60") else "against the trend")
    elif z > 0.5:
        score, label = Decimal("65"), "above-average volume"
    elif z > -1:
        score, label = Decimal("50"), "ordinary volume"
    else:
        score, label = Decimal("35"), "unusually thin volume"
    return score, f"volume z-score {z:+.2f} ({label})", z


def _location_score(close: pd.Series) -> tuple[Decimal, str, float | None]:
    zone = indicators.support_resistance(close)
    if zone.position is None:
        return Decimal("50"), "no established range", None
    if zone.position > 0.9:
        score, label = Decimal("40"), "at the top of its 6-month range"
    elif zone.position < 0.15:
        score, label = Decimal("45"), "at the bottom of its 6-month range"
    elif 0.35 <= zone.position <= 0.75:
        score, label = Decimal("70"), "mid-range"
    else:
        score, label = Decimal("60"), "inside its range"
    return score, f"{zone.position:.0%} of the 6-month range ({label})", zone.position


def components(bars: Sequence[Bar]) -> dict[str, tuple[Decimal, str]]:
    """The five component scores and their labels, before weighting.

    Public because the golden tests assert each component exactly and then check
    that the composite is their weighted sum. Asserting only the composite would
    let two components drift in opposite directions and still pass.
    """
    close, _high, _low, volume = frames(bars)
    trend, trend_label = _trend_score(close)
    momentum, momentum_label, _ = _momentum_score(close)
    rsi_value, rsi_label, _ = _rsi_score(close)
    volume_value, volume_label, _ = _volume_score(volume, trend)
    location, location_label, _ = _location_score(close)
    return {
        "trend": (trend, trend_label),
        "momentum": (momentum, momentum_label),
        "rsi": (rsi_value, rsi_label),
        "volume": (volume_value, volume_label),
        "location": (location, location_label),
    }


def score(bars: Sequence[Bar], *, as_of: dt.date | None = None) -> Signal:
    """Score one ticker's price history. Raises rather than guessing when the
    history is too short — a made-up trend is worse than no signal."""
    if len(bars) < MIN_BARS:
        raise InsufficientHistory(
            f"{bars[0].ticker if bars else '?'}: {len(bars)} bars, need at least {MIN_BARS}"
        )
    ticker = bars[-1].ticker
    as_of = as_of or bars[-1].date
    parts = components(bars)
    trend_label = parts["trend"][1]
    composite = sum(value * WEIGHTS[key] for key, (value, _) in parts.items())
    composite = max(Decimal("0"), min(Decimal("100"), dec(composite).quantize(Decimal("0.01"))))

    evidence = tuple(
        Evidence(key=key, value=label, source="technical", weight=(value * WEIGHTS[key]).quantize(Decimal("0.01")))
        for key, (value, label) in parts.items()
    )

    # Confidence is about how much of the read was actually available, not about
    # how strong the signal is. A 90 computed without a 200-day average is a
    # weaker claim than a 70 computed with one.
    confidence = Decimal("1") if len(bars) >= FULL_BARS else (
        dec(len(bars)) / dec(FULL_BARS)
    ).quantize(Decimal("0.01"))

    return Signal(
        module=ModuleName.TECHNICAL, module_version=TECHNICAL_VERSION, ticker=ticker,
        as_of=as_of, score=composite, confidence=confidence, evidence=evidence,
        notes=trend_label,
    )


def atr_stop(bars: Sequence[Bar], *, multiple: Decimal = Decimal("2"), period: int = 14) -> Decimal | None:
    """A volatility-scaled stop level, in the instrument's own currency.

    This is the technical module's one output with money on the other end of it,
    so it returns a price rather than a score and the risk layer does the
    arithmetic. A fixed-percentage stop would put the same distance on a utility
    and a biotech; ATR makes the stop as wide as the instrument actually is.
    """
    if len(bars) < period + 1:
        return None
    close, high, low, _ = frames(bars)
    series = indicators.atr(high, low, close, period).dropna()
    if series.empty:
        return None
    distance = dec(float(series.iloc[-1])) * multiple
    stop = dec(float(close.iloc[-1])) - distance
    return stop.quantize(Decimal("0.0001")) if stop > 0 else None
