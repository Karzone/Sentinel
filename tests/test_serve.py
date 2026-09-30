"""`sentinel serve`: self-setup, the schedule, and the refusals.

The clock tests use real instants either side of a DST change rather than
mocking the timezone, because "the brief still lands at 07:00 local after the
clocks change" is the property being claimed.
"""

from __future__ import annotations

import datetime as dt
import json
import sys
import textwrap
import tomllib

import pytest

from sentinel import serve
from sentinel.config import AutopilotConfig, STARTER_CONFIG, Config, _decimalise, load_config
from sentinel.dashboard import jobs

UTC = dt.UTC
AP = AutopilotConfig()          # 07:00 weekdays / 18:00 Sundays, Europe/London


def at(iso: str) -> dt.datetime:
    return dt.datetime.fromisoformat(iso).replace(tzinfo=UTC)


# ------------------------------------------------------------------ providers


class TestProviders:
    @pytest.mark.parametrize("env, expected", [
        ({}, ("fixture", "fixture", "fixture")),
        ({"EODHD_API_KEY": "k"}, ("eodhd", "eodhd", "fixture")),
        ({"EODHD_API_KEY": "k", "FMP_API_KEY": "k"}, ("eodhd", "fmp,eodhd", "fixture")),
        ({"FMP_API_KEY": "k"}, ("fixture", "fmp", "fixture")),
        ({"FINNHUB_API_KEY": "k"}, ("fixture", "fixture", "finnhub")),
        ({"EODHD_API_KEY": ""}, ("fixture", "fixture", "fixture")),    # empty = absent
    ])
    def test_keys_imply_vendors(self, env, expected):
        got = serve.detect_providers(env)
        assert (got["price_provider"], got["fundamentals_provider"],
                got["news_provider"]) == expected

    def test_a_real_vendor_line_is_never_overwritten(self):
        text = STARTER_CONFIG.replace('price_provider        = "fixture"',
                                      'price_provider        = "eodhd"', 1)
        new, changes = serve.upgrade_providers(text, {"EODHD_API_KEY": "k"})
        assert "price_provider: fixture" not in " ".join(changes)
        assert new.count('price_provider        = "eodhd"') == 1

    def test_a_removed_key_never_downgrades_real_data_to_fixtures(self):
        """The dangerous direction: a lapsed key must not quietly turn a real
        brief into one built on generated prices."""
        text, _ = serve.upgrade_providers(STARTER_CONFIG, {"EODHD_API_KEY": "k"})
        again, changes = serve.upgrade_providers(text, {})
        assert again == text and changes == []


# ------------------------------------------------------------------ bootstrap


class TestBootstrap:
    def test_an_empty_directory_becomes_a_runnable_home(self, tmp_path):
        report = serve.bootstrap(tmp_path / "vol", {})
        assert "wrote sentinel.toml" in report.actions
        assert (tmp_path / "vol" / "sentinel.toml").exists()
        assert (tmp_path / "vol" / "data" / "sentinel.sqlite").exists()
        config = load_config(tmp_path / "vol" / "sentinel.toml")
        assert config.data.price_provider == "fixture"

    def test_first_boot_with_keys_writes_real_vendors(self, tmp_path):
        serve.bootstrap(tmp_path, {"EODHD_API_KEY": "k", "FINNHUB_API_KEY": "k"})
        config = load_config(tmp_path / "sentinel.toml")
        assert (config.data.price_provider, config.data.news_provider) == ("eodhd", "finnhub")

    def test_a_second_boot_changes_nothing(self, tmp_path):
        serve.bootstrap(tmp_path, {})
        before = (tmp_path / "sentinel.toml").read_text()
        assert serve.bootstrap(tmp_path, {}).actions == []
        assert (tmp_path / "sentinel.toml").read_text() == before

    def test_adding_a_key_later_upgrades_the_existing_file(self, tmp_path):
        serve.bootstrap(tmp_path, {})
        report = serve.bootstrap(tmp_path, {"EODHD_API_KEY": "k"})
        assert any("price_provider: fixture -> eodhd" in a for a in report.actions)
        assert load_config(tmp_path / "sentinel.toml").data.price_provider == "eodhd"

    def test_an_owner_edited_file_is_not_overwritten(self, tmp_path):
        serve.bootstrap(tmp_path, {})
        path = tmp_path / "sentinel.toml"
        path.write_text(path.read_text().replace("satellite_capital_gbp = 10000",
                                                 "satellite_capital_gbp = 25000"))
        serve.bootstrap(tmp_path, {"EODHD_API_KEY": "k"})
        assert load_config(path).satellite_capital_gbp == 25000


