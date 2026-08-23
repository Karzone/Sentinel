"""The scheduled runner script.

The script's whole job is handling the ways a cron job fails *quietly*, so
those are exactly what gets tested: a dead pipeline must alert, a stale brief
must not be reported as success, and two runs must never touch the database at
once. Eyeballing a shell script does not establish any of that.

A fake `uv` on PATH stands in for the CLI, so these run offline and in
milliseconds without invoking the real pipeline.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "deploy" / "sentinel-daily.sh"


@pytest.fixture()
def rig(tmp_path):
    """A fake `uv` whose exit codes are scripted per subcommand."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls = tmp_path / "calls.log"
    fake = bin_dir / "uv"
    fake.write_text(
        "#!/usr/bin/env bash\n"
        f'echo "$@" >> "{calls}"\n'
        'case "$*" in\n'
        f'  *"sentinel ingest"*)  exit "${{FAKE_INGEST_RC:-0}}" ;;\n'
        f'  *"sentinel brief"*)   exit "${{FAKE_BRIEF_RC:-0}}" ;;\n'
        f'  *"notify failure"*)   exit "${{FAKE_ALERT_RC:-0}}" ;;\n'
        f'  *"sentinel readout"*) exit "${{FAKE_READOUT_RC:-0}}" ;;\n'
        "esac\n"
        "exit 0\n"
    )
    fake.chmod(0o755)

    home = tmp_path / "sentinel"
    home.mkdir()

    def run(**env_overrides):
        env = dict(os.environ)
        env.update({
            "PATH": f"{bin_dir}:{env['PATH']}",
            "SENTINEL_HOME": str(home),
            "SENTINEL_LOG_DIR": str(tmp_path / "logs"),
            "SENTINEL_LOCK": str(tmp_path / "lock"),
            "UV_BIN": str(fake),
        })
        env.update({k: str(v) for k, v in env_overrides.items()})
        # Absolute bash: one test blanks PATH, and a relative "bash" then
        # cannot be resolved by subprocess itself.
        proc = subprocess.run(["/bin/bash", str(SCRIPT)], env=env,
                              capture_output=True, text=True)
        proc.calls = calls.read_text() if calls.exists() else ""   # type: ignore[attr-defined]
        return proc

    run.calls_file = calls        # type: ignore[attr-defined]
    run.home = home               # type: ignore[attr-defined]
    run.lock = tmp_path / "lock"  # type: ignore[attr-defined]
    return run


class TestExitCodeRouting:
    def test_a_clean_run_exits_zero_and_raises_no_alert(self, rig):
        proc = rig()
        assert proc.returncode == 0
        assert "sentinel ingest" in proc.calls
        assert "sentinel brief" in proc.calls
        assert "notify failure" not in proc.calls

    def test_a_failed_ingest_alerts_and_never_reaches_the_brief(self, rig):
        """Briefing on top of a failed ingest would score yesterday's data and
        present it as today's."""
        proc = rig(FAKE_INGEST_RC=1)
        assert proc.returncode == 1
        assert "notify failure" in proc.calls
        assert "sentinel brief" not in proc.calls

    def test_a_blocked_ingest_still_proceeds_to_the_brief(self, rig):
        """Exit 2 means some tickers were blocked, not that the run is dead —
        the brief carries its own banner about it."""
        proc = rig(FAKE_INGEST_RC=2)
        assert "sentinel brief" in proc.calls

    def test_a_failed_brief_alerts_and_propagates_its_code(self, rig):
        proc = rig(FAKE_BRIEF_RC=1)
        assert proc.returncode == 1
        assert "notify failure" in proc.calls

    def test_a_stale_brief_is_not_flattened_into_success(self, rig):
        """The brief went out, so this is not a failure — but it is flagged
        incomplete, and reporting 0 would hide that from every monitor."""
        proc = rig(FAKE_BRIEF_RC=2)
        assert proc.returncode == 2
        assert "notify failure" in proc.calls

    def test_a_dead_alert_channel_does_not_mask_the_original_failure(self, rig):
        """If alerting fails too, the run must still report the fault it was
        trying to announce."""
        proc = rig(FAKE_BRIEF_RC=1, FAKE_ALERT_RC=1)
        assert proc.returncode == 1
        assert "could not send the failure alert" in (proc.stdout + proc.stderr)


