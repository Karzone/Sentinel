"""Assembling the daily brief.

The cap of three new candidate ideas is a feature, not a display limit. A brief
with twelve ideas on it is not more useful than one with three — it is a
watchlist, and it moves the actual selection decision back onto the reader while
appearing to have done the work. If more than three clear the risk layer, the
brief shows the three best and says how many it held back.

The disclaimer is added by the renderer, not here, so no code path can produce a
rendered brief without it.
"""

from __future__ import annotations

import datetime as dt
import hashlib
from decimal import Decimal
from typing import Mapping, Sequence

from ..domain.enums import Severity
from ..domain.models import Brief, DataQualityIssue, Idea, RiskVerdict
from ..risk import PortfolioState, RiskEngine, sector_allocation

BRIEF_VERSION = "brief-v1"

MAX_NEW_IDEAS = 3


def brief_id(as_of: dt.date, generated_at: dt.datetime) -> str:
    return hashlib.sha256(f"{as_of}|{generated_at.date()}".encode()).hexdigest()[:16]


def portfolio_lines(state: PortfolioState, engine: RiskEngine) -> list[str]:
    lines: list[str] = []
    open_positions = state.open_positions
    invested = sum((state.exposure_gbp(p) for p in open_positions), Decimal("0"))
    lines.append(
        f"NAV £{state.nav:,.2f} · cash £{state.cash:,.2f} · "
        f"{len(open_positions)} open position{'s' if len(open_positions) != 1 else ''} "
        f"(£{invested:,.2f} invested)"
    )
    for position in open_positions:
        mark = state.marks.get(position.ticker, position.entry)
        move = (mark / position.entry - 1) if position.entry else Decimal("0")
        distance = ""
        if position.stop is not None and mark > 0:
            distance = f" · {((mark - position.stop) / mark):.1%} above its stop"
        lines.append(
            f"{position.ticker} ({position.sector}, {position.idea_class.value}): "
            f"{position.shares} @ {position.entry} → {mark} ({move:+.1%}){distance}"
        )
    if not open_positions:
        lines.append("No open positions. Satellite capital is entirely in cash.")
    return lines


def risk_lines(state: PortfolioState, engine: RiskEngine) -> list[str]:
    lines: list[str] = []
    drawdown = state.drawdown()
    limit = engine.limits.drawdown_kill_pct / Decimal("100")
    lines.append(
        f"Drawdown {drawdown:.1%} from a high-water mark of £{state.high_water_mark:,.2f} "
        f"(kill switch at {limit:.0%})"
    )
    if engine.kill_switch_active(state):
        lines.append(engine.review_required(state) or "")

    allocation = sector_allocation(state)
    cap = engine.limits.max_sector_pct / Decimal("100")
    for sector, weight in allocation.items():
        flag = "  ← at the limit" if weight >= cap else ""
        lines.append(f"Sector {sector}: {weight:.1%} of satellite (cap {cap:.0%}){flag}")
    if not allocation:
        lines.append("No sector exposure.")

    from ..domain.enums import IdeaClass

    swing = state.class_exposure_gbp(IdeaClass.SWING)
    swing_cap = state.satellite_capital * engine.limits.swing_max_pct / Decimal("100")
    lines.append(
        f"Short-term book £{swing:,.2f} of a £{swing_cap:,.2f} cap "
        f"({engine.limits.swing_max_pct}% of satellite)"
    )
    return lines


def triggered_lines(
    state: PortfolioState, engine: RiskEngine, marks: Mapping[str, Decimal]
) -> list[str]:
    lines: list[str] = []
    for position, mark in engine.stops_hit(state, marks):
        lines.append(
            f"STOP HIT — {position.ticker} at {mark}, stop was {position.stop}. "
            f"The position should be closed."
        )
    for position, distance in engine.approaching_stop(state, marks):
        lines.append(f"{position.ticker} is {distance:.1%} from its stop.")
    return lines