# ------------------------------------------------------------ what gets scored


class TestScoringTickers:
    def _config(self, **overrides) -> Config:
        data = _decimalise(tomllib.loads(STARTER_CONFIG))
        for key, value in overrides.items():
            section, _, name = key.partition("__")
            data.setdefault(section, {})[name] = value
        return Config.model_validate(data)

    def test_keyless_defaults_to_the_offline_demo_universe(self):
        assert serve.scoring_tickers(self._config(), {}) == list(
            self._config().universes["demo"])

    def test_a_real_price_vendor_defaults_to_the_ai_universe(self):
        config = self._config(data__price_provider="eodhd")
        assert serve.scoring_tickers(config, {}) == list(config.universes["ai"])

    def test_an_explicit_universe_wins(self):
        config = self._config(data__price_provider="eodhd", autopilot__universe="demo")
        assert serve.scoring_tickers(config, {}) == list(config.universes["demo"])

    def test_the_watchlist_rides_along_without_duplicates(self):
        config = self._config(data__price_provider="eodhd")
        config = config.model_copy(update={"watchlist": ("VWRP.LSE", "NVDA.US")})
        got = serve.scoring_tickers(config, {})
        assert got[-1] == "VWRP.LSE" and got.count("NVDA.US") == 1


# ------------------------------------------------------------------ the clock


class TestDue:
    def test_first_boot_runs_immediately_even_on_a_saturday(self):
        saturday = serve.local_now(AP, at("2026-10-03T03:00"))
        assert saturday.weekday() == 5
        assert serve.due(saturday, {}, AP) == "daily"

    def test_before_the_time_nothing_is_due(self):
        now = serve.local_now(AP, at("2026-10-05T05:59"))     # Mon 06:59 BST
        assert serve.due(now, {"daily_attempted": "2026-10-02"}, AP) is None

    def test_after_the_time_the_daily_job_is_due_once(self):
        now = serve.local_now(AP, at("2026-10-05T06:01"))     # Mon 07:01 BST
        state = {"daily_attempted": "2026-10-02"}
        assert serve.due(now, state, AP) == "daily"
        assert serve.due(now, {"daily_attempted": "2026-10-05"}, AP) is None

    def test_a_late_start_catches_up_within_the_day(self):
        """Container was down at 07:00 and restarted at 15:30."""
        now = serve.local_now(AP, at("2026-10-05T14:30"))
        assert serve.due(now, {"daily_attempted": "2026-10-02"}, AP) == "daily"

    def test_yesterdays_missed_run_is_not_replayed_on_the_weekend(self):
        sat = serve.local_now(AP, at("2026-10-03T10:00"))
        assert serve.due(sat, {"daily_attempted": "2026-09-30"}, AP) is None

    def test_sunday_runs_the_weekly_review_after_six(self):
        state = {"daily_attempted": "2026-10-02", "weekly_attempted": "2026-09-27"}
        early = serve.local_now(AP, at("2026-10-04T16:00"))    # Sun 17:00 BST
        late = serve.local_now(AP, at("2026-10-04T17:30"))     # Sun 18:30 BST
        assert serve.due(early, state, AP) is None
        assert serve.due(late, state, AP) == "weekly"
        assert serve.due(late, {**state, "weekly_attempted": "2026-10-04"}, AP) is None

    def test_disabled_means_never(self):
        off = AutopilotConfig(enabled=False)
        assert serve.due(serve.local_now(off, at("2026-10-05T12:00")), {}, off) is None

    def test_seven_oclock_stays_seven_oclock_local_across_the_clock_change(self):
        """06:00Z is 07:00 in summer (BST) but 06:00 in winter (GMT): the same UTC
        instant is due in one and not the other. A fixed-UTC cron would be wrong
        for half the year."""
        state = {"daily_attempted": "1999-01-01"}
        summer = serve.local_now(AP, at("2026-07-01T06:00"))
        winter = serve.local_now(AP, at("2026-12-01T06:00"))
        assert summer.hour == 7 and serve.due(summer, state, AP) == "daily"
        assert winter.hour == 6 and serve.due(winter, state, AP) is None

    def test_an_unknown_timezone_falls_back_to_utc_rather_than_crashing(self):
        bad = AutopilotConfig(timezone="Mars/Olympus")
        assert serve.local_now(bad, at("2026-10-05T07:00")).utcoffset() == dt.timedelta(0)


