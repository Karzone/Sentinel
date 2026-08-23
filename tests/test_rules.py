"""Phase 2: the rules layer. Every test here is a memo the LLM was happy with."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from sentinel.analysis import rules, sentiment as sentiment_mod, news as news_mod, synthesis
from sentinel.domain import (
    CatalystRead, CatalystType, Conviction, Direction, Evidence, IdeaClass,
    IdeaMemo, ModuleName, SentimentRead, Signal,
)

AS_OF = dt.date(2024, 6, 28)


def signal(module: ModuleName, score="70", confidence="1", keys=("k1",)) -> Signal:
    return Signal(
        module=module, module_version="v1", ticker="X.LSE", as_of=AS_OF,
        score=Decimal(score), confidence=Decimal(confidence),
        evidence=tuple(Evidence(key=k, value="v", source=module.value) for k in keys),
    )


def memo(**kw) -> IdeaMemo:
    defaults = dict(
        ticker="X.LSE",
        thesis="Margins are recovering. The market has not repriced it. Cash generation covers the debt.",
        bull_case="Operating leverage.", bear_case="Input costs could spike again.",
        invalidation="Operating margin falls below 12% in the FY25 interim results.",
        idea_class=IdeaClass.LONG_TERM, conviction=Conviction.MEDIUM,
        horizon_days=365, claims=("k1",),
    )
    defaults.update(kw)
    return IdeaMemo(**defaults)


def catalyst(materiality=4, direction=Direction.LONG) -> CatalystRead:
    return CatalystRead(
        ticker="X.LSE", as_of=AS_OF, catalyst_type=CatalystType.EARNINGS,
        direction=direction, materiality=materiality, horizon_days=30,
        summary="Beat and raised.", headline_refs=("X beats",),
    )


def sentiment_read(value=1, herding=False) -> SentimentRead:
    return SentimentRead(
        ticker="X.LSE", as_of=AS_OF, sentiment=value, conviction=Decimal("0.8"),
        herding_risk=herding, rationale="tone", sample_size=20,
    )


def vet(m: IdeaMemo, *, signals=None, composite="70", **kw) -> list[str]:
    signals = signals if signals is not None else [
        signal(ModuleName.FUNDAMENTAL), signal(ModuleName.TECHNICAL)
    ]
    evidence = kw.pop("evidence", None)
    if evidence is None:
        evidence = [e for s in signals for e in s.evidence]
    return [
        r.rule for r in rules.vet(
            m, signals=signals, composite=Decimal(composite), evidence=evidence, **kw
        )
    ]


class TestR1SentimentIsNeverPrimary:
    def test_positive_sentiment_alone_cannot_carry_an_idea(self):
        """The philosophy's rule, enforced structurally: strip sentiment out and
        see whether anything is left standing."""
        weak = [signal(ModuleName.FUNDAMENTAL, "45"), signal(ModuleName.TECHNICAL, "40")]
        assert "R1" in vet(memo(), signals=weak, composite="55", sentiment=sentiment_read(2))

    def test_sentiment_alongside_a_real_setup_is_fine(self):
        strong = [signal(ModuleName.FUNDAMENTAL, "72"), signal(ModuleName.TECHNICAL, "68")]
        assert "R1" not in vet(memo(), signals=strong, sentiment=sentiment_read(2))

    def test_high_conviction_on_a_crowded_name_is_capped(self):
        out = vet(memo(conviction=Conviction.HIGH), composite="80",
                  sentiment=sentiment_read(2, herding=True))
        assert "R1b" in out


class TestR2Falsifiability:
    @pytest.mark.parametrize("text", [
        "Operating margin falls below 12% in the FY25 interim results.",
        "The shares close below £4.20.",
        "No contract renewal is announced by 2025-03-31.",
        "Q3 revenue misses consensus by more than 5%.",
    ])
    def test_a_checkable_invalidation_passes(self, text):
        assert "R2" not in vet(memo(invalidation=text))

    @pytest.mark.parametrize("text", [
        "If the thesis breaks.",
        "If fundamentals deteriorate.",
        "Should sentiment turn against the name.",
        "We exit if things get worse.",
    ])
    def test_a_vague_invalidation_is_rejected(self, text):
        out = vet(memo(invalidation=text))
        assert "R2" in out or "R2b" in out


class TestR3Hallucination:
    def test_a_claim_citing_evidence_no_module_produced_is_rejected(self):
        out = vet(memo(claims=("k1", "insider_buying")))
        assert "R3" in out

    def test_a_memo_citing_nothing_is_rejected(self):
        assert "R3b" in vet(memo(claims=()))

    def test_claims_that_all_trace_to_evidence_pass(self):
        signals = [signal(ModuleName.FUNDAMENTAL, keys=("growth", "valuation"))]
        assert "R3" not in vet(memo(claims=("growth", "valuation")), signals=signals)


class TestR4SwingNeedsACatalyst:
    def test_a_swing_with_no_catalyst_is_rejected(self):
        assert "R4" in vet(memo(idea_class=IdeaClass.SWING, horizon_days=21), catalyst=None)

    def test_a_swing_on_a_trivial_catalyst_is_rejected(self):
        out = vet(memo(idea_class=IdeaClass.SWING, horizon_days=21), catalyst=catalyst(1))
        assert "R4" in out

    def test_a_swing_built_on_bad_news_is_rejected(self):
        out = vet(memo(idea_class=IdeaClass.SWING, horizon_days=21),
                  catalyst=catalyst(5, Direction.AVOID))
        assert "R4b" in out

    def test_a_swing_on_a_material_catalyst_passes(self):
        out = vet(memo(idea_class=IdeaClass.SWING, horizon_days=21), catalyst=catalyst(4))
        assert out == []


class TestR5ThesisLength:
    def test_a_four_sentence_thesis_is_rejected(self):
        long = "One. Two. Three. Four."
        assert "R5" in vet(memo(thesis=long))

    def test_three_sentences_pass(self):
        assert "R5" not in vet(memo(thesis="One. Two. Three."))


class TestR6ConvictionMustBeEarned:
    def test_high_conviction_below_the_score_bar_is_rejected(self):
        assert "R6" in vet(memo(conviction=Conviction.HIGH), composite="62")

    def test_high_conviction_on_thin_data_is_rejected(self):
        thin = [signal(ModuleName.FUNDAMENTAL, "80", confidence="0.4"),
                signal(ModuleName.TECHNICAL, "80")]
        assert "R6b" in vet(memo(conviction=Conviction.HIGH), signals=thin, composite="80")

    def test_high_conviction_with_strong_agreement_passes(self):
        strong = [signal(ModuleName.FUNDAMENTAL, "82"), signal(ModuleName.TECHNICAL, "78")]
        assert vet(memo(conviction=Conviction.HIGH), signals=strong, composite="80") == []


class TestR7HorizonMatchesClass:
    def test_a_long_term_idea_with_a_two_week_horizon_is_rejected(self):
        assert "R7" in vet(memo(idea_class=IdeaClass.LONG_TERM, horizon_days=14))

    def test_a_swing_with_a_two_year_horizon_is_rejected(self):
        out = vet(memo(idea_class=IdeaClass.SWING, horizon_days=730), catalyst=catalyst())
        assert "R7" in out


class TestR8ProseCannotOutvoteNumbers:
    def test_a_below_neutral_composite_blocks_the_idea_however_good_the_memo(self):
        assert "R8" in vet(memo(), composite="41")


class TestConvictionCeiling:
    def test_it_reports_what_the_numbers_would_have_supported(self):
        strong = [signal(ModuleName.FUNDAMENTAL, "82"), signal(ModuleName.TECHNICAL, "78")]
        assert rules.conviction_ceiling(Decimal("80"), strong, None) is Conviction.HIGH
        assert rules.conviction_ceiling(Decimal("55"), strong, None) is Conviction.MEDIUM
        assert rules.conviction_ceiling(Decimal("30"), strong, None) is Conviction.LOW

    def test_herding_caps_it_at_medium_however_strong_the_score(self):
        strong = [signal(ModuleName.FUNDAMENTAL, "90")]
        ceiling = rules.conviction_ceiling(Decimal("90"), strong, sentiment_read(2, herding=True))
        assert ceiling is Conviction.MEDIUM


class TestSignalMapping:
    def test_a_crowded_name_scores_worse_for_being_liked(self):
        """The contrarian rule as arithmetic, so it holds without the model's
        cooperation."""
        calm = sentiment_mod.to_signal(sentiment_read(2, herding=False))
        crowded = sentiment_mod.to_signal(sentiment_read(2, herding=True))
        assert crowded.score == calm.score - sentiment_mod.HERDING_PENALTY

    def test_herding_does_not_flatter_negative_sentiment(self):
        # The penalty is for crowded enthusiasm; crowded pessimism is left alone.
        assert sentiment_mod.to_signal(sentiment_read(-2, True)).score == Decimal("30")

    def test_catalyst_direction_and_materiality_drive_the_score(self):
        assert news_mod.to_signal(catalyst(5, Direction.LONG)).score == Decimal("90")
        assert news_mod.to_signal(catalyst(5, Direction.AVOID)).score == Decimal("10")
        assert news_mod.to_signal(catalyst(3, Direction.FLAT)).score == Decimal("50")

    def test_a_trivial_catalyst_carries_little_confidence(self):
        assert news_mod.to_signal(catalyst(1)).confidence == Decimal("0.20")
        assert news_mod.to_signal(catalyst(5)).confidence == Decimal("1.00")


class TestComposite:
    def test_it_is_the_confidence_weighted_mean_of_the_modules(self):
        signals = [signal(ModuleName.FUNDAMENTAL, "80"), signal(ModuleName.TECHNICAL, "60")]
        # (80*0.40 + 60*0.30) / 0.70
        assert synthesis.composite_score(signals) == Decimal("71.43")

    def test_a_low_confidence_module_cannot_dominate(self):
        confident = [signal(ModuleName.FUNDAMENTAL, "90"), signal(ModuleName.TECHNICAL, "50")]
        unsure = [signal(ModuleName.FUNDAMENTAL, "90", confidence="0.2"),
                  signal(ModuleName.TECHNICAL, "50")]
        assert synthesis.composite_score(unsure) < synthesis.composite_score(confident)

    def test_no_signals_at_all_is_neutral_rather_than_zero(self):
        assert synthesis.composite_score([]) == Decimal("50")


class TestIdeaAssembly:
    def test_a_rejected_memo_still_produces_a_stored_idea(self):
        """Deleting rejections would make the rejection rate unmeasurable."""
        idea = synthesis.build_idea(
            "X.LSE", [signal(ModuleName.FUNDAMENTAL, "80"), signal(ModuleName.TECHNICAL, "80")],
            AS_OF, memo=memo(claims=("invented_key",)),
        )
        assert idea.accepted is False
        assert any("R3" in r for r in idea.rejected_by_rules)
        assert idea.direction is Direction.FLAT

    def test_the_inputs_digest_is_stable_across_module_ordering(self):
        """§5.4: two runs that saw the same facts must fingerprint identically."""
        a = [signal(ModuleName.FUNDAMENTAL), signal(ModuleName.TECHNICAL)]
        b = list(reversed(a))
        assert synthesis.inputs_digest(a, None, None) == synthesis.inputs_digest(b, None, None)

    def test_model_versions_are_recorded_on_every_idea(self):
        idea = synthesis.build_idea("X.LSE", [signal(ModuleName.FUNDAMENTAL)], AS_OF, memo=memo())
        assert idea.model_versions["synthesis"] == synthesis.SYNTHESIS_VERSION
        assert idea.model_versions["rules"] == rules.RULES_VERSION

    def test_the_composite_is_not_shown_to_the_memo_writer(self):
        """If the model could see the target it would write towards it, and the
        memo would stop being independent evidence about the score."""
        prompt = synthesis.build_prompt(
            "X.LSE", [signal(ModuleName.FUNDAMENTAL, "80")], AS_OF,
            catalyst=None, sentiment=None,
        )
        assert "composite" not in prompt.lower()
