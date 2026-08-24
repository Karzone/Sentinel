"""Everything the dashboard reads, as pure functions over a read-only connection.

Two rules shape this module.

**The dashboard cannot write.** ``read_only_connect`` opens SQLite through a
``file:…?mode=ro`` URI, so a write is refused by the database engine rather than
by anyone remembering not to. The spec says the dashboard is read-only in v1;
this is what makes that true even if a future page grows a button. A test asserts
the refusal.

**No statistics are re-derived here.** Direction accuracy, conviction
calibration, Brier and the rest come from ``evals/`` — the same code the CLI and
the weekly review use. A dashboard that computed its own version of a hit rate
would eventually disagree with the eval that gates real money, and the wrong one
would be the one on screen.
"""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Sequence

import pandas as pd

from ..config import Config
from ..domain.enums import IdeaClass, Severity
from ..domain.models import Idea
from ..evals import calibration, dataset, signal_quality
from ..money import dec
from ..storage import audit
from ..risk import RiskEngine, sector_allocation as risk_sector_allocation
from ..storage import audit, repo

QUERIES_VERSION = "dashboard-queries-v1"


class DashboardIsReadOnly(RuntimeError):
    pass


def read_only_connect(path: str | Path) -> sqlite3.Connection:
    """Open the database read-only.

    ``mode=ro`` also means SQLite will not create the file, so pointing the
    dashboard at a missing database fails loudly here instead of silently
    serving an empty one that looks like "no data yet".
    """
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"no sentinel database at {p}; run `sentinel init` first")
    conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def is_demo_database(conn: sqlite3.Connection) -> bool:
    """True when this database was written by scripts/seed_demo.py.

    The seeder stamps ``schema_meta.demo_data``; the dashboard renders a banner
    on any database carrying it. A fabricated track record mistaken for a real
    one is the most damaging thing this repository could produce, so the warning
    travels with the data rather than with whoever remembers.
    """
    try:
        row = conn.execute(
            "SELECT value FROM schema_meta WHERE key = 'demo_data'"
        ).fetchone()
    except sqlite3.Error:
        return False
    return bool(row) and str(row["value"]).lower() == "true"


def _f(value: Any) -> float:
    return float(value) if value is not None else float("nan")


# ---------------------------------------------------------------- portfolio


@dataclass(frozen=True, slots=True)
class PortfolioSnapshot:
    nav: Decimal
    cash: Decimal
    invested: Decimal
    high_water: Decimal
    drawdown: Decimal
    open_positions: int
    total_return: Decimal
    satellite_capital: Decimal
    kill_switch: bool
    kill_switch_reason: str | None


def portfolio_snapshot(conn: sqlite3.Connection, config: Config) -> PortfolioSnapshot:
    positions = repo.get_all_positions(conn)
    open_positions = [p for p in positions if p.is_open]
    curve = repo.get_equity_curve(conn)

    marks: dict[str, Decimal] = {}
    for position in open_positions:
        bars = repo.get_bars(conn, position.ticker)
        if bars:
            marks[position.ticker] = bars[-1].adjusted_close

    invested = sum(
        (marks.get(p.ticker, p.entry) * Decimal(p.shares) * p.fx_rate_at_entry
         for p in open_positions),
        Decimal("0"),
    )
    cash = curve[-1][2] if curve else config.satellite_capital_gbp
    nav = curve[-1][1] if curve else cash + invested
    high_water = repo.high_water_mark(conn) or max(nav, config.satellite_capital_gbp)
    drawdown = ((high_water - nav) / high_water) if high_water > 0 else Decimal("0")

    engine = RiskEngine(config.risk, sectors=config.sectors)
    from ..risk import PortfolioState

    state = PortfolioState(
        satellite_capital=config.satellite_capital_gbp, cash=cash, positions=positions,
        nav=nav, high_water_mark=high_water, marks=marks,
    )
    kill = engine.kill_switch_active(state)
    return PortfolioSnapshot(
        nav=nav, cash=cash, invested=invested, high_water=high_water, drawdown=drawdown,
        open_positions=len(open_positions),
        total_return=(nav / config.satellite_capital_gbp - 1)
        if config.satellite_capital_gbp else Decimal("0"),
        satellite_capital=config.satellite_capital_gbp,
        kill_switch=kill, kill_switch_reason=engine.review_required(state),
    )