class TestClassify:
    @pytest.mark.parametrize("code, verdict", [
        (0, "ok"), (2, "degraded"), (1, "failed"), (137, "failed"), (None, "failed")])
    def test_exit_codes_match_the_shell_runner(self, code, verdict):
        assert serve.classify("brief", code) == verdict


# ------------------------------------------------------------- the tick


class TestSchedulerTick:
    def _home(self, tmp_path):
        serve.bootstrap(tmp_path, {})
        return tmp_path

    def test_it_records_the_attempt_before_running_so_a_crash_cannot_loop(self, tmp_path):
        home = self._home(tmp_path)

        def crashing(*_a, **_k):
            assert serve.load_state(home)["daily_attempted"]      # already on disk
            raise RuntimeError("boom")

        with pytest.raises(RuntimeError):
            serve.scheduler_tick(home, {}, now_utc=at("2026-10-05T12:00"), runner=crashing)
        # ...and the next tick the same day does not start another run.
        calls = []
        assert serve.scheduler_tick(home, {}, now_utc=at("2026-10-05T12:01"),
                                    runner=lambda *a, **k: calls.append(a) or []) is None
        assert calls == []

    def test_a_clean_run_records_its_result_and_is_not_repeated(self, tmp_path):
        home = self._home(tmp_path)
        ran = []

        def runner(kind, *_a, **_k):
            ran.append(kind)
            return [serve.StepResult("ingest", 0), serve.StepResult("brief", 0)]

        assert serve.scheduler_tick(home, {}, now_utc=at("2026-10-05T12:00"),
                                    runner=runner) == "daily"
        state = serve.load_state(home)
        assert state["daily_result"] == {"ingest": 0, "brief": 0}
        assert serve.scheduler_tick(home, {}, now_utc=at("2026-10-05T12:05"),
                                    runner=runner) is None
        assert ran == ["daily"]

    def test_a_settings_change_takes_effect_on_the_next_tick_without_a_restart(self, tmp_path):
        from sentinel import settings

        home = self._home(tmp_path)
        state = {"daily_attempted": "2026-10-02"}
        serve.save_state(home, state)
        runner = lambda *a, **k: []          # noqa: E731
        # 09:00 local is past the default 07:00 -> would run; push it to 23:00.
        settings.save(home / "sentinel.toml", daily_at="23:00")
        assert serve.scheduler_tick(home, {}, now_utc=at("2026-10-05T08:00"),
                                    runner=runner) is None
        settings.save(home / "sentinel.toml", daily_at="07:00")
        assert serve.scheduler_tick(home, {}, now_utc=at("2026-10-05T08:00"),
                                    runner=runner) == "daily"


