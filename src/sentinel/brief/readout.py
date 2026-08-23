"""The one-page readout: every dashboard surface as a single self-contained file.

The dashboard needs a server, a browser session and a password. This produces
one HTML file with the data baked in, which opens from disk, survives being
emailed, and needs nothing running. It is the artefact you look at when you do
not want to *operate* anything.

**It reads through `dashboard.queries`, deliberately.** Re-deriving a hit rate or
a drawdown here would create a second definition of every number, and the two
would disagree the first time one changed — which is exactly the class of bug the
evals tile had (a tile counting `len(calls)` beside a verdict counting only the
scoreable ones). One query layer, two renderers.

Provenance is read from the database rather than passed in: a page built from
`scripts/seed_demo.py` output cannot omit the fabrication warning, and a page
built from a real portfolio cannot wrongly carry it.
"""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
from decimal import Decimal
from pathlib import Path
from typing import Any

from ..config import Config
from ..dashboard import queries

TEMPLATE = Path(__file__).with_name("readout.html")

#: Daily series are thinned before they reach the page. At one point per session
#: the SVG path for four benchmarks over three years is most of the file size,
#: and every second point is visually identical at any width a screen has.
STRIDE = 2


def _thin(rows: list[dict], stride: int = STRIDE) -> list[dict]:
    if len(rows) <= 2:
        return rows
    kept = rows[::stride]
    if kept[-1] is not rows[-1]:
        kept.append(rows[-1])
    return kept


def _plain(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (dt.date, dt.datetime)):
        return value.isoformat()
    return str(value)


def collect(conn: sqlite3.Connection, config: Config, *, as_of: dt.date) -> dict:
    """Everything the page renders, in one dict."""
    snapshot = queries.portfolio_snapshot(conn, config)
    equity = queries.equity_frame(conn)
    benchmarks = queries.benchmark_frame(conn, config)
    calls = queries.catalyst_calls(conn)
    scoreable, abstained = queries.catalyst_call_counts(calls)
    accuracy = queries.direction_accuracy_frame(calls)
    materiality = queries.materiality_frame(calls)
    conviction = queries.conviction_frame(queries.conviction_outcomes(conn))

    by_series: dict[str, list[dict]] = {}
    for row in benchmarks.to_dict("records"):
        by_series.setdefault(str(row["series"]), []).append(row)
    for name in by_series:
        by_series[name].sort(key=lambda r: str(r["date"]))
        by_series[name] = _thin(by_series[name])

    # The headline the whole system exists to be honest about. Computed from the
    # indexed series, which is the only basis on which the four are comparable —
    # and the same basis the NAV tile prints, so the page cannot show two
    # different answers to "how has this done".
    verdict = [
        {"series": name,
         "pct": (rows[-1]["value"] / rows[0]["value"] - 1.0) * 100.0 if rows[0]["value"] else 0.0,
         "end": rows[-1]["value"]}
        for name, rows in by_series.items() if rows
    ]

    return {
        "generated": as_of.isoformat(),
        "demo": queries.is_demo_database(conn),
        "capital": float(config.satellite_capital_gbp),
        "risk": {k: float(v) if isinstance(v, (int, float, Decimal)) else v
                 for k, v in config.risk.model_dump().items()},
        "snapshot": {
            "nav": float(snapshot.nav), "cash": float(snapshot.cash),
            "invested": float(snapshot.invested), "drawdown": float(snapshot.drawdown),
            "high_water": float(snapshot.high_water), "positions": snapshot.open_positions,
        },
        "equity": _thin([{"date": str(r["date"]), "nav": float(r["nav"]),
                          "drawdown": float(r["drawdown"])}
                         for r in equity.to_dict("records")]),
        "benchmarks": [r for rows in by_series.values() for r in rows],
        "verdict": verdict,
        "positions": queries.positions_frame(conn).to_dict("records"),
        "sectors": queries.sector_frame(conn, config).to_dict("records"),
        "ideas": queries.ideas_frame(conn).head(40).to_dict("records"),
        "evals": {
            "scoreable": scoreable, "abstained": abstained,
            "accuracy": accuracy.to_dict("records"),
            "materiality": materiality.to_dict("records"),
            "materiality_verdict": str(materiality.attrs.get("verdict", "")),
            "conviction": conviction.to_dict("records"),
            "conviction_verdict": str(conviction.attrs.get("verdict", "")),
        },
        "severity": queries.severity_counts(conn),
        "freshness": queries.freshness_frame(conn).to_dict("records"),
    }


def render(payload: dict) -> str:
    """Bake the payload into the template.

    ``</script>`` inside the JSON would close the block early and drop the rest
    of the page into the document as text; the escape is not cosmetic.
    """
    data = json.dumps(payload, default=_plain, separators=(",", ":"))
    data = data.replace("</", "<\\/")
    return TEMPLATE.read_text(encoding="utf-8").replace("__DATA__", data)


def build(conn: sqlite3.Connection, config: Config, *, as_of: dt.date | None = None) -> str:
    return render(collect(conn, config, as_of=as_of or dt.date.today()))
