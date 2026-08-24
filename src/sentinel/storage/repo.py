"""Repositories: the only place that turns domain objects into rows and back.

Nothing outside this module writes SQL against the sentinel database, so the
Postgres migration is one file's worth of work rather than a grep.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import sqlite3
from decimal import Decimal
from typing import Any, Iterable, Sequence

from ..domain.enums import PositionStatus, Severity
from ..domain.models import (
    Bar, Brief, DataQualityIssue, Fill, Fundamentals, Idea, NewsItem, Position,
)
from ..money import dec
from . import audit
from .db import transaction

# ------------------------------------------------------------------ helpers


def _now() -> str:
    return dt.datetime.now(dt.UTC).isoformat()


def _d(value: Any) -> Decimal | None:
    return None if value is None else dec(value)


def _dumps(model: Any) -> str:
    return model.model_dump_json()


# ------------------------------------------------------------------ bars


def save_bars(conn: sqlite3.Connection, bars: Iterable[Bar], *, source: str) -> int:
    rows = [
        (b.ticker, b.date.isoformat(), str(b.open), str(b.high), str(b.low),
         str(b.close), str(b.adjusted_close), b.volume, b.currency, _now(), source)
        for b in bars
    ]
    if not rows:
        return 0
    with transaction(conn):
        # Re-ingesting a day overwrites it: vendors restate splits and dividends,
        # and a stale adjusted_close is exactly the bad data §Phase 1 calls Sev-1.
        conn.executemany(
            "INSERT INTO bars(ticker,date,open,high,low,close,adjusted_close,volume,currency,ingested_at,source) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(ticker,date) DO UPDATE SET "
            "open=excluded.open, high=excluded.high, low=excluded.low, close=excluded.close, "
            "adjusted_close=excluded.adjusted_close, volume=excluded.volume, "
            "ingested_at=excluded.ingested_at, source=excluded.source",
            rows,
        )
    return len(rows)


def get_bars(
    conn: sqlite3.Connection,
    ticker: str,
    *,
    start: dt.date | None = None,
    end: dt.date | None = None,
    limit: int | None = None,
) -> list[Bar]:
    clauses = ["ticker = ?"]
    args: list[Any] = [ticker]
    if start:
        clauses.append("date >= ?")
        args.append(start.isoformat())
    if end:
        clauses.append("date <= ?")
        args.append(end.isoformat())
    sql = f"SELECT * FROM bars WHERE {' AND '.join(clauses)} ORDER BY date ASC"
    rows = conn.execute(sql, args).fetchall()
    if limit is not None:
        rows = rows[-limit:]
    return [
        Bar(
            ticker=r["ticker"], date=dt.date.fromisoformat(r["date"]),
            open=_d(r["open"]), high=_d(r["high"]), low=_d(r["low"]),
            close=_d(r["close"]), adjusted_close=_d(r["adjusted_close"]),
            volume=r["volume"], currency=r["currency"],
        )
        for r in rows
    ]


def latest_bar_date(conn: sqlite3.Connection, ticker: str) -> dt.date | None:
    row = conn.execute("SELECT MAX(date) d FROM bars WHERE ticker = ?", (ticker,)).fetchone()
    return dt.date.fromisoformat(row["d"]) if row and row["d"] else None


def tickers_with_bars(conn: sqlite3.Connection) -> list[str]:
    return [r["ticker"] for r in conn.execute("SELECT DISTINCT ticker FROM bars ORDER BY ticker")]


# ------------------------------------------------------------------ fundamentals


def save_fundamentals(conn: sqlite3.Connection, items: Iterable[Fundamentals], *, source: str) -> int:
    rows = [(f.ticker, f.as_of.isoformat(), _dumps(f), _now(), source) for f in items]
    if not rows:
        return 0
    with transaction(conn):
        conn.executemany(
            "INSERT INTO fundamentals(ticker, as_of, payload, ingested_at, source) VALUES(?,?,?,?,?) "
            "ON CONFLICT(ticker, as_of) DO UPDATE SET payload=excluded.payload, "
            "ingested_at=excluded.ingested_at, source=excluded.source",
            rows,
        )
    return len(rows)


def get_fundamentals(
    conn: sqlite3.Connection, ticker: str, *, as_of: dt.date | None = None
) -> Fundamentals | None:
    """Point-in-time read. With ``as_of`` set, returns the latest snapshot whose
    filing date is *on or before* that date — which is what stops a backtest
    scoring 2019 on numbers that were published in 2021."""
    if as_of:
        row = conn.execute(
            "SELECT payload FROM fundamentals WHERE ticker = ? AND as_of <= ? "
            "ORDER BY as_of DESC LIMIT 1",
            (ticker, as_of.isoformat()),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT payload FROM fundamentals WHERE ticker = ? ORDER BY as_of DESC LIMIT 1",
            (ticker,),
        ).fetchone()
    return Fundamentals.model_validate_json(row["payload"]) if row else None


def latest_fundamentals_date(conn: sqlite3.Connection, ticker: str) -> dt.date | None:
    row = conn.execute("SELECT MAX(as_of) d FROM fundamentals WHERE ticker = ?", (ticker,)).fetchone()
    return dt.date.fromisoformat(row["d"]) if row and row["d"] else None


# ------------------------------------------------------------------ news


def _news_id(item: NewsItem) -> str:
    raw = f"{item.ticker}|{item.published_at.isoformat()}|{item.headline}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def save_news(conn: sqlite3.Connection, items: Iterable[NewsItem]) -> int:
    rows = [
        (_news_id(i), i.ticker, i.published_at.isoformat(), i.headline,
         i.summary, i.source, i.url, _now())
        for i in items
    ]
    if not rows:
        return 0
    with transaction(conn):
        # Same story from two feeds collapses on the content hash.
        conn.executemany(
            "INSERT INTO news(id,ticker,published_at,headline,summary,source,url,ingested_at) "
            "VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(id) DO NOTHING",
            rows,
        )
    return len(rows)


def get_news(
    conn: sqlite3.Connection, ticker: str, *, since: dt.datetime | None = None, limit: int = 50
) -> list[NewsItem]:
    args: list[Any] = [ticker]
    clause = ""
    if since:
        clause = "AND published_at >= ?"
        args.append(since.isoformat())
    rows = conn.execute(
        f"SELECT * FROM news WHERE ticker = ? {clause} ORDER BY published_at DESC LIMIT ?",
        (*args, limit),
    ).fetchall()
    return [
        NewsItem(
            ticker=r["ticker"], published_at=dt.datetime.fromisoformat(r["published_at"]),
            headline=r["headline"], summary=r["summary"], source=r["source"], url=r["url"],
        )
        for r in rows
    ]


# ------------------------------------------------------------------ ideas


def save_idea(conn: sqlite3.Connection, idea: Idea) -> None:
    """Insert only. The table's triggers reject UPDATE, so re-running a day
    raises rather than silently rewriting history — supersede with a new id."""
    conn.execute(
        "INSERT INTO ideas(id,created_at,as_of,ticker,idea_class,conviction,direction,"
        "composite_score,accepted,inputs_digest,payload) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (
            idea.id, idea.created_at.isoformat(), idea.as_of.isoformat(), idea.ticker,
            idea.idea_class.value, idea.conviction.value, idea.direction.value,
            str(idea.composite_score), int(idea.accepted), idea.inputs_digest,
            _dumps(idea),
        ),
    )
    audit.record(
        conn, audit.AuditEvent.IDEA_GENERATED, ticker=idea.ticker,
        payload={
            "idea_id": idea.id, "accepted": idea.accepted,
            "composite_score": str(idea.composite_score),
            "conviction": idea.conviction.value,
            "rejected_by_rules": list(idea.rejected_by_rules),
            "model_versions": dict(idea.model_versions),
            "inputs_digest": idea.inputs_digest,
        },
    )


def get_idea(conn: sqlite3.Connection, idea_id: str) -> Idea | None:
    row = conn.execute("SELECT payload FROM ideas WHERE id = ?", (idea_id,)).fetchone()
    return Idea.model_validate_json(row["payload"]) if row else None


def get_ideas(
    conn: sqlite3.Connection,
    *,
    ticker: str | None = None,
    accepted_only: bool = False,
    since: dt.date | None = None,
    limit: int = 200,
) -> list[Idea]:
    clauses: list[str] = []
    args: list[Any] = []
    if ticker:
        clauses.append("ticker = ?")
        args.append(ticker)
    if accepted_only:
        clauses.append("accepted = 1")
    if since:
        clauses.append("as_of >= ?")
        args.append(since.isoformat())
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = conn.execute(
        f"SELECT payload FROM ideas {where} ORDER BY as_of DESC, created_at DESC LIMIT ?",
        (*args, limit),
    ).fetchall()
    return [Idea.model_validate_json(r["payload"]) for r in rows]


# ------------------------------------------------------------------ positions


def position_id(ticker: str, opened_on: dt.date, idea_id: str) -> str:
    return hashlib.sha256(f"{ticker}|{opened_on}|{idea_id}".encode()).hexdigest()[:20]


def save_position(conn: sqlite3.Connection, pos: Position) -> str:
    pid = position_id(pos.ticker, pos.opened_on, pos.idea_id)
    conn.execute(
        "INSERT INTO positions(id,ticker,idea_id,idea_class,sector,opened_on,shares,entry,"
        "currency,fx_rate_at_entry,stop,invalidation,status,closed_on,exit_price) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(id) DO UPDATE SET shares=excluded.shares, status=excluded.status, "
        "closed_on=excluded.closed_on, exit_price=excluded.exit_price, stop=excluded.stop",
        (
            pid, pos.ticker, pos.idea_id, pos.idea_class.value, pos.sector,
            pos.opened_on.isoformat(), pos.shares, str(pos.entry), pos.currency,
            str(pos.fx_rate_at_entry), None if pos.stop is None else str(pos.stop),
            pos.invalidation, pos.status.value,
            pos.closed_on.isoformat() if pos.closed_on else None,
            None if pos.exit_price is None else str(pos.exit_price),
        ),
    )
    return pid


def _row_to_position(r: sqlite3.Row) -> Position:
    return Position(
        ticker=r["ticker"], idea_id=r["idea_id"], idea_class=r["idea_class"],
        sector=r["sector"], opened_on=dt.date.fromisoformat(r["opened_on"]),
        shares=r["shares"], entry=_d(r["entry"]), currency=r["currency"],
        fx_rate_at_entry=_d(r["fx_rate_at_entry"]), stop=_d(r["stop"]),
        invalidation=r["invalidation"], status=PositionStatus(r["status"]),
        closed_on=dt.date.fromisoformat(r["closed_on"]) if r["closed_on"] else None,
        exit_price=_d(r["exit_price"]),
    )


def get_position(conn: sqlite3.Connection, position_id_: str) -> Position | None:
    row = conn.execute("SELECT * FROM positions WHERE id = ?", (position_id_,)).fetchone()
    return _row_to_position(row) if row else None


def get_open_positions(conn: sqlite3.Connection) -> list[Position]:
    rows = conn.execute(
        "SELECT * FROM positions WHERE status = ? ORDER BY opened_on", (PositionStatus.OPEN.value,)
    ).fetchall()
    return [_row_to_position(r) for r in rows]


def get_all_positions(conn: sqlite3.Connection) -> list[Position]:
    rows = conn.execute("SELECT * FROM positions ORDER BY opened_on").fetchall()
    return [_row_to_position(r) for r in rows]


def save_fill(conn: sqlite3.Connection, position_id_: str, fill: Fill) -> None:
    conn.execute(
        "INSERT INTO fills(position_id,ticker,date,shares,price,currency,fx_rate,"
        "commission_gbp,stamp_duty_gbp,slippage_gbp) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (
            position_id_, fill.ticker, fill.date.isoformat(), fill.shares, str(fill.price),
            fill.currency, str(fill.fx_rate), str(fill.commission_gbp),
            str(fill.stamp_duty_gbp), str(fill.slippage_gbp),
        ),
    )


def get_fills(conn: sqlite3.Connection, *, ticker: str | None = None) -> list[Fill]:
    if ticker:
        rows = conn.execute("SELECT * FROM fills WHERE ticker = ? ORDER BY date", (ticker,)).fetchall()
    else:
        rows = conn.execute("SELECT * FROM fills ORDER BY date").fetchall()
    return [
        Fill(
            ticker=r["ticker"], date=dt.date.fromisoformat(r["date"]), shares=r["shares"],
            price=_d(r["price"]), currency=r["currency"], fx_rate=_d(r["fx_rate"]),
            commission_gbp=_d(r["commission_gbp"]), stamp_duty_gbp=_d(r["stamp_duty_gbp"]),
            slippage_gbp=_d(r["slippage_gbp"]),
        )
        for r in rows
    ]


# ------------------------------------------------------------------ equity curve


def save_equity_point(
    conn: sqlite3.Connection, date: dt.date, nav: Decimal, cash: Decimal, high_water: Decimal
) -> None:
    conn.execute(
        "INSERT INTO equity_curve(date,nav_gbp,cash_gbp,high_water_gbp) VALUES(?,?,?,?) "
        "ON CONFLICT(date) DO UPDATE SET nav_gbp=excluded.nav_gbp, cash_gbp=excluded.cash_gbp, "
        "high_water_gbp=excluded.high_water_gbp",
        (date.isoformat(), str(nav), str(cash), str(high_water)),
    )


def get_equity_curve(conn: sqlite3.Connection) -> list[tuple[dt.date, Decimal, Decimal, Decimal]]:
    rows = conn.execute("SELECT * FROM equity_curve ORDER BY date").fetchall()
    return [
        (dt.date.fromisoformat(r["date"]), _d(r["nav_gbp"]), _d(r["cash_gbp"]), _d(r["high_water_gbp"]))
        for r in rows
    ]


def high_water_mark(conn: sqlite3.Connection) -> Decimal | None:
    row = conn.execute("SELECT MAX(CAST(high_water_gbp AS REAL)) h, high_water_gbp FROM equity_curve").fetchone()
    if not row or row["h"] is None:
        return None
    # Re-read as text: the CAST above is only for MAX ordering, never for the value.
    best = conn.execute(
        "SELECT high_water_gbp FROM equity_curve ORDER BY CAST(high_water_gbp AS REAL) DESC LIMIT 1"
    ).fetchone()
    return _d(best["high_water_gbp"])


# ------------------------------------------------------------------ operations


def add_favourite(conn: sqlite3.Connection, ticker: str) -> None:
    conn.execute(
        "INSERT INTO watchlist(ticker, added_at) VALUES(?, ?) "
        "ON CONFLICT(ticker) DO NOTHING",
        (ticker.upper(), _now()),
    )


def remove_favourite(conn: sqlite3.Connection, ticker: str) -> None:
    conn.execute("DELETE FROM watchlist WHERE ticker = ?", (ticker.upper(),))


def list_favourites(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute("SELECT ticker FROM watchlist ORDER BY ticker").fetchall()
    return [r["ticker"] for r in rows]


def save_quality_issues(conn: sqlite3.Connection, issues: Sequence[DataQualityIssue]) -> None:
    if not issues:
        return
    with transaction(conn):
        conn.executemany(
            "INSERT INTO quality_issues(as_of,check_name,severity,ticker,detail,logged_at) "
            "VALUES(?,?,?,?,?,?)",
            [(i.as_of.isoformat(), i.check, i.severity.value, i.ticker, i.detail, _now()) for i in issues],
        )
        audit.bulk_record(
            conn,
            [(audit.AuditEvent.QUALITY_ISSUE, i.ticker,
              {"check": i.check, "severity": i.severity.value, "detail": i.detail})
             for i in issues],
        )


def get_quality_issues(
    conn: sqlite3.Connection, *, since: dt.date | None = None, limit: int = 200
) -> list[DataQualityIssue]:
    clause, args = ("WHERE as_of >= ?", [since.isoformat()]) if since else ("", [])
    rows = conn.execute(
        f"SELECT * FROM quality_issues {clause} ORDER BY id DESC LIMIT ?", (*args, limit)
    ).fetchall()
    return [
        DataQualityIssue(
            check=r["check_name"], severity=Severity(r["severity"]), ticker=r["ticker"],
            detail=r["detail"], as_of=dt.date.fromisoformat(r["as_of"]),
        )
        for r in rows
    ]


def save_brief(conn: sqlite3.Connection, brief: Brief, markdown: str) -> None:
    conn.execute(
        "INSERT INTO briefs(id,generated_at,as_of,markdown,payload) VALUES(?,?,?,?,?) "
        "ON CONFLICT(id) DO UPDATE SET markdown=excluded.markdown, payload=excluded.payload",
        (brief.id, brief.generated_at.isoformat(), brief.as_of.isoformat(), markdown, _dumps(brief)),
    )
    audit.record(
        conn, audit.AuditEvent.BRIEF_GENERATED,
        payload={"brief_id": brief.id, "as_of": brief.as_of.isoformat(),
                 "ideas": [i.id for i in brief.ideas], "stale": brief.stale},
    )


def get_brief(conn: sqlite3.Connection, brief_id: str) -> tuple[Brief, str] | None:
    row = conn.execute("SELECT payload, markdown FROM briefs WHERE id = ?", (brief_id,)).fetchone()
    return (Brief.model_validate_json(row["payload"]), row["markdown"]) if row else None


def latest_brief(conn: sqlite3.Connection) -> tuple[Brief, str] | None:
    row = conn.execute(
        "SELECT payload, markdown FROM briefs ORDER BY generated_at DESC LIMIT 1"
    ).fetchone()
    return (Brief.model_validate_json(row["payload"]), row["markdown"]) if row else None


def record_llm_call(
    conn: sqlite3.Connection, *, module: str, model: str, prompt_hash: str,
    attempts: int, schema_ok: bool, repaired: bool, error: str | None = None,
) -> None:
    conn.execute(
        "INSERT INTO llm_calls(ts,module,model,prompt_hash,attempts,schema_ok,repaired,error) "
        "VALUES(?,?,?,?,?,?,?,?)",
        (_now(), module, model, prompt_hash, attempts, int(schema_ok), int(repaired), error),
    )


def llm_schema_compliance(conn: sqlite3.Connection, *, module: str | None = None) -> dict[str, Any]:
    """§5.2's schema-compliance rate, target >= 99% after retry.

    Two numbers, because they answer different questions: ``first_pass_rate`` is
    how good the prompt is, ``rate`` is what the pipeline actually delivers.
    """
    clause, args = ("WHERE module = ?", [module]) if module else ("", [])
    row = conn.execute(
        f"SELECT COUNT(*) n, SUM(schema_ok) ok, SUM(repaired) repaired FROM llm_calls {clause}", args
    ).fetchone()
    total = row["n"] or 0
    ok = row["ok"] or 0
    repaired = row["repaired"] or 0
    return {
        "calls": total,
        "schema_ok": ok,
        "repaired": repaired,
        "rate": (ok / total) if total else None,
        "first_pass_rate": ((ok - repaired) / total) if total else None,
    }


def record_notification(
    conn: sqlite3.Connection, *, channel: str, event: str, subject: str,
    delivered: bool, error: str | None = None,
) -> None:
    conn.execute(
        "INSERT INTO notifications(ts,channel,event,subject,delivered,error) VALUES(?,?,?,?,?,?)",
        (_now(), channel, event, subject, int(delivered), error),
    )
    audit.record(
        conn, audit.AuditEvent.NOTIFICATION,
        payload={"channel": channel, "event": event, "delivered": delivered, "error": error},
    )


def save_fx_rates(conn: sqlite3.Connection, as_of: dt.date, rates: dict[str, Decimal]) -> None:
    with transaction(conn):
        conn.executemany(
            "INSERT INTO fx_rates(as_of,currency,rate) VALUES(?,?,?) "
            "ON CONFLICT(as_of,currency) DO UPDATE SET rate=excluded.rate",
            [(as_of.isoformat(), ccy, str(rate)) for ccy, rate in rates.items()],
        )


def get_fx_rates(conn: sqlite3.Connection, as_of: dt.date) -> dict[str, Decimal]:
    """Latest rate at or before ``as_of`` per currency — a brief replayed for a
    past date must use the rates of that date, not today's."""
    rows = conn.execute(
        "SELECT currency, rate FROM fx_rates WHERE as_of = "
        "(SELECT MAX(as_of) FROM fx_rates WHERE as_of <= ?)",
        (as_of.isoformat(),),
    ).fetchall()
    return {r["currency"]: _d(r["rate"]) for r in rows}
