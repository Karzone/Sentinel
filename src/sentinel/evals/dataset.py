"""Assembling eval inputs from the store.

This lives in ``evals/`` rather than in whichever surface happened to need it
first. Every consumer — the dashboard, the weekly review, the CLI's `evals`
command — reads its inputs from here, so there is exactly one definition of
"which catalyst calls are scoreable" and one of "what a conviction outcome is".

That matters more than the small amount of code involved. A dashboard that
assembled its own version of a hit rate would eventually disagree with the eval
that gates real money, and the wrong number would be the one on screen. The
functions were originally written in ``dashboard/queries.py``; moving them here
is what makes that guarantee structural instead of a promise.
"""

from __future__ import annotations

import datetime as dt
import sqlite3
from typing import Any

from ..storage import repo
from . import signal_quality

DATASET_VERSION = "eval-dataset-v1"


def realised_return(
    conn: sqlite3.Connection, ticker: str, start: dt.date, horizon_days: int
) -> float | None:
    """Total return from ``start`` over ``horizon_days``, or None if unknowable."""
    bars = repo.get_bars(conn, ticker, start=start)
    if len(bars) < 2:
        return None
    entry = bars[0].adjusted_close
    target_date = start + dt.timedelta(days=horizon_days)
    exit_bar = None
    for bar in bars:
        exit_bar = bar
        if bar.date >= target_date:
            break
    if exit_bar is None or entry <= 0 or exit_bar.date <= start:
        return None
    return float(exit_bar.adjusted_close / entry - 1)


def catalyst_calls(
    conn: sqlite3.Connection, *, days: int = 730, as_of: dt.date | None = None
) -> list[signal_quality.DirectionalCall]:
    """Every stored catalyst read whose horizon has fully elapsed, scored against
    what the price actually did.

    The elapsed-horizon filter is the load-bearing part: scoring a 90-day call
    after 20 days quietly biases the hit rate toward whatever the last three
    weeks happened to do, which is exactly the kind of flattering error a
    calibration eval exists to avoid.
    """
    as_of = as_of or dt.date.today()
    since = as_of - dt.timedelta(days=days)
    calls: list[signal_quality.DirectionalCall] = []
    for item in repo.get_ideas(conn, since=since, limit=5000):
        catalyst = item.catalyst
        if catalyst is None:
            continue
        if item.as_of + dt.timedelta(days=catalyst.horizon_days) > as_of:
            continue
        realised = realised_return(conn, item.ticker, item.as_of, catalyst.horizon_days)
        if realised is None:
            continue
        calls.append(signal_quality.DirectionalCall(
            ticker=item.ticker, predicted=catalyst.direction.value, realised_return=realised,
            materiality=catalyst.materiality, horizon_days=catalyst.horizon_days,
        ))
    return calls


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


def stop_outcomes(conn: sqlite3.Connection) -> list[tuple[Any, Any]]:
    """(stop level, best price reached after the stop) for stopped-out positions.

    Feeds §5.3's stop-quality eval, which asks whether the stops are cutting
    losses or harvesting noise. A position with no bars after its exit is
    skipped rather than counted as "never recovered" — absence of evidence is
    not evidence the stop was right.
    """
    from ..domain.enums import PositionStatus

    out: list[tuple[Any, Any]] = []
    for position in repo.get_all_positions(conn):
        if position.status is not PositionStatus.CLOSED_STOP or position.stop is None:
            continue
        after = repo.get_bars(conn, position.ticker, start=position.closed_on)
        if len(after) < 2:
            continue
        best = max(bar.adjusted_close for bar in after[1:])
        out.append((position.stop, best))
    return out


def llm_compliance(conn: sqlite3.Connection) -> dict[str, Any]:
    stats = repo.llm_schema_compliance(conn)
    stats["verdict"] = signal_quality.schema_compliance_verdict(stats)
    return stats
