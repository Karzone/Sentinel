"""The one-page HTML readout.

The tests worth having are about what the page CLAIMS, not that it rendered.
Three things must hold: the numbers are the query layer's (not re-derived here,
or the page and the dashboard drift apart), the provenance banner follows the
database rather than a flag someone remembers to pass, and the payload cannot
break out of its script block.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import sqlite3
from decimal import Decimal
from pathlib import Path

import pytest

from sentinel.brief import readout
from sentinel.dashboard import queries
from sentinel.domain import IdeaClass
from sentinel.portfolio import Ledger
from sentinel.storage import repo

AS_OF = dt.date(2026, 8, 23)


def _payload(html: str) -> dict:
    """Recover the baked payload, which is what every assertion here reads."""
    match = re.search(r"const D = (\{.*?\});\n", html, re.S)
    assert match, "no payload was baked into the page"
    return json.loads(match.group(1).replace("<\\/", "</"))


@pytest.fixture()
def populated(conn, config):
    from sentinel.data import ingest as ingest_mod

    ingest_mod.ingest(conn, config, ["DEMO1.LSE", "DEMO2.LSE"], history_days=400)
    ledger = Ledger(Decimal("10000"))
    bars = repo.get_bars(conn, "DEMO1.LSE")
    entry = bars[-40].adjusted_close
    ledger.open(ticker="DEMO1.LSE", idea_id="i-1", idea_class=IdeaClass.LONG_TERM,
                sector="consumer", shares=5, price=entry, date=bars[-40].date,
                stop=entry * Decimal("0.9"))
    for bar in bars[-40:]:
        point = ledger.mark_to_market(bar.date, {"DEMO1.LSE": bar.adjusted_close})
        repo.save_equity_point(conn, bar.date, point.nav_gbp, point.cash_gbp, point.high_water_gbp)
    for position in ledger.positions.values():
        repo.save_position(conn, position)
    return conn


class TestProvenanceFollowsTheDatabase:
    """A page that misreports where its numbers came from is worse than no page."""

    def test_a_real_database_does_not_claim_to_be_fabricated(self, populated, config):
        html = readout.build(populated, config, as_of=AS_OF)
        assert _payload(html)["demo"] is False
        # The warning is rendered from the flag, so the flag is the assertion —
        # but the template must not carry the claim unconditionally either.
        assert "Every number on this page is fabricated" not in html.split("const D =")[0]

    def test_a_seeded_database_is_flagged(self, populated, config):
        with sqlite3.connect(populated.execute("PRAGMA database_list").fetchone()[2]) as writer:
            writer.execute(
                "INSERT OR REPLACE INTO schema_meta (key, value) VALUES ('demo_data','true')")
        html = readout.build(populated, config, as_of=AS_OF)
        assert _payload(html)["demo"] is True


class TestNumbersComeFromTheQueryLayer:
    def test_the_snapshot_matches_the_dashboard_s_own(self, populated, config):
        """Not a re-derivation: the same call the dashboard makes."""
        expected = queries.portfolio_snapshot(populated, config)
        got = _payload(readout.build(populated, config, as_of=AS_OF))["snapshot"]
        assert got["nav"] == pytest.approx(float(expected.nav))
        assert got["drawdown"] == pytest.approx(float(expected.drawdown))
        assert got["positions"] == expected.open_positions

    def test_the_catalyst_count_is_the_scoreable_one(self, populated, config):
        """The evals-tile bug, in a second renderer. `len(calls)` here would
        reintroduce it on a page nobody would think to check."""
        calls = queries.catalyst_calls(populated)
        scoreable, abstained = queries.catalyst_call_counts(calls)
        evals = _payload(readout.build(populated, config, as_of=AS_OF))["evals"]
        assert evals["scoreable"] == scoreable
        assert evals["abstained"] == abstained

    def test_every_benchmark_series_reaches_the_page(self, populated, config):
        expected = set(queries.benchmark_frame(populated, config)["series"])
        got = {r["series"] for r in _payload(readout.build(populated, config, as_of=AS_OF))["benchmarks"]}
        assert got == expected

    def test_the_verdict_is_computed_on_one_basis_for_every_series(self, populated, config):
        """Sentinel and its benchmarks must be measured the same way, or 'behind'
        is an artefact of the arithmetic rather than a fact."""
        payload = _payload(readout.build(populated, config, as_of=AS_OF))
        series = {r["series"]: [] for r in payload["benchmarks"]}
        for row in payload["benchmarks"]:
            series[row["series"]].append(row["value"])
        for entry in payload["verdict"]:
            rows = series[entry["series"]]
            assert entry["pct"] == pytest.approx((rows[-1] / rows[0] - 1) * 100)
            assert entry["end"] == pytest.approx(rows[-1])


class TestThePayloadCannotEscapeItsScriptBlock:
    def test_a_closing_script_tag_in_the_data_is_neutralised(self):
        html = readout.render({"generated": "2026-08-23", "demo": False,
                               "note": "</script><h1>injected</h1>"})
        assert "</script><h1>injected" not in html
        assert "<\\/script>" in html

    def test_the_template_placeholder_is_always_consumed(self, populated, config):
        assert "__DATA__" not in readout.build(populated, config, as_of=AS_OF)


class TestThinning:
    def test_the_last_point_is_never_dropped(self):
        """The endpoint label is the number a reader takes away; thinning that
        away would silently report a stale figure as current."""
        rows = [{"i": i} for i in range(10)]
        assert readout._thin(rows, 3)[-1] is rows[-1]
        rows = [{"i": i} for i in range(9)]
        assert readout._thin(rows, 2)[-1] is rows[-1]

    def test_short_series_are_left_alone(self):
        rows = [{"i": 0}, {"i": 1}]
        assert readout._thin(rows, 5) == rows


class TestTheFileStandsAlone:
    def test_it_references_no_host_but_the_font_service(self, populated, config):
        """A readout that needs a CDN is not self-contained: it degrades the day
        someone opens it offline or the host disappears."""
        html = readout.build(populated, config, as_of=AS_OF)
        hosts = set(re.findall(r'https?://([a-zA-Z0-9.-]+)', html))
        allowed = {
            "fonts.googleapis.com",   # the stylesheet, with a real fallback stack behind it
            "fonts.gstatic.com",      # the faces that stylesheet pulls
            "www.w3.org",             # the SVG namespace URI — an identifier, never requested
        }
        assert hosts <= allowed, hosts

    def test_the_disclaimer_survives_into_the_markup(self, populated, config):
        html = readout.build(populated, config, as_of=AS_OF)
        assert "Research output, not financial advice" in html
