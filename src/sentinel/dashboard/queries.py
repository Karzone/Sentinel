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


def ideas_frame(conn: sqlite3.Connection, *, days: int = 365, limit: int = 500) -> pd.DataFrame:
    since = dt.date.today() - dt.timedelta(days=days)
    rows = [
        {
            "id": idea.id, "as_of": idea.as_of, "ticker": idea.ticker,
            "class": idea.idea_class.value, "conviction": idea.conviction.value,
            "direction": idea.direction.value, "score": _f(idea.composite_score),
            "accepted": not idea.rejected_by_rules,
            "rejections": len(idea.rejected_by_rules),
            "has_memo": idea.memo is not None,
        }
        for idea in repo.get_ideas(conn, since=since, limit=limit)
    ]
    return pd.DataFrame(rows) if rows else pd.DataFrame(
        columns=["id", "as_of", "ticker", "class", "conviction", "direction", "score",
                 "accepted", "rejections", "has_memo"]
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