def equity_frame(conn: sqlite3.Connection) -> pd.DataFrame:
    """The paper equity curve, with drawdown from the running high-water mark."""
    rows = repo.get_equity_curve(conn)
    if not rows:
        return pd.DataFrame(columns=["date", "nav", "cash", "high_water", "drawdown"])
    frame = pd.DataFrame(
        [{"date": pd.Timestamp(date), "nav": _f(nav), "cash": _f(cash),
          "high_water": _f(high_water)} for date, nav, cash, high_water in rows]
    )
    frame["drawdown"] = frame["nav"] / frame["high_water"] - 1.0
    return frame


def benchmark_frame(
    conn: sqlite3.Connection, config: Config, *, starting: Decimal | None = None,
    risk_free_annual: Decimal = Decimal("0.045"),
) -> pd.DataFrame:
    """Strategy and benchmarks on one axis, indexed to the same starting capital.

    Indexing to a common base is what lets four series share **one** y-axis. A
    second axis would let the chart invent a relationship that is not in the
    data, which is the single most common charting mistake and is why nothing in
    this dashboard has one.

    A benchmark whose bars are not ingested is simply absent — never
    back-filled, never flat-lined, because a flat line reads as "the index did
    nothing" rather than "we have no data".
    """
    equity = equity_frame(conn)
    if equity.empty:
        return pd.DataFrame(columns=["date", "series", "value"])

    starting = starting or dec(equity["nav"].iloc[0])
    out: list[dict[str, Any]] = [
        {"date": row.date, "series": "Sentinel (paper)", "value": row.nav, "role": "strategy"}
        for row in equity.itertuples()
    ]

    dates = list(equity["date"])
    labels = {"B1": "B1 · global index", "B2": "B2 · S&P 500", "B3": "B3 · cash"}
    for key in ("B1", "B2"):
        symbol = config.benchmarks.get(key)
        if not symbol or symbol in ("CASH", "RANDOM"):
            continue
        bars = repo.get_bars(conn, symbol)
        if not bars:
            continue
        by_date = {pd.Timestamp(b.date): b.adjusted_close for b in bars}
        base = None
        for date in dates:
            price = by_date.get(date)
            if price is None:
                continue
            if base is None:
                base = price
            out.append({
                "date": date, "series": f"{labels[key]} ({symbol})",
                "value": _f(starting * price / base), "role": "benchmark",
            })

    # B3 is always computable — it needs no vendor, only a rate.
    per_day = float(risk_free_annual) / 252.0
    value = float(starting)
    for index, date in enumerate(dates):
        out.append({"date": date, "series": labels["B3"], "value": value, "role": "benchmark"})
        value *= 1.0 + per_day
        if index == 0:
            continue
    return pd.DataFrame(out)


def positions_frame(conn: sqlite3.Connection) -> pd.DataFrame:
    """Open positions with distance to stop — the column the brief leads on."""
    rows: list[dict[str, Any]] = []
    for position in repo.get_open_positions(conn):
        bars = repo.get_bars(conn, position.ticker)
        mark = bars[-1].adjusted_close if bars else position.entry
        move = (mark / position.entry - 1) if position.entry else Decimal("0")
        distance = (
            (mark - position.stop) / mark
            if position.stop is not None and mark > 0 else None
        )
        rows.append({
            "ticker": position.ticker,
            "class": position.idea_class.value,
            "sector": position.sector,
            "shares": position.shares,
            "entry": _f(position.entry),
            "mark": _f(mark),
            "move": _f(move),
            "stop": _f(position.stop) if position.stop is not None else float("nan"),
            "to_stop": _f(distance) if distance is not None else float("nan"),
            "value_gbp": _f(mark * Decimal(position.shares) * position.fx_rate_at_entry),
            "opened_on": position.opened_on,
        })
    return pd.DataFrame(rows)


