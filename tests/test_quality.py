"""Phase 1: the checks that stand between a vendor and a Sev-1 signal."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from sentinel.data import quality
from sentinel.domain import Severity
from tests.conftest import make_bar, make_fundamentals, series


def _adjusted(ticker: str, rows: list[tuple[str, str, str]]) -> list:
    """rows of (iso date, close, adjusted_close)."""
    out = []
    for date_s, close, adj in rows:
        c = Decimal(close)
        out.append(
            make_bar(
                ticker=ticker, date=dt.date.fromisoformat(date_s),
                open=c, high=c, low=c, close=c, adjusted_close=Decimal(adj),
            )
        )
    return out


class TestAdjustmentIntegrity:
    """The check that catches the failure mode you cannot see by eye."""

    def test_a_clean_series_passes(self):
        bars = _adjusted("X", [
            ("2024-01-02", "100", "98"),    # factor 1.0204
            ("2024-01-03", "101", "99.5"),  # factor 1.0151
            ("2024-01-04", "102", "102"),   # factor 1.0
        ])
        assert quality.check_adjustment_integrity("X", bars, dt.date(2024, 1, 4)) == []

    def test_a_factor_that_rises_forward_in_time_is_critical(self):
        # An un-applied adjustment mid-history: the factor must never increase.
        bars = _adjusted("X", [
            ("2024-01-02", "100", "99"),     # 1.0101
            ("2024-01-03", "101", "98"),     # 1.0306  <- rose
            ("2024-01-04", "102", "102"),    # 1.0
        ])
        issues = quality.check_adjustment_integrity("X", bars, dt.date(2024, 1, 4))
        assert [i.severity for i in issues] == [Severity.CRITICAL]
        assert "un-applied" in issues[0].detail

    def test_a_series_not_normalised_to_the_present_is_critical(self):
        bars = _adjusted("X", [("2024-01-02", "100", "90"), ("2024-01-03", "101", "91")])
        issues = quality.check_adjustment_integrity("X", bars, dt.date(2024, 1, 3))
        assert issues and issues[0].severity is Severity.CRITICAL
        assert "expected 1.0" in issues[0].detail

    def test_an_unadjusted_two_for_one_split_is_caught(self):
        """The concrete bug: a 2:1 split hits `close` but never `adjusted_close`.

        To every indicator in Phase 2 this is a -50% crash. The factor test sees
        it as a factor that halved and then had to climb back, which is exactly
        the invariant violation above.
        """
        bars = _adjusted("X", [
            ("2024-01-02", "200", "200"),
            ("2024-01-03", "100", "200"),   # split applied to close only: factor 0.5
            ("2024-01-04", "101", "101"),   # ...and back to 1.0 -> the rise
        ])
        issues = quality.check_adjustment_integrity("X", bars, dt.date(2024, 1, 4))
        assert any(i.severity is Severity.CRITICAL for i in issues)

    def test_non_positive_adjusted_close_is_critical(self):
        bars = [make_bar(ticker="X", close=Decimal("10"), adjusted_close=Decimal("0"))]
        issues = quality.check_adjustment_integrity("X", bars, dt.date(2024, 1, 2))
        assert issues[0].severity is Severity.CRITICAL


class TestFreshness:
    def test_no_history_at_all_is_critical(self):
        issues = quality.check_freshness("X", [], dt.date(2024, 1, 10))
        assert issues[0].severity is Severity.CRITICAL

    def test_yesterdays_close_is_fine(self):
        bars = [make_bar(date=dt.date(2024, 1, 9))]
        assert quality.check_freshness("X", bars, dt.date(2024, 1, 10)) == []

    def test_a_week_behind_is_critical(self):
        bars = [make_bar(date=dt.date(2024, 1, 2))]
        issues = quality.check_freshness("X", bars, dt.date(2024, 1, 12))
        assert issues[0].severity is Severity.CRITICAL

    def test_two_days_behind_only_warns(self):
        bars = [make_bar(date=dt.date(2024, 1, 8))]
        issues = quality.check_freshness("X", bars, dt.date(2024, 1, 10))
        assert issues[0].severity is Severity.WARN


class TestMissingBars:
    def test_a_complete_run_is_clean(self):
        assert quality.check_missing_bars("X", series("X", [10] * 20), dt.date(2023, 2, 1)) == []

    def test_a_holey_series_is_critical(self):
        bars = series("X", [10] * 30)
        kept = bars[:3] + bars[20:]           # ~17 weekdays vanish
        issues = quality.check_missing_bars("X", kept, dt.date(2023, 3, 1))
        assert issues[0].severity is Severity.CRITICAL

    def test_a_long_weekend_is_only_informational(self):
        bars = series("X", [10] * 40)
        kept = bars[:20] + bars[21:]          # one day missing
        issues = quality.check_missing_bars("X", kept, dt.date(2023, 3, 1))
        assert issues[0].severity is Severity.INFO


class TestPriceSanity:
    def test_an_extreme_move_warns_rather_than_blocks(self):
        bars = series("X", [100, 100, 40])
        issues = quality.check_price_sanity("X", bars, dt.date(2023, 1, 5))
        assert [i.severity for i in issues] == [Severity.WARN]
        assert "unadjusted split" in issues[0].detail

    def test_a_run_of_zero_volume_warns(self):
        bars = [make_bar(date=dt.date(2024, 1, d), volume=0) for d in range(2, 12)]
        issues = quality.check_price_sanity("X", bars, dt.date(2024, 1, 12))
        assert any(i.severity is Severity.WARN and "zero-volume" in i.detail for i in issues)


class TestFundamentals:
    def test_a_filing_dated_in_the_future_is_critical(self):
        """Lookahead in the rawest form: a snapshot we could not have had."""
        f = make_fundamentals(as_of=dt.date(2024, 12, 31))
        issues = quality.check_fundamentals("X", f, dt.date(2024, 6, 1))
        assert any(i.severity is Severity.CRITICAL for i in issues)

    def test_a_missing_snapshot_warns(self):
        issues = quality.check_fundamentals("X", None, dt.date(2024, 6, 1))
        assert issues[0].severity is Severity.WARN

    def test_a_two_year_old_snapshot_warns(self):
        f = make_fundamentals(as_of=dt.date(2022, 1, 1))
        issues = quality.check_fundamentals("X", f, dt.date(2024, 6, 1))
        assert any("days old" in i.detail for i in issues)


class TestReport:
    def test_blocked_tickers_are_exactly_the_critical_ones(self):
        report = quality.QualityReport(dt.date(2024, 1, 10))
        report.extend(quality.check_freshness("STALE", [], dt.date(2024, 1, 10)))
        report.extend(quality.check_fundamentals("FINE", None, dt.date(2024, 1, 10)))
        assert report.blocking is True
        assert report.blocked_tickers() == {"STALE"}
