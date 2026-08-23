"""The append-only audit trail.

Every consequential act writes one row here: an idea generated, a risk check
failed, an LLM call made, a notification sent, a position opened or closed.
The table has UPDATE/DELETE triggers on it (storage/db.py), so this module is
write-and-read, never edit.
"""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
from typing import Any, Iterable, Mapping

from ..logging_setup import RUN_ID


class AuditEvent:
    """Event names. String constants rather than an enum because the trail
    outlives the code: an archived row must stay readable after its constant is
    deleted, and a stale name should read as data, not crash a replay."""

    INGEST_STARTED = "ingest.started"
    INGEST_COMPLETED = "ingest.completed"
    QUALITY_ISSUE = "quality.issue"
    MODULE_SCORED = "module.scored"
    LLM_CALL = "llm.call"
    LLM_SCHEMA_FAILURE = "llm.schema_failure"
    RULES_REJECTED = "rules.rejected"
    IDEA_GENERATED = "idea.generated"
    RISK_CHECK_FAILED = "risk.check_failed"
    RISK_APPROVED = "risk.approved"
    KILL_SWITCH = "risk.kill_switch"
    POSITION_OPENED = "position.opened"
    POSITION_CLOSED = "position.closed"
    STOP_TRIGGERED = "position.stop_triggered"
    INVALIDATION_HIT = "position.invalidation_hit"
    BRIEF_GENERATED = "brief.generated"
    NOTIFICATION = "notification.sent"
    EVAL_RUN = "eval.run"


def record(
    conn: sqlite3.Connection,
    event: str,
    *,
    ticker: str | None = None,
    payload: Mapping[str, Any] | None = None,
    at: dt.datetime | None = None,
) -> None:
    conn.execute(
        "INSERT INTO audit(ts, run_id, event, ticker, payload) VALUES(?,?,?,?,?)",
        (
            (at or dt.datetime.now(dt.UTC)).isoformat(),
            RUN_ID,
            event,
            ticker,
            json.dumps(payload or {}, default=str, sort_keys=True),
        ),
    )


def read(
    conn: sqlite3.Connection,
    *,
    event: str | None = None,
    ticker: str | None = None,
    since: dt.datetime | None = None,
    limit: int = 500,
) -> list[dict[str, Any]]:
    clauses: list[str] = []
    args: list[Any] = []
    if event:
        clauses.append("event = ?")
        args.append(event)
    if ticker:
        clauses.append("ticker = ?")
        args.append(ticker)
    if since:
        clauses.append("ts >= ?")
        args.append(since.isoformat())
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = conn.execute(
        f"SELECT ts, run_id, event, ticker, payload FROM audit {where} ORDER BY id DESC LIMIT ?",
        (*args, limit),
    ).fetchall()
    return [
        {
            "ts": r["ts"],
            "run_id": r["run_id"],
            "event": r["event"],
            "ticker": r["ticker"],
            "payload": json.loads(r["payload"]),
        }
        for r in rows
    ]


def trace_for_idea(conn: sqlite3.Connection, idea_id: str) -> list[dict[str, Any]]:
    """§5.4: any real-money decision must be traceable to the exact brief, inputs
    and model versions that informed it. This is that query."""
    rows = conn.execute(
        "SELECT ts, run_id, event, ticker, payload FROM audit "
        "WHERE payload LIKE ? ORDER BY id ASC",
        (f'%"{idea_id}"%',),
    ).fetchall()
    return [
        {"ts": r["ts"], "run_id": r["run_id"], "event": r["event"],
         "ticker": r["ticker"], "payload": json.loads(r["payload"])}
        for r in rows
    ]


def counts_by_event(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute("SELECT event, COUNT(*) n FROM audit GROUP BY event").fetchall()
    return {r["event"]: r["n"] for r in rows}


def bulk_record(conn: sqlite3.Connection, events: Iterable[tuple[str, str | None, Mapping[str, Any]]]) -> None:
    now = dt.datetime.now(dt.UTC).isoformat()
    conn.executemany(
        "INSERT INTO audit(ts, run_id, event, ticker, payload) VALUES(?,?,?,?,?)",
        [
            (now, RUN_ID, event, ticker, json.dumps(dict(payload), default=str, sort_keys=True))
            for event, ticker, payload in events
        ],
    )