class TestRunNow:
    """The dashboard's "Refresh the report now" — a request flag, not a write."""

    def test_a_request_runs_the_daily_job_outside_the_schedule_and_only_once(self, tmp_path):
        serve.bootstrap(tmp_path, {})
        # already ran today, and it is 03:00 — nothing would be due on the clock
        serve.save_state(tmp_path, {"daily_attempted": "2026-10-05", "weekly_attempted": ""})
        ran = []
        runner = lambda kind, *a, **k: ran.append(kind) or []          # noqa: E731
        now = at("2026-10-05T02:00")
        assert serve.scheduler_tick(tmp_path, {}, now_utc=now, runner=runner) is None
        assert serve.request_run(tmp_path) is True
        assert serve.scheduler_tick(tmp_path, {}, now_utc=now, runner=runner) == "daily"
        assert serve.scheduler_tick(tmp_path, {}, now_utc=now, runner=runner) is None
        assert ran == ["daily"]

    def test_it_works_with_the_schedule_switched_off(self, tmp_path):
        from sentinel import settings

        serve.bootstrap(tmp_path, {})
        settings.save(tmp_path / "sentinel.toml", autopilot_enabled=False)
        serve.request_run(tmp_path)
        assert serve.scheduler_tick(tmp_path, {}, now_utc=at("2026-10-03T03:00"),
                                    runner=lambda *a, **k: []) == "daily"

    def test_the_flag_is_consumed_before_the_run_so_a_crash_cannot_replay_it(self, tmp_path):
        serve.bootstrap(tmp_path, {})
        serve.request_run(tmp_path)

        def crashing(*_a, **_k):
            assert serve.run_requested_at(tmp_path) is None         # already gone
            raise RuntimeError("boom")

        with pytest.raises(RuntimeError):
            serve.scheduler_tick(tmp_path, {}, now_utc=at("2026-10-05T12:00"), runner=crashing)
        assert serve.run_requested_at(tmp_path) is None

    def test_a_second_request_while_one_is_pending_is_a_no_op(self, tmp_path):
        serve.bootstrap(tmp_path, {})
        assert serve.request_run(tmp_path) is True
        assert serve.request_run(tmp_path) is False


# --------------------------------------------- real subprocesses, one shared lock


FAKE = textwrap.dedent("""
    import json, os, sys
    # argv = [<command...>]; the harness passes the recording file and exit codes by env.
    record = os.environ["FAKE_RECORD"]
    with open(record, "a") as handle:
        handle.write(json.dumps(sys.argv[1:]) + "\\n")
    codes = json.loads(os.environ.get("FAKE_CODES", "{}"))
    sys.exit(codes.get(sys.argv[1], 0))
""")


@pytest.fixture()
def fake_cli(tmp_path, monkeypatch):
    script = tmp_path / "fake_sentinel.py"
    script.write_text(FAKE)
    record = tmp_path / "record.jsonl"
    monkeypatch.setenv("FAKE_RECORD", str(record))

    def calls() -> list[list[str]]:
        if not record.exists():
            return []
        return [json.loads(line) for line in record.read_text().splitlines()]

    return [sys.executable, str(script)], calls


