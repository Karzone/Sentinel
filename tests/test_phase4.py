"""Phase 4: costs, ledger, backtester, benchmarks, metrics, evals."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from sentinel.backtest import benchmarks, costs as costs_mod, engine as bt
from sentinel.backtest.costs import CostModel
from sentinel.domain import Bar, IdeaClass, PositionStatus
from sentinel.evals import calibration, metrics, signal_quality
from sentinel.money import FxRates
from sentinel.portfolio import InsufficientCash, Ledger, apply_exits

USD = FxRates("2024-01-01", {"USD": Decimal("0.80")})


def bars(ticker, closes, start=dt.date(2023, 1, 2), currency="GBP", opens=None):
    out, day = [], start
    for i, close in enumerate(closes):
        while day.weekday() >= 5:
            day += dt.timedelta(days=1)
        c = Decimal(str(close))
        o = Decimal(str(opens[i])) if opens else c
        out.append(Bar(
            ticker=ticker, date=day, open=o, high=max(o, c) * Decimal("1.001"),
            low=min(o, c) * Decimal("0.999"), close=c, adjusted_close=c,
            volume=1_000_000, currency=currency,
        ))
        day += dt.timedelta(days=1)
    return out


# ---------------------------------------------------------------- costs


class TestUkCosts:
    def test_stamp_duty_is_charged_on_uk_buys_only(self):
        model = CostModel()
        buy = costs_mod.compute(model, ticker="A.LSE", shares=100, price=Decimal("10"), is_buy=True)
        sell = costs_mod.compute(model, ticker="A.LSE", shares=100, price=Decimal("10"), is_buy=False)
        assert buy.stamp_duty_gbp == Decimal("5.00")     # 0.5% of £1,000
        assert sell.stamp_duty_gbp == Decimal("0")

    def test_a_us_name_pays_no_stamp_duty(self):
        """Modelling a US line as if it paid SDRT overstates costs — dishonest
        in the other direction, and it would bias the system away from US names
        the spec expects most candidates to be."""
        c = costs_mod.compute(CostModel(), ticker="A.US", shares=100, price=Decimal("10"),
                              currency="USD", fx_rate=Decimal("0.80"), is_buy=True)
        assert c.stamp_duty_gbp == Decimal("0")
        assert c.fx_spread_gbp > 0

    def test_an_exempt_uk_line_pays_no_stamp_duty(self):
        model = CostModel(stamp_duty_exempt=frozenset({"VWRP.LSE"}))
        c = costs_mod.compute(model, ticker="VWRP.LSE", shares=100, price=Decimal("10"), is_buy=True)
        assert c.stamp_duty_gbp == Decimal("0")

    def test_the_ptm_levy_applies_above_the_threshold_only(self):
        model = CostModel()
        small = costs_mod.compute(model, ticker="A.LSE", shares=100, price=Decimal("10"), is_buy=True)
        large = costs_mod.compute(model, ticker="A.LSE", shares=2000, price=Decimal("10"), is_buy=True)
        assert small.ptm_levy_gbp == Decimal("0")
        assert large.ptm_levy_gbp == Decimal("1.00")

    def test_round_trip_drag_is_reported_as_a_fraction(self):
        """~1.7% on a £1,000 UK round trip: what a weekly rebalance has to clear
        fifty times a year before the strategy has been right about anything."""
        drag = costs_mod.round_trip_drag(CostModel(), ticker="A.LSE", notional_gbp=Decimal("1000"))
        assert Decimal("0.015") < drag < Decimal("0.020")

    def test_a_zero_notional_trade_costs_nothing(self):
        assert costs_mod.compute(CostModel(), ticker="A.LSE", shares=0,
                                 price=Decimal("10"), is_buy=True).total_gbp == Decimal("0")


# ---------------------------------------------------------------- ledger


class TestLedger:
    def test_cash_is_reduced_by_the_costs_as_well_as_the_notional(self):
        ledger = Ledger(Decimal("10000"))
        ledger.open(ticker="A.LSE", idea_id="i", idea_class=IdeaClass.LONG_TERM,
                    sector="consumer", shares=20, price=Decimal("50"),
                    date=dt.date(2024, 1, 2))
        # £1,000 notional + £5 commission + £1 slippage + £5 stamp duty
        assert ledger.cash == Decimal("8989.00")

    def test_a_round_trip_pnl_carries_both_halves_of_the_costs(self):
        """Otherwise a ClosedTrade's net P&L is only the sell half of the truth."""
        ledger = Ledger(Decimal("10000"))
        ledger.open(ticker="A.LSE", idea_id="i", idea_class=IdeaClass.LONG_TERM,
                    sector="consumer", shares=20, price=Decimal("50"), date=dt.date(2024, 1, 2))
        trade = ledger.close("A.LSE", price=Decimal("60"), date=dt.date(2024, 3, 1))
        assert trade.gross_pnl_gbp == Decimal("200")
        assert trade.costs_gbp == Decimal("17.20")
        assert trade.net_pnl_gbp == Decimal("182.80")
        assert trade.is_win

    def test_buying_more_than_the_cash_available_is_refused(self):
        ledger = Ledger(Decimal("100"))
        with pytest.raises(InsufficientCash):
            ledger.open(ticker="A.LSE", idea_id="i", idea_class=IdeaClass.LONG_TERM,
                        sector="c", shares=20, price=Decimal("50"), date=dt.date(2024, 1, 2))

    def test_a_usd_position_is_valued_in_gbp(self):
        ledger = Ledger(Decimal("10000"), fx=USD)
        ledger.open(ticker="A.US", idea_id="i", idea_class=IdeaClass.LONG_TERM, sector="tech",
                    shares=10, price=Decimal("100"), date=dt.date(2024, 1, 2), currency="USD")
        assert ledger.market_value({"A.US": Decimal("100")}) == Decimal("800")

    def test_a_position_with_no_mark_is_held_at_entry_not_dropped(self):
        """Dropping it would shrink NAV and manufacture a drawdown out of a
        missing data point."""
        ledger = Ledger(Decimal("10000"))
        ledger.open(ticker="A.LSE", idea_id="i", idea_class=IdeaClass.LONG_TERM, sector="c",
                    shares=20, price=Decimal("50"), date=dt.date(2024, 1, 2))
        assert ledger.market_value({}) == Decimal("1000")

    def test_the_high_water_mark_only_ever_rises(self):
        ledger = Ledger(Decimal("10000"))
        ledger.open(ticker="A.LSE", idea_id="i", idea_class=IdeaClass.LONG_TERM, sector="c",
                    shares=20, price=Decimal("50"), date=dt.date(2024, 1, 2))
        ledger.mark_to_market(dt.date(2024, 1, 3), {"A.LSE": Decimal("80")})
        peak = ledger.high_water
        ledger.mark_to_market(dt.date(2024, 1, 4), {"A.LSE": Decimal("20")})
        assert ledger.high_water == peak
        assert ledger.drawdown({"A.LSE": Decimal("20")}) > Decimal("0.05")

    def test_a_stop_fills_at_the_stop_level_not_the_mark(self):
        """Deliberately optimistic and documented as such: a real stop can gap
        through. It must never fill BETTER than the stop, which would be fantasy."""
        ledger = Ledger(Decimal("10000"))
        ledger.open(ticker="A.LSE", idea_id="i", idea_class=IdeaClass.LONG_TERM, sector="c",
                    shares=20, price=Decimal("50"), date=dt.date(2024, 1, 2), stop=Decimal("45"))
        closed = apply_exits(ledger, {"A.LSE": Decimal("30")}, dt.date(2024, 2, 1))
        assert len(closed) == 1
        assert closed[0].exit == Decimal("45")
        assert closed[0].status is PositionStatus.CLOSED_STOP

    def test_an_invalidated_position_closes_at_the_mark(self):
        ledger = Ledger(Decimal("10000"))
        ledger.open(ticker="A.LSE", idea_id="i", idea_class=IdeaClass.LONG_TERM, sector="c",
                    shares=20, price=Decimal("50"), date=dt.date(2024, 1, 2), stop=Decimal("45"))
        closed = apply_exits(ledger, {"A.LSE": Decimal("52")}, dt.date(2024, 2, 1),
                             invalidated=["A.LSE"])
        assert closed[0].status is PositionStatus.CLOSED_INVALIDATED
        assert closed[0].exit == Decimal("52")

    def test_exposure_fraction_answers_signal_or_just_being_long(self):
        ledger = Ledger(Decimal("10000"))
        ledger.open(ticker="A.LSE", idea_id="i", idea_class=IdeaClass.LONG_TERM, sector="c",
                    shares=20, price=Decimal("50"), date=dt.date(2024, 1, 2))
        assert Decimal("0.09") < ledger.exposure_fraction({"A.LSE": Decimal("50")}) < Decimal("0.11")


