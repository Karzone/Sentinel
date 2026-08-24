"""SQLite schema and connection handling.

Two decisions worth knowing before you edit this file.

**Every money/price column is TEXT, never REAL.** SQLite's REAL is a float64;
round-tripping ``Decimal("0.1")`` through it gives back something that is not
0.1, and rule 5 says all financial calculations are Decimal. Storing the digits
as text is the only way the Decimal that comes out equals the one that went in.

**The audit and idea tables are append-only, enforced by triggers.** Not by
convention, not by "we only ever INSERT" — by ``RAISE(ABORT)`` on UPDATE and
DELETE. §5.4 requires that a real-money decision be traceable to the exact brief
and inputs that informed it; a trail that can be quietly edited afterwards
proves nothing, and a future maintainer with a migration script counts as
"quietly".

The upgrade path to Postgres is why nothing here uses a SQLite-only type.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

SCHEMA_VERSION = 1

SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- ---------------------------------------------------------------- market data
CREATE TABLE IF NOT EXISTS bars (
    ticker        TEXT NOT NULL,
    date          TEXT NOT NULL,          -- ISO yyyy-mm-dd
    open          TEXT NOT NULL,
    high          TEXT NOT NULL,
    low           TEXT NOT NULL,
    close         TEXT NOT NULL,
    adjusted_close TEXT NOT NULL,
    volume        INTEGER NOT NULL,
    currency      TEXT NOT NULL,
    ingested_at   TEXT NOT NULL,
    source        TEXT NOT NULL,
    PRIMARY KEY (ticker, date)
);
CREATE INDEX IF NOT EXISTS ix_bars_date ON bars(date);

CREATE TABLE IF NOT EXISTS fundamentals (
    ticker      TEXT NOT NULL,
    as_of       TEXT NOT NULL,
    payload     TEXT NOT NULL,            -- Idea/Fundamentals JSON
    ingested_at TEXT NOT NULL,
    source      TEXT NOT NULL,
    PRIMARY KEY (ticker, as_of)
);

CREATE TABLE IF NOT EXISTS news (
    id           TEXT PRIMARY KEY,        -- sha256(ticker|published_at|headline)
    ticker       TEXT NOT NULL,
    published_at TEXT NOT NULL,
    headline     TEXT NOT NULL,
    summary      TEXT NOT NULL DEFAULT '',
    source       TEXT NOT NULL DEFAULT '',
    url          TEXT NOT NULL DEFAULT '',
    ingested_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_news_ticker_date ON news(ticker, published_at);

CREATE TABLE IF NOT EXISTS fx_rates (
    as_of    TEXT NOT NULL,
    currency TEXT NOT NULL,
    rate     TEXT NOT NULL,               -- GBP per 1 unit of `currency`
    PRIMARY KEY (as_of, currency)
);

-- ---------------------------------------------------------------- the trail
-- Append-only. See the module docstring; the triggers below are load-bearing.
CREATE TABLE IF NOT EXISTS ideas (
    id              TEXT PRIMARY KEY,
    created_at      TEXT NOT NULL,
    as_of           TEXT NOT NULL,
    ticker          TEXT NOT NULL,
    idea_class      TEXT NOT NULL,
    conviction      TEXT NOT NULL,
    direction       TEXT NOT NULL,
    composite_score TEXT NOT NULL,
    accepted        INTEGER NOT NULL,
    inputs_digest   TEXT NOT NULL,
    payload         TEXT NOT NULL         -- full Idea JSON, the eval dataset
);
CREATE INDEX IF NOT EXISTS ix_ideas_ticker ON ideas(ticker, as_of);

CREATE TABLE IF NOT EXISTS audit (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    ts       TEXT NOT NULL,
    run_id   TEXT NOT NULL,
    event    TEXT NOT NULL,
    ticker   TEXT,
    payload  TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS ix_audit_event ON audit(event, ts);

CREATE TRIGGER IF NOT EXISTS audit_no_update
BEFORE UPDATE ON audit BEGIN
    SELECT RAISE(ABORT, 'audit trail is append-only');
END;
CREATE TRIGGER IF NOT EXISTS audit_no_delete
BEFORE DELETE ON audit BEGIN
    SELECT RAISE(ABORT, 'audit trail is append-only');
END;
CREATE TRIGGER IF NOT EXISTS ideas_no_update
BEFORE UPDATE ON ideas BEGIN
    SELECT RAISE(ABORT, 'ideas are immutable; supersede with a new idea');
END;
CREATE TRIGGER IF NOT EXISTS ideas_no_delete
BEFORE DELETE ON ideas BEGIN
    SELECT RAISE(ABORT, 'ideas are immutable; supersede with a new idea');
END;

-- ---------------------------------------------------------------- portfolio
CREATE TABLE IF NOT EXISTS positions (
    id              TEXT PRIMARY KEY,
    ticker          TEXT NOT NULL,
    idea_id         TEXT NOT NULL,
    idea_class      TEXT NOT NULL,
    sector          TEXT NOT NULL,
    opened_on       TEXT NOT NULL,
    shares          INTEGER NOT NULL,
    entry           TEXT NOT NULL,
    currency        TEXT NOT NULL,
    fx_rate_at_entry TEXT NOT NULL,
    stop            TEXT,
    invalidation    TEXT NOT NULL DEFAULT '',
    status          TEXT NOT NULL,
    closed_on       TEXT,
    exit_price      TEXT
);
CREATE INDEX IF NOT EXISTS ix_positions_status ON positions(status);

CREATE TABLE IF NOT EXISTS fills (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    position_id     TEXT NOT NULL,
    ticker          TEXT NOT NULL,
    date            TEXT NOT NULL,
    shares          INTEGER NOT NULL,
    price           TEXT NOT NULL,
    currency        TEXT NOT NULL,
    fx_rate         TEXT NOT NULL,
    commission_gbp  TEXT NOT NULL,
    stamp_duty_gbp  TEXT NOT NULL,
    slippage_gbp    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS equity_curve (
    date            TEXT PRIMARY KEY,
    nav_gbp         TEXT NOT NULL,
    cash_gbp        TEXT NOT NULL,
    high_water_gbp  TEXT NOT NULL
);

-- ---------------------------------------------------------------- operations
CREATE TABLE IF NOT EXISTS watchlist (
    ticker      TEXT PRIMARY KEY,
    added_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS quality_issues (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    as_of     TEXT NOT NULL,
    check_name TEXT NOT NULL,
    severity  TEXT NOT NULL,
    ticker    TEXT,
    detail    TEXT NOT NULL,
    logged_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS briefs (
    id           TEXT PRIMARY KEY,
    generated_at TEXT NOT NULL,
    as_of        TEXT NOT NULL,
    markdown     TEXT NOT NULL,
    payload      TEXT NOT NULL
);

-- One row per LLM call. This is what makes §5.2's "schema compliance rate"
-- a measurement rather than an impression.
CREATE TABLE IF NOT EXISTS llm_calls (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           TEXT NOT NULL,
    module       TEXT NOT NULL,
    model        TEXT NOT NULL,
    prompt_hash  TEXT NOT NULL,
    attempts     INTEGER NOT NULL,
    schema_ok    INTEGER NOT NULL,
    repaired     INTEGER NOT NULL DEFAULT 0,
    error        TEXT
);

CREATE TABLE IF NOT EXISTS notifications (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        TEXT NOT NULL,
    channel   TEXT NOT NULL,
    event     TEXT NOT NULL,
    subject   TEXT NOT NULL,
    delivered INTEGER NOT NULL,
    error     TEXT
);
"""


def connect(path: str | Path, *, create: bool = True) -> sqlite3.Connection:
    p = Path(path)
    if p.parent and str(p.parent) not in ("", "."):
        p.parent.mkdir(parents=True, exist_ok=True)
    if not create and not p.exists():
        raise FileNotFoundError(f"no sentinel database at {p}; run `sentinel init`")
    conn = sqlite3.connect(p, isolation_level=None, detect_types=0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def migrate(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.execute(
        "INSERT INTO schema_meta(key, value) VALUES('version', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (str(SCHEMA_VERSION),),
    )


def init_db(path: str | Path) -> sqlite3.Connection:
    conn = connect(path)
    migrate(conn)
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Explicit transaction. ``isolation_level=None`` means autocommit by
    default, so a multi-row ingest needs this or a half-written universe
    survives a crash."""
    conn.execute("BEGIN")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")