class TestRunSteps:
    def _config(self, tmp_path) -> Config:
        serve.bootstrap(tmp_path, {})
        return load_config(tmp_path / "sentinel.toml")

    def test_daily_is_ingest_then_brief_with_the_same_tickers_and_send(self, tmp_path, fake_cli):
        prefix, calls = fake_cli
        config = self._config(tmp_path)
        results = serve.run_steps("daily", config, tmp_path, {}, argv_prefix=prefix)
        assert [(r.name, r.code) for r in results] == [("ingest", 0), ("brief", 0)]
        ingest, brief = calls()
        assert ingest[0] == "ingest" and brief[0] == "brief"
        assert "--send" in brief and "--send" not in ingest
        assert ingest[ingest.index("--tickers") + 1] == brief[brief.index("--tickers") + 1]
        assert not any(c[:2] == ["notify", "failure"] for c in calls())

    def test_a_failed_ingest_stops_the_run_and_raises_an_alert(self, tmp_path, fake_cli, monkeypatch):
        prefix, calls = fake_cli
        monkeypatch.setenv("FAKE_CODES", json.dumps({"ingest": 1}))
        results = serve.run_steps("daily", self._config(tmp_path), tmp_path, {}, argv_prefix=prefix)
        assert [(r.name, r.code) for r in results] == [("ingest", 1)]
        names = [c[0] for c in calls()]
        assert "brief" not in names                   # never a brief from a failed fetch
        assert ["notify", "failure"] == calls()[-1][:2]

    def test_ingest_exit_2_is_degraded_not_fatal_and_the_brief_still_runs(self, tmp_path, fake_cli, monkeypatch):
        prefix, calls = fake_cli
        monkeypatch.setenv("FAKE_CODES", json.dumps({"ingest": 2}))
        results = serve.run_steps("daily", self._config(tmp_path), tmp_path, {}, argv_prefix=prefix)
        assert [r.name for r in results] == ["ingest", "brief"]
        assert not any(c[:2] == ["notify", "failure"] for c in calls())

    def test_a_brief_flagged_incomplete_still_alerts(self, tmp_path, fake_cli, monkeypatch):
        prefix, calls = fake_cli
        monkeypatch.setenv("FAKE_CODES", json.dumps({"brief": 2}))
        serve.run_steps("daily", self._config(tmp_path), tmp_path, {}, argv_prefix=prefix)
        assert any(c[:2] == ["notify", "failure"] and "incomplete" in c[2] for c in calls())

    def test_weekly_sends_the_review(self, tmp_path, fake_cli):
        prefix, calls = fake_cli
        serve.run_steps("weekly", self._config(tmp_path), tmp_path, {}, argv_prefix=prefix)
        assert calls() == [["weekly", "--send"]]

    def test_nothing_to_score_is_loud_not_silent(self, tmp_path, fake_cli):
        prefix, calls = fake_cli
        empty = Config.model_validate({})
        results = serve.run_steps("daily", empty, tmp_path, {}, argv_prefix=prefix)
        assert results[0].code == 1
        assert [c[0] for c in calls()] == ["notify"]          # the alert, and no ingest

    def test_the_scheduler_waits_for_the_dashboards_job_instead_of_racing_it(self, tmp_path, fake_cli):
        """One lock for the Run buttons and the schedule: two writers on one
        SQLite file is what the lock exists to prevent."""
        prefix, calls = fake_cli
        config = self._config(tmp_path)
        db = tmp_path / config.paths.db
        blocker = jobs.start("ingest", db_path=db,
                             argv_prefix=[sys.executable, "-c", "import time; time.sleep(1.5)"])
        try:
            sleeps: list[float] = []
            started = serve._start_when_free(
                "weekly", db, [], prefix,
                sleep=lambda s: (sleeps.append(s), __import__("time").sleep(0.6)))
            assert started is not None and sleeps        # it had to wait at least once
            jobs.wait(started, db_path=db, timeout=10, poll=0.1)
        finally:
            jobs.wait(blocker, db_path=db, timeout=10, poll=0.1)


# ---------------------------------------------------------------- jobs.wait


class TestJobsWait:
    def test_it_returns_the_real_exit_code(self, tmp_path):
        db = tmp_path / "j.sqlite"
        db.touch()
        job = jobs.start("ingest", db_path=db,
                         argv_prefix=[sys.executable, "-c", "import sys; sys.exit(3)"])
        assert jobs.wait(job, db_path=db, timeout=10, poll=0.05) == 3
        assert jobs.running(db) is None                   # the lock was released

    def test_a_timeout_returns_none_not_a_fake_zero(self, tmp_path):
        db = tmp_path / "j.sqlite"
        db.touch()
        job = jobs.start("ingest", db_path=db,
                         argv_prefix=[sys.executable, "-c", "import time; time.sleep(5)"])
        try:
            ticks = iter(range(100))
            assert jobs.wait(job, db_path=db, timeout=2, poll=0,
                             sleep=lambda _s: None, clock=lambda: next(ticks)) is None
        finally:
            import os
            os.kill(job.pid, 9)
            jobs.wait(job, db_path=db, timeout=5, poll=0.05)


# ------------------------------------------------------------------- refusals


class TestRefusals:
    def test_no_password_means_no_bootstrap_no_port_nothing(self, tmp_path):
        home = tmp_path / "vol"
        assert serve.serve(home, 0, env={}) == 1
        assert not home.exists()

    def test_a_short_password_is_refused_as_on_the_phone_path(self, tmp_path):
        assert serve.serve(tmp_path / "v", 0, env={"SENTINEL_DASHBOARD_PASSWORD": "abc"}) == 1

    def test_the_dashboard_is_started_on_all_interfaces_so_its_own_gate_applies(self):
        argv = serve.dashboard_argv(8080)
        assert argv[argv.index("--address") + 1] == "0.0.0.0"
        assert "--tunnel" not in argv       # a non-loopback bind already demands the password

    def test_the_cli_command_is_registered(self):
        from typer.testing import CliRunner
        from sentinel.cli import app

        result = CliRunner().invoke(app, ["serve"], env={"SENTINEL_DASHBOARD_PASSWORD": ""})
        assert result.exit_code == 1
