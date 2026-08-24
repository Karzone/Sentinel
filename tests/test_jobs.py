"""Dashboard-started jobs: the two failure modes a web button adds.

A terminal command inherits two properties for free that a button does not:
the process outlives nothing it shouldn't (the terminal IS the session), and
you cannot type the same command twice into one prompt at once. `jobs` has to
provide both explicitly — a detached process, and a pid lock.
"""

from __future__ import annotations

import json
import os
import sys

import pytest

from sentinel.dashboard import jobs


@pytest.fixture()
def db(tmp_path):
    path = tmp_path / "j.sqlite"
    path.touch()
    return path


def _fake_job(seconds: str = "30") -> list[str]:
    """An argv prefix that ignores the appended command name and just sleeps —
    the job itself is not under test, its lifecycle is."""
    return [sys.executable, "-c", "import sys, time; time.sleep(float(sys.argv[1]))", seconds]


class TestTheLock:
    def test_a_second_job_is_refused_while_the_first_runs(self, db):
        job = jobs.start("ingest", db_path=db, argv_prefix=_fake_job())
        try:
            with pytest.raises(jobs.JobRefused, match="one job at a time"):
                jobs.start("weekly", db_path=db, argv_prefix=_fake_job())
        finally:
            os.kill(job.pid, 9)

    def test_a_stale_lock_from_a_dead_pid_is_cleared(self, db):
        """The machine rebooted mid-run: the lock file survived, the process
        did not. The next look must clear it, or ingest is wedged forever."""
        jobs.lock_path(db).write_text(json.dumps(
            {"name": "ingest", "pid": 2 ** 22 + 12345, "started_at": "x", "log_path": ""}))
        assert jobs.running(db) is None
        assert not jobs.lock_path(db).exists()

    def test_a_garbled_lock_is_treated_as_absent(self, db):
        jobs.lock_path(db).write_text("{not json")
        assert jobs.running(db) is None

    def test_the_lock_reports_the_running_job(self, db):
        job = jobs.start("ingest", db_path=db, argv_prefix=_fake_job())
        try:
            current = jobs.running(db)
            assert current is not None
            assert (current.name, current.pid) == ("ingest", job.pid)
        finally:
            os.kill(job.pid, 9)

    def test_after_the_job_exits_the_lock_clears_on_next_look(self, db):
        job = jobs.start("ingest", db_path=db, argv_prefix=_fake_job("0.05"))
        import time
        for _ in range(100):
            if not jobs._pid_alive(job.pid):
                break
            time.sleep(0.05)
        assert jobs.running(db) is None
        assert not jobs.lock_path(db).exists()


class TestTheSurface:
    def test_only_allow_listed_names_run(self, db):
        """The page hands over a choice, never a command line."""
        with pytest.raises(jobs.JobRefused, match="unknown job"):
            jobs.start("rm -rf /", db_path=db, argv_prefix=_fake_job())
        with pytest.raises(jobs.JobRefused):
            jobs.start("ingest; weekly", db_path=db, argv_prefix=_fake_job())

    def test_a_failed_spawn_releases_the_lock(self, db):
        with pytest.raises(FileNotFoundError):
            jobs.start("ingest", db_path=db,
                       argv_prefix=["/nonexistent/interpreter"])
        assert not jobs.lock_path(db).exists(), (
            "a spawn failure left the lock behind — every later job is refused")

    def test_the_job_runs_detached_from_its_parent_session(self, db):
        """start_new_session is what lets the job outlive the dashboard."""
        job = jobs.start("ingest", db_path=db, argv_prefix=_fake_job())
        try:
            assert os.getsid(job.pid) != os.getsid(os.getpid())
        finally:
            os.kill(job.pid, 9)

    def test_output_lands_in_the_log(self, db):
        prefix = [sys.executable, "-c",
                  "import sys; print('hello from the job'); sys.argv"]
        jobs.start("ingest", db_path=db, argv_prefix=prefix)
        import time
        for _ in range(100):
            if "hello from the job" in jobs.tail(db):
                break
            time.sleep(0.05)
        assert "hello from the job" in jobs.tail(db)
        assert "=== ingest started" in jobs.tail(db)