def sector_frame(conn: sqlite3.Connection, config: Config) -> pd.DataFrame:
    """Sector weights against the concentration cap.

    Every sector is a ratio against the *same* limit, so the cap is one rule
    line across the chart rather than a per-bar annotation.
    """
    from ..risk import PortfolioState

    positions = repo.get_all_positions(conn)
    marks: dict[str, Decimal] = {}
    for position in positions:
        if position.is_open:
            bars = repo.get_bars(conn, position.ticker)
            if bars:
                marks[position.ticker] = bars[-1].adjusted_close
    state = PortfolioState(
        satellite_capital=config.satellite_capital_gbp,
        cash=Decimal("0"), positions=positions, marks=marks,
    )
    cap = float(config.risk.max_sector_pct) / 100.0
    weights = risk_sector_allocation(state)
    rows = [
        {"sector": sector, "weight": _f(weight), "cap": cap, "over": float(weight) > cap}
        for sector, weight in weights.items()
    ]
    return pd.DataFrame(rows).sort_values("weight", ascending=False) if rows else pd.DataFrame(
        columns=["sector", "weight", "cap", "over"]
    )


def class_exposure(conn: sqlite3.Connection, config: Config) -> dict[str, float]:
    """Short-term book against its sub-allocation cap."""
    from ..risk import PortfolioState

    positions = repo.get_all_positions(conn)
    state = PortfolioState(
        satellite_capital=config.satellite_capital_gbp, cash=Decimal("0"), positions=positions,
    )
    swing = state.class_exposure_gbp(IdeaClass.SWING)
    cap = config.satellite_capital_gbp * config.risk.swing_max_pct / Decimal("100")
    return {
        "swing_gbp": _f(swing), "cap_gbp": _f(cap),
        "fraction": _f(swing / cap) if cap else 0.0,
    }


# ---------------------------------------------------------------- risk


def risk_failures_frame(conn: sqlite3.Connection, *, limit: int = 500) -> pd.DataFrame:
    """The risk-check failure log, from the audit trail."""
    rows: list[dict[str, Any]] = []
    for event in audit.read(conn, event=audit.AuditEvent.RISK_CHECK_FAILED, limit=limit):
        for reason in event["payload"].get("reasons", []):
            check, _, detail = reason.partition(": ")
            rows.append({
                "ts": pd.Timestamp(event["ts"]), "ticker": event["ticker"] or "—",
                "check": check, "detail": detail,
                "idea_id": event["payload"].get("idea_id", ""),
            })
    return pd.DataFrame(rows) if rows else pd.DataFrame(
        columns=["ts", "ticker", "check", "detail", "idea_id"]
    )


