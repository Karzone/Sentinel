"""The weekly review.

Spec §5: performance versus benchmark, eval scores, and *what the system got
wrong this week* — a section the spec marks mandatory, on the grounds that it
must always find something.

That last requirement is the reason this module is written the way it is. It
would be easy to produce a review that reads well every week by reporting only
what worked; the whole point of a research system that has to earn real money is
that it tells you when it is not earning it. So the review leads with
risk-adjusted return rather than raw return, prints the benchmark comparison
even when the benchmark is winning, and always finds a fault — with a fallback
line that is itself a finding rather than a reassurance.

Nothing here computes its own statistics. Every number comes from ``evals/``,
which is the same code the CLI's `evals` command and the dashboard read.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Sequence

from ..config import Config
from ..domain.enums import PositionStatus, Severity
from ..evals import calibration, dataset, metrics, signal_quality
from ..storage import repo

WEEKLY_VERSION = "weekly-v1"

#: Trading days in a week, for annualising a short window.
WEEK = 5


@dataclass(slots=True)
class WeeklyReview:
    as_of: dt.date
    period_start: dt.date
    performance: str
    benchmark_lines: tuple[str, ...] = ()
    eval_lines: tuple[str, ...] = ()
    wrong: tuple[str, ...] = ()
    kill_criteria: tuple[str, ...] = ()
    closed_trades: int = 0
    ideas_generated: int = 0


def _period_returns(conn, start: dt.date, end: dt.date) -> tuple[list[Decimal], list[Decimal]]:
    rows = [r for r in repo.get_equity_curve(conn) if start <= r[0] <= end]
    navs = [nav for _d, nav, _c, _h in rows]
    returns: list[Decimal] = []
    for prev, curr in zip(navs, navs[1:]):
        if prev > 0:
            returns.append(curr / prev - Decimal("1"))
    return returns, navs


def performance_line(conn, config: Config, start: dt.date, end: dt.date) -> str:
    """Risk-adjusted return first, raw return after.

    A 40% year at triple the benchmark's volatility is not a better year, and a
    report that leads with the 40% invites exactly the conclusion §5.5's kill
    criteria exist to prevent.
    """
    returns, navs = _period_returns(conn, start, end)
    if len(navs) < 2:
        return (
            "No paper equity curve for this period. Nothing can be said about "
            "performance yet, and saying so is the correct output."
        )

    summary = metrics.summarise(returns, navs, periods_per_year=252)
    whole = repo.get_equity_curve(conn)
    since_inception = (
        (whole[-1][1] / whole[0][1] - 1) if whole and whole[0][1] > 0 else Decimal("0")
    )

    sharpe = "n/a" if summary.sharpe is None else f"{summary.sharpe:.2f}"
    note = ""
    if summary.sharpe is None:
        note = (
            " — Sharpe is not computable over this few observations, which is a "
            "different statement from 'the Sharpe was zero'."
        )
    return (
        f"Week: {summary.total_return:+.2%} · Sharpe {sharpe} · "
        f"max drawdown {summary.max_drawdown:.1%} over {summary.periods} sessions. "
        f"Since inception: {float(since_inception):+.2%}.{note}"
    )


def benchmark_lines(conn, config: Config, start: dt.date, end: dt.date) -> list[str]:
    """B1–B3 over the same window, in GBP, said plainly either way."""
    from ..backtest import benchmarks

    returns, navs = _period_returns(conn, start, end)
    if len(navs) < 2:
        return ["No equity curve to compare against the benchmarks yet."]

    strategy_return = navs[-1] / navs[0] - Decimal("1")
    lines: list[str] = []

    labels = {"B1": "global index", "B2": "S&P 500"}
    for key in ("B1", "B2"):
        symbol = config.benchmarks.get(key)
        if not symbol or symbol in ("CASH", "RANDOM"):
            continue
        bars = [b for b in repo.get_bars(conn, symbol) if start <= b.date <= end]
        if len(bars) < 2:
            lines.append(
                f"{key} {symbol} ({labels[key]}): not ingested for this window — "
                f"run `sentinel ingest --tickers {symbol}` to enable the comparison."
            )
            continue
        held = benchmarks.buy_and_hold(bars, name=key, label=symbol)
        verdict = "AHEAD of" if strategy_return > held.total_return else "BEHIND"
        lines.append(
            f"{key} {symbol}: {held.total_return:+.2%} — Sentinel is {verdict} it "
            f"({strategy_return:+.2%})."
        )

    cash = benchmarks.cash(len(navs))
    verdict = "ahead of" if strategy_return > cash.total_return else "BEHIND"
    lines.append(f"B3 cash: {cash.total_return:+.2%} — Sentinel is {verdict} it.")
    lines.append(
        "B4 (random portfolios) is a backtest-scale question, not a weekly one — "
        "run `sentinel backtest` for the skill-versus-luck comparison."
    )
    return lines


def eval_lines(conn, config: Config) -> list[str]:
    """Every eval that can return a verdict, and an honest note when it cannot."""
    lines: list[str] = []

    compliance = dataset.llm_compliance(conn)
    lines.append(f"LLM schema compliance: {compliance['verdict']}")

    calls = dataset.catalyst_calls(conn)
    accuracy = signal_quality.direction_accuracy(calls)
    lines.append(f"Catalyst direction: {accuracy.verdict()}")

    materiality = signal_quality.materiality_calibration(calls)
    lines.append(f"Materiality: {materiality.verdict()}")

    outcomes = dataset.conviction_outcomes(conn)
    conviction = calibration.conviction_calibration(outcomes)
    lines.append(f"Conviction: {conviction.verdict()}")

    stops = calibration.stop_quality(dataset.stop_outcomes(conn))
    lines.append(f"Stops: {stops.verdict()}")
    return lines


def _benchmark_series(conn, config: Config, key: str = "B1"):
    """Daily returns for a benchmark over the whole paper history.

    Returns ``(returns, total_return)``, or ``(None, None)`` when the benchmark's
    prices have not been ingested — which the kill criteria then reports as
    "cannot be evaluated" rather than treating a missing benchmark as a pass.
    """
    symbol = config.benchmarks.get(key)
    if not symbol or symbol in ("CASH", "RANDOM"):
        return None, None
    curve = repo.get_equity_curve(conn)
    if len(curve) < 2:
        return None, None
    start, end = curve[0][0], curve[-1][0]
    bars = [b for b in repo.get_bars(conn, symbol) if start <= b.date <= end]
    if len(bars) < 2:
        return None, None
    closes = [b.adjusted_close for b in bars]
    returns = [
        curr / prev - Decimal("1") for prev, curr in zip(closes, closes[1:]) if prev > 0
    ]
    return returns, float(closes[-1] / closes[0] - 1)


def kill_criteria_lines(conn, config: Config, *, paper_months: float) -> list[str]:
    """§5.5, evaluated mechanically so future-you cannot rationalise past it.

    The four comparison inputs are computed here rather than passed as None.
    Leaving them None meant the six-month gate printed nothing once the paper
    period had elapsed, which reads exactly like a pass — the criteria now
    report an unevaluated gate explicitly, but they should also be *able* to
    evaluate it whenever the data exists.
    """
    calls = dataset.catalyst_calls(conn)
    accuracy = signal_quality.direction_accuracy(calls)

    curve = repo.get_equity_curve(conn)
    strategy_returns = [
        curr[1] / prev[1] - Decimal("1") for prev, curr in zip(curve, curve[1:]) if prev[1] > 0
    ]
    strategy_sharpe = metrics.sharpe(strategy_returns) if strategy_returns else None
    strategy_return = (
        float(curve[-1][1] / curve[0][1] - 1) if len(curve) > 1 and curve[0][1] > 0 else None
    )
    benchmark_returns, benchmark_return = _benchmark_series(conn, config)
    benchmark_sharpe = metrics.sharpe(benchmark_returns) if benchmark_returns else None

    criteria = calibration.KillCriteria(
        paper_months=paper_months,
        strategy_sharpe=strategy_sharpe, benchmark_sharpe=benchmark_sharpe,
        strategy_return=strategy_return, benchmark_return=benchmark_return,
        catalyst_samples=accuracy.scoreable,
        catalyst_beats_coin_flip=accuracy.beats_coin_flip,
    )
    return criteria.verdicts()


def what_the_week_got_wrong(conn, config: Config, start: dt.date, end: dt.date) -> list[str]:
    """The mandatory section. It must always find something.

    Not a rhetorical flourish: a research system that never reports a fault is
    not fault-free, it is not looking. Every item is drawn from something that
    actually happened in the window, and the fallback is itself a finding.
    """
    findings: list[str] = []

    ideas = [i for i in repo.get_ideas(conn, since=start, limit=2000) if i.as_of <= end]
    rejected = [i for i in ideas if i.rejected_by_rules]
    if rejected:
        counts: dict[str, int] = {}
        for idea in rejected:
            for reason in idea.rejected_by_rules:
                rule = reason.split(":")[0]
                counts[rule] = counts.get(rule, 0) + 1
        summary = ", ".join(f"{r} × {c}" for r, c in sorted(counts.items()))
        findings.append(
            f"{len(rejected)} of {len(ideas)} memos were written and then rejected by the "
            f"rules layer ({summary}). That is the rules working, but a rising share means "
            f"the synthesis prompts are drifting."
        )

    issues = [i for i in repo.get_quality_issues(conn, since=start) if i.as_of <= end]
    critical = [i for i in issues if i.severity is Severity.CRITICAL]
    if critical:
        findings.append(
            f"{len(critical)} ticker-day(s) could not be scored at all on data quality: "
            + "; ".join(sorted({f"{i.ticker} ({i.check})" for i in critical})[:6])
        )
    warnings = [i for i in issues if i.severity is Severity.WARN]
    if warnings:
        findings.append(
            f"{len(warnings)} data warning(s) were tolerated rather than blocking. "
            f"Tolerated warnings are how a slow feed decay goes unnoticed."
        )

    stopped = [
        p for p in repo.get_all_positions(conn)
        if p.status is PositionStatus.CLOSED_STOP and p.closed_on and start <= p.closed_on <= end
    ]
    if stopped:
        findings.append(
            f"{len(stopped)} position(s) were stopped out this week: "
            + ", ".join(p.ticker for p in stopped)
            + ". Check the stop-quality eval above before assuming the stops were right."
        )

    thin = [i for i in ideas if any(s.confidence < Decimal("0.6") for s in i.signals)]
    if thin:
        findings.append(
            f"{len(thin)} idea(s) this week rest on at least one module with under 60% "
            f"data coverage. Their scores are less certain than they look."
        )

    if not repo.get_equity_curve(conn):
        findings.append(
            "There is no paper equity curve at all, so none of the performance or "
            "calibration evals can return a verdict. Until positions are being tracked, "
            "this review is a data-quality report and nothing more."
        )

    if not findings:
        findings.append(
            "Nothing was rejected, blocked, stopped out or flagged this week — which is "
            "itself worth noting. Either the universe is too small to be interesting or "
            "the checks are not biting; a clean week every week would mean the rules are "
            "not doing anything."
        )
    return findings


def build(
    conn, config: Config, *, as_of: dt.date | None = None, weeks: int = 1
) -> WeeklyReview:
    as_of = as_of or dt.date.today()
    start = as_of - dt.timedelta(days=7 * weeks)

    curve = repo.get_equity_curve(conn)
    paper_months = 0.0
    if curve:
        paper_months = (curve[-1][0] - curve[0][0]).days / 30.44

    ideas = [i for i in repo.get_ideas(conn, since=start, limit=2000) if i.as_of <= as_of]
    closed = [
        p for p in repo.get_all_positions(conn)
        if not p.is_open and p.closed_on and start <= p.closed_on <= as_of
    ]

    return WeeklyReview(
        as_of=as_of,
        period_start=start,
        performance=performance_line(conn, config, start, as_of),
        benchmark_lines=tuple(benchmark_lines(conn, config, start, as_of)),
        eval_lines=tuple(eval_lines(conn, config)),
        wrong=tuple(what_the_week_got_wrong(conn, config, start, as_of)),
        kill_criteria=tuple(kill_criteria_lines(conn, config, paper_months=paper_months)),
        closed_trades=len(closed),
        ideas_generated=len(ideas),
    )
