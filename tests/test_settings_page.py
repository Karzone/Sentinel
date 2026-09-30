"""The Settings page, rendered for real with Streamlit's AppTest.

Asserts what a user would see and what lands on disk — not that the page loaded.
"""

from __future__ import annotations

import tomllib

import pytest
from streamlit.testing.v1 import AppTest

from sentinel import serve
from sentinel.config import load_config


def _script():
    import os
    from pathlib import Path

    from sentinel.config import load_config
    from sentinel.dashboard import views

    import streamlit as st

    config = load_config(Path(os.environ["SENTINEL_CONFIG"]))
    ctx = views.Context(conn=None, config=config, mode="light", db_path=None,
                        settings_writable=os.environ.get("T_WRITABLE") == "1")
    views.settings(st, ctx)


@pytest.fixture()
def home(tmp_path, monkeypatch):
    serve.bootstrap(tmp_path, {})
    monkeypatch.setenv("SENTINEL_CONFIG", str(tmp_path / "sentinel.toml"))
    monkeypatch.setenv("T_WRITABLE", "1")
    for name in ("ANTHROPIC_API_KEY", "EODHD_API_KEY", "FMP_API_KEY", "FINNHUB_API_KEY",
                 "RESEND_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    return tmp_path


def _app():
    return AppTest.from_function(_script, default_timeout=30).run()


def _button(at, label):
    return next(b for b in at.button if b.label == label)


def test_saving_a_watchlist_writes_the_file_and_the_page_shows_it_after(home):
    at = _app()
    assert not at.exception
    at.text_area[0].set_value("nvda.us\nVWRP.LSE")
    _button(at, "Save settings").click().run()
    assert not at.exception
    assert load_config(home / "sentinel.toml").watchlist == ("NVDA.US", "VWRP.LSE")
    # after the rerun the widget is hydrated from the file, not from session state
    assert "NVDA.US" in at.text_area[0].value and "VWRP.LSE" in at.text_area[0].value
    # ...and the user is TOLD it saved: the rerun must not swallow the confirmation
    assert any("Saved" in s.value and "watchlist" in s.value for s in at.success)


def test_a_bad_ticker_is_reported_and_nothing_is_written(home):
    before = (home / "sentinel.toml").read_text()
    at = _app()
    at.text_area[0].set_value("NVDA")
    _button(at, "Save settings").click().run()
    assert any("SYMBOL.EXCHANGE" in e.value for e in at.error)
    assert (home / "sentinel.toml").read_text() == before


def test_on_a_demo_database_the_form_is_disabled_and_says_why(home, monkeypatch):
    monkeypatch.setenv("T_WRITABLE", "0")
    at = _app()
    assert at.text_area[0].disabled and _button(at, "Save settings").disabled
    assert _button(at, "Refresh the report now").disabled
    assert any("demo database" in i.value for i in at.info)


def test_key_status_shows_presence_and_never_the_value(home, monkeypatch):
    monkeypatch.setenv("FINNHUB_API_KEY", "sk-very-secret-value")
    at = _app()
    page = " ".join(m.value for m in at.markdown)
    assert "Finnhub" in page and "set" in page
    assert "sk-very-secret-value" not in page


def test_saving_the_schedule_changes_what_the_scheduler_reads(home):
    at = _app()
    at.checkbox[0].set_value(False)
    _button(at, "Save settings").click().run()
    assert not at.exception
    assert tomllib.loads((home / "sentinel.toml").read_text())["autopilot"]["enabled"] is False
    assert serve.due(serve.local_now(load_config(home / "sentinel.toml").autopilot,
                                     __import__("datetime").datetime.now(__import__("datetime").UTC)),
                     {}, load_config(home / "sentinel.toml").autopilot) is None


def test_refresh_now_queues_a_request_the_scheduler_will_honour(home):
    at = _app()
    assert not _button(at, "Refresh the report now").disabled
    _button(at, "Refresh the report now").click().run()
    assert not at.exception
    assert serve.run_requested_at(home) is not None
    # once queued the button is disabled: no double-queueing from a double tap
    assert _button(at, "Refresh the report now").disabled
    assert any("queued" in i.value for i in at.info)