def failure_counts(frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty:
        return pd.DataFrame(columns=["check", "count"])
    counts = frame.groupby("check").size().reset_index(name="count")
    return counts.sort_values("count", ascending=False)


def rule_rejection_counts(conn: sqlite3.Connection, *, days: int = 365) -> pd.DataFrame:
    """Rules-layer rejections by rule — the rate that says whether the rules are
    biting and whether the synthesis prompts are drifting."""
    since = dt.date.today() - dt.timedelta(days=days)
    counts: dict[str, int] = {}
    for idea in repo.get_ideas(conn, since=since, limit=5000):
        for reason in idea.rejected_by_rules:
            rule = reason.split(":")[0]
            counts[rule] = counts.get(rule, 0) + 1
    if not counts:
        return pd.DataFrame(columns=["rule", "count"])
    return pd.DataFrame(
        [{"rule": rule, "count": count} for rule, count in sorted(counts.items())]
    )


# ---------------------------------------------------------------- ideas


def risk_outcomes(conn: sqlite3.Connection) -> dict[str, bool]:
    """``idea_id -> approved``, read from the audit trail.

    The risk verdict is NOT on the stored idea. ``score_universe`` persists an
    idea before ``assess`` runs, and ``assess`` returns its verdicts to the
    caller without writing them back — ideas are append-only, so it could not
    update them anyway. ``Idea.risk`` is therefore always None on anything read
    from the database, which makes ``Idea.accepted`` always False and unusable
    as "did this clear the risk layer".

    The audit trail is where the decision survives: ``assess`` records
    RISK_APPROVED or RISK_CHECK_FAILED against the idea id for every idea it
    evaluates. This reads that back, so "accepted" on any surface can mean what
    it says — cleared the rules layer AND approved by the risk layer.
    """
    outcomes: dict[str, bool] = {}
    for event, approved in ((audit.AuditEvent.RISK_CHECK_FAILED, False),
                            (audit.AuditEvent.RISK_APPROVED, True)):
        for row in conn.execute(
            "SELECT payload FROM audit WHERE event = ? ORDER BY id", (event,)
        ):
            try:
                idea_id = json.loads(row[0]).get("idea_id")
            except (TypeError, ValueError):
                continue
            if idea_id:
                outcomes[str(idea_id)] = approved
    return outcomes


def ideas_frame(conn: sqlite3.Connection, *, days: int = 365, limit: int = 500) -> pd.DataFrame:
    since = dt.date.today() - dt.timedelta(days=days)
    risk = risk_outcomes(conn)
    rows = [
        {
            "id": idea.id, "as_of": idea.as_of, "ticker": idea.ticker,
            "class": idea.idea_class.value, "conviction": idea.conviction.value,
            "direction": idea.direction.value, "score": _f(idea.composite_score),
            "accepted": (not idea.rejected_by_rules) and risk.get(idea.id) is True,
            "rules_ok": not idea.rejected_by_rules,
            "risk_ok": risk.get(idea.id),
            "rejections": len(idea.rejected_by_rules),
            "has_memo": idea.memo is not None,
        }
        for idea in repo.get_ideas(conn, since=since, limit=limit)
    ]
    return pd.DataFrame(rows) if rows else pd.DataFrame(
        columns=["id", "as_of", "ticker", "class", "conviction", "direction", "score",
                 "accepted", "rules_ok", "risk_ok", "rejections", "has_memo"]
    )


def idea(conn: sqlite3.Connection, idea_id: str) -> Idea | None:
    return repo.get_idea(conn, idea_id)


def module_scores_frame(item: Idea) -> pd.DataFrame:
    """Module scores as a deviation from neutral 50.

    Plotted diverging rather than as absolute bars: the question a reader has is
    "which modules are for this and which are against it", which is polarity,
    and a 0–100 bar chart makes 51 and 92 look like neighbours at the same end.
    """
    rows = [
        {
            "module": signal.module.value,
            "score": _f(signal.score),
            "delta": _f(signal.score) - 50.0,
            "confidence": _f(signal.confidence),
            "version": signal.module_version,
        }
        for signal in item.signals
    ]
    return pd.DataFrame(rows) if rows else pd.DataFrame(
        columns=["module", "score", "delta", "confidence", "version"]
    )


def evidence_frame(item: Idea) -> pd.DataFrame:
    rows = [
        {"module": signal.module.value, "key": e.key, "value": e.value,
         "weight": _f(e.weight), "source": e.source}
        for signal in item.signals for e in signal.evidence
    ]
    return pd.DataFrame(rows) if rows else pd.DataFrame(
        columns=["module", "key", "value", "weight", "source"]
    )


# ---------------------------------------------------------------- evals


# Eval inputs are assembled in evals/dataset.py and re-exported here, so the
# dashboard and the weekly review cannot drift into two different definitions
# of "which catalyst calls are scoreable".
catalyst_calls = dataset.catalyst_calls
conviction_outcomes = dataset.conviction_outcomes
stop_outcomes = dataset.stop_outcomes
llm_compliance = dataset.llm_compliance


def catalyst_call_counts(
    calls: Sequence[signal_quality.DirectionalCall],
) -> tuple[int, int]:
    """``(scoreable, abstained)``, counted by the function that writes the verdict.

    A "flat" call makes no directional claim, so ``direction_accuracy`` excludes
    it — otherwise a module could farm accuracy by never committing. The evals
    page used to headline ``len(calls)`` against the 100-sample gate while the
    verdict directly beneath it counted only the committed ones, so a live page
    read "26 — 100 needed for a verdict" above "48% on 21 calls". On the surface
    whose entire job is honest measurement, that reported MORE progress toward
    the gate than the gate had actually seen. Both numbers now come from here.
    """
    result = signal_quality.direction_accuracy(calls)
    return result.scoreable, result.calls - result.scoreable


def direction_accuracy_frame(calls: Sequence[signal_quality.DirectionalCall]) -> pd.DataFrame:
    """Hit rate with its Wilson interval, plus the coin-flip baseline.

    The interval is the point of this chart: 60% on ten calls and 60% on four
    hundred are the same number and completely different evidence, so a bare
    bar would be a misleading form here.
    """
    result = signal_quality.direction_accuracy(calls)
    if result.hit_rate is None or result.interval is None:
        return pd.DataFrame(columns=["label", "rate", "low", "high", "n", "verdict"])
    return pd.DataFrame([{
        "label": "Catalyst direction",
        "rate": result.hit_rate, "low": result.interval.low, "high": result.interval.high,
        "n": result.scoreable, "verdict": result.verdict(),
        "significant": bool(result.beats_coin_flip),
    }])


def materiality_frame(calls: Sequence[signal_quality.DirectionalCall]) -> pd.DataFrame:
    result = signal_quality.materiality_calibration(calls)
    rows = [
        {"bucket": str(b.bucket), "mean_abs_move": b.mean_abs_move, "samples": b.samples}
        for b in result.buckets
    ]
    frame = pd.DataFrame(rows) if rows else pd.DataFrame(
        columns=["bucket", "mean_abs_move", "samples"]
    )
    frame.attrs["verdict"] = result.verdict()
    frame.attrs["monotonic"] = result.monotonic
    return frame


def conviction_outcomes(conn: sqlite3.Connection) -> list[tuple[str, float]]:
    """(conviction label, realised return) for every closed position."""
    out: list[tuple[str, float]] = []
    for position in repo.get_all_positions(conn):
        if position.is_open or position.exit_price is None or position.entry <= 0:
            continue
        item = repo.get_idea(conn, position.idea_id)
        if item is None:
            continue
        out.append((item.conviction.value, float(position.exit_price / position.entry - 1)))
    return out


def conviction_frame(outcomes: Sequence[tuple[str, float]]) -> pd.DataFrame:
    result = calibration.conviction_calibration(list(outcomes))
    rows = [
        {"conviction": b.label, "mean_return": b.mean_return, "samples": b.samples,
         "win_rate": b.win_rate}
        for b in result.bands
    ]
    frame = pd.DataFrame(rows) if rows else pd.DataFrame(
        columns=["conviction", "mean_return", "samples", "win_rate"]
    )
    frame.attrs["verdict"] = result.verdict()
    frame.attrs["ordered"] = result.ordered
    return frame


# ---------------------------------------------------------------- data health


def freshness_frame(conn: sqlite3.Connection, *, as_of: dt.date | None = None) -> pd.DataFrame:
    """Age of the newest bar per ticker, with a status band.

    The bands are the same thresholds the quality layer uses, so the dashboard
    and `sentinel health` cannot disagree about what "stale" means.
    """
    as_of = as_of or dt.date.today()
    from ..data.quality import business_days_between

    rows: list[dict[str, Any]] = []
    for ticker in repo.tickers_with_bars(conn):
        last = repo.latest_bar_date(conn, ticker)
        age = business_days_between(last, as_of) if last else 999
        status = "good" if age <= 1 else ("warning" if age <= 4 else "critical")
        rows.append({
            "ticker": ticker, "last_bar": last, "age_days": age, "status": status,
            "fundamentals": repo.latest_fundamentals_date(conn, ticker),
        })
    return pd.DataFrame(rows) if rows else pd.DataFrame(
        columns=["ticker", "last_bar", "age_days", "status", "fundamentals"]
    )


def quality_history_frame(conn: sqlite3.Connection, *, days: int = 60) -> pd.DataFrame:
    """Quality issues per day by severity."""
    since = dt.date.today() - dt.timedelta(days=days)
    issues = repo.get_quality_issues(conn, since=since, limit=5000)
    if not issues:
        return pd.DataFrame(columns=["as_of", "severity", "count"])
    frame = pd.DataFrame([
        {"as_of": pd.Timestamp(i.as_of), "severity": i.severity.value} for i in issues
    ])
    counts = frame.groupby(["as_of", "severity"]).size().reset_index(name="count")
    return counts


def quality_issue_table(conn: sqlite3.Connection, *, limit: int = 200) -> pd.DataFrame:
    issues = repo.get_quality_issues(conn, limit=limit)
    rows = [
        {"as_of": i.as_of, "severity": i.severity.value, "ticker": i.ticker or "—",
         "check": i.check, "detail": i.detail}
        for i in issues
    ]
    return pd.DataFrame(rows) if rows else pd.DataFrame(
        columns=["as_of", "severity", "ticker", "check", "detail"]
    )


def severity_counts(conn: sqlite3.Connection, *, days: int = 60) -> dict[str, int]:
    since = dt.date.today() - dt.timedelta(days=days)
    counts = {level.value: 0 for level in Severity}
    for issue in repo.get_quality_issues(conn, since=since, limit=5000):
        counts[issue.severity.value] += 1
    return counts


def audit_counts(conn: sqlite3.Connection) -> pd.DataFrame:
    counts = audit.counts_by_event(conn)
    rows = [{"event": event, "count": count} for event, count in sorted(counts.items())]
    return pd.DataFrame(rows) if rows else pd.DataFrame(columns=["event", "count"])

# ---------------------------------------------------------------- one ticker


@dataclass(frozen=True, slots=True)
class TickerVerdict:
    """What the system says about one ticker, and why.

    Never a bare signal. `stance` is derived from decisions the pipeline already
    made — it applies no threshold of its own — and every stance carries the
    reasons behind it plus, where the memo supplies one, what would falsify it.
    A ticker the pipeline has not scored gets NOT SCORED rather than a guess:
    the honest answer to "should I buy this" is sometimes "this system has no
    opinion".
    """

    ticker: str
    stance: str                       # BUY | HOLD | AVOID | NOT SCORED
    headline: str
    as_of: dt.date | None = None
    composite: float | None = None
    conviction: str | None = None
    horizon_days: int | None = None
    reasons: tuple[str, ...] = ()
    blockers: tuple[str, ...] = ()
    falsifier: str | None = None
    thesis: str | None = None

    @property
    def ready_to_buy(self) -> bool:
        return self.stance == "BUY"


def searchable_tickers(conn: sqlite3.Connection) -> list[str]:
    return repo.tickers_with_bars(conn)


def latest_idea_for(conn: sqlite3.Connection, ticker: str, *, days: int = 365) -> Idea | None:
    since = dt.date.today() - dt.timedelta(days=days)
    matches = [i for i in repo.get_ideas(conn, since=since, limit=2000) if i.ticker == ticker]
    return max(matches, key=lambda i: i.as_of) if matches else None


def price_frame(conn: sqlite3.Connection, ticker: str, *, days: int = 365) -> pd.DataFrame:
    bars = repo.get_bars(conn, ticker)
    rows = [
        {"date": b.date, "close": _f(b.adjusted_close), "volume": int(b.volume or 0)}
        for b in bars[-days:]
    ]
    return pd.DataFrame(rows) if rows else pd.DataFrame(columns=["date", "close", "volume"])


def conviction_board(
    conn: sqlite3.Connection, *, min_score: int = 80, days: int = 14
) -> tuple[list[Idea], Idea | None]:
    """(qualifying ideas, best near-miss).

    A "strong buy" here is strict by construction: the LATEST idea per ticker,
    ACCEPTED by both the rules and risk layers, with composite >= min_score.
    A high score that failed risk is not a buy at any threshold — showing it
    would be exactly the bare-signal surface the spec forbids.

    The near-miss is returned so an empty board can say "the best accepted
    idea today is X at 74" instead of just "nothing" — an honest empty state
    names the distance to the bar rather than quietly lowering it.
    """
    since = dt.date.today() - dt.timedelta(days=days)
    risk = risk_outcomes(conn)
    latest: dict[str, Idea] = {}
    for idea_ in repo.get_ideas(conn, since=since, limit=1000):
        # get_ideas returns newest first; keep the first seen per ticker.
        latest.setdefault(idea_.ticker, idea_)
    accepted = [
        idea_ for idea_ in latest.values()
        if not idea_.rejected_by_rules and risk.get(idea_.id) is True
    ]
    accepted.sort(key=lambda i: i.composite_score, reverse=True)
    qualifying = [i for i in accepted if i.composite_score >= min_score]
    near_miss = next((i for i in accepted if i.composite_score < min_score), None)
    return qualifying, near_miss


def list_reports(briefs_dir: str | Path) -> list[Path]:
    """Every written brief/review, newest first — the files `sentinel brief`
    and `sentinel weekly` leave in `paths.briefs`. The terminal was the only
    place these ever appeared; the Reports page renders them."""
    directory = Path(briefs_dir)
    if not directory.is_dir():
        return []
    return sorted(directory.glob("*.md"), key=lambda p: p.name, reverse=True)


def normalize_ticker(raw: str) -> str:
    """Free-text ticker -> the exchange-suffixed form the pipeline uses.

    A bare US symbol ("sofi") becomes SOFI.US; anything already carrying a
    suffix is respected. The suffix is required because currency inference
    hangs off it — a bare ticker would be treated as GBP.
    """
    ticker = raw.strip().upper()
    if not ticker:
        return ""
    return ticker if "." in ticker else f"{ticker}.US"


def memo_absence_reason(conn: sqlite3.Connection, item: Idea) -> str | None:
    """Why this idea has no memo — from the audit trail, not a guess.

    The detail page used to show one static sentence ("Without an LLM
    configured …") for every memo-less idea. But the pipeline has three
    distinct reasons a memo is absent, and two of them mean the opposite of
    "not configured": the composite never reached the memo bar, or the LLM ran
    and its call FAILED — which the pipeline records per ticker as
    LLM_SCHEMA_FAILURE precisely so the failure rate is measurable (§5.2). The
    audit trail knows which one happened; showing a guess instead of reading
    it is how "why is everything rejected?" becomes a support question.
    """
    if item.memo is not None:
        return None
    if item.composite_score < Decimal("50"):
        return (f"No memo: the composite ({item.composite_score:.0f}) is below the "
                f"memo bar of 50, so the pipeline never asks the LLM for one. A "
                f"long-term idea without a memo has no written invalidation, so "
                f"the risk layer refuses it — the intended degradation.")
    day = item.as_of.isoformat()
    for row in conn.execute(
        "SELECT payload FROM audit WHERE event = ? AND ticker = ? "
        "AND substr(ts, 1, 10) = ? ORDER BY id DESC LIMIT 1",
        (audit.AuditEvent.LLM_SCHEMA_FAILURE, item.ticker, day),
    ):
        error = (json.loads(row["payload"]) or {}).get("error", "unknown error")
        return (f"No memo: the LLM was configured but its call FAILED on this run — "
                f"{error} — so there is no written invalidation and the risk layer "
                f"refuses the idea. Fix the LLM error and re-run `sentinel weekly`.")
    return ("No memo: no LLM was configured when this run happened (the composite "
            "cleared the bar and no failed call is recorded). The deterministic "
            "modules still score, but nothing writes an invalidation, so the risk "
            "layer refuses every long-term idea. Set ANTHROPIC_API_KEY in .env and "
            "re-run `sentinel weekly`.")


def news_frame(conn: sqlite3.Connection, ticker: str, *, days: int = 14,
               limit: int = 25) -> pd.DataFrame:
    """Recent headlines for one ticker, newest first.

    The news is already in the database — Finnhub writes it at every ingest and
    the sentiment module scores it — but nothing ever showed it to the person,
    so "where is the news captured?" was a fair question with a bad answer:
    captured, scored, and invisible.
    """
    since = dt.datetime.now(dt.UTC) - dt.timedelta(days=days)
    items = repo.get_news(conn, ticker, since=since, limit=limit)
    rows = [
        {"published": i.published_at.date(), "headline": i.headline,
         "source": i.source or "—", "url": i.url or ""}
        for i in items
    ]
    return pd.DataFrame(rows) if rows else pd.DataFrame(
        columns=["published", "headline", "source", "url"])


def sma_crosses(frame: pd.DataFrame) -> pd.DataFrame:
    """Golden/death crosses of the 50-day SMA over the 200-day.

    This is the honest version of "show me when to buy on the chart": a
    deterministic, widely used trend event, plotted where it happened — not a
    prediction. The verdict banner above the chart stays the system's actual
    opinion; these markers only say what the two averages did.
    """
    if frame.empty or len(frame) < 200:
        return pd.DataFrame(columns=["date", "close", "kind", "label"])
    data = frame.copy()
    sma50 = data["close"].rolling(50).mean()
    sma200 = data["close"].rolling(200).mean()
    above = sma50 > sma200
    flips = above.ne(above.shift()) & above.shift().notna() & sma200.notna()
    rows = [
        {"date": data["date"].iloc[i], "close": data["close"].iloc[i],
         "kind": "golden" if above.iloc[i] else "death",
         "label": "Golden cross · SMA50 above SMA200" if above.iloc[i]
                  else "Death cross · SMA50 below SMA200"}
        for i in [j for j, f in enumerate(flips) if f]
    ]
    return pd.DataFrame(rows) if rows else pd.DataFrame(
        columns=["date", "close", "kind", "label"])


def ticker_stats(conn: sqlite3.Connection, ticker: str) -> dict[str, Any]:
    """Deterministic statistics, computed by the same indicators the technical
    module scores with — not a second implementation."""
    from ..analysis import indicators

    bars = repo.get_bars(conn, ticker)
    if not bars:
        return {}
    closes = indicators.to_series([_f(b.adjusted_close) for b in bars])
    highs = indicators.to_series([_f(b.high) for b in bars])
    lows = indicators.to_series([_f(b.low) for b in bars])
    volumes = indicators.to_series([float(b.volume or 0) for b in bars])

    def last(series: pd.Series) -> float | None:
        value = series.dropna()
        return float(value.iloc[-1]) if len(value) else None

    latest = _f(bars[-1].adjusted_close)
    sma50, sma200 = last(indicators.sma(closes, 50)), last(indicators.sma(closes, 200))
    return {
        "last_close": latest,
        "last_bar": bars[-1].date,
        "bars": len(bars),
        "rsi14": last(indicators.rsi(closes)),
        "sma50": sma50,
        "sma200": sma200,
        "above_sma200": None if sma200 is None else latest > sma200,
        "atr14": last(indicators.atr(highs, lows, closes)),
        "momentum_12_1": indicators.momentum_12_1(closes),
        "realised_vol": indicators.realised_volatility(closes),
        "volume_z": indicators.volume_zscore(volumes),
        "drawdown": indicators.drawdown_from_peak(closes),
    }


def _risk_reasons(conn: sqlite3.Connection, idea_id: str) -> tuple[str, ...]:
    for row in conn.execute(
        "SELECT payload FROM audit WHERE event = ? ORDER BY id DESC",
        (audit.AuditEvent.RISK_CHECK_FAILED,),
    ):
        try:
            payload = json.loads(row[0])
        except (TypeError, ValueError):
            continue
        if payload.get("idea_id") == idea_id:
            return tuple(str(r) for r in payload.get("reasons", ()))
    return ()


def verdict_for(conn: sqlite3.Connection, ticker: str) -> TickerVerdict:
    """Turn the pipeline's own decisions into a readable answer.

    Applies no scoring of its own. Every branch here reads a decision some other
    layer already recorded, which is what keeps this page from becoming a second
    opinion that can disagree with the brief.
    """
    item = latest_idea_for(conn, ticker)
    if item is None:
        return TickerVerdict(
            ticker=ticker, stance="NOT SCORED",
            headline="This system has not scored this ticker.",
            reasons=("No idea has been generated for it in the last year. It may not be "
                     "in a configured universe, or its data may have failed a quality check.",),
        )

    blockers = tuple(f"rules layer: {code}" for code in item.rejected_by_rules)
    # From the audit trail, not item.risk — see risk_outcomes().
    approved = risk_outcomes(conn).get(item.id)
    if approved is False:
        blockers += tuple(f"risk layer: {reason}" for reason in _risk_reasons(conn, item.id)) \
            or ("risk layer: refused",)
    elif approved is None:
        blockers += ("risk layer: not evaluated for this idea",)

    reasons = tuple(
        f"{signal.module}: {_f(signal.score):.0f}/100" for signal in item.signals
    )
    memo = item.memo
    common = {
        "ticker": ticker, "as_of": item.as_of,
        "composite": _f(item.composite_score),
        "conviction": item.conviction.value,
        "horizon_days": memo.horizon_days if memo else None,
        "reasons": reasons, "blockers": blockers,
        "falsifier": memo.invalidation if memo else None,
        "thesis": memo.thesis if memo else None,
    }

    if blockers:
        return TickerVerdict(
            stance="AVOID",
            headline="Scored, then refused — this is not a buy.",
            **common,
        )
    if item.direction.value == "long":
        return TickerVerdict(
            stance="BUY",
            headline="Cleared the rules layer and the risk layer.",
            **common,
        )
    if item.direction.value == "avoid":
        return TickerVerdict(
            stance="AVOID", headline="The pipeline's direction on this is avoid.", **common)
    return TickerVerdict(
        stance="HOLD",
        headline="Scored, but the pipeline commits to no direction.",
        **common,
    )
