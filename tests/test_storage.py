"""Phase 0: the audit trail is only worth having if it cannot be rewritten."""

from __future__ import annotations

import datetime as dt
import sqlite3
from decimal import Decimal

import pytest

from sentinel.domain import Conviction, Direction, Idea, IdeaClass
from sentinel.storage import audit, repo
from tests.conftest import make_bar, make_fundamentals, make_news


def an_idea(idea_id="idea-1") -> Idea:
    return Idea(
        id=idea_id, created_at=dt.datetime(2024, 1, 2, 7, 0, tzinfo=dt.UTC),
        as_of=dt.date(2024, 1, 2), ticker="DEMO1.LSE", idea_class=IdeaClass.LONG_TERM,
        conviction=Conviction.MEDIUM, direction=Direction.LONG, signals=(),
        composite_score=Decimal("61.5"), inputs_digest="abc123",
        model_versions={"technical": "tech-v1"},
    )


class TestImmutability:
    def test_audit_rows_cannot_be_updated(self, conn):
        audit.record(conn, "test.event", ticker="X", payload={"a": 1})
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute("UPDATE audit SET event = 'tampered'")

    def test_audit_rows_cannot_be_deleted(self, conn):
        audit.record(conn, "test.event")
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute("DELETE FROM audit")

    def test_ideas_cannot_be_updated_or_deleted(self, conn):
        repo.save_idea(conn, an_idea())
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute("UPDATE ideas SET composite_score = '99'")
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute("DELETE FROM ideas")

    def test_saving_an_idea_writes_its_audit_row(self, conn):
        repo.save_idea(conn, an_idea())
        events = audit.read(conn, event=audit.AuditEvent.IDEA_GENERATED)
        assert len(events) == 1
        assert events[0]["payload"]["idea_id"] == "idea-1"
        assert events[0]["payload"]["model_versions"] == {"technical": "tech-v1"}

    def test_trace_for_idea_finds_every_row_mentioning_it(self, conn):
        repo.save_idea(conn, an_idea())
        audit.record(conn, audit.AuditEvent.RISK_APPROVED, payload={"idea_id": "idea-1"})
        audit.record(conn, audit.AuditEvent.RISK_APPROVED, payload={"idea_id": "other"})
        trace = audit.trace_for_idea(conn, "idea-1")
        assert [t["event"] for t in trace] == [
            audit.AuditEvent.IDEA_GENERATED, audit.AuditEvent.RISK_APPROVED
        ]


class TestDecimalFidelity:
    def test_prices_round_trip_without_float_corruption(self, conn):
        # 0.1 + 0.2 in float64 is 0.30000000000000004. If any column on the way
        # in or out were REAL, this assertion is the one that breaks.
        bar = make_bar(close="0.1", volume=7)
        repo.save_bars(conn, [bar], source="test")
        got = repo.get_bars(conn, bar.ticker)[0]
        assert got.close == Decimal("0.1")
        assert got.close + Decimal("0.2") == Decimal("0.3")

    def test_long_decimals_survive(self, conn):
        bar = make_bar(close="123.456789012345")
        repo.save_bars(conn, [bar], source="test")
        assert repo.get_bars(conn, bar.ticker)[0].close == Decimal("123.456789012345")


class TestRoundTrips:
    def test_bars_upsert_on_reingest(self, conn):
        d = dt.date(2024, 1, 2)
        repo.save_bars(conn, [make_bar(date=d, close="100")], source="v1")
        repo.save_bars(conn, [make_bar(date=d, close="105")], source="v2")
        bars = repo.get_bars(conn, "DEMO1.LSE")
        assert len(bars) == 1 and bars[0].close == Decimal("105")

    def test_fundamentals_point_in_time_never_reads_the_future(self, conn):
        repo.save_fundamentals(
            conn,
            [make_fundamentals(as_of=dt.date(2023, 6, 30), eps_ttm=Decimal("1")),
             make_fundamentals(as_of=dt.date(2024, 6, 30), eps_ttm=Decimal("9"))],
            source="test",
        )
        past = repo.get_fundamentals(conn, "DEMO1.LSE", as_of=dt.date(2024, 1, 1))
        assert past is not None and past.eps_ttm == Decimal("1")
        latest = repo.get_fundamentals(conn, "DEMO1.LSE")
        assert latest is not None and latest.eps_ttm == Decimal("9")

    def test_news_dedupes_on_content(self, conn):
        item = make_news()
        repo.save_news(conn, [item, item])
        assert len(repo.get_news(conn, item.ticker)) == 1

    def test_fx_rates_are_read_as_of_a_date(self, conn):
        repo.save_fx_rates(conn, dt.date(2024, 1, 1), {"USD": Decimal("0.80")})
        repo.save_fx_rates(conn, dt.date(2024, 6, 1), {"USD": Decimal("0.75")})
        assert repo.get_fx_rates(conn, dt.date(2024, 3, 1))["USD"] == Decimal("0.80")
        assert repo.get_fx_rates(conn, dt.date(2024, 7, 1))["USD"] == Decimal("0.75")


class TestLlmComplianceMetric:
    def test_first_pass_rate_separates_prompt_quality_from_pipeline_output(self, conn):
        repo.record_llm_call(conn, module="news", model="m", prompt_hash="a",
                             attempts=1, schema_ok=True, repaired=False)
        repo.record_llm_call(conn, module="news", model="m", prompt_hash="b",
                             attempts=2, schema_ok=True, repaired=True)
        repo.record_llm_call(conn, module="news", model="m", prompt_hash="c",
                             attempts=2, schema_ok=False, repaired=False, error="bad json")
        stats = repo.llm_schema_compliance(conn, module="news")
        assert stats["calls"] == 3
        assert stats["rate"] == pytest.approx(2 / 3)
        assert stats["first_pass_rate"] == pytest.approx(1 / 3)
