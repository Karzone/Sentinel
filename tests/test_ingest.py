"""Phase 1: the ingest job, end to end against the fixture vendor."""

from __future__ import annotations

import datetime as dt

from sentinel.data import ingest
from sentinel.data.base import ProviderError
from sentinel.domain import Severity
from sentinel.storage import audit, repo


class BrokenPrices:
    name = "broken"

    def available(self) -> bool:
        return True

    def fetch_bars(self, ticker, start, end):
        raise ProviderError("502 from the vendor")


def test_ingest_writes_bars_fundamentals_and_news(conn, config):
    as_of = dt.date.today()
    result = ingest.ingest(conn, config, ["DEMO1.LSE", "DEMO3.US"], as_of=as_of, history_days=400)
    assert result.bars_written > 200
    assert result.fundamentals_written == 2
    assert repo.latest_bar_date(conn, "DEMO1.LSE") is not None
    assert repo.get_fundamentals(conn, "DEMO3.US") is not None


def test_ingest_records_both_ends_of_the_run_in_the_audit_trail(conn, config):
    ingest.ingest(conn, config, ["DEMO1.LSE"], history_days=400)
    events = {e["event"] for e in audit.read(conn)}
    assert audit.AuditEvent.INGEST_STARTED in events
    assert audit.AuditEvent.INGEST_COMPLETED in events


def test_a_vendor_outage_blocks_only_the_ticker_it_touched(conn, config, monkeypatch):
    """A 502 on one name must not take the universe down — but it must also not
    pass silently, or the next brief scores a ticker with no fresh data."""
    monkeypatch.setattr(ingest.registry, "price_provider", lambda _c: BrokenPrices())
    result = ingest.ingest(conn, config, ["DEMO1.LSE"], history_days=400)
    assert result.ok is False
    assert result.vendor_failures
    assert "DEMO1.LSE" in result.report.blocked_tickers()


def test_checks_run_against_the_database_not_just_the_fetched_delta(conn, config):
    """Re-ingesting only today must still validate the whole stored series."""
    old = dt.date.today() - dt.timedelta(days=400)
    ingest.ingest(conn, config, ["DEMO1.LSE"], as_of=old, history_days=500)
    # Now ask for a run dated today while having ingested nothing since `old`.
    result = ingest.ingest(
        conn, config, ["DEMO1.LSE"], as_of=dt.date.today(), history_days=0, with_news=False
    )
    stale = [i for i in result.report.issues if i.check == "freshness"]
    assert stale and stale[0].severity is Severity.CRITICAL


def test_quality_issues_are_persisted_for_the_health_command(conn, config):
    ingest.ingest(conn, config, ["DEMO1.LSE"], history_days=30)  # short history -> WARN
    stored = repo.get_quality_issues(conn)
    assert any(i.check == "history_depth" for i in stored)


class TestVendorFailuresAreVisible:
    """A failing vendor must not look like a vendor with nothing to say.

    A fundamentals failure is deliberately non-fatal — a ticker with prices can
    still be scored on trend. But it used to append to `vendor_failures` and
    raise no quality issue, and nothing printed that list, so 25 failed calls
    against a live account rendered as "0 fundamentals · 0 critical" with the
    reason nowhere on screen or in the brief.
    """

    def _vendor(self, exc):
        from sentinel.data.base import ProviderError

        class _Failing:
            name = "stub"
            def available(self): return True
            def fetch_fundamentals(self, ticker): raise ProviderError(exc)
        return _Failing()

    def test_a_failed_fundamentals_call_becomes_a_reported_warning(
        self, conn, config, monkeypatch
    ):
        import datetime as dt
        from sentinel.data import ingest as ingest_mod, registry

        monkeypatch.setattr(registry, "fundamentals_provider",
                            lambda _c: self._vendor("402 payment required"))
        result = ingest_mod.ingest(conn, config, ["X.US"],
                                   as_of=dt.date(2026, 8, 23), with_news=False)

        assert result.vendor_failures, "the failure was not recorded at all"
        reported = [i for i in result.report.issues
                    if "402 payment required" in i.detail]
        assert reported, (
            "the vendor failed and the quality report says nothing — the brief "
            "would show 0 fundamentals with no reason"
        )

    def test_it_stays_non_fatal(self, conn, config, monkeypatch):
        """Prices still ingest; the run is degraded, not blocked."""
        import datetime as dt
        from sentinel.data import ingest as ingest_mod, registry
        from sentinel.domain.enums import Severity

        monkeypatch.setattr(registry, "fundamentals_provider",
                            lambda _c: self._vendor("quota exhausted"))
        result = ingest_mod.ingest(conn, config, ["X.US"],
                                   as_of=dt.date(2026, 8, 23), with_news=False)
        vendor_issues = [i for i in result.report.issues if i.check == "vendor"]
        assert vendor_issues
        assert all(i.severity is Severity.WARN for i in vendor_issues)
