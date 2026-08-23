"""The tunnel runner script.

A tunnel turns a private dashboard into a public URL, so the things worth
testing are the ways that goes wrong QUIETLY: starting with no password (the
portfolio is then open to anyone with the link), opening the public side before
the origin is up (visitors meet a 502), and leaving one half running when the
other dies. Reading the script establishes none of that.

Fakes for `uv`, `cloudflared` and `curl` stand in on PATH, so these run offline
in milliseconds and never open a real tunnel.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "deploy" / "sentinel-tunnel.sh"


@pytest.fixture()
def rig(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls = tmp_path / "calls.log"

    # `uv run ... sentinel dashboard` — a server, so it must not exit on its own.
    (bin_dir / "uv").write_text(
        "#!/usr/bin/env bash\n"
        f'echo "uv $@" >> "{calls}"\n'
        'if [ "${FAKE_DASHBOARD_DIES:-0}" = "1" ]; then exit 1; fi\n'
        "sleep ${FAKE_DASHBOARD_LIFETIME:-30}\n"
    )
    # cloudflared, likewise long-running.
    (bin_dir / "cloudflared").write_text(
        "#!/usr/bin/env bash\n"
        f'echo "cloudflared $@" >> "{calls}"\n'
        "sleep ${FAKE_TUNNEL_LIFETIME:-30}\n"
    )
    # curl stands in for the health probe.
    (bin_dir / "curl").write_text(
        "#!/usr/bin/env bash\n"
        'exit "${FAKE_HEALTH_RC:-0}"\n'
    )
    for name in ("uv", "cloudflared", "curl"):
        (bin_dir / name).chmod(0o755)

    home = tmp_path / "sentinel"
    home.mkdir()

    def run(timeout=25, **env_overrides):
        env = dict(os.environ)
        env.update({
            "PATH": f"{bin_dir}:{env['PATH']}",
            "SENTINEL_HOME": str(home),
            "SENTINEL_LOG_DIR": str(tmp_path / "logs"),
            "UV_BIN": str(bin_dir / "uv"),
            "CLOUDFLARED_BIN": str(bin_dir / "cloudflared"),
            "SENTINEL_DASHBOARD_PASSWORD": "s3cret",
            "SENTINEL_DASHBOARD_WAIT": "3",
            # Both processes exit quickly so the shared-fate loop terminates.
            "FAKE_DASHBOARD_LIFETIME": "2",
            "FAKE_TUNNEL_LIFETIME": "2",
        })
        env.update({k: str(v) for k, v in env_overrides.items()})
        proc = subprocess.run(["/bin/bash", str(SCRIPT)], env=env,
                              capture_output=True, text=True, timeout=timeout)
        proc.calls = calls.read_text() if calls.exists() else ""   # type: ignore[attr-defined]
        return proc

    return run


class TestTunnelRunner:
    def test_no_password_means_nothing_starts_at_all(self, rig):
        """The one that matters. Without this the public URL serves the
        portfolio to anyone who has it."""
        proc = rig(SENTINEL_DASHBOARD_PASSWORD="")
        assert proc.returncode == 1
        assert "SENTINEL_DASHBOARD_PASSWORD is not set" in proc.stdout
        assert "cloudflared" not in proc.calls
        assert "sentinel dashboard" not in proc.calls

    def test_the_dashboard_is_started_with_tunnel_not_bare(self, rig):
        """--tunnel is what stops the loopback bind counting as a local
        session. Dropping it re-opens the hole the password exists to close."""
        proc = rig()
        assert "--tunnel" in proc.calls
        assert "--address 127.0.0.1" in proc.calls

    def test_the_tunnel_opens_only_after_the_origin_is_healthy(self, rig):
        proc = rig()
        dashboard_at = proc.calls.index("uv ")
        tunnel_at = proc.calls.index("cloudflared ")
        assert dashboard_at < tunnel_at, "cloudflared must not front a dead origin"

    def test_an_origin_that_never_comes_up_opens_no_tunnel(self, rig):
        # The process stays ALIVE but never answers /_stcore/health, which is
        # the interesting case: a dead process is caught by a different branch.
        proc = rig(FAKE_HEALTH_RC=1, FAKE_DASHBOARD_LIFETIME=20)
        assert proc.returncode == 1
        assert "never became healthy" in proc.stdout
        assert "cloudflared" not in proc.calls

    def test_a_dashboard_that_dies_immediately_opens_no_tunnel(self, rig):
        proc = rig(FAKE_DASHBOARD_DIES=1, FAKE_HEALTH_RC=1)
        assert proc.returncode == 1
        assert "cloudflared" not in proc.calls

    def test_a_dead_tunnel_takes_the_dashboard_down_with_it(self, rig):
        proc = rig(FAKE_TUNNEL_LIFETIME=1, FAKE_DASHBOARD_LIFETIME=20)
        assert proc.returncode == 1
        assert "cloudflared exited" in proc.stdout

    def test_a_dead_dashboard_takes_the_tunnel_down_with_it(self, rig):
        """A tunnel outliving its origin is a public 502 that looks like an
        outage — and a restarted-but-unprotected origin would be worse."""
        proc = rig(FAKE_DASHBOARD_LIFETIME=1, FAKE_TUNNEL_LIFETIME=20)
        assert proc.returncode == 1
        assert "the dashboard exited" in proc.stdout

    def test_a_named_tunnel_is_used_when_one_is_configured(self, rig):
        proc = rig(CLOUDFLARE_TUNNEL_NAME="sentinel")
        assert "cloudflared tunnel run sentinel" in proc.calls

    def test_a_quick_tunnel_is_the_fallback(self, rig):
        proc = rig()
        assert "--url http://127.0.0.1:8501" in proc.calls

    def test_a_missing_cloudflared_fails_before_serving_anything(self, rig, tmp_path):
        """A path that is SET but not executable is the trap: it is non-empty,
        so a presence check passes it, the dashboard starts, and only the
        tunnel fails — leaving a served origin with no tunnel in front."""
        proc = rig(CLOUDFLARED_BIN=str(tmp_path / "nope"))
        assert proc.returncode == 1
        assert "cloudflared not found or not executable" in proc.stdout
        assert "sentinel dashboard" not in proc.calls

    def test_a_misconfigured_uv_path_is_caught_the_same_way(self, rig, tmp_path):
        proc = rig(UV_BIN=str(tmp_path / "nope"))
        assert proc.returncode == 1
        assert "uv not found or not executable" in proc.stdout
        assert "cloudflared" not in proc.calls

    def test_it_writes_a_dated_log(self, rig, tmp_path):
        rig()
        logs = list((tmp_path / "logs").glob("tunnel-*.log"))
        assert len(logs) == 1
        assert "dashboard healthy" in logs[0].read_text()
