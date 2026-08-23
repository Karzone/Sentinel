"""The daily pipeline: data -> modules -> rules -> risk -> brief.

This is the only place the layers meet, and the order is load-bearing.

Quality gates come first and are absolute: a ticker with a CRITICAL issue is not
scored at all. Not scored-and-flagged — *not scored*. Phase 1's rule is that a
signal generated from bad data is a Sev-1, and the cheapest way to honour that
is never to produce the signal.

The LLM modules are optional at every step. With no ``ANTHROPIC_API_KEY`` the
deterministic modules still run and still produce composite scores; what is lost
is the memo, and without a memo an idea cannot pass the risk layer's
invalidation check, so nothing reaches the brief as a candidate. That is the
correct degradation: the system goes quiet rather than issuing unexplained
scores.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Sequence

from .analysis import fundamental, news, sentiment, synthesis, technical
from .config import Config
from .data import quality, registry
from .domain.enums import IdeaClass, Severity
from .domain.models import Bar, DataQualityIssue, Idea, Signal
from .llm.client import LlmClient, LlmError
from .llm.fake import UnavailableClient
from .logging_setup import get_logger
from .risk import PortfolioState, RiskEngine
from .storage import audit, repo

log = get_logger("pipeline")
PIPELINE_VERSION = "pipeline-v1"


@dataclass(slots=True)
class TickerResult:
    ticker: str
    idea: Idea | None = None
    signals: list[Signal] = field(default_factory=list)
    issues: list[DataQualityIssue] = field(default_factory=list)
    skipped: str | None = None
    llm_error: str | None = None


@dataclass(slots=True)
class PipelineResult:
    as_of: dt.date
    results: list[TickerResult] = field(default_factory=list)
    report: quality.QualityReport | None = None

    @property
    def ideas(self) -> list[Idea]:
        return [r.idea for r in self.results if r.idea is not None]

    @property
    def accepted(self) -> list[Idea]:
        return [i for i in self.ideas if not i.rejected_by_rules]

    @property
    def skipped(self) -> list[TickerResult]:
        return [r for r in self.results if r.skipped]


def score_ticker(
    conn,
    config: Config,
    ticker: str,
    as_of: dt.date,
    *,
    llm: LlmClient | None = None,
    blocked: set[str] | None = None,
) -> TickerResult:
    """Run every module for one ticker and assemble an Idea."""
    result = TickerResult(ticker=ticker)
    llm = llm or UnavailableClient()

    if blocked and ticker in blocked:
        result.skipped = "blocked by a critical data-quality issue"
        return result

    bars: Sequence[Bar] = repo.get_bars(conn, ticker, end=as_of)
    snapshot = repo.get_fundamentals(conn, ticker, as_of=as_of)

    issues = quality.run_all(
        ticker, bars, snapshot, as_of,
        staleness_hours=config.data.staleness_hours,
        min_history_bars=config.data.min_history_bars,
    )
    result.issues = issues
    if any(i.severity is Severity.CRITICAL for i in issues):
        result.skipped = "; ".join(i.detail for i in issues if i.severity is Severity.CRITICAL)
        return result

    signals: list[Signal] = []
    try:
        signals.append(technical.score(bars, as_of=as_of))
    except technical.InsufficientHistory as exc:
        log.info("technical module skipped for %s: %s", ticker, exc)
    if snapshot is not None:
        signals.append(fundamental.score(snapshot, as_of=as_of))

    if not signals:
        result.skipped = "no module could score this ticker"
        return result

    catalyst = sentiment_read = None
    if llm.available():
        since = dt.datetime.combine(
            as_of - dt.timedelta(days=config.data.news_lookback_days), dt.time.min, dt.UTC
        )
        items = repo.get_news(conn, ticker, since=since)
        try:
            catalyst = news.read_catalyst(llm, ticker, items, as_of)
            if catalyst is not None:
                signals.append(news.to_signal(catalyst))
            sentiment_read = sentiment.read_sentiment(llm, ticker, items, as_of)
            if sentiment_read is not None:
                signals.append(sentiment.to_signal(sentiment_read))
        except LlmError as exc:
            # A failed LLM call degrades the idea, it does not fail the run —
            # but it is recorded, because §5.2 measures the failure rate.
            result.llm_error = str(exc)
            audit.record(conn, audit.AuditEvent.LLM_SCHEMA_FAILURE, ticker=ticker,
                         payload={"error": str(exc)})

    memo = None
    composite = synthesis.composite_score(signals)
    if llm.available() and composite >= Decimal("50") and result.llm_error is None:
        try:
            memo = synthesis.write_memo(llm, ticker, signals, as_of,
                                        catalyst=catalyst, sentiment=sentiment_read)
        except LlmError as exc:
            result.llm_error = str(exc)
            audit.record(conn, audit.AuditEvent.LLM_SCHEMA_FAILURE, ticker=ticker,
                         payload={"module": "synthesis", "error": str(exc)})

    idea = synthesis.build_idea(
        ticker, signals, as_of, memo=memo, catalyst=catalyst, sentiment=sentiment_read
    )
    result.idea = idea
    result.signals = signals
    if idea.rejected_by_rules:
        audit.record(conn, audit.AuditEvent.RULES_REJECTED, ticker=ticker,
                     payload={"idea_id": idea.id, "reasons": list(idea.rejected_by_rules)})
    return result


def run(
    conn,
    config: Config,
    tickers: Sequence[str],
    *,
    as_of: dt.date | None = None,
    llm: LlmClient | None = None,
    persist: bool = True,
) -> PipelineResult:
    as_of = as_of or dt.date.today()
    result = PipelineResult(as_of=as_of)
    report = quality.QualityReport(as_of)

    for ticker in tickers:
        outcome = score_ticker(conn, config, ticker, as_of, llm=llm)
        report.extend(outcome.issues)
        result.results.append(outcome)
        if persist and outcome.idea is not None and repo.get_idea(conn, outcome.idea.id) is None:
            repo.save_idea(conn, outcome.idea)

    result.report = report
    if persist:
        repo.save_quality_issues(conn, report.issues)
    return result


def portfolio_state(conn, config: Config, *, as_of: dt.date) -> PortfolioState:
    """Assemble the live portfolio picture the risk layer needs."""
    positions = repo.get_all_positions(conn)
    curve = repo.get_equity_curve(conn)
    nav = curve[-1][1] if curve else config.satellite_capital_gbp
    cash = curve[-1][2] if curve else config.satellite_capital_gbp
    high_water = repo.high_water_mark(conn) or config.satellite_capital_gbp
    marks: dict[str, Decimal] = {}
    for position in positions:
        if position.is_open:
            bars = repo.get_bars(conn, position.ticker, end=as_of)
            if bars:
                marks[position.ticker] = bars[-1].adjusted_close
    return PortfolioState(
        satellite_capital=config.satellite_capital_gbp, cash=cash, positions=positions,
        nav=nav, high_water_mark=high_water,
        fx=_fx_for(conn, as_of), marks=marks,
    )


def _fx_for(conn, as_of: dt.date):
    from .money import FxRates

    rates = repo.get_fx_rates(conn, as_of)
    return FxRates(as_of.isoformat(), rates) if rates else FxRates.identity(as_of.isoformat())


def assess(
    conn, config: Config, ideas: Sequence[Idea], *, as_of: dt.date,
    state: PortfolioState | None = None, blocked: set[str] | None = None,
) -> list[tuple[Idea, object]]:
    """Put each idea through the risk layer, returning (idea, verdict) pairs."""
    engine = RiskEngine(config.risk, sectors=config.sectors)
    state = state or portfolio_state(conn, config, as_of=as_of)
    out: list[tuple[Idea, object]] = []
    for idea in ideas:
        bars = repo.get_bars(conn, idea.ticker, end=as_of)
        if not bars:
            continue
        entry = bars[-1].adjusted_close
        stop = technical.atr_stop(bars)
        verdict = engine.evaluate(
            idea, entry=entry, stop=stop, state=state, as_of=as_of,
            data_stale=bool(blocked and idea.ticker in blocked),
            currency=bars[-1].currency,
        )
        if verdict.approved:
            audit.record(conn, audit.AuditEvent.RISK_APPROVED, ticker=idea.ticker,
                         payload={"idea_id": idea.id,
                                  "shares": verdict.plan.shares if verdict.plan else 0})
        else:
            audit.record(conn, audit.AuditEvent.RISK_CHECK_FAILED, ticker=idea.ticker,
                         payload={"idea_id": idea.id, "reasons": list(verdict.failure_reasons)})
        out.append((idea, verdict))
    return out
