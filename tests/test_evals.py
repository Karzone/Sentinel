"""Phase 4 evals: the tests that check the system can report bad news."""

from __future__ import annotations

from decimal import Decimal

import pytest

from sentinel.evals import calibration, judge, signal_quality as sq
from sentinel.llm.fake import ScriptedClient
from sentinel.llm.client import SchemaViolation


def call(predicted, realised, materiality=3):
    return sq.DirectionalCall(ticker="X", predicted=predicted, realised_return=realised,
                              materiality=materiality)


class TestWilsonInterval:
    def test_a_small_sample_produces_a_wide_interval(self):
        narrow = sq.wilson_interval(300, 500)
        wide = sq.wilson_interval(6, 10)
        assert (wide.high - wide.low) > (narrow.high - narrow.low) * 3

    def test_the_interval_never_escapes_zero_to_one(self):
        """The normal approximation can put a lower bound below zero at these
        sample sizes, which reads as reassuring nonsense."""
        interval = sq.wilson_interval(1, 5)
        assert 0.0 <= interval.low and interval.high <= 1.0

    def test_no_trials_means_no_interval(self):
        assert sq.wilson_interval(0, 0) is None


class TestDirectionAccuracy:
    def test_flat_calls_are_not_scored_as_right_or_wrong(self):
        """Otherwise a module farms accuracy by never committing."""
        calls = [call("long", 0.05), call("flat", 0.20), call("flat", -0.20)]
        result = sq.direction_accuracy(calls)
        assert result.calls == 3 and result.scoreable == 1
        assert result.abstention_rate == pytest.approx(2 / 3)

    def test_an_avoid_call_is_correct_when_the_price_falls(self):
        result = sq.direction_accuracy([call("avoid", -0.10), call("avoid", 0.10)])
        assert result.correct == 1

    def test_a_small_sample_gets_no_verdict_however_good_the_hit_rate(self):
        """The kill criteria specify >= 100 samples. Ten out of ten is not a
        result, and a system that reported it as one would be lying."""
        result = sq.direction_accuracy([call("long", 0.05)] * 10)
        assert result.hit_rate == 1.0
        assert "no verdict yet" in result.verdict()

    def test_a_coin_flip_over_a_large_sample_is_named_as_one(self):
        calls = [call("long", 0.01) for _ in range(60)] + [call("long", -0.01) for _ in range(60)]
        result = sq.direction_accuracy(calls)
        assert result.beats_coin_flip is False
        assert "summary-only" in result.verdict()

    def test_a_real_edge_over_a_large_sample_is_recognised(self):
        calls = [call("long", 0.01) for _ in range(75)] + [call("long", -0.01) for _ in range(25)]
        result = sq.direction_accuracy(calls)
        assert result.beats_coin_flip is True
        assert "better than chance" in result.verdict()

    def test_being_significantly_worse_than_a_coin_is_said_out_loud(self):
        calls = [call("long", -0.01) for _ in range(80)] + [call("long", 0.01) for _ in range(20)]
        result = sq.direction_accuracy(calls)
        assert "WORSE than a coin flip" in result.verdict()

    def test_no_calls_at_all_is_reported_not_crashed(self):
        assert sq.direction_accuracy([]).verdict() == "no directional calls to score"


class TestMaterialityCalibration:
    def test_a_calibrated_scale_has_bigger_moves_at_higher_ratings(self):
        calls = ([call("long", 0.01, materiality=1)] * 5
                 + [call("long", 0.05, materiality=3)] * 5
                 + [call("long", 0.15, materiality=5)] * 5)
        result = sq.materiality_calibration(calls)
        assert result.monotonic is True
        assert "is calibrated" in result.verdict()

    def test_a_decorative_scale_is_called_decorative(self):
        calls = ([call("long", 0.15, materiality=1)] * 5
                 + [call("long", 0.01, materiality=5)] * 5)
        result = sq.materiality_calibration(calls)
        assert result.monotonic is False
        assert "decorative" in result.verdict()

    def test_magnitude_is_scored_on_absolute_moves(self):
        """Materiality is a claim about size, not direction; mixing the two
        would make a well-calibrated scale look broken over a directional miss."""
        calls = [call("long", -0.20, materiality=5)] * 3 + [call("long", 0.01, materiality=1)] * 3
        assert sq.materiality_calibration(calls).monotonic is True

    def test_one_bucket_yields_no_verdict(self):
        assert sq.materiality_calibration([call("long", 0.1)]).monotonic is None


class TestConsistency:
    def test_identical_repeat_runs_score_one_hundred_percent(self):
        runs = [{"sentiment": 1}, {"sentiment": 1}, {"sentiment": 1}]
        result = sq.inter_run_consistency(runs, key="sentiment")
        assert result.rate == 1.0 and result.meets_target

    def test_drifting_runs_fall_below_the_target(self):
        runs = [{"sentiment": 1}, {"sentiment": 2}, {"sentiment": 1}, {"sentiment": 0}]
        result = sq.inter_run_consistency(runs, key="sentiment")
        assert result.meets_target is False
        assert "below the 90% target" in result.verdict()

    def test_one_run_cannot_measure_consistency(self):
        assert sq.inter_run_consistency([{"sentiment": 1}], key="sentiment").rate is None


