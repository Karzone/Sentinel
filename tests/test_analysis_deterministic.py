"""Golden-dataset tests for the two deterministic modules.

Structure: assert each *component* exactly (derivable by hand from the bands in
the module), then assert the composite is the weighted sum of those components.
Asserting only the composite would let two components drift in opposite
directions and still pass — which is the failure mode a regression suite is
supposed to catch.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from sentinel.analysis import fundamental, technical
from sentinel.domain import Bar, ModuleName
from tests.conftest import make_fundamentals


def path(ticker: str, closes, volumes=None, start=dt.date(2020, 1, 1)) -> list[Bar]:
    bars: list[Bar] = []
    day = start
    for i, close in enumerate(closes):
        while day.weekday() >= 5:
            day += dt.timedelta(days=1)
        price = Decimal(str(round(close, 4)))
        volume = int(volumes[i]) if volumes else 1_000_000
        bars.append(
            Bar(
                ticker=ticker, date=day, open=price, high=price, low=price,
                close=price, adjusted_close=price, volume=volume, currency="GBP",
            )
        )
        day += dt.timedelta(days=1)
    return bars


STEADY_RISE = [100 * (1.002 ** i) for i in range(400)]
STEADY_FALL = [100 * (0.998 ** i) for i in range(400)]
FLAT = [100.0] * 400


class TestTechnicalComponents:
    def test_a_relentless_uptrend_scores_trend_95_and_rsi_10(self):
        parts = technical.components(path("UP", STEADY_RISE))
        # price > 50d > 200d is 90, plus 5 for a 50-day rising more than 1%/month.
        assert parts["trend"][0] == Decimal("95")
        # Every day is an up day, so Wilder RSI pins at 100 -> "deeply overbought".
        assert parts["rsi"][0] == Decimal("10")
        # At the very top of its 6-month range: a poor entry, not a good one.
        assert parts["location"][0] == Decimal("40")

    def test_a_relentless_downtrend_scores_trend_10(self):
        parts = technical.components(path("DOWN", STEADY_FALL))
        assert parts["trend"][0] == Decimal("10")   # 15 below both, minus 5 falling
        assert parts["rsi"][0] == Decimal("25")     # RSI 0 -> extremely oversold
        assert parts["location"][0] == Decimal("45")

    def test_a_flat_series_is_neutral_on_every_directional_component(self):
        parts = technical.components(path("FLAT", FLAT))
        assert parts["rsi"][0] == Decimal("65")     # RSI 50 -> neutral band
        assert parts["momentum"][0] == Decimal("50")  # tanh(0) == 0
        assert parts["volume"][0] == Decimal("50")  # constant volume -> no z-score

    def test_composite_is_exactly_the_weighted_sum_of_components(self):
        bars = path("UP", STEADY_RISE)
        parts = technical.components(bars)
        expected = sum(value * technical.WEIGHTS[key] for key, (value, _) in parts.items())
        assert technical.score(bars).score == expected.quantize(Decimal("0.01"))

    def test_heavy_volume_against_a_downtrend_is_distribution_not_confirmation(self):
        """Same three-sigma volume print, opposite trends, opposite scores.

        This is the one component whose meaning depends on another component,
        so it gets its own test rather than riding on the composite.
        """
        spike = [1_000_000] * 395 + [9_000_000] * 5
        up = technical.components(path("UP", STEADY_RISE, spike))
        down = technical.components(path("DOWN", STEADY_FALL, spike))
        assert up["volume"][0] == Decimal("80")
        assert down["volume"][0] == Decimal("35")

    def test_an_uptrend_outscores_a_downtrend_overall(self):
        up = technical.score(path("UP", STEADY_RISE))
        down = technical.score(path("DOWN", STEADY_FALL))
        assert up.score > down.score
        assert up.module is ModuleName.TECHNICAL

    def test_short_history_lowers_confidence_rather_than_the_score(self):
        long_bars = path("X", STEADY_RISE)
        short_bars = path("X", STEADY_RISE[:120])
        assert technical.score(long_bars).confidence == Decimal("1")
        assert technical.score(short_bars).confidence < Decimal("0.5")

    def test_too_little_history_refuses_to_score_at_all(self):
        with pytest.raises(technical.InsufficientHistory):
            technical.score(path("X", STEADY_RISE[:30]))

    def test_evidence_weights_sum_to_the_composite(self):
        signal = technical.score(path("UP", STEADY_RISE))
        assert float(sum(e.weight for e in signal.evidence)) == pytest.approx(
            float(signal.score), abs=0.05
        )


class TestAtrStop:
    def test_the_stop_sits_two_atrs_below_the_last_close(self):
        # A series with a constant 2-point daily range gives ATR 2, so a 2x stop
        # is 4 below the close.
        bars = []
        day = dt.date(2023, 1, 2)
        for i in range(60):
            while day.weekday() >= 5:
                day += dt.timedelta(days=1)
            bars.append(Bar(
                ticker="X", date=day, open=Decimal("100"), high=Decimal("101"),
                low=Decimal("99"), close=Decimal("100"), adjusted_close=Decimal("100"),
                volume=1000, currency="GBP",
            ))
            day += dt.timedelta(days=1)
        assert technical.atr_stop(bars, multiple=Decimal("2")) == Decimal("96.0000")

    def test_a_wider_instrument_gets_a_wider_stop(self):
        """The reason ATR is used at all: a fixed percentage would put the same
        stop distance on a utility and a biotech."""
        calm = path("CALM", [100 + (i % 2) * 0.2 for i in range(80)])
        wild = path("WILD", [100 + (i % 2) * 12 for i in range(80)])
        calm_stop = technical.atr_stop(calm)
        wild_stop = technical.atr_stop(wild)
        assert calm_stop is not None and wild_stop is not None
        assert (Decimal("100") - wild_stop) > (Decimal("100") - calm_stop) * 5

    def test_no_stop_when_history_is_too_short(self):
        assert technical.atr_stop(path("X", [100] * 5)) is None


class TestPiotroski:
    def test_a_perfect_company_scores_nine_from_nine(self):
        f = make_fundamentals(
            net_income_ttm=Decimal("100"), net_income_prior_ttm=Decimal("50"),
            total_assets=Decimal("1000"), total_assets_prior=Decimal("1000"),
            operating_cash_flow_ttm=Decimal("150"),
            total_debt=Decimal("100"), total_debt_prior=Decimal("200"),
            current_assets=Decimal("300"), current_liabilities=Decimal("100"),
            current_assets_prior=Decimal("300"), current_liabilities_prior=Decimal("200"),
            shares_outstanding=Decimal("100"), shares_outstanding_prior=Decimal("100"),
            gross_margin=Decimal("0.5"), gross_margin_prior=Decimal("0.4"),
            revenue_ttm=Decimal("900"), revenue_prior_ttm=Decimal("800"),
        )
        passed, available, _ = fundamental.piotroski_f_score(f)
        assert (passed, available) == (9, 9)

    def test_a_failing_company_scores_zero_from_nine(self):
        f = make_fundamentals(
            net_income_ttm=Decimal("-100"), net_income_prior_ttm=Decimal("-50"),
            total_assets=Decimal("1000"), total_assets_prior=Decimal("1000"),
            operating_cash_flow_ttm=Decimal("-150"),
            total_debt=Decimal("300"), total_debt_prior=Decimal("200"),
            current_assets=Decimal("100"), current_liabilities=Decimal("300"),
            current_assets_prior=Decimal("300"), current_liabilities_prior=Decimal("200"),
            shares_outstanding=Decimal("150"), shares_outstanding_prior=Decimal("100"),
            gross_margin=Decimal("0.3"), gross_margin_prior=Decimal("0.4"),
            revenue_ttm=Decimal("700"), revenue_prior_ttm=Decimal("800"),
        )
        passed, available, _ = fundamental.piotroski_f_score(f)
        assert (passed, available) == (0, 9)

    def test_the_denominator_reports_what_was_computable(self):
        """5 of 9 and 5 of 5 are different claims. Reporting only the numerator
        would flatter every company with patchy vendor coverage."""
        sparse = make_fundamentals(
            net_income_ttm=Decimal("100"), total_assets=Decimal("1000"),
            operating_cash_flow_ttm=Decimal("150"),
            net_income_prior_ttm=None, total_assets_prior=None,
            total_debt_prior=None, current_assets_prior=None,
            current_liabilities_prior=None, shares_outstanding_prior=None,
            gross_margin_prior=None, revenue_prior_ttm=None,
        )
        passed, available, _ = fundamental.piotroski_f_score(sparse)
        assert available < 9 and passed <= available


class TestFundamentalScore:
    def test_a_strong_cheap_grower_beats_a_weak_expensive_one(self):
        strong = make_fundamentals(
            ticker="STRONG", revenue_ttm=Decimal("600"), revenue_prior_ttm=Decimal("400"),
            eps_ttm=Decimal("3"), eps_prior_ttm=Decimal("2"),
            net_margin=Decimal("0.25"), operating_margin=Decimal("0.30"),
            market_cap=Decimal("1000"), free_cash_flow_ttm=Decimal("90"),
            total_debt=Decimal("50"), total_equity=Decimal("500"),
            pe_ratio=Decimal("10"), pe_5y_median=Decimal("20"), pe_sector_median=Decimal("18"),
            ev_ebitda=Decimal("6"), ev_ebitda_sector_median=Decimal("12"),
        )
        weak = make_fundamentals(
            ticker="WEAK", revenue_ttm=Decimal("300"), revenue_prior_ttm=Decimal("400"),
            eps_ttm=Decimal("-1"), eps_prior_ttm=Decimal("2"),
            net_margin=Decimal("-0.10"), operating_margin=Decimal("-0.05"),
            market_cap=Decimal("1000"), free_cash_flow_ttm=Decimal("-40"),
            total_debt=Decimal("900"), total_equity=Decimal("200"),
            pe_ratio=Decimal("60"), pe_5y_median=Decimal("20"), pe_sector_median=Decimal("18"),
            ev_ebitda=Decimal("30"), ev_ebitda_sector_median=Decimal("12"),
        )
        assert fundamental.score(strong).score > Decimal("75")
        assert fundamental.score(weak).score < Decimal("30")

    def test_missing_components_lower_confidence_not_the_score(self):
        """The honest-uncertainty rule: unknown must not read as average."""
        full = make_fundamentals(ticker="FULL")
        thin = make_fundamentals(
            ticker="THIN", pe_ratio=None, pe_5y_median=None, pe_sector_median=None,
            ev_ebitda=None, ev_ebitda_sector_median=None,
        )
        assert fundamental.score(full).confidence == Decimal("1.00")
        # valuation carries 0.25 of the weight
        assert fundamental.score(thin).confidence == Decimal("0.75")

    def test_a_completely_empty_snapshot_is_neutral_with_zero_confidence(self):
        empty = fundamental.Fundamentals(ticker="EMPTY", as_of=dt.date(2024, 1, 1))
        signal = fundamental.score(empty)
        assert signal.score == Decimal("50") and signal.confidence == Decimal("0")

    def test_eps_crossing_zero_is_reported_as_a_turn_not_a_percentage(self):
        turned = make_fundamentals(eps_ttm=Decimal("0.5"), eps_prior_ttm=Decimal("-1"))
        value, label = fundamental._growth(turned)
        assert "EPS turned positive" in label

    def test_negative_equity_is_scored_as_the_hazard_it_is(self):
        broken = make_fundamentals(total_equity=Decimal("-100"), total_debt=Decimal("500"))
        value, label = fundamental._balance_sheet(broken)
        assert "negative shareholders' equity" in label
        assert value <= fundamental.NEGATIVE_EQUITY_CAP


class TestDeterminism:
    def test_the_same_input_scores_identically_every_time(self):
        """§5.4 reproducibility, for the half of the pipeline that has no excuse."""
        bars = path("X", STEADY_RISE)
        assert technical.score(bars).score == technical.score(bars).score
        f = make_fundamentals()
        assert fundamental.score(f).score == fundamental.score(f).score
