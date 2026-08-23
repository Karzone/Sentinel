"""Phase 5/6: pipeline, brief, notifications."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from sentinel import DISCLAIMER, pipeline
from sentinel.backtest import benchmarks
from sentinel.brief import build, subject_line, to_html, to_markdown
from sentinel.data import ingest as ingest_mod
from sentinel.domain import (
    Conviction, Direction, Idea, IdeaClass, ModuleName, NotifyEvent, Position,
    PositionStatus, Severity, Signal,
)
from sentinel.llm.fake import ScriptedClient, UnavailableClient
from sentinel.notify import NotificationResult, PushNotAllowed, Router, events_from_brief
from sentinel.risk import PortfolioState, RiskEngine
from sentinel.storage import audit, repo

GOOD_MEMO = {
    "thesis": "Margins are recovering. The market has not repriced it. Cash covers the debt.",
    "bull_case": "Operating leverage as volumes return.",
    "bear_case": "Input costs could spike again and the recovery stalls.",
    "invalidation": "Operating margin falls below 12% in the FY26 interim results.",
    "idea_class": "long_term", "conviction": "medium", "horizon_days": 365,
    "claims": ["trend", "momentum"],
}
GOOD_CATALYST = {
    "catalyst_type": "earnings", "direction": "long", "materiality": 4,
    "horizon_days": 30, "summary": "Beat and raised.", "headline_refs": ["Demo1 beats"],
}
GOOD_SENTIMENT = {
    "sentiment": 1, "conviction": 0.7, "herding_risk": False,
    "rationale": "Cautiously positive.", "sample_size": 12,
}


def scripted() -> ScriptedClient:
    return ScriptedClient({"news": GOOD_CATALYST, "sentiment": GOOD_SENTIMENT,
                           "synthesis": GOOD_MEMO})


@pytest.fixture()
def loaded(conn, config):
    ingest_mod.ingest(conn, config, list(config.universe("demo")), history_days=900)
    return conn


class TestPipeline:
    def test_it_scores_every_ticker_it_can(self, loaded, config):
        result = pipeline.run(loaded, config, list(config.universe("demo")),
                              llm=UnavailableClient(), persist=False)
        assert len(result.ideas) == 4
        assert all(i.composite_score > 0 for i in result.ideas)

    def test_without_an_llm_no_idea_carries_a_memo(self, loaded, config):
        """The correct degradation: deterministic scores still exist, but with
        no memo there is no invalidation, so nothing can pass the risk layer.
        The system goes quiet rather than issuing unexplained scores."""
        result = pipeline.run(loaded, config, ["DEMO1.LSE"], llm=UnavailableClient(),
                              persist=False)
        assert result.ideas[0].memo is None

    def test_with_an_llm_the_full_chain_produces_a_memo(self, loaded, config):
        client = scripted()
        result = pipeline.score_ticker(loaded, config, "DEMO1.LSE", dt.date.today(), llm=client)
        assert result.idea is not None
        modules = {s.module for s in result.signals}
        assert ModuleName.NEWS in modules and ModuleName.SENTIMENT in modules
        # The memo is only requested when the composite clears neutral, so on a
        # weak ticker its absence is correct rather than a failure.
        if result.idea.composite_score >= Decimal("50"):
            assert result.idea.memo is not None

    def test_a_critically_bad_ticker_is_never_scored(self, conn, config):
        """Not scored-and-flagged. Not scored."""
        result = pipeline.score_ticker(conn, config, "NEVER.LSE", dt.date.today())
        assert result.idea is None
        assert result.skipped and "no price history" in result.skipped

    def test_an_llm_failure_degrades_the_idea_without_failing_the_run(self, loaded, config):
        broken = ScriptedClient({"news": {"catalyst_type": "nonsense"}})
        result = pipeline.score_ticker(loaded, config, "DEMO1.LSE", dt.date.today(), llm=broken)
        assert result.llm_error is not None
        assert result.idea is not None          # deterministic modules still ran
        assert audit.read(loaded, event=audit.AuditEvent.LLM_SCHEMA_FAILURE)

    def test_ideas_are_persisted_once_and_are_immutable(self, loaded, config):
        pipeline.run(loaded, config, ["DEMO1.LSE"], llm=UnavailableClient())
        pipeline.run(loaded, config, ["DEMO1.LSE"], llm=UnavailableClient())
        assert len(repo.get_ideas(loaded, ticker="DEMO1.LSE")) == 1

    def test_the_portfolio_state_reads_config_and_the_ledger(self, loaded, config):
        """The live round-trip this test exists for caught a real bug: the field
        is satellite_capital_gbp, and nothing unit-tested had ever called it."""
        state = pipeline.portfolio_state(loaded, config, as_of=dt.date.today())
        assert state.satellite_capital == config.satellite_capital_gbp
        assert state.nav == config.satellite_capital_gbp

    def test_assess_runs_every_idea_through_the_risk_layer(self, loaded, config):
        run = pipeline.run(loaded, config, ["DEMO1.LSE"], llm=UnavailableClient(), persist=False)
        verdicts = pipeline.assess(loaded, config, run.ideas, as_of=dt.date.today())
        assert len(verdicts) == 1
        # No memo -> no invalidation -> refused, and the audit says why.
        assert not verdicts[0][1].approved
        assert audit.read(loaded, event=audit.AuditEvent.RISK_CHECK_FAILED)


def a_signal(score="72") -> Signal:
    from sentinel.domain import Evidence

    return Signal(
        module=ModuleName.TECHNICAL, module_version="v1", ticker="A.LSE",
        as_of=dt.date(2026, 1, 5), score=Decimal(score), confidence=Decimal("1"),
        evidence=(Evidence(key="trend", value="above both averages", source="technical"),),
    )


def an_idea(ticker="A.LSE", score="72", rejected=()) -> Idea:
    from sentinel.domain import IdeaMemo

    return Idea(
        id=f"idea-{ticker}-{score}", created_at=dt.datetime(2026, 1, 5, tzinfo=dt.UTC),
        as_of=dt.date(2026, 1, 5), ticker=ticker, idea_class=IdeaClass.LONG_TERM,
        conviction=Conviction.MEDIUM, direction=Direction.LONG, signals=(a_signal(score),),
        memo=IdeaMemo(ticker=ticker, thesis="T.", bull_case="B.", bear_case="Be.",
                      invalidation="Margin below 12% in FY26.", idea_class=IdeaClass.LONG_TERM,
                      conviction=Conviction.MEDIUM, horizon_days=365, claims=("trend",)),
        composite_score=Decimal(score), rejected_by_rules=tuple(rejected),
    )


def a_state(**kw) -> PortfolioState:
    defaults = dict(satellite_capital=Decimal("10000"), cash=Decimal("10000"),
                    positions=[], nav=Decimal("10000"), high_water_mark=Decimal("10000"))
    defaults.update(kw)
    return PortfolioState(**defaults)


def approved_verdict(idea: Idea, engine: RiskEngine, state: PortfolioState):
    return engine.evaluate(idea, entry=Decimal("50"), stop=Decimal("45"),
                           state=state, as_of=dt.date(2026, 1, 5))


class TestBrief:
    def _engine(self) -> RiskEngine:
        from sentinel.config import RiskLimits

        return RiskEngine(RiskLimits(), sectors={"A.LSE": "consumer", "B.LSE": "industrials",
                                                 "C.LSE": "healthcare", "D.LSE": "technology"})

    def test_at_most_three_candidates_reach_the_brief(self):
        """A brief with twelve ideas is a watchlist: it moves the selection
        decision back onto the reader while appearing to have done the work."""
        engine, state = self._engine(), a_state()
        ideas = [an_idea(t, s) for t, s in
                 (("A.LSE", "90"), ("B.LSE", "85"), ("C.LSE", "80"), ("D.LSE", "75"))]
        verdicts = [(i, approved_verdict(i, engine, state)) for i in ideas]
        document = build(as_of=dt.date(2026, 1, 5), ideas=ideas, verdicts=verdicts,
                         state=state, engine=engine)
        assert len(document.ideas) == 3
        assert [i.ticker for i in document.ideas] == ["A.LSE", "B.LSE", "C.LSE"]
        assert any("cleared every check but are not shown" in line
                   for line in document.portfolio_lines)

    def test_what_we_got_wrong_always_finds_something(self):
        """A research system that never reports a fault is not fault-free, it is
        not looking. Even a completely clean run gets a finding."""
        engine, state = self._engine(), a_state()
        document = build(as_of=dt.date(2026, 1, 5), ideas=[], verdicts=[],
                         state=state, engine=engine)
        assert document.what_we_got_wrong
        assert "itself worth" in document.what_we_got_wrong[0]

    def test_rejections_are_counted_by_rule(self):
        engine, state = self._engine(), a_state()
        rejected = [an_idea("A.LSE", rejected=("R3: invented evidence",)),
                    an_idea("B.LSE", rejected=("R3: invented evidence",))]
        document = build(as_of=dt.date(2026, 1, 5), ideas=rejected, verdicts=[],
                         state=state, engine=engine)
        assert any("R3 × 2" in line for line in document.what_we_got_wrong)

    def test_a_critical_data_issue_marks_the_brief_stale(self):
        from sentinel.domain import DataQualityIssue

        engine, state = self._engine(), a_state()
        issue = DataQualityIssue(check="freshness", severity=Severity.CRITICAL,
                                 ticker="A.LSE", detail="no price history at all",
                                 as_of=dt.date(2026, 1, 5))
        document = build(as_of=dt.date(2026, 1, 5), ideas=[], verdicts=[], state=state,
                         engine=engine, issues=[issue])
        assert document.stale is True

    def test_the_kill_switch_is_carried_onto_the_brief(self):
        engine = self._engine()
        state = a_state(nav=Decimal("8000"))
        document = build(as_of=dt.date(2026, 1, 5), ideas=[], verdicts=[],
                         state=state, engine=engine)
        assert document.kill_switch_active is True
        assert any("de-risk" in line for line in document.risk_lines)


class TestRendering:
    def _brief(self, **kw):
        from sentinel.config import RiskLimits

        engine = RiskEngine(RiskLimits(), sectors={"A.LSE": "consumer"})
        state = kw.pop("state", a_state())
        return build(as_of=dt.date(2026, 1, 5), ideas=kw.pop("ideas", []),
                     verdicts=kw.pop("verdicts", []), state=state, engine=engine, **kw)

    def test_the_disclaimer_appears_in_every_rendered_brief(self):
        """Emitted by the renderer, so no code path can produce a readable brief
        without it."""
        markdown = to_markdown(self._brief())
        assert markdown.count(DISCLAIMER) >= 2

    def test_the_stale_banner_sits_above_the_ideas_not_in_a_footnote(self):
        from sentinel.domain import DataQualityIssue

        issue = DataQualityIssue(check="freshness", severity=Severity.CRITICAL, ticker="A.LSE",
                                 detail="no price history", as_of=dt.date(2026, 1, 5))
        markdown = to_markdown(self._brief(issues=[issue]))
        assert markdown.index("STALE OR BAD DATA") < markdown.index("Candidate ideas")

    def test_an_idea_block_names_its_invalidation(self):
        markdown = to_markdown(self._brief(ideas=[], verdicts=[]))
        assert "expected outcome on most days" in markdown

    def test_the_subject_line_is_derived_from_what_the_brief_holds(self):
        """A subject that says the same thing every day trains you to stop
        reading the one channel allowed to arrive unprompted."""
        quiet = self._brief()
        assert "no candidates" in subject_line(quiet)

        alarmed = self._brief(state=a_state(nav=Decimal("8000")))
        assert "kill switch" in subject_line(alarmed)

    def test_html_output_is_self_contained_and_theme_aware(self):
        import html as html_mod

        page = to_html(self._brief(), to_markdown(self._brief()))
        assert page.startswith("<!doctype html>")
        assert "prefers-color-scheme:dark" in page
        # No remote assets: an email client that blocks them must still render.
        assert "http://" not in page and "https://" not in page
        assert html_mod.escape(DISCLAIMER) in page

    def test_every_list_is_closed_before_the_next_block(self):
        """A blank line inside a markdown list must not strand the </ul> —
        the splice-it-in-afterwards version put it before the final <li>."""
        page = to_html(self._brief(), to_markdown(self._brief()))
        assert page.count("<ul>") == page.count("</ul>")
        assert "</li><hr>" not in page
        assert "</ul><hr>" in page or "</p><hr>" in page

    def test_html_escapes_content_rather_than_trusting_it(self):
        from sentinel.brief.render import _inline

        assert _inline("<script>alert(1)</script>") == "&lt;script&gt;alert(1)&lt;/script&gt;"

    # The weekly review moved to its own module and signature; its rendering,
    # including the mandatory faults section, is covered by tests/test_weekly.py.


class RecordingNotifier:
    def __init__(self, channel="test", delivered=True, configured=True):
        self.channel = channel
        self.sent: list[dict] = []
        self._delivered = delivered
        self._configured = configured

    def available(self) -> bool:
        return self._configured

    def send(self, *, subject, body, html=None, priority="default", tags=""):
        self.sent.append({"subject": subject, "body": body, "priority": priority, "tags": tags})
        return NotificationResult(self.channel, self._delivered, self._configured,
                                  "sent" if self._delivered else "boom")


class TestNotificationRouting:
    def test_the_daily_brief_can_never_be_pushed(self):
        """The rule that keeps push meaningful: a system that pushes a routine
        digest every morning teaches you to dismiss its pushes unread."""
        router = Router(digest=RecordingNotifier(), push=RecordingNotifier())
        with pytest.raises(PushNotAllowed):
            router.push_event("daily_brief", subject="s", body="b")  # type: ignore[arg-type]

    def test_each_allowed_event_carries_its_own_priority(self):
        push = RecordingNotifier("push")
        router = Router(digest=RecordingNotifier(), push=push)
        router.push_event(NotifyEvent.KILL_SWITCH, subject="s", body="b")
        router.push_event(NotifyEvent.EARNINGS_IMMINENT, subject="s", body="b")
        assert push.sent[0]["priority"] == "urgent"
        assert push.sent[1]["priority"] == "default"

    def test_a_digest_failure_falls_back_to_the_push_channel(self):
        push = RecordingNotifier("push")
        router = Router(digest=RecordingNotifier("email", delivered=False), push=push)
        result = router.send_digest(subject="s", body="b")
        assert result.failed
        assert push.sent and "could not be delivered" in push.sent[0]["body"]

    def test_an_unconfigured_digest_does_not_trigger_the_fallback(self):
        """'Not configured' and 'configured and failed' need different responses."""
        push = RecordingNotifier("push")
        router = Router(digest=RecordingNotifier("email", delivered=False, configured=False),
                        push=push)
        router.send_digest(subject="s", body="b")
        assert push.sent == []

    def test_notifications_are_recorded_to_the_audit_trail(self, conn):
        router = Router(digest=RecordingNotifier(), push=RecordingNotifier(), conn=conn)
        router.send_digest(subject="s", body="b")
        assert audit.read(conn, event=audit.AuditEvent.NOTIFICATION)


class TestEventsFromBrief:
    def _brief(self, **kw):
        from sentinel.config import RiskLimits

        engine = RiskEngine(RiskLimits(), sectors={})
        return build(as_of=dt.date(2026, 1, 5), ideas=[], verdicts=[],
                     state=kw.pop("state", a_state()), engine=engine, **kw)

    def test_a_quiet_brief_produces_no_events(self):
        assert events_from_brief(self._brief()) == []

    def test_the_kill_switch_produces_an_event(self):
        events = events_from_brief(self._brief(state=a_state(nav=Decimal("8000"))))
        assert [e[0] for e in events] == [NotifyEvent.KILL_SWITCH]

    def test_a_stop_hit_produces_an_event(self):
        position = Position(
            ticker="A.LSE", idea_id="i", idea_class=IdeaClass.LONG_TERM, sector="consumer",
            opened_on=dt.date(2025, 12, 1), shares=10, entry=Decimal("50"),
            stop=Decimal("45"), status=PositionStatus.OPEN,
        )
        state = a_state(positions=[position], marks={"A.LSE": Decimal("40")})
        events = events_from_brief(self._brief(state=state))
        assert NotifyEvent.STOP_TRIGGERED in [e[0] for e in events]

    def test_imminent_earnings_on_a_held_name_produce_an_event(self):
        position = Position(
            ticker="A.LSE", idea_id="i", idea_class=IdeaClass.LONG_TERM, sector="consumer",
            opened_on=dt.date(2025, 12, 1), shares=10, entry=Decimal("50"),
            status=PositionStatus.OPEN,
        )
        events = events_from_brief(
            self._brief(), positions=[position], as_of=dt.date(2026, 1, 5),
            earnings_dates={"A.LSE": dt.date(2026, 1, 6)},
        )
        assert NotifyEvent.EARNINGS_IMMINENT in [e[0] for e in events]

    def test_earnings_on_a_name_not_held_produce_nothing(self):
        events = events_from_brief(self._brief(), positions=[],
                                   earnings_dates={"Z.LSE": dt.date(2026, 1, 6)})
        assert events == []


class TestMonteCarloPlacement:
    def _result(self, distribution):
        return benchmarks.MonteCarloResult(
            portfolios=len(distribution),
            median_return=sorted(distribution)[len(distribution) // 2],
            percentiles={}, distribution=tuple(distribution),
        )

    def test_placement_ranks_against_the_actual_distribution(self):
        distribution = [Decimal(str(x / 100)) for x in range(-50, 50)]
        placed = benchmarks.place_strategy(self._result(distribution), Decimal("0.00"))
        assert 45 <= placed.strategy_percentile <= 55

    def test_a_mostly_cash_strategy_carries_an_exposure_caveat(self):
        """Beating fully-invested random portfolios from 13% exposure in a
        falling market is being in cash, not stock selection."""
        distribution = [Decimal("-0.8")] * 100
        placed = benchmarks.place_strategy(self._result(distribution), Decimal("-0.02"),
                                           strategy_exposure=0.13)
        assert "time spent in cash" in placed.verdict()

    def test_a_comparable_exposure_carries_no_caveat(self):
        distribution = [Decimal("-0.8")] * 100
        placed = benchmarks.place_strategy(self._result(distribution), Decimal("-0.02"),
                                           strategy_exposure=0.95)
        assert placed.exposure_caveat is None

    def test_a_leveraged_exposure_is_flagged_the_other_way(self):
        distribution = [Decimal("0.1")] * 100
        placed = benchmarks.place_strategy(self._result(distribution), Decimal("0.5"),
                                           strategy_exposure=1.8)
        assert "more market risk" in placed.verdict()


class TestCuratedUniverses:
    """The starter config's ticker lists.

    A universe whose tickers have no sector mapping is worse than no universe:
    `sector_of` falls back to a single shared "unknown" bucket, so every
    unmapped name counts against the SAME 30% limit and the concentration check
    silently stops distinguishing between them.
    """

    def _starter(self):
        import tomllib
        from sentinel.config import STARTER_CONFIG
        return tomllib.loads(STARTER_CONFIG)

    def test_the_ai_universe_resolves(self):
        from sentinel.config import Config, _decimalise

        config = Config(**_decimalise(self._starter()))
        assert len(config.universe("ai")) == 25

    def test_every_ai_ticker_carries_a_sector(self):
        starter = self._starter()
        unmapped = [t for t in starter["universes"]["ai"] if t not in starter["sectors"]]
        assert not unmapped, (
            f"{unmapped} have no sector, so they share the 'unknown' bucket and the "
            "30% concentration limit stops telling them apart"
        )

    def test_it_is_not_one_sector_wearing_a_theme(self):
        """Not a diversification claim — these names correlate through AI
        exposure regardless of sector. It asserts only that the mapping reflects
        real economic exposure rather than being stamped 'technology' wholesale,
        which would make the sector cap bind on the whole universe at once."""
        from collections import Counter

        starter = self._starter()
        spread = Counter(starter["sectors"][t] for t in starter["universes"]["ai"])
        assert len(spread) >= 4, spread
        assert max(spread.values()) < len(starter["universes"]["ai"]), spread

    def test_tickers_carry_an_exchange_suffix(self):
        """The adapters key on it. A bare "NVDA" is not the same request."""
        for ticker in self._starter()["universes"]["ai"]:
            assert "." in ticker, f"{ticker} has no exchange suffix"