# ---------------------------------------------------------------- backtester


class TestNoLookahead:
    def test_a_ranker_never_sees_a_bar_after_the_decision_date(self):
        seen: list[tuple[dt.date, dt.date]] = []

        def spy(date, history):
            latest = max(b.date for series in history.values() for b in series)
            seen.append((date, latest))
            return []

        data = {"A.LSE": bars("A.LSE", [100 + i for i in range(120)])}
        bt.run(data, spy, bt.BacktestConfig(warmup_bars=30, rebalance_days=5))
        assert seen, "the ranker was never called"
        assert all(latest <= decision for decision, latest in seen)

    def test_orders_fill_at_the_next_session_open_not_the_signal_price(self):
        """Filling at the close that generated the signal is the single most
        common way a backtest invents returns that cannot be earned."""
        closes = [100] * 40 + [100] * 40
        opens = [100] * 40 + [130] * 40      # a gap up the morning after the signal
        data = {"A.LSE": bars("A.LSE", closes, opens=opens)}

        def ranker(date, history):
            return [("A.LSE", Decimal("99"))]

        result = bt.run(data, ranker, bt.BacktestConfig(warmup_bars=45, rebalance_days=100,
                                                       stop_pct=Decimal("0.10")))
        fills = [f for f in result.ledger.fills if f.shares > 0]
        assert fills, "nothing was bought"
        assert fills[0].price == Decimal("130"), "filled at the signal price, not the next open"


