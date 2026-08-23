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
