"""Recording REAL positions by hand — `sentinel paper buy` / `paper sell`.

The dashboard's Portfolio and Risk pages were empty on a live database for a
structural reason: every number on them derives from the paper ledger, and the
only writer of that ledger was the automated pipeline. Someone who already
holds stock at a broker had no way to tell Sentinel, so the risk layer was
guarding an empty book while the real one carried the risk.

The design line these tests hold: recording reality is NEVER refused for
breaking a limit. A limit you are already past is exactly what the risk layer
exists to surface — so the truth goes in the book and the warning goes to the
screen, in that order.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from typer.testing import CliRunner

from sentinel.cli import app
from sentinel.storage import audit, db, repo

runner = CliRunner()


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """A real database file the CLI commands open by env var."""
    path = tmp_path / "paper.sqlite"
    monkeypatch.setenv("SENTINEL_DB", str(path))
    conn = db.connect(str(path))
    db.migrate(conn)
    conn.commit()
    conn.close()
    return path


def _open(path):
    return db.connect(str(path))


class TestBuyRecordsTheTruth:
    def test_the_position_is_persisted_and_read_back(self, env):
        result = runner.invoke(app, ["paper", "buy", "NVDA.US", "--shares", "10",
                                     "--price", "120.50", "--stop", "100",
                                     "--date", "2026-08-20"])
        assert result.exit_code == 0, result.output
        conn = _open(env)
        positions = repo.get_open_positions(conn)
        assert len(positions) == 1
        p = positions[0]
        assert (p.ticker, p.shares, p.entry, p.stop) == (
            "NVDA.US", 10, Decimal("120.50"), Decimal("100"))
        assert p.idea_id == "manual", "a hand-recorded fill must be distinguishable"
        assert p.opened_on == dt.date(2026, 8, 20)

    def test_cash_moves_by_the_cost_basis(self, env):
        runner.invoke(app, ["paper", "buy", "ABC.LSE", "--shares", "100",
                            "--price", "5", "--date", "2026-08-20"])
        conn = _open(env)
        curve = repo.get_equity_curve(conn)
        assert curve, "no equity point was written — the Portfolio page stays empty"
        _, nav, cash, _ = curve[-1]
        # GBP ticker at fx 1: £500 of cash became £500 of stock, NAV unchanged.
        assert nav - cash == Decimal("500")
        assert nav == Decimal("10000"), "NAV must not move at the fill itself"

    def test_the_audit_trail_marks_it_manual(self, env):
        runner.invoke(app, ["paper", "buy", "ABC.LSE", "--shares", "1", "--price", "10"])
        conn = _open(env)
        events = [e for e in audit.read(conn) if e["event"] == audit.AuditEvent.POSITION_OPENED]
        assert events and events[-1]["payload"]["manual"] is True

    def test_a_stop_above_the_entry_is_refused_as_a_typo(self, env):
        result = runner.invoke(app, ["paper", "buy", "ABC.LSE", "--shares", "10",
                                     "--price", "5", "--stop", "6"])
        assert result.exit_code != 0
        conn = _open(env)
        assert repo.get_open_positions(conn) == []

    def test_a_limit_breach_warns_but_records_anyway(self, env):
        """The one behaviour that must survive every refactor here."""
        # Default satellite capital is £10,000 and the single-position cap 10%,
        # so £5,000 in one name is flagrantly over.
        result = runner.invoke(app, ["paper", "buy", "ABC.LSE", "--shares", "1000",
                                     "--price", "5"])
        assert result.exit_code == 0, result.output
        assert "single-position limit" in result.output
        conn = _open(env)
        assert len(repo.get_open_positions(conn)) == 1, (
            "the warning became a refusal — the book no longer matches the broker")

    def test_a_missing_fx_rate_warns_and_records_at_parity(self, env):
        result = runner.invoke(app, ["paper", "buy", "NVDA.US", "--shares", "1",
                                     "--price", "100"])
        assert result.exit_code == 0
        assert "no USD/GBP rate" in result.output
        conn = _open(env)
        assert repo.get_open_positions(conn)[0].fx_rate_at_entry == Decimal("1")


class TestSellClosesWhatBuyOpened:
    def test_round_trip(self, env):
        runner.invoke(app, ["paper", "buy", "ABC.LSE", "--shares", "100",
                            "--price", "5", "--date", "2026-08-20"])
        result = runner.invoke(app, ["paper", "sell", "ABC.LSE", "--price", "6",
                                     "--date", "2026-08-22"])
        assert result.exit_code == 0, result.output
        assert "P&L £100.00" in result.output
        conn = _open(env)
        assert repo.get_open_positions(conn) == []
        closed = [p for p in repo.get_all_positions(conn) if not p.is_open]
        assert closed[0].exit_price == Decimal("6")
        assert closed[0].closed_on == dt.date(2026, 8, 22)

    def test_selling_what_you_do_not_hold_is_an_error(self, env):
        result = runner.invoke(app, ["paper", "sell", "ABC.LSE", "--price", "6"])
        assert result.exit_code != 0
        assert "no open position" in result.output

    def test_the_close_is_audited(self, env):
        runner.invoke(app, ["paper", "buy", "ABC.LSE", "--shares", "1", "--price", "5"])
        runner.invoke(app, ["paper", "sell", "ABC.LSE", "--price", "6"])
        conn = _open(env)
        events = [e for e in audit.read(conn) if e["event"] == audit.AuditEvent.POSITION_CLOSED]
        assert events and events[-1]["payload"]["manual"] is True


class TestTheSharedRecorder:
    """`portfolio.manual` — one implementation under both the CLI and the
    dashboard form, so the rules cannot drift apart."""

    def _conn(self, tmp_path):
        path = tmp_path / "m.sqlite"
        c = db.connect(str(path))
        db.migrate(c)
        return c

    def _config(self):
        from sentinel.config import Config
        return Config()

    def test_the_same_fill_twice_is_refused_and_cash_moves_once(self, tmp_path):
        """The double-submit case the web form makes likely: the position row
        upserts idempotently, but the cash deduction would not — so the second
        identical submission must be refused outright."""
        from sentinel.portfolio import manual

        conn, config = self._conn(tmp_path), self._config()
        kwargs = dict(ticker="ABC.LSE", shares=100, price=Decimal("5"),
                      opened_on=dt.date(2026, 8, 20))
        manual.record_buy(conn, config, **kwargs)
        with pytest.raises(manual.ManualEntryError, match="already recorded"):
            manual.record_buy(conn, config, **kwargs)

        _, nav, cash, _ = repo.get_equity_curve(conn)[-1]
        assert nav - cash == Decimal("500"), "cash was deducted twice"
        assert len(repo.get_all_positions(conn)) == 1

    def test_a_second_lot_on_another_date_is_a_new_position(self, tmp_path):
        from sentinel.portfolio import manual

        conn, config = self._conn(tmp_path), self._config()
        manual.record_buy(conn, config, ticker="ABC.LSE", shares=10,
                          price=Decimal("5"), opened_on=dt.date(2026, 8, 20))
        manual.record_buy(conn, config, ticker="ABC.LSE", shares=10,
                          price=Decimal("6"), opened_on=dt.date(2026, 8, 21))
        assert len(repo.get_open_positions(conn)) == 2

    def test_the_dashboard_gate_needs_local_and_real_together(self):
        from sentinel.portfolio import manual

        assert manual.allowed_in(dashboard_local=True, demo=False) is True
        # Hosted deploy: never local, whatever the database claims.
        assert manual.allowed_in(dashboard_local=False, demo=False) is False
        # A demo database stays fabricated end to end: one real fill written
        # into invented history would be the one real-looking number on a page
        # that promises there are none.
        assert manual.allowed_in(dashboard_local=True, demo=True) is False
        assert manual.allowed_in(dashboard_local=False, demo=True) is False

    def test_the_context_defaults_to_not_writable(self):
        """A view can only render the form if app.py decided so explicitly —
        a Context built anywhere else (tests, future pages) stays read-only."""
        from sentinel.dashboard.views import Context

        ctx = Context(conn=None, config=self._config(), mode="light")
        assert ctx.writable is False