class TestConcurrency:
    def test_it_refuses_to_run_while_another_run_holds_the_lock(self, rig):
        """Two processes writing one SQLite file is how the audit trail gets
        corrupted. Non-blocking on purpose: queueing behind yesterday's run
        would just produce two briefs at once."""
        import fcntl

        with open(rig.lock, "w") as held:                       # type: ignore[attr-defined]
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            proc = rig()
        assert proc.returncode == 0                              # not an error
        assert "sentinel ingest" not in proc.calls               # but it did nothing
        assert "another run holds" in proc.stdout


class TestEnvironment:
    def test_a_missing_uv_fails_loudly_rather_than_silently(self, rig, tmp_path):
        """HOME is redirected too: the script falls back to $HOME/.local/bin/uv,
        which on a developer machine exists and would rescue the run, hiding the
        very failure this asserts."""
        empty = tmp_path / "empty"
        empty.mkdir(exist_ok=True)
        proc = rig(UV_BIN="", PATH=str(empty), HOME=str(tmp_path))
        assert proc.returncode == 1
        assert "uv not found" in proc.stdout

    def test_it_repairs_a_near_empty_path_so_it_can_still_log(self, rig, tmp_path):
        """cron can supply a PATH without coreutils on it, and the script's own
        logging needs `date` and `tee` before it can report anything at all."""
        empty = tmp_path / "empty2"
        empty.mkdir(exist_ok=True)
        proc = rig(PATH=str(empty), HOME=str(tmp_path))
        assert "command not found" not in proc.stderr
        assert "=== ingest ===" in proc.stdout

    def test_a_missing_home_fails_before_touching_anything(self, rig, tmp_path):
        proc = rig(SENTINEL_HOME=str(tmp_path / "nope"))
        assert proc.returncode == 1
        assert "does not exist" in proc.stdout

    def test_the_universe_is_passed_through_when_set(self, rig):
        proc = rig(SENTINEL_UNIVERSE="uk-large")
        assert "--universe uk-large" in proc.calls

    def test_no_universe_flag_is_sent_when_unset(self, rig):
        """Otherwise the CLI receives `--universe ''` and fails on an empty name."""
        proc = rig()
        assert "--universe" not in proc.calls


class TestLogging:
    def test_it_writes_a_dated_log(self, rig, tmp_path):
        rig()
        logs = list((tmp_path / "logs").glob("daily-*.log"))
        assert len(logs) == 1
        assert "=== ingest ===" in logs[0].read_text()


class TestReadout:
    """The readout is a convenience view. It must be refreshed by the run, and
    it must never be able to fail the run."""

    def test_a_readout_is_written_after_the_brief(self, rig):
        proc = rig()
        assert "sentinel readout" in proc.calls
        assert proc.calls.index("sentinel brief") < proc.calls.index("sentinel readout")

    def test_a_failed_readout_does_not_fail_the_run_or_raise_an_alert(self, rig):
        """A push alert means "the pipeline is broken". An HTML file that could
        not be written is not that, and crying wolf costs the channel."""
        proc = rig(FAKE_READOUT_RC=1)
        assert proc.returncode == 0
        assert "notify failure" not in proc.calls
        assert "WARNING: the readout could not be written" in proc.stdout

    def test_it_writes_to_a_stable_path_so_a_bookmark_keeps_working(self, rig, tmp_path):
        target = tmp_path / "somewhere" / "readout.html"
        proc = rig(SENTINEL_READOUT=str(target))
        assert f"-o {target}" in proc.calls
