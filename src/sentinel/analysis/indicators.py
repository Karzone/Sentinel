"""Technical indicators, implemented here rather than pulled from a library.

That is a deliberate cost. `pandas-ta` and `ta-lib` disagree with each other on
smoothing conventions — Wilder vs simple, SMA-seeded EMA vs first-value-seeded —
and the differences are small enough to hide and large enough to move a score
across a threshold. §5.2 requires golden-dataset tests where "known input ->
exact expected score", and you cannot pin an exact expected score to a
convention a dependency may change in a point release.

So every convention used is stated in the docstring of the function that
implements it, and the golden tests assert the numbers those conventions
produce. Indicator maths runs in float: these outputs are dimensionless and
never become pounds. The Decimal boundary is money — see money.py.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

INDICATORS_VERSION = "indicators-v1"


def to_series(values, index=None) -> pd.Series:
    return pd.Series([float(v) for v in values], index=index, dtype="float64")


def sma(series: pd.Series, window: int) -> pd.Series:
    """Simple moving average. NaN until `window` observations exist — never
    a partial average, which would make an early 200-day 'trend' out of 30 bars."""
    return series.rolling(window=window, min_periods=window).mean()


def ema(series: pd.Series, span: int) -> pd.Series:
    """Exponential moving average, **SMA-seeded**.

    The first ``span`` values are NaN; the value at index ``span-1`` is the SMA
    of the first ``span`` observations; from there
    ``ema_t = alpha * x_t + (1 - alpha) * ema_{t-1}`` with ``alpha = 2/(span+1)``.

    This is the convention MetaStock/most charting packages use, and it differs
    from ``pandas.ewm(adjust=False)``, which seeds with the *first observation*
    and therefore carries a startup bias for the first few hundred bars.
    """
    values = series.to_numpy(dtype="float64")
    out = np.full(values.shape, np.nan)
    if len(values) < span:
        return pd.Series(out, index=series.index)
    alpha = 2.0 / (span + 1.0)
    out[span - 1] = values[:span].mean()
    for i in range(span, len(values)):
        out[i] = alpha * values[i] + (1 - alpha) * out[i - 1]
    return pd.Series(out, index=series.index)


def wilder_smooth(values: np.ndarray, period: int) -> np.ndarray:
    """Wilder's smoothing: seed with the mean of the first `period` values, then
    ``s_t = (s_{t-1} * (period - 1) + x_t) / period``. Used by RSI and ATR."""
    out = np.full(values.shape, np.nan)
    if len(values) < period:
        return out
    out[period - 1] = values[:period].mean()
    for i in range(period, len(values)):
        out[i] = (out[i - 1] * (period - 1) + values[i]) / period
    return out


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    """Wilder's RSI.

    A zero average loss gives RSI 100 (not NaN, not a division error) — an
    unbroken run of up days is genuinely maximally overbought.
    """
    delta = np.array(series.diff().to_numpy(dtype="float64"), copy=True)
    delta[0] = 0.0
    gains = np.where(delta > 0, delta, 0.0)
    losses = np.where(delta < 0, -delta, 0.0)
    # Wilder's first average covers `period` *changes*, i.e. bars 1..period.
    avg_gain = wilder_smooth(gains[1:], period)
    avg_loss = wilder_smooth(losses[1:], period)
    out = np.full(series.shape, np.nan)
    with np.errstate(divide="ignore", invalid="ignore"):
        rs = np.divide(avg_gain, avg_loss, out=np.full(avg_gain.shape, np.inf), where=avg_loss != 0)
    computed = 100.0 - (100.0 / (1.0 + rs))
    computed = np.where((avg_loss == 0) & ~np.isnan(avg_gain), 100.0, computed)
    computed = np.where((avg_gain == 0) & (avg_loss == 0), 50.0, computed)
    out[1:] = computed
    return pd.Series(out, index=series.index)


@dataclass(frozen=True, slots=True)
class Macd:
    macd: pd.Series
    signal: pd.Series
    histogram: pd.Series


def macd(series: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> Macd:
    """MACD on SMA-seeded EMAs. Histogram = macd - signal."""
    line = ema(series, fast) - ema(series, slow)
    signal_line = ema(line.dropna(), signal).reindex(series.index)
    return Macd(macd=line, signal=signal_line, histogram=line - signal_line)


def true_range(high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    prev_close = close.shift(1)
    ranges = pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1
    )
    tr = ranges.max(axis=1)
    tr.iloc[0] = float(high.iloc[0] - low.iloc[0])
    return tr


def atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    """Wilder's ATR. This is the one indicator here with a *monetary*
    consequence — Phase 3 sizes positions off the ATR-derived stop distance —
    so it is also the one whose convention matters most."""
    tr = true_range(high, low, close).to_numpy(dtype="float64")
    return pd.Series(wilder_smooth(tr, period), index=close.index)


def momentum_12_1(series: pd.Series, lookback: int = 252, skip: int = 21) -> float | None:
    """Classic 12-1 momentum: total return from t-252 to t-21.

    The most recent month is skipped on purpose — short-horizon reversal
    contaminates raw 12-month momentum, and the skip is what makes this the
    factor the literature documents rather than a lookalike.
    """
    if len(series) < lookback + 1:
        return None
    start = float(series.iloc[-(lookback + 1)])
    end = float(series.iloc[-(skip + 1)])
    if start <= 0:
        return None
    return (end / start) - 1.0


def realised_volatility(series: pd.Series, window: int = 20) -> float | None:
    """Annualised standard deviation of daily log returns over `window`."""
    if len(series) < window + 1:
        return None
    log_returns = np.diff(np.log(series.to_numpy(dtype="float64")[-(window + 1):]))
    if len(log_returns) < 2:
        return None
    return float(np.std(log_returns, ddof=1) * np.sqrt(252))


def volume_zscore(volume: pd.Series, short: int = 5, long: int = 100) -> float | None:
    """How unusual recent volume is, in standard deviations of the long window.

    Volume is the corroborating channel: a breakout on no volume and a breakout
    on three-sigma volume are different events, and the technical score treats
    them differently.
    """
    if len(volume) < long:
        return None
    values = volume.to_numpy(dtype="float64")
    baseline = values[-long:]
    std = float(np.std(baseline, ddof=1))
    if std == 0:
        return None
    return float((values[-short:].mean() - baseline.mean()) / std)


@dataclass(frozen=True, slots=True)
class Zone:
    support: float | None
    resistance: float | None
    #: Where price sits in the range, 0.0 at support and 1.0 at resistance.
    position: float | None


def support_resistance(series: pd.Series, window: int = 120) -> Zone:
    if len(series) < window:
        return Zone(None, None, None)
    window_values = series.to_numpy(dtype="float64")[-window:]
    low, high = float(window_values.min()), float(window_values.max())
    last = float(series.iloc[-1])
    position = None if high == low else (last - low) / (high - low)
    return Zone(support=low, resistance=high, position=position)


def drawdown_from_peak(series: pd.Series) -> float | None:
    """Current drawdown from the running peak, as a negative fraction."""
    if series.empty:
        return None
    values = series.to_numpy(dtype="float64")
    peak = float(np.maximum.accumulate(values)[-1])
    if peak <= 0:
        return None
    return float(values[-1] / peak - 1.0)
