"""Golden-dataset tests for the indicators.

Known input -> exact expected value, computed by hand from the conventions
stated in each function's docstring. These exist because indicator libraries
disagree with one another on smoothing, and a convention that drifts moves
scores across thresholds without anything looking broken.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from sentinel.analysis import indicators as ind


def s(values) -> pd.Series:
    return pd.Series([float(v) for v in values], dtype="float64")


class TestMovingAverages:
    def test_sma_is_nan_until_the_window_is_full(self):
        out = ind.sma(s([1, 2, 3, 4]), 3)
        assert out.isna().tolist() == [True, True, False, False]
        assert out.iloc[2] == pytest.approx(2.0)
        assert out.iloc[3] == pytest.approx(3.0)

    def test_ema_is_sma_seeded_not_first_value_seeded(self):
        # span 3, alpha 0.5. Seed at index 2 = mean(1,2,3) = 2.0.
        # index 3: 0.5*4 + 0.5*2.0 = 3.0 ; index 4: 0.5*5 + 0.5*3.0 = 4.0
        out = ind.ema(s([1, 2, 3, 4, 5]), 3)
        assert out.iloc[:2].isna().all()
        assert out.iloc[2] == pytest.approx(2.0)
        assert out.iloc[3] == pytest.approx(3.0)
        assert out.iloc[4] == pytest.approx(4.0)

    def test_ema_of_a_constant_series_is_that_constant(self):
        out = ind.ema(s([7] * 20), 5)
        assert out.iloc[-1] == pytest.approx(7.0)

    def test_ema_returns_all_nan_when_shorter_than_the_span(self):
        assert ind.ema(s([1, 2]), 5).isna().all()


class TestRsi:
    def test_hand_computed_wilder_rsi(self):
        """closes 10, 11, 12, 11, 13 at period 3.

        deltas +1 +1 -1 +2. First average covers the first three deltas:
        avg gain 2/3, avg loss 1/3 -> RS 2 -> RSI 66.667 at index 3.
        Then avg gain (2/3*2 + 2)/3 = 10/9, avg loss (1/3*2 + 0)/3 = 2/9,
        RS 5 -> RSI 83.333 at index 4.
        """
        out = ind.rsi(s([10, 11, 12, 11, 13]), period=3)
        assert out.iloc[3] == pytest.approx(200.0 / 3.0, abs=1e-9)
        assert out.iloc[4] == pytest.approx(500.0 / 6.0, abs=1e-9)

    def test_an_unbroken_run_of_gains_is_100_not_a_division_error(self):
        out = ind.rsi(s(range(1, 40)), period=14)
        assert out.iloc[-1] == pytest.approx(100.0)

    def test_a_flat_series_is_50(self):
        out = ind.rsi(s([5] * 40), period=14)
        assert out.iloc[-1] == pytest.approx(50.0)

    def test_an_unbroken_run_of_losses_is_0(self):
        out = ind.rsi(s(range(60, 20, -1)), period=14)
        assert out.iloc[-1] == pytest.approx(0.0)


class TestMacd:
    def test_histogram_is_macd_minus_signal(self):
        series = s(list(range(1, 120)))
        m = ind.macd(series)
        last = m.histogram.dropna().index[-1]
        assert m.histogram[last] == pytest.approx(m.macd[last] - m.signal[last])

    def test_a_steady_uptrend_puts_the_macd_line_above_zero(self):
        m = ind.macd(s([100 * (1.01 ** i) for i in range(200)]))
        assert m.macd.dropna().iloc[-1] > 0

    def test_a_steady_downtrend_puts_it_below_zero(self):
        m = ind.macd(s([100 * (0.99 ** i) for i in range(200)]))
        assert m.macd.dropna().iloc[-1] < 0


class TestAtr:
    def test_hand_computed_true_range(self):
        high = s([11, 12, 11.5])
        low = s([9, 10.5, 10])
        close = s([10, 11, 11])
        tr = ind.true_range(high, low, close)
        assert tr.iloc[0] == pytest.approx(2.0)          # first bar: high - low
        assert tr.iloc[1] == pytest.approx(2.0)          # max(1.5, |12-10|, |10.5-10|)
        assert tr.iloc[2] == pytest.approx(1.5)          # max(1.5, |11.5-11|, |10-11|)

    def test_atr_seeds_with_the_mean_of_the_first_true_ranges(self):
        high = s([11, 12, 11.5, 12.5])
        low = s([9, 10.5, 10, 11])
        close = s([10, 11, 11, 12])
        out = ind.atr(high, low, close, period=3)
        assert out.iloc[2] == pytest.approx((2.0 + 2.0 + 1.5) / 3.0)
        # index 3: TR = max(1.5, |12.5-11|, |11-11|) = 1.5 -> (1.8333*2 + 1.5)/3
        assert out.iloc[3] == pytest.approx((out.iloc[2] * 2 + 1.5) / 3.0)

    def test_atr_of_a_flat_series_is_zero(self):
        flat = s([10] * 30)
        assert ind.atr(flat, flat, flat, 14).iloc[-1] == pytest.approx(0.0)


class TestMomentum:
    def test_the_most_recent_month_is_skipped(self):
        """A series that doubles then collapses in the final three weeks must
        still show positive 12-1 momentum — that skip is the whole point."""
        values = [100.0] + [100.0 * (1 + i / 252) for i in range(1, 232)] + [10.0] * 21
        mom = ind.momentum_12_1(s(values), lookback=252, skip=21)
        assert mom is not None and mom > 0.5

    def test_none_when_the_history_is_too_short(self):
        assert ind.momentum_12_1(s([1] * 100)) is None

    def test_a_flat_series_has_zero_momentum(self):
        assert ind.momentum_12_1(s([50] * 300)) == pytest.approx(0.0)


class TestVolumeAndRange:
    def test_a_volume_spike_shows_a_positive_zscore(self):
        volume = s([1000] * 99 + [5000])
        assert ind.volume_zscore(volume, short=1, long=100) > 3

    def test_constant_volume_has_no_zscore_rather_than_dividing_by_zero(self):
        assert ind.volume_zscore(s([1000] * 200)) is None

    def test_position_in_range_is_zero_at_the_low_and_one_at_the_high(self):
        rising = s(list(range(1, 200)))
        assert ind.support_resistance(rising, 120).position == pytest.approx(1.0)
        falling = s(list(range(200, 1, -1)))
        assert ind.support_resistance(falling, 120).position == pytest.approx(0.0)

    def test_drawdown_from_peak_is_negative_after_a_fall(self):
        assert ind.drawdown_from_peak(s([100, 120, 60])) == pytest.approx(-0.5)

    def test_realised_volatility_of_a_flat_series_is_zero(self):
        assert ind.realised_volatility(s([10] * 50)) == pytest.approx(0.0)

    def test_realised_volatility_scales_with_dispersion(self):
        rng = np.random.default_rng(0)
        calm = s(100 * np.exp(np.cumsum(rng.normal(0, 0.002, 300))))
        wild = s(100 * np.exp(np.cumsum(rng.normal(0, 0.02, 300))))
        assert ind.realised_volatility(wild, 200) > ind.realised_volatility(calm, 200) * 5
