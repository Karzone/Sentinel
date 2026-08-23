"""Phase 3: the risk layer.

Written before the implementation, because this is the code with money on the
other end of it. Every limit in the spec gets a test that would pass a position
through if the limit were absent, and §5.5 pre-commits that any month with a
risk-layer bypass bug suspends real-money use — so a gap here is not a missing
test, it is a suspended system.

Nothing in this file imports anything from `analysis/` or `llm/`. That is the
point: no signal, no conviction level and no model output can reach in here.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from sentinel.config import RiskLimits
from sentinel.domain import (
    Conviction, Direction, Idea, IdeaClass, Position, PositionStatus, RiskCheckId,
)
from sentinel.money import FxRates
from sentinel.risk import PortfolioState, RiskEngine

AS_OF = dt.date(2024, 6, 28)
SECTORS = {"A.LSE": "consumer", "B.LSE": "consumer", "C.LSE": "technology",
           "D.US": "technology", "E.LSE": "healthcare"}
USD = FxRates("2024-06-28", {"USD": Decimal("0.80")})


def idea(ticker="A.LSE", idea_class=IdeaClass.LONG_TERM, invalidation="Margin below 12% in FY25") -> Idea:
    from sentinel.domain import IdeaMemo

    memo = IdeaMemo(
        ticker=ticker, thesis="T.", bull_case="B.", bear_case="Be.",
        invalidation=invalidation, idea_class=idea_class,
        conviction=Conviction.MEDIUM,
        horizon_days=365 if idea_class is IdeaClass.LONG_TERM else 21,
        claims=("k1",),
    )
    return Idea(
        id=f"idea-{ticker}", created_at=dt.datetime(2024, 6, 28, tzinfo=dt.UTC), as_of=AS_OF,
        ticker=ticker, idea_class=idea_class, conviction=Conviction.MEDIUM,
        direction=Direction.LONG, signals=(), memo=memo, composite_score=Decimal("70"),
    )


def position(ticker, *, shares, entry, idea_class=IdeaClass.LONG_TERM,
             sector=None, currency="GBP", fx=Decimal("1")) -> Position:
    return Position(
        ticker=ticker, idea_id=f"idea-{ticker}", idea_class=idea_class,
        sector=sector or SECTORS.get(ticker, "unknown"), opened_on=dt.date(2024, 1, 2),
        shares=shares, entry=Decimal(str(entry)), currency=currency,
        fx_rate_at_entry=fx, stop=Decimal(str(entry)) * Decimal("0.9"),
        status=PositionStatus.OPEN,
    )


def state(*, capital="10000", cash=None, positions=(), nav=None, high_water=None) -> PortfolioState:
    capital_d = Decimal(capital)
    return PortfolioState(
        satellite_capital=capital_d,
        cash=Decimal(cash) if cash is not None else capital_d,
        positions=list(positions),
        nav=Decimal(nav) if nav is not None else capital_d,
        high_water_mark=Decimal(high_water) if high_water is not None else capital_d,
        fx=USD,
    )


@pytest.fixture()
def engine() -> RiskEngine:
    return RiskEngine(RiskLimits(), sectors=SECTORS)


def failures(verdict) -> set[RiskCheckId]:
    return {c.check for c in verdict.failures}


# ---------------------------------------------------------------- sizing


class TestPositionSizing:
    def test_size_is_the_risk_budget_divided_by_the_stop_distance(self, engine):
        """£10,000 satellite, 1% risk = £100. Entry 50, stop 45 -> £5 at risk
        per share -> 20 shares -> £1,000 exposure, which is 10% of satellite."""
        verdict = engine.evaluate(idea(), entry=Decimal("50"), stop=Decimal("45"),
                                  state=state(), as_of=AS_OF)
        assert verdict.approved
        assert verdict.plan.shares == 20
        assert verdict.plan.gbp_exposure == Decimal("1000.00")
        assert verdict.plan.gbp_risk == Decimal("100.00")

    def test_a_tighter_stop_buys_more_shares_for_the_same_risk(self, engine):
        wide = engine.evaluate(idea(), entry=Decimal("50"), stop=Decimal("40"),
                               state=state(), as_of=AS_OF)
        tight = engine.evaluate(idea(), entry=Decimal("50"), stop=Decimal("49"),
                                state=state(), as_of=AS_OF)
        assert tight.plan.shares > wide.plan.shares
        # ...but the pounds at risk are the same, which is the whole idea.
        assert wide.plan.gbp_risk == Decimal("100.00")

    def test_the_ten_percent_cap_overrides_the_risk_formula(self, engine):
        """A very tight stop would otherwise buy a position larger than the
        whole satellite allocation. The cap is what stops that."""
        verdict = engine.evaluate(idea(), entry=Decimal("50"), stop=Decimal("49.90"),
                                  state=state(), as_of=AS_OF)
        assert verdict.plan.gbp_exposure <= Decimal("1000")
        assert verdict.plan.fraction_of_satellite <= Decimal("0.10")
        # And the real risk taken is now *below* the 1% budget, never above it.
        assert verdict.plan.gbp_risk < Decimal("100")

    def test_a_usd_name_is_sized_in_gbp_through_the_fx_rate(self, engine):
        """Entry $50 at 0.80 is £40; stop $45 is £36; £4 of GBP risk per share
        -> 25 shares. Sizing a USD name off dollar figures would put 25% more
        money at risk than intended."""
        verdict = engine.evaluate(idea("D.US"), entry=Decimal("50"), stop=Decimal("45"),
                                  state=state(), as_of=AS_OF)
        assert verdict.plan.shares == 25
        assert verdict.plan.gbp_exposure == Decimal("1000.00")
        assert verdict.plan.fx_rate_used == Decimal("0.80")

    def test_fractional_shares_round_down_never_up(self, engine):
        """£100 risk budget / £3 stop distance = 33.33 shares. Rounding up would
        put £102 at risk — over budget on every position, every time.

        Entry is £10 here so the 10% cap (£1,000) does not bind first and the
        rounding is genuinely what is being tested.
        """
        verdict = engine.evaluate(idea(), entry=Decimal("10"), stop=Decimal("7"),
                                  state=state(), as_of=AS_OF)
        assert verdict.plan.shares == 33
        assert verdict.plan.gbp_risk == Decimal("99.00")
        assert verdict.plan.gbp_risk <= Decimal("100")

    def test_a_stop_above_the_entry_is_rejected(self, engine):
        verdict = engine.evaluate(idea(), entry=Decimal("50"), stop=Decimal("55"),
                                  state=state(), as_of=AS_OF)
        assert RiskCheckId.STOP_BELOW_ENTRY in failures(verdict)
        assert not verdict.approved

    def test_a_stop_equal_to_the_entry_is_rejected_not_infinite(self, engine):
        """Zero stop distance divides by zero in the sizing formula."""
        verdict = engine.evaluate(idea(), entry=Decimal("50"), stop=Decimal("50"),
                                  state=state(), as_of=AS_OF)
        assert RiskCheckId.STOP_BELOW_ENTRY in failures(verdict)

    def test_a_position_too_small_to_be_worth_the_costs_is_rejected(self, engine):
        verdict = engine.evaluate(idea(), entry=Decimal("50"), stop=Decimal("45"),
                                  state=state(capital="1000"), as_of=AS_OF)
        assert RiskCheckId.POSITION_SIZE_POSITIVE in failures(verdict)

    def test_an_entry_larger_than_the_whole_cap_yields_no_shares(self, engine):
        verdict = engine.evaluate(idea(), entry=Decimal("5000"), stop=Decimal("4000"),
                                  state=state(), as_of=AS_OF)
        assert RiskCheckId.POSITION_SIZE_POSITIVE in failures(verdict)


# ---------------------------------------------------------------- limits


class TestConcentrationLimits:
    def test_sector_exposure_is_capped_at_thirty_percent(self, engine):
        """A.LSE and B.LSE are both consumer. £2,500 already there; a third
        £1,000 consumer position would breach 30% of £10,000."""
        held = [position("B.LSE", shares=50, entry=50)]     # £2,500 consumer
        first = engine.evaluate(idea("A.LSE"), entry=Decimal("50"), stop=Decimal("45"),
                                state=state(positions=held), as_of=AS_OF)
        assert first.approved  # 2,500 + 1,000 = 3,500... over 3,000
        assert first.plan.gbp_exposure == Decimal("500.00")

    def test_a_sector_already_at_its_limit_admits_nothing(self, engine):
        held = [position("B.LSE", shares=60, entry=50)]     # £3,000 = the whole 30%
        verdict = engine.evaluate(idea("A.LSE"), entry=Decimal("50"), stop=Decimal("45"),
                                  state=state(positions=held), as_of=AS_OF)
        assert RiskCheckId.MAX_SECTOR_CONCENTRATION in failures(verdict)
        assert not verdict.approved

    def test_a_different_sector_is_unaffected_by_a_full_one(self, engine):
        held = [position("B.LSE", shares=60, entry=50)]     # consumer full
        verdict = engine.evaluate(idea("C.LSE"), entry=Decimal("50"), stop=Decimal("45"),
                                  state=state(positions=held), as_of=AS_OF)
        assert verdict.approved

    def test_unmapped_tickers_share_one_bucket_rather_than_escaping_the_limit(self):
        """An unknown sector must concentrate, not evade. The safe direction."""
        engine = RiskEngine(RiskLimits(), sectors={})
        held = [position("Z.LSE", shares=60, entry=50, sector="unknown")]
        verdict = engine.evaluate(idea("Y.LSE"), entry=Decimal("50"), stop=Decimal("45"),
                                  state=state(positions=held), as_of=AS_OF)
        assert RiskCheckId.MAX_SECTOR_CONCENTRATION in failures(verdict)

    def test_the_swing_sub_allocation_is_capped_below_the_long_term_one(self, engine):
        held = [position("C.LSE", shares=50, entry=50, idea_class=IdeaClass.SWING)]  # £2,500
        verdict = engine.evaluate(
            idea("E.LSE", IdeaClass.SWING), entry=Decimal("50"), stop=Decimal("45"),
            state=state(positions=held), as_of=AS_OF,
        )
        # 25% of £10,000 is £2,500 — already spent.
        assert RiskCheckId.SWING_SUB_ALLOCATION in failures(verdict)

    def test_swing_exposure_does_not_restrict_a_long_term_idea(self, engine):
        held = [position("C.LSE", shares=50, entry=50, idea_class=IdeaClass.SWING)]
        verdict = engine.evaluate(idea("E.LSE"), entry=Decimal("50"), stop=Decimal("45"),
                                  state=state(positions=held), as_of=AS_OF)
        assert verdict.approved

    def test_a_second_position_in_the_same_name_is_refused(self, engine):
        held = [position("A.LSE", shares=10, entry=50)]
        verdict = engine.evaluate(idea("A.LSE"), entry=Decimal("50"), stop=Decimal("45"),
                                  state=state(positions=held), as_of=AS_OF)
        assert RiskCheckId.NO_DUPLICATE_POSITION in failures(verdict)

    def test_a_closed_position_does_not_block_a_new_one(self, engine):
        closed = position("A.LSE", shares=10, entry=50).model_copy(
            update={"status": PositionStatus.CLOSED_STOP}
        )
        verdict = engine.evaluate(idea("A.LSE"), entry=Decimal("50"), stop=Decimal("45"),
                                  state=state(positions=[closed]), as_of=AS_OF)
        assert verdict.approved

    def test_the_open_position_count_is_capped(self, engine):
        held = [position(f"T{i}.LSE", shares=1, entry=10, sector=f"s{i}") for i in range(12)]
        verdict = engine.evaluate(idea("A.LSE"), entry=Decimal("50"), stop=Decimal("45"),
                                  state=state(positions=held), as_of=AS_OF)
        assert RiskCheckId.MAX_OPEN_POSITIONS in failures(verdict)
        assert not verdict.approved

    def test_a_position_larger_than_available_cash_is_refused(self, engine):
        verdict = engine.evaluate(idea(), entry=Decimal("50"), stop=Decimal("45"),
                                  state=state(cash="100"), as_of=AS_OF)
        assert RiskCheckId.SUFFICIENT_CASH in failures(verdict)


# ---------------------------------------------------------------- mandatory fields


class TestMandatoryLevels:
    def test_a_swing_idea_without_a_stop_is_refused(self, engine):
        verdict = engine.evaluate(idea("A.LSE", IdeaClass.SWING), entry=Decimal("50"),
                                  stop=None, state=state(), as_of=AS_OF)
        assert RiskCheckId.HAS_STOP in failures(verdict)

    def test_a_long_term_idea_without_a_written_invalidation_is_refused(self, engine):
        blank = idea(invalidation="")
        verdict = engine.evaluate(blank, entry=Decimal("50"), stop=Decimal("45"),
                                  state=state(), as_of=AS_OF)
        assert RiskCheckId.HAS_INVALIDATION in failures(verdict)

    def test_an_idea_with_no_memo_at_all_is_refused(self, engine):
        bare = idea().model_copy(update={"memo": None})
        verdict = engine.evaluate(bare, entry=Decimal("50"), stop=Decimal("45"),
                                  state=state(), as_of=AS_OF)
        assert RiskCheckId.HAS_INVALIDATION in failures(verdict)


# ---------------------------------------------------------------- kill switch


class TestKillSwitch:
    def test_a_fifteen_percent_drawdown_stops_new_swing_ideas(self, engine):
        drawn = state(nav="8400", high_water="10000")     # -16%
        verdict = engine.evaluate(idea("A.LSE", IdeaClass.SWING), entry=Decimal("50"),
                                  stop=Decimal("45"), state=drawn, as_of=AS_OF)
        assert RiskCheckId.DRAWDOWN_KILL_SWITCH in failures(verdict)

    def test_long_term_ideas_survive_the_kill_switch(self, engine):
        """The spec stops *short-term* ideas. A long-term thesis is not
        invalidated by the portfolio being down."""
        drawn = state(nav="8400", high_water="10000")
        verdict = engine.evaluate(idea("A.LSE"), entry=Decimal("50"), stop=Decimal("45"),
                                  state=drawn, as_of=AS_OF)
        assert RiskCheckId.DRAWDOWN_KILL_SWITCH not in failures(verdict)

    def test_a_drawdown_just_inside_the_limit_does_not_trip_it(self, engine):
        assert engine.kill_switch_active(state(nav="8600", high_water="10000")) is False

    def test_exactly_at_the_limit_does_not_trip_it(self, engine):
        assert engine.kill_switch_active(state(nav="8500", high_water="10000")) is False

    def test_a_hair_past_the_limit_trips_it(self, engine):
        assert engine.kill_switch_active(state(nav="8499", high_water="10000")) is True

    def test_drawdown_is_measured_from_the_high_water_mark_not_the_start(self, engine):
        """Up 50% then down 16% from the peak is a kill-switch event even though
        the portfolio is well ahead of where it started."""
        assert engine.kill_switch_active(
            state(capital="10000", nav="12600", high_water="15000")
        ) is True

    def test_a_zero_high_water_mark_does_not_divide_by_zero(self, engine):
        assert engine.kill_switch_active(state(nav="0", high_water="0")) is False


# ---------------------------------------------------------------- data quality


class TestDataFreshness:
    def test_stale_data_blocks_the_idea_entirely(self, engine):
        """Phase 1's rule with teeth: a signal generated from bad data is a
        Sev-1, so it must not become a position."""
        verdict = engine.evaluate(idea(), entry=Decimal("50"), stop=Decimal("45"),
                                  state=state(), as_of=AS_OF, data_stale=True)
        assert RiskCheckId.DATA_FRESHNESS in failures(verdict)
        assert not verdict.approved


# ---------------------------------------------------------------- reporting


class TestVerdictReporting:
    def test_every_check_is_reported_not_just_the_first_failure(self, engine):
        """'Failed checks are logged with reasons' — plural. Short-circuiting
        would hide a second breach behind the first and make the next run look
        like a new problem."""
        held = [position("B.LSE", shares=60, entry=50)]
        verdict = engine.evaluate(idea("A.LSE", IdeaClass.SWING, invalidation=""),
                                  entry=Decimal("50"), stop=None,
                                  state=state(positions=held, nav="8000", high_water="10000"),
                                  as_of=AS_OF, data_stale=True)
        assert len(verdict.failures) >= 3
        assert all(c.reason for c in verdict.failures)

    def test_a_clean_idea_reports_every_check_as_passed(self, engine):
        verdict = engine.evaluate(idea(), entry=Decimal("50"), stop=Decimal("45"),
                                  state=state(), as_of=AS_OF)
        assert verdict.approved
        assert verdict.failures == ()
        assert len(verdict.checks) >= 8

    def test_failure_reasons_name_the_check_and_the_numbers(self, engine):
        held = [position("B.LSE", shares=60, entry=50)]
        verdict = engine.evaluate(idea("A.LSE"), entry=Decimal("50"), stop=Decimal("45"),
                                  state=state(positions=held), as_of=AS_OF)
        reasons = verdict.failure_reasons
        assert any("max_sector_concentration" in r and "consumer" in r for r in reasons)

    def test_a_rejected_idea_carries_no_plan(self, engine):
        verdict = engine.evaluate(idea(), entry=Decimal("50"), stop=Decimal("55"),
                                  state=state(), as_of=AS_OF)
        assert verdict.plan is None


class TestNoOverrides:
    def test_conviction_cannot_buy_a_bigger_position(self, engine):
        """No signal, no conviction level and no LLM output may override a
        limit. The engine never reads conviction at all — this test is what
        stops someone adding that."""
        low = idea().model_copy(update={"conviction": Conviction.LOW})
        high = idea().model_copy(update={"conviction": Conviction.HIGH})
        a = engine.evaluate(low, entry=Decimal("50"), stop=Decimal("45"), state=state(), as_of=AS_OF)
        b = engine.evaluate(high, entry=Decimal("50"), stop=Decimal("45"), state=state(), as_of=AS_OF)
        assert a.plan.shares == b.plan.shares

    def test_a_higher_composite_score_cannot_buy_a_bigger_position(self, engine):
        weak = idea().model_copy(update={"composite_score": Decimal("51")})
        strong = idea().model_copy(update={"composite_score": Decimal("99")})
        a = engine.evaluate(weak, entry=Decimal("50"), stop=Decimal("45"), state=state(), as_of=AS_OF)
        b = engine.evaluate(strong, entry=Decimal("50"), stop=Decimal("45"), state=state(), as_of=AS_OF)
        assert a.plan.gbp_exposure == b.plan.gbp_exposure


class TestExplicitCurrency:
    def test_an_explicitly_named_currency_beats_the_ticker_suffix(self, engine):
        """A GBP-suffixed ticker quoted in USD (a depositary receipt, a dual
        listing) must size off the currency it actually trades in."""
        verdict = engine.evaluate(idea("A.LSE"), entry=Decimal("50"), stop=Decimal("45"),
                                  state=state(), as_of=AS_OF, currency="USD")
        assert verdict.plan.fx_rate_used == Decimal("0.80")
        assert verdict.plan.shares == 25

    def test_an_unknown_currency_falls_back_to_parity_rather_than_crashing(self, engine):
        verdict = engine.evaluate(idea("A.LSE"), entry=Decimal("50"), stop=Decimal("45"),
                                  state=state(), as_of=AS_OF, currency="JPY")
        assert verdict.plan.fx_rate_used == Decimal("1")


class TestDeRiskBrief:
    def test_no_brief_while_the_portfolio_is_healthy(self, engine):
        assert engine.review_required(state()) is None

    def test_the_kill_switch_produces_an_actionable_brief(self, engine):
        text = engine.review_required(state(nav="8000", high_water="10000"))
        assert text is not None
        assert "20.0%" in text and "no new short-term ideas" in text.lower()


class TestStopMonitoring:
    def test_a_mark_at_or_below_the_stop_is_a_hit(self, engine):
        held = [position("A.LSE", shares=10, entry=50)]      # stop = 45
        s = state(positions=held)
        assert engine.stops_hit(s, {"A.LSE": Decimal("44")}) != []
        assert engine.stops_hit(s, {"A.LSE": Decimal("45")}) != []
        assert engine.stops_hit(s, {"A.LSE": Decimal("46")}) == []

    def test_a_position_with_no_mark_is_not_assumed_safe_or_stopped(self, engine):
        held = [position("A.LSE", shares=10, entry=50)]
        assert engine.stops_hit(state(positions=held), {}) == []

    def test_a_position_with_no_stop_cannot_be_stopped_out(self, engine):
        held = [position("A.LSE", shares=10, entry=50).model_copy(update={"stop": None})]
        s = state(positions=held)
        assert engine.stops_hit(s, {"A.LSE": Decimal("1")}) == []
        assert engine.approaching_stop(s, {"A.LSE": Decimal("1")}) == []

    def test_positions_close_to_their_stop_are_listed_for_the_brief(self, engine):
        held = [position("A.LSE", shares=10, entry=50)]      # stop = 45
        s = state(positions=held)
        assert engine.approaching_stop(s, {"A.LSE": Decimal("46")}) != []   # 2.2% away
        assert engine.approaching_stop(s, {"A.LSE": Decimal("60")}) == []   # 25% away

    def test_an_already_stopped_position_is_not_also_reported_as_approaching(self, engine):
        held = [position("A.LSE", shares=10, entry=50)]
        assert engine.approaching_stop(state(positions=held), {"A.LSE": Decimal("40")}) == []

    def test_approaching_stop_skips_names_with_no_mark(self, engine):
        held = [position("A.LSE", shares=10, entry=50)]
        assert engine.approaching_stop(state(positions=held), {}) == []


class TestSectorAllocation:
    def test_weights_are_fractions_of_satellite_capital(self, engine):
        from sentinel.risk import sector_allocation

        held = [position("A.LSE", shares=20, entry=50),      # £1,000 consumer
                position("C.LSE", shares=10, entry=50)]      # £500 technology
        weights = sector_allocation(state(positions=held))
        assert weights == {"consumer": Decimal("0.1000"), "technology": Decimal("0.0500")}

    def test_current_marks_are_used_when_supplied(self, engine):
        """Cost basis understates a position that has run; the concentration
        limit should see what the money is worth now."""
        from sentinel.risk import sector_allocation

        held = [position("A.LSE", shares=20, entry=50)]
        s = state(positions=held)
        s.marks = {"A.LSE": Decimal("100")}
        assert sector_allocation(s)["consumer"] == Decimal("0.2000")

    def test_no_capital_yields_no_weights_rather_than_dividing_by_zero(self, engine):
        from sentinel.risk import sector_allocation

        s = state()
        s.satellite_capital = Decimal("0")
        assert sector_allocation(s) == {}


class TestSizingGuards:
    def test_a_negative_stop_is_refused(self):
        from sentinel.risk import size

        result = size(entry=Decimal("50"), stop=Decimal("-5"),
                      satellite_capital=Decimal("10000"), risk_per_trade_pct=Decimal("1"))
        assert result.shares == 0 and result.binding_cap == "invalid_stop"

    def test_a_zero_entry_is_refused(self):
        from sentinel.risk import size

        result = size(entry=Decimal("0"), stop=Decimal("0"),
                      satellite_capital=Decimal("10000"), risk_per_trade_pct=Decimal("1"))
        assert result.shares == 0

    def test_a_zero_fx_rate_cannot_produce_an_infinite_position(self):
        """A vendor returning 0.0 for a rate would otherwise make the stop
        distance zero and the position size unbounded."""
        from sentinel.risk import size

        result = size(entry=Decimal("50"), stop=Decimal("45"), fx_rate=Decimal("0"),
                      satellite_capital=Decimal("10000"), risk_per_trade_pct=Decimal("1"))
        assert result.shares == 0 and result.binding_cap == "invalid_stop"

    def test_the_unconstrained_size_is_reported_alongside_the_capped_one(self):
        """So the brief can say 'the cap is what limited this', not just 'here
        is a number'."""
        from sentinel.risk import size, SizingCap

        result = size(entry=Decimal("10"), stop=Decimal("9"),
                      satellite_capital=Decimal("10000"), risk_per_trade_pct=Decimal("1"),
                      caps=(SizingCap("max_single_position", Decimal("500")),))
        assert result.unconstrained_shares == 100
        assert result.shares == 50
        assert result.binding_cap == "max_single_position"
