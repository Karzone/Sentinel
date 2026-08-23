"""The weekly review.

The section that gets the most attention here is "what the system got wrong",
because the spec marks it mandatory and a review that quietly omits it on a good
week is worse than no review at all.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from sentinel.brief import weekly
from sentinel.brief.render import weekly_review, weekly_subject
from sentinel.domain import IdeaClass, PositionStatus
from sentinel.portfolio import Ledger
from sentinel.storage import repo
from tests.conftest import make_bar


@pytest.fixture()
def account(conn, config):
    """A small paper account with an equity curve and one closed trade."""
    from sentinel.data import ingest as ingest_mod

    ingest_mod.ingest(conn, config, ["DEMO1.LSE"], history_days=400)
    bars = repo.get_bars(conn, "DEMO1.LSE")
    ledger = Ledger(Decimal("10000"))
    entry = bars[-30].adjusted_close
    ledger.open(ticker="DEMO1.LSE", idea_id="i-1", idea_class=IdeaClass.LONG_TERM,
                sector="consumer", shares=5, price=entry, date=bars[-30].date,
                stop=entry * Decimal("0.9"))
    for bar in bars[-30:]:
        point = ledger.mark_to_market(bar.date, {"DEMO1.LSE": bar.adjusted_close})
        repo.save_equity_point(conn, bar.date, point.nav_gbp, point.cash_gbp,
                               point.high_water_gbp)
    ledger.close("DEMO1.LSE", price=bars[-1].adjusted_close, date=bars[-1].date)
    for position in ledger.positions.values():
        repo.save_position(conn, position)
    return conn


class TestAssembly:
    def test_it_builds_over_the_requested_window(self, account, config):
        review = weekly.build(account, config, as_of=dt.date.today(), weeks=2)
        assert review.period_start == dt.date.today() - dt.timedelta(days=14)
        assert review.performance

    def test_performance_leads_with_risk_adjusted_return(self, account, config):
        """A 40% year at triple the volatility is not a better year."""
        review = weekly.build(account, config)
        assert "Sharpe" in review.performance
        assert review.performance.index("Sharpe") < review.performance.index("Since inception")

    def test_an_empty_account_says_so_rather_than_reporting_zero(self, conn, config):
        review = weekly.build(conn, config)
        assert "Nothing can be said about performance yet" in review.performance

    def test_an_uncomputable_sharpe_is_explained_not_omitted(self, conn, config):
        """'n/a' with no explanation reads as 'the Sharpe was bad'."""
        today = dt.date.today()
        for offset in (2, 1):
            repo.save_equity_point(conn, today - dt.timedelta(days=offset),
                                   Decimal("10000"), Decimal("10000"), Decimal("10000"))
        review = weekly.build(conn, config)
        assert "not computable" in review.performance

    def test_closed_trades_and_idea_counts_are_reported(self, account, config):
        review = weekly.build(account, config, weeks=52)
        assert review.closed_trades == 1


class TestBenchmarks:
    def test_an_uningested_benchmark_is_named_rather_than_skipped(self, account, config):
        """Silently dropping B1 would leave the review looking complete."""
        review = weekly.build(account, config)
        joined = " ".join(review.benchmark_lines)
        assert "not ingested" in joined
        assert "VWRP.LSE" in joined

    def test_cash_is_always_comparable_because_it_needs_no_vendor(self, account, config):
        assert any("B3 cash" in line for line in weekly.build(account, config).benchmark_lines)

    def test_it_says_plainly_when_the_benchmark_is_winning(self, account, config):
        review = weekly.build(account, config)
        joined = " ".join(review.benchmark_lines)
        assert "BEHIND" in joined or "ahead of" in joined


class TestTheMandatoryFaultSection:
    def test_it_always_finds_something_even_on_a_spotless_week(self, conn, config):
        """A research system that never reports a fault is not fault-free — it is
        not looking."""
        review = weekly.build(conn, config)
        assert review.wrong
        assert any("itself worth noting" in w or "no paper equity curve" in w
                   for w in review.wrong)

    def test_tolerated_data_warnings_are_reported(self, account, config):
        review = weekly.build(account, config, weeks=52)
        assert any("tolerated rather than blocking" in w for w in review.wrong)

    def test_a_stopped_out_position_is_named(self, account, config):
        position = repo.get_all_positions(account)[0]
        repo.save_position(account, position.model_copy(
            update={"status": PositionStatus.CLOSED_STOP,
                    "closed_on": dt.date.today() - dt.timedelta(days=1)}
        ))
        review = weekly.build(account, config, weeks=2)
        assert any("stopped out" in w and "DEMO1.LSE" in w for w in review.wrong)


class TestKillCriteria:
    def test_the_six_month_gate_is_never_silent(self, account, config):
        """It printed nothing at all once the paper period elapsed but the
        comparison inputs were missing — which reads exactly like a pass."""
        review = weekly.build(account, config)
        assert review.kill_criteria
        joined = " ".join(review.kill_criteria)
        assert ("gate is 6" in joined or "CANNOT be evaluated" in joined
                or "KILL CRITERION MET" in joined or "gate passed" in joined)


class TestRendering:
    def test_the_disclaimer_appears_twice(self, account, config):
        from sentinel import DISCLAIMER

        markdown = weekly_review(weekly.build(account, config))
        assert markdown.count(DISCLAIMER) >= 2

    def test_every_mandatory_heading_is_present(self, account, config):
        markdown = weekly_review(weekly.build(account, config))
        for heading in ("## Performance", "## Versus the benchmarks", "## Evals",
                        "## What the system got wrong this week"):
            assert heading in markdown

    def test_an_empty_fault_list_renders_as_a_reporting_bug(self, account, config):
        review = weekly.build(account, config)
        review.wrong = ()
        assert "reporting bug" in weekly_review(review)

    def test_the_subject_leads_with_a_met_kill_criterion(self, account, config):
        review = weekly.build(account, config)
        review.kill_criteria = ("KILL CRITERION MET: something",)
        assert "kill criterion has been met" in weekly_subject(review)

    def test_the_subject_otherwise_reports_the_trade_count(self, account, config):
        review = weekly.build(account, config)
        review.kill_criteria = ()
        review.closed_trades = 3
        assert "3 trade(s) closed" in weekly_subject(review)