def what_we_got_wrong(
    ideas: Sequence[Idea], verdicts: Sequence[tuple[Idea, RiskVerdict]],
    issues: Sequence[DataQualityIssue],
) -> list[str]:
    """The mandatory section. It must always find something.

    Not a rhetorical flourish: a research system that never reports a fault is
    not fault-free, it is not looking. Every item here is drawn from something
    that actually happened this run, and the fallback line is itself a finding
    rather than a reassurance.
    """
    findings: list[str] = []

    rejected = [i for i in ideas if i.rejected_by_rules]
    if rejected:
        reasons: dict[str, int] = {}
        for idea in rejected:
            for reason in idea.rejected_by_rules:
                rule = reason.split(":")[0]
                reasons[rule] = reasons.get(rule, 0) + 1
        summary = ", ".join(f"{rule} × {count}" for rule, count in sorted(reasons.items()))
        findings.append(
            f"{len(rejected)} memo(s) were written and then rejected by the rules layer "
            f"({summary}). The synthesis model produced them happily; that is what the rules "
            f"are for, but a rising rejection rate means the prompts need work."
        )

    blocked = [i for i in issues if i.severity is Severity.CRITICAL]
    if blocked:
        findings.append(
            f"{len(blocked)} ticker(s) could not be scored at all because of data quality: "
            + "; ".join(f"{i.ticker} ({i.check})" for i in blocked[:5])
        )

    warnings = [i for i in issues if i.severity is Severity.WARN]
    if warnings:
        findings.append(
            f"{len(warnings)} data warning(s) were tolerated rather than blocking: "
            + "; ".join(f"{i.ticker}: {i.check}" for i in warnings[:5])
        )

    failed_risk = [(i, v) for i, v in verdicts if not v.approved]
    if failed_risk:
        counted: dict[str, int] = {}
        for _idea, verdict in failed_risk:
            for check in verdict.failures:
                counted[check.check.value] = counted.get(check.check.value, 0) + 1
        findings.append(
            f"{len(failed_risk)} idea(s) cleared the rules and then failed the risk layer ("
            + ", ".join(f"{k} × {v}" for k, v in sorted(counted.items())) + ")."
        )

    thin = [i for i in ideas if any(s.confidence < Decimal("0.6") for s in i.signals)]
    if thin:
        findings.append(
            f"{len(thin)} idea(s) rest on at least one module with under 60% data coverage. "
            f"Their scores are less certain than they look."
        )

    if not findings:
        findings.append(
            "Nothing was rejected, blocked or flagged this run — which is itself worth "
            "noting. Either the universe is small or the checks are not biting; a clean "
            "run every day would mean the rules are not doing anything."
        )
    return findings


def build(
    *,
    as_of: dt.date,
    ideas: Sequence[Idea],
    verdicts: Sequence[tuple[Idea, RiskVerdict]],
    state: PortfolioState,
    engine: RiskEngine,
    issues: Sequence[DataQualityIssue] = (),
    marks: Mapping[str, Decimal] | None = None,
    generated_at: dt.datetime | None = None,
) -> Brief:
    generated_at = generated_at or dt.datetime.now(dt.UTC)
    marks = marks or dict(state.marks)

    approved = [(idea, verdict) for idea, verdict in verdicts if verdict.approved]
    approved.sort(key=lambda pair: pair[0].composite_score, reverse=True)
    shortlist = [idea for idea, _ in approved[:MAX_NEW_IDEAS]]
    held_back = max(0, len(approved) - MAX_NEW_IDEAS)

    rejected = [idea for idea in ideas if idea not in shortlist]

    portfolio = portfolio_lines(state, engine)
    if held_back:
        portfolio.append(
            f"{held_back} further idea(s) cleared every check but are not shown; the brief "
            f"caps new candidates at {MAX_NEW_IDEAS} so the decision stays a decision."
        )

    critical = [i for i in issues if i.severity is Severity.CRITICAL]
    return Brief(
        id=brief_id(as_of, generated_at),
        generated_at=generated_at,
        as_of=as_of,
        ideas=tuple(shortlist),
        rejected=tuple(rejected),
        portfolio_lines=tuple(portfolio),
        risk_lines=tuple(risk_lines(state, engine)),
        triggered=tuple(triggered_lines(state, engine, marks)),
        data_issues=tuple(issues),
        stale=bool(critical),
        kill_switch_active=engine.kill_switch_active(state),
        what_we_got_wrong=tuple(what_we_got_wrong(ideas, verdicts, issues)),
    )