class TestBacktestBehaviour:
    def _rising(self):
        return {
            "A.LSE": bars("A.LSE", [100 * (1.002 ** i) for i in range(400)]),
            "B.LSE": bars("B.LSE", [100 * (0.998 ** i) for i in range(400)]),
        }

    def test_a_rising_market_with_a_perfect_ranker_makes_money(self):
        """Vacuity guard: if this does not make money the harness is broken, not
        the strategy."""
        def ranker(date, history):
            return [("A.LSE", Decimal("90"))]

        result = bt.run(self._rising(), ranker,
                        bt.BacktestConfig(warmup_bars=60, rebalance_days=20))
        assert result.nav_series[-1] > result.nav_series[0]

    def test_a_ranker_that_picks_nothing_stays_in_cash(self):
        result = bt.run(self._rising(), lambda d, h: [],
                        bt.BacktestConfig(warmup_bars=60, rebalance_days=20))
        assert result.ledger.open_positions == []
        assert result.nav_series[-1] == Decimal("10000.00")

    def test_scores_below_the_minimum_are_ignored(self):
        def ranker(date, history):
            return [("A.LSE", Decimal("10"))]

        result = bt.run(self._rising(), ranker,
                        bt.BacktestConfig(warmup_bars=60, rebalance_days=20,
                                          min_score=Decimal("60")))
        assert result.ledger.closed == []

    def test_the_risk_layer_caps_each_position_at_ten_percent(self):
        def ranker(date, history):
            return [(t, Decimal("90")) for t in ("A.LSE", "B.LSE")]

        result = bt.run(self._rising(), ranker,
                        bt.BacktestConfig(warmup_bars=60, rebalance_days=20))
        for fill in (f for f in result.ledger.fills if f.shares > 0):
            assert fill.price * fill.shares <= Decimal("1000") * Decimal("1.001")

    def test_everything_open_is_closed_at_the_end_of_the_run(self):
        """So trade statistics describe the whole run, not only the positions
        that happened to exit."""
        def ranker(date, history):
            return [("A.LSE", Decimal("90"))]

        result = bt.run(self._rising(), ranker,
                        bt.BacktestConfig(warmup_bars=60, rebalance_days=20))
        assert result.ledger.open_positions == []
        assert result.trades

    def test_too_little_data_returns_an_empty_result_rather_than_guessing(self):
        data = {"A.LSE": bars("A.LSE", [100] * 10)}
        assert bt.run(data, lambda d, h: [], bt.BacktestConfig(warmup_bars=250)).equity == []


