"""Silent history truncation.

The live case: one ingest wrote 568 bars per ticker, the next wrote exactly
250, and the run reported `0 critical, 35 warnings` with nothing about depth.
`check_history_depth` compares against a fixed floor of 250, so a series that
came back a third of the requested length cleared it exactly. The window that
was *asked for* was never compared to the window that arrived, and the audit
trail did not record it either — so the two runs could not be told apart after
the fact.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from sentinel.data import quality
from sentinel.domain.models import Bar

AS_OF = dt.date(2026, 8, 23)


def _bars(ticker: str, first: dt.date, count: int = 250) -> list[Bar]:
    out = []
    day = first
    while len(out) < count:
        if day.weekday() < 5:
            out.append(Bar(
                ticker=ticker, date=day, open=Decimal("10"), high=Decimal("10"),
                low=Decimal("10"), close=Decimal("10"),
                adjusted_close=Decimal("10"), volume=1000, currency="USD",
            ))
        day += dt.timedelta(days=1)
    return out


class TestTheRequestedWindowIsCompared:
    REQUESTED = dt.date(2024, 6, 15)  # ~800 days before AS_OF

    def test_a_series_much_shorter_than_requested_is_reported(self):
        """The exact hole: 250 bars clears the depth floor of 250, so depth is
        silent, and only the requested start can show the shortfall."""
        bars = _bars("NVDA.US", dt.date(2025, 8, 25))
        issues = quality.check_history_span("NVDA.US", bars, self.REQUESTED, AS_OF)
        assert issues, "800 days requested, one year returned, nothing said"
        assert "requested from 2024-06-15" in issues[0].detail
        assert "earliest bar 2025-08-25" in issues[0].detail

    def test_the_depth_check_alone_would_have_stayed_silent(self):
        """Guards against 'fixed' by lowering a threshold: at exactly the floor
        the old check passes, and it must keep passing — a 250-bar series IS
        scoreable on trend. The new fact is orthogonal."""
        bars = _bars("NVDA.US", dt.date(2025, 8, 25), count=250)
        assert quality.check_history_depth("NVDA.US", bars, AS_OF, minimum=250) == []
        assert quality.check_history_span("NVDA.US", bars, self.REQUESTED, AS_OF)

    def test_a_window_that_was_honoured_says_nothing(self):
        bars = _bars("NVDA.US", self.REQUESTED + dt.timedelta(days=2), count=560)
        assert quality.check_history_span("NVDA.US", bars, self.REQUESTED, AS_OF) == []

    def test_a_weekend_start_is_within_tolerance(self):
        """The requested date lands on a Saturday often enough that a few days'
        slack is normal, not a truncation."""
        bars = _bars("NVDA.US", self.REQUESTED + dt.timedelta(days=3), count=560)
        assert quality.check_history_span("NVDA.US", bars, self.REQUESTED, AS_OF) == []

    def test_an_empty_series_is_left_to_the_other_checks(self):
        assert quality.check_history_span("NVDA.US", [], self.REQUESTED, AS_OF) == []

    def test_it_runs_as_part_of_run_all(self):
        issues = quality.run_all(
            "NVDA.US", _bars("NVDA.US", dt.date(2025, 8, 25)), None, AS_OF,
            requested_start=self.REQUESTED,
        )
        assert any(i.check == "history_span" for i in issues)

    def test_run_all_without_a_requested_start_is_unchanged(self):
        """Callers that do not know the window (backfills, tests) must not gain
        a warning they cannot act on."""
        issues = quality.run_all(
            "NVDA.US", _bars("NVDA.US", dt.date(2025, 8, 25)), None, AS_OF)
        assert not any(i.check == "history_span" for i in issues)


class TestAVendorCapIsToldApartFromAYoungListing:
    """The distinction is the point. One short series is a company's own
    history; several short to the same week is the plan you are paying for."""

    REQUESTED = dt.date(2024, 6, 15)

    def test_a_shared_floor_across_many_names_is_named_as_a_cap(self):
        first = {t: dt.date(2025, 8, 25) for t in ("NVDA.US", "AMD.US", "ARM.US", "MU.US")}
        issues = quality.check_history_cap(first, self.REQUESTED, AS_OF)
        assert len(issues) == 1
        assert issues[0].check == "history_cap"
        assert issues[0].ticker is None, "a cap is a run-level fact, not a ticker's"
        assert "vendor history cap" in issues[0].detail
        assert "2025-08-25" in issues[0].detail

    def test_one_young_listing_is_not_a_cap(self):
        first = {
            "ARM.US": dt.date(2025, 8, 25),          # short
            "NVDA.US": dt.date(2024, 6, 17),         # honoured
            "AMD.US": dt.date(2024, 6, 17),
        }
        assert quality.check_history_cap(first, self.REQUESTED, AS_OF) == []

    def test_short_series_at_their_own_dates_are_not_a_cap(self):
        """Three IPOs across three years look nothing like one boundary."""
        first = {
            "A.US": dt.date(2024, 11, 1), "B.US": dt.date(2025, 3, 4),
            "C.US": dt.date(2025, 9, 30),
        }
        assert quality.check_history_cap(first, self.REQUESTED, AS_OF) == []

    def test_two_short_names_are_below_the_evidence_bar(self):
        first = {"A.US": dt.date(2025, 8, 25), "B.US": dt.date(2025, 8, 26)}
        assert quality.check_history_cap(first, self.REQUESTED, AS_OF) == []

    def test_an_honoured_run_says_nothing(self):
        first = {t: dt.date(2024, 6, 17) for t in ("A.US", "B.US", "C.US", "D.US")}
        assert quality.check_history_cap(first, self.REQUESTED, AS_OF) == []

    def test_the_count_reported_is_the_short_ones_not_the_universe(self):
        first = {t: dt.date(2025, 8, 25) for t in ("A.US", "B.US", "C.US")}
        first["D.US"] = dt.date(2024, 6, 17)
        detail = quality.check_history_cap(first, self.REQUESTED, AS_OF)[0].detail
        assert "3 of 4 series" in detail


class TestIngestRecordsTheWindowItAsked_For:
    def test_the_cap_verdict_reaches_the_run_report(self, conn, config, monkeypatch):
        """Run-level, so it can only be produced after every ticker."""
        import datetime as dt

        from sentinel.data import ingest as ingest_mod
        from sentinel.data.base import ProviderError  # noqa: F401

        class _Capped:
            name = "capped"
            def available(self): return True
            def fetch_bars(self, ticker, start, end):
                # Every name truncated to the same floor — a plan, not a market.
                return _bars(ticker, dt.date(2025, 8, 25), count=250)

        monkeypatch.setattr(ingest_mod.registry, "price_provider", lambda _c: _Capped())
        result = ingest_mod.ingest(
            conn, config, ["A.US", "B.US", "C.US"], as_of=AS_OF,
            with_news=False, history_days=800,
        )
        caps = [i for i in result.report.issues if i.check == "history_cap"]
        assert len(caps) == 1, "a per-ticker warning is not a verdict"
        assert "3 of 3 series" in caps[0].detail

    def test_the_audit_trail_can_tell_two_runs_apart(self, conn, config, monkeypatch):
        """568 bars one day and 250 the next was unexplainable because nothing
        recorded what had been asked for."""
        import datetime as dt

        from sentinel.data import ingest as ingest_mod
        from sentinel.storage import audit

        class _Capped:
            name = "capped"
            def available(self): return True
            def fetch_bars(self, ticker, start, end):
                return _bars(ticker, dt.date(2025, 8, 25), count=250)

        monkeypatch.setattr(ingest_mod.registry, "price_provider", lambda _c: _Capped())
        ingest_mod.ingest(conn, config, ["A.US"], as_of=AS_OF, with_news=False,
                          history_days=800)
        completed = [e for e in audit.read(conn)
                     if e["event"] == audit.AuditEvent.INGEST_COMPLETED]
        payload = completed[-1]["payload"]
        assert payload["history_days"] == 800
        assert payload["requested_start"] == (AS_OF - dt.timedelta(days=800)).isoformat()
        assert payload["first_bars"]["A.US"] == "2025-08-25"


class TestWarningsAreGroupedNotTruncated:
    """`warnings[:10]` was the same silent truncation, in the layer whose job
    is to report it: sixteen identical 402s used all ten lines and hid the
    other twenty-five warnings — the run-level cap verdict among them."""

    def _issue(self, check, ticker, detail):
        from sentinel.domain.enums import Severity
        from sentinel.domain.models import DataQualityIssue
        return DataQualityIssue(check=check, severity=Severity.WARN, ticker=ticker,
                                detail=detail, as_of=AS_OF)

    def test_the_same_failure_on_many_tickers_costs_one_line(self):
        from sentinel.cli import group_warnings

        warnings = [
            self._issue("vendor", f"{t}.US",
                        f"fundamentals vendor fmp failed: 'symbol' {t} not in your plan")
            for t in ("AVGO", "MU", "ARM", "ASML", "MRVL", "SMCI", "ORCL", "IBM")
        ]
        lines = group_warnings(warnings)
        assert len(lines) == 1
        assert lines[0].startswith("8 tickers (")
        assert "AVGO.US" in lines[0] and "+2 more" in lines[0]

    def test_nothing_distinct_is_ever_dropped(self):
        from sentinel.cli import group_warnings

        warnings = [self._issue("vendor", f"T{i}.US", f"distinct failure {i}")
                    for i in range(25)]
        lines = group_warnings(warnings)
        assert len(lines) == 25, "a distinct warning was hidden"

    def test_a_run_level_warning_survives_a_wall_of_repeats(self):
        """It sorts last (no ticker) and was exactly what the cap-off dropped."""
        from sentinel.cli import group_warnings

        warnings = [self._issue("vendor", f"T{i}.US", "not in your plan")
                    for i in range(20)]
        warnings.append(self._issue("history_cap", None, "vendor history cap"))
        lines = group_warnings(warnings)
        assert any("vendor history cap" in line for line in lines)

    def test_a_run_level_line_carries_no_ticker_prefix(self):
        from sentinel.cli import group_warnings

        lines = group_warnings([self._issue("history_cap", None, "vendor history cap")])
        assert lines == ["vendor history cap"]

    def test_a_lone_warning_still_names_its_ticker(self):
        from sentinel.cli import group_warnings

        lines = group_warnings([self._issue("vendor", "NVDA.US", "something odd")])
        assert lines == ["NVDA.US: something odd"]

    def test_different_failures_under_one_check_do_not_collapse(self):
        from sentinel.cli import group_warnings

        lines = group_warnings([
            self._issue("vendor", "A.US", "price vendor eodhd failed: 500"),
            self._issue("vendor", "B.US", "fundamentals vendor fmp failed: 402"),
        ])
        assert len(lines) == 2