class TestSchemaCompliance:
    def test_a_compliant_module_meets_the_target(self):
        text = sq.schema_compliance_verdict({"rate": 0.995, "first_pass_rate": 0.97})
        assert "meets" in text and "97%" in text

    def test_a_non_compliant_module_is_flagged(self):
        assert "BELOW" in sq.schema_compliance_verdict({"rate": 0.90, "first_pass_rate": 0.85})

    def test_no_calls_yet_is_said_plainly(self):
        assert sq.schema_compliance_verdict({"rate": None}) == "no LLM calls recorded yet"


class TestBrier:
    def test_a_perfectly_calibrated_forecaster_scores_zero(self):
        assert calibration.brier_score([(1.0, True), (0.0, False)]).score == 0.0

    def test_confident_and_wrong_scores_worst(self):
        assert calibration.brier_score([(1.0, False)]).score == 1.0

    def test_it_is_reported_against_the_base_rate_not_alone(self):
        """A raw Brier of 0.20 sounds respectable and can still be worse than
        always guessing the base rate."""
        calls = [(0.5, True)] * 8 + [(0.5, False)] * 2
        result = calibration.brier_score(calls)
        assert result.baseline is not None and result.skill is not None
        assert result.skill < 0
        assert "carry no information" in result.verdict()

    def test_a_genuinely_informative_forecast_shows_positive_skill(self):
        calls = [(0.9, True)] * 8 + [(0.1, False)] * 2
        assert calibration.brier_score(calls).skill > 0

    def test_no_calls_yields_no_score(self):
        assert calibration.brier_score([]).score is None


class TestConvictionCalibration:
    def test_ordered_bands_are_reported_as_calibrated(self):
        outcomes = ([("low", 0.01)] * 5 + [("medium", 0.05)] * 5 + [("high", 0.12)] * 5)
        result = calibration.conviction_calibration(outcomes)
        assert result.ordered is True
        assert "is calibrated" in result.verdict()

    def test_inverted_bands_are_called_noise(self):
        """The verdict the spec pre-commits to: if high does not beat low, the
        label is noise and must not be read as information."""
        outcomes = ([("low", 0.12)] * 5 + [("medium", 0.05)] * 5 + [("high", 0.01)] * 5)
        result = calibration.conviction_calibration(outcomes)
        assert result.ordered is False
        assert "NOT calibrated" in result.verdict()
        assert "noise" in result.verdict()

    def test_one_band_gives_no_verdict(self):
        assert calibration.conviction_calibration([("high", 0.1)]).ordered is None


class TestStopQuality:
    def test_stops_that_mostly_recover_are_called_noise_harvesting(self):
        outcomes = [(Decimal("45"), Decimal("60"))] * 8 + [(Decimal("45"), Decimal("30"))] * 2
        result = calibration.stop_quality(outcomes)
        assert result.recovery_rate == 0.8
        assert "harvesting noise" in result.verdict()

    def test_stops_that_mostly_catch_real_breaks_are_recognised(self):
        outcomes = [(Decimal("45"), Decimal("20"))] * 9 + [(Decimal("45"), Decimal("60"))]
        assert "catching real breaks" in calibration.stop_quality(outcomes).verdict()

    def test_nothing_stopped_out_yields_no_verdict(self):
        assert calibration.stop_quality([]).recovery_rate is None


class TestKillCriteria:
    def _base(self, **kw):
        defaults = dict(
            paper_months=7.0, strategy_sharpe=0.9, benchmark_sharpe=0.6,
            strategy_return=0.14, benchmark_return=0.10,
            catalyst_samples=150, catalyst_beats_coin_flip=True, risk_bypass_bugs=0,
        )
        defaults.update(kw)
        return calibration.KillCriteria(**defaults)

    def test_the_six_month_gate_blocks_a_verdict_however_good_the_numbers(self):
        criteria = self._base(paper_months=3.0, strategy_sharpe=3.0, strategy_return=0.9)
        assert criteria.short_term_module_demoted is None
        assert "the gate is 6" in criteria.verdicts()[0]

    def test_losing_on_both_sharpe_and_return_demotes_the_short_term_module(self):
        criteria = self._base(strategy_sharpe=0.3, strategy_return=0.05)
        assert criteria.short_term_module_demoted is True
        assert any("stays indexed" in v for v in criteria.verdicts())

    def test_winning_on_either_measure_keeps_the_module(self):
        """Both conditions must fail, per the spec's wording — a strategy with a
        better Sharpe on a lower return has not failed."""
        assert self._base(strategy_return=0.05).short_term_module_demoted is False

    def test_a_coin_flip_catalyst_module_loses_its_directional_field(self):
        criteria = self._base(catalyst_beats_coin_flip=False)
        assert criteria.catalyst_module_demoted is True
        assert any("summary-only" in v for v in criteria.verdicts())

    def test_a_risk_bypass_bug_suspends_real_money_immediately(self):
        criteria = self._base(risk_bypass_bugs=1)
        assert criteria.real_money_suspended is True
        assert any("suspended" in v for v in criteria.verdicts())