class TestWalkForward:
    def test_folds_are_rolling_and_do_not_overlap_in_test_windows(self):
        from sentinel.backtest import make_folds

        dates = [dt.date(2020, 1, 1) + dt.timedelta(days=i) for i in range(1000)]
        folds = make_folds(dates, folds=3, train_periods=400, test_periods=150)
        assert len(folds) == 3
        for earlier, later in zip(folds, folds[1:]):
            assert earlier.test_end < later.test_start
            # Rolling, not expanding: every fold trains on the same span.
            assert (earlier.train_end - earlier.train_start) == (later.train_end - later.train_start)

    def test_a_request_for_more_folds_than_the_data_supports_returns_fewer(self):
        from sentinel.backtest import make_folds

        dates = [dt.date(2020, 1, 1) + dt.timedelta(days=i) for i in range(100)]
        assert make_folds(dates, folds=10, train_periods=60, test_periods=30) != []
        assert len(make_folds(dates, folds=10, train_periods=60, test_periods=30)) == 1


# ---------------------------------------------------------------- benchmarks


class TestBenchmarks:
    def test_buy_and_hold_tracks_the_index_total_return(self):
        series = benchmarks.buy_and_hold(bars("VWRP.LSE", [100, 110, 121]),
                                         name="B1", label="All-World")
        assert series.total_return == pytest.approx(Decimal("0.21"))

    def test_cash_compounds_at_the_stated_rate(self):
        series = benchmarks.cash(253, annual_rate=Decimal("0.05"))
        assert Decimal("0.048") < series.total_return < Decimal("0.052")

    def test_monte_carlo_is_seeded_so_the_bar_cannot_move_between_runs(self):
        data = {f"T{i}.LSE": bars(f"T{i}.LSE", [100 + i * j for j in range(120)])
                for i in range(10)}
        a = benchmarks.random_portfolios(data, portfolios=50, holdings=4, seed=7)
        b = benchmarks.random_portfolios(data, portfolios=50, holdings=4, seed=7)
        assert a.median_return == b.median_return

    def test_a_universe_too_small_to_sample_returns_no_verdict(self):
        data = {"A.LSE": bars("A.LSE", [100, 101])}
        assert benchmarks.random_portfolios(data, holdings=8).portfolios == 0

    def test_the_verdict_says_plainly_when_random_did_better(self):
        result = benchmarks.MonteCarloResult(
            portfolios=1000, median_return=Decimal("0.10"), percentiles={},
            strategy_return=Decimal("0.02"), strategy_percentile=22.0,
        )
        assert "worse than picking at random" in result.verdict()
        assert result.beats_median is False

    def test_being_ahead_of_the_median_but_inside_the_luck_range_is_said_so(self):
        result = benchmarks.MonteCarloResult(
            portfolios=1000, median_return=Decimal("0.10"), percentiles={},
            strategy_return=Decimal("0.12"), strategy_percentile=62.0,
        )
        assert "Not yet evidence of skill" in result.verdict()


# ---------------------------------------------------------------- metrics


class TestMetrics:
    def test_total_return_compounds_rather_than_sums(self):
        assert metrics.total_return([0.5, 0.5]) == pytest.approx(1.25)

    def test_sharpe_is_none_not_zero_when_there_is_no_data(self):
        """0.0 and 'we cannot compute this' mean completely different things to
        someone deciding whether to fund a strategy."""
        assert metrics.sharpe([0.01]) is None
        assert metrics.sharpe([]) is None

    def test_a_riskless_series_has_no_sharpe_rather_than_an_infinite_one(self):
        assert metrics.sharpe([0.001] * 50) is None

    def test_sortino_ignores_upside_volatility(self):
        """Same mean, but one series is volatile only upwards. Sharpe punishes
        that volatility; Sortino should not, so Sortino must come out higher."""
        upside_spiky = [0.0] * 50 + [0.008] * 50
        assert metrics.sortino(upside_spiky) > metrics.sharpe(upside_spiky)

    def test_a_series_that_never_fell_has_no_sortino_rather_than_an_infinite_one(self):
        assert metrics.sortino([0.004] * 100) is None

    def test_sortino_divides_by_the_full_sample_not_the_loss_count(self):
        """A common implementation error that inflates the ratio precisely for
        strategies that rarely lose."""
        returns = [0.01] * 99 + [-0.05]
        n = len(returns)
        rf = metrics.DEFAULT_RISK_FREE_ANNUAL / metrics.TRADING_DAYS
        excess = [r - rf for r in returns]
        downside = [min(0.0, e) for e in excess]
        expected_dd = (sum(d * d for d in downside) / n) ** 0.5
        expected = (metrics.mean(excess) / expected_dd) * (metrics.TRADING_DAYS ** 0.5)
        assert metrics.sortino(returns) == pytest.approx(expected)

    def test_max_drawdown_reports_depth_and_whether_it_recovered(self):
        dd = metrics.max_drawdown([100, 120, 60, 80, 130])
        assert dd.max_drawdown == pytest.approx(-0.5)
        assert dd.recovery_periods == 2

    def test_an_unrecovered_drawdown_says_so(self):
        dd = metrics.max_drawdown([100, 120, 60, 70])
        assert dd.recovery_periods is None

    def test_alpha_strips_out_the_market_the_strategy_was_simply_long(self):
        """A tracker that just halves the market has no skill, and alpha says so.

        The expected value is not zero but -rf/2 annualised: halving every
        return leaves the other half earning nothing, and CAPM charges for that.
        Being long half the time in a rising market is not alpha, which is
        exactly the confusion §5.1 asks this metric to settle.
        """
        market = [0.01, -0.005, 0.02, -0.01, 0.015] * 20
        tracker = [r * 0.5 for r in market]
        assert metrics.beta(tracker, market) == pytest.approx(0.5)
        assert metrics.alpha(tracker, market) == pytest.approx(
            -metrics.DEFAULT_RISK_FREE_ANNUAL / 2, abs=1e-6
        )

    def test_a_strategy_that_genuinely_outperforms_shows_positive_alpha(self):
        market = [0.01, -0.005, 0.02, -0.01, 0.015] * 20
        skilled = [r + 0.002 for r in market]
        assert metrics.alpha(skilled, market) > 0.3

    def test_profit_factor_is_none_when_nothing_lost_rather_than_infinite(self):
        class T:
            net_pnl_gbp = Decimal("10")
            is_win = True
            holding_days = 5

        stats = metrics.trade_stats([T(), T()])
        assert stats.profit_factor is None
        assert stats.win_rate == 1.0

    def test_expectancy_makes_a_win_rate_interpretable(self):
        class Win:
            net_pnl_gbp = Decimal("30")
            is_win = True
            holding_days = 5

        class Loss:
            net_pnl_gbp = Decimal("-10")
            is_win = False
            holding_days = 5

        stats = metrics.trade_stats([Win()] + [Loss()] * 3)   # 25% win rate, 3:1 ratio
        assert stats.win_rate == 0.25
        assert stats.win_loss_ratio == pytest.approx(3.0)
        assert stats.expectancy == pytest.approx(0.0)

    def test_the_headline_leads_with_risk_adjusted_return(self):
        summary = metrics.summarise([0.01] * 50 + [-0.02] * 10, [100, 110, 105])
        assert summary.headline().startswith("Sharpe")