class TestJudge:
    MEMO_EVIDENCE = ()

    def _memo(self):
        from sentinel.domain import Conviction, IdeaClass, IdeaMemo

        return IdeaMemo(
            ticker="X.LSE", thesis="T.", bull_case="B.", bear_case="Be.",
            invalidation="Margin below 12% in FY25.", idea_class=IdeaClass.LONG_TERM,
            conviction=Conviction.MEDIUM, horizon_days=365, claims=("growth",),
        )

    def test_a_hallucination_fails_the_memo_whatever_the_scores(self):
        """5/5 across the board still fails. Averaging a fabricated fact into a
        4.7 buries the only finding that mattered."""
        client = ScriptedClient({"judge": {
            "thesis_clarity": 5, "evidence_grounding": 5, "falsifiability": 5,
            "hallucinated_claims": ["insiders bought 2m shares in May"],
            "comment": "otherwise strong",
        }})
        verdict = judge.judge_memo(client, self._memo(), ())
        assert verdict.mean_score == 5.0
        assert verdict.passed is False
        assert "FAIL" in verdict.summary()

    def test_a_clean_memo_above_the_pass_mark_passes(self):
        client = ScriptedClient({"judge": {
            "thesis_clarity": 4, "evidence_grounding": 4, "falsifiability": 3,
            "hallucinated_claims": [], "comment": "fine",
        }})
        assert judge.judge_memo(client, self._memo(), ()).passed is True

    def test_a_clean_but_weak_memo_still_fails(self):
        client = ScriptedClient({"judge": {
            "thesis_clarity": 2, "evidence_grounding": 2, "falsifiability": 2,
            "hallucinated_claims": [], "comment": "vague",
        }})
        assert judge.judge_memo(client, self._memo(), ()).passed is False

    def test_the_judge_output_is_schema_checked_like_any_other_call(self):
        client = ScriptedClient({"judge": {"thesis_clarity": 9}})
        with pytest.raises(SchemaViolation):
            judge.judge_memo(client, self._memo(), ())

    def test_the_judge_sees_the_evidence_not_the_market(self):
        """It scores whether the memo is supported, not whether it was right;
        conflating the two makes a lucky memo look well-argued."""
        prompt = judge.build_prompt(self._memo(), ())
        assert "EVIDENCE AVAILABLE TO THE AUTHOR" in prompt
        assert "return" not in prompt.lower().split("evidence available")[0].split("invalidation")[0]

    def test_sampling_is_deterministic_so_a_rerun_judges_the_same_memos(self):
        month_one = [judge.should_judge(i, 1) for i in range(10)]
        assert all(month_one)
        later = [judge.should_judge(i, 4) for i in range(10)]
        assert later == [judge.should_judge(i, 4) for i in range(10)]
        assert sum(later) == 2      # 20% sampling


class TestKillCriteriaNeverGoesSilent:
    def _base(self, **kw):
        defaults = dict(
            paper_months=8.0, strategy_sharpe=None, benchmark_sharpe=None,
            strategy_return=None, benchmark_return=None,
            catalyst_samples=150, catalyst_beats_coin_flip=True, risk_bypass_bugs=0,
        )
        defaults.update(kw)
        return calibration.KillCriteria(**defaults)

    def test_past_the_gate_with_missing_inputs_says_so_rather_than_nothing(self):
        """Silence here reads identically to 'the gate passed' while actually
        meaning it was never evaluated. Caught by running a real weekly review."""
        verdicts = self._base().verdicts()
        assert any("CANNOT be evaluated" in v for v in verdicts)
        assert any("not the same as passed" in v for v in verdicts)

    def test_it_names_which_inputs_are_missing(self):
        verdicts = self._base(strategy_sharpe=0.4, strategy_return=0.05).verdicts()
        joined = " ".join(verdicts)
        assert "benchmark Sharpe" in joined and "benchmark return" in joined
        assert "strategy Sharpe" not in joined

    def test_a_fully_specified_comparison_still_gives_a_real_verdict(self):
        passed = self._base(strategy_sharpe=0.9, benchmark_sharpe=0.6,
                            strategy_return=0.14, benchmark_return=0.10).verdicts()
        assert any("Six-month paper gate passed" in v for v in passed)
        assert not any("CANNOT be evaluated" in v for v in passed)
