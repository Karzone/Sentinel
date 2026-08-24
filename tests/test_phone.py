"""`sentinel phone` — the pure decisions under the two-process supervisor.

The bash tunnel script's invariants, restated for the cross-platform command:
a password before anything serves, origin before tunnel, shared fate. What is
testable without spawning is tested here; the spawn/stop pair is exercised by
the fake-process supervisor tests at the bottom.
"""

from __future__ import annotations

import sys

from sentinel import phone

CLOUDFLARED_NOISE = """\
2026-08-24T07:00:01Z INF Thank you for trying Cloudflare Tunnel.
2026-08-24T07:00:02Z INF Requesting new quick Tunnel on trycloudflare.com...
+--------------------------------------------------------------------------------------------+
|  Your quick Tunnel has been created! Visit it at (it may take some time to be reachable):  |
|  https://random-words-here-1234.trycloudflare.com                                          |
+--------------------------------------------------------------------------------------------+
2026-08-24T07:00:03Z INF Registered tunnel connection
"""


class TestPasswordPolicy:
    def test_empty_and_whitespace_are_refused(self):
        assert phone.password_problem(None)
        assert phone.password_problem("")
        assert phone.password_problem("   ")

    def test_short_is_refused_with_the_reason(self):
        problem = phone.password_problem("abc123")
        assert problem and "public URL" in problem

    def test_a_real_password_passes(self):
        assert phone.password_problem("correct-horse-battery") is None


class TestTunnelUrlParsing:
    def test_the_url_is_found_inside_cloudflareds_ascii_box(self):
        assert (phone.parse_tunnel_url(CLOUDFLARED_NOISE)
                == "https://random-words-here-1234.trycloudflare.com")

    def test_no_url_yet_is_none_not_a_crash(self):
        assert phone.parse_tunnel_url("still connecting...") is None
        assert phone.parse_tunnel_url("") is None


class TestArgvConstruction:
    def test_the_dashboard_is_started_in_tunnel_mode(self):
        """--tunnel is load-bearing: without it the loopback bind counts as a
        local session and the dashboard would serve the PUBLIC URL with no
        password at all. This assertion is the whole reason phone mode exists
        as a command instead of a README paragraph."""
        argv = phone.dashboard_argv(8501, "light")
        assert "--tunnel" in argv
        assert argv[:3] == [sys.executable, "-m", "sentinel"]

    def test_the_tunnel_points_at_loopback_only(self):
        argv = phone.cloudflared_argv("/usr/bin/cloudflared", 8501)
        assert "--url" in argv
        assert argv[argv.index("--url") + 1] == "http://127.0.0.1:8501"
        assert "--no-autoupdate" in argv


class TestCloudflaredDiscovery:
    def test_an_explicit_path_that_does_not_exist_fails_now_not_later(self):
        """The bash script's lesson: a bad CLOUDFLARED_BIN that sails past a
        presence check means the dashboard starts and THEN the tunnel fails,
        leaving a served origin behind."""
        env = {"CLOUDFLARED_BIN": "/nonexistent/cloudflared", "PATH": ""}
        assert phone.find_cloudflared(env) is None

    def test_an_explicit_existing_path_wins(self, tmp_path):
        binary = tmp_path / "cloudflared"
        binary.touch()
        assert phone.find_cloudflared(
            {"CLOUDFLARED_BIN": str(binary), "PATH": ""}) == str(binary)

    def test_absent_everywhere_is_none(self):
        assert phone.find_cloudflared({"PATH": "/nonexistent"}) is None


class TestSharedFate:
    def test_both_alive_keeps_watching(self):
        assert phone.arbitrate(False, False) is None

    def test_a_dead_dashboard_is_named_as_the_failure(self):
        verdict = phone.arbitrate(True, False)
        assert verdict and verdict.failed == "dashboard"

    def test_a_dead_tunnel_is_named_as_the_failure(self):
        verdict = phone.arbitrate(False, True)
        assert verdict and verdict.failed == "tunnel"

    def test_both_dead_blames_the_origin_first(self):
        """The dashboard dying kills the tunnel by design; blaming the tunnel
        would send the operator to the wrong log."""
        verdict = phone.arbitrate(True, True)
        assert verdict and verdict.failed == "dashboard"


class TestOriginGate:
    """Health first, tunnel second — the ordering that can never publish a 502."""

    def test_waits_until_the_probe_answers(self):
        answers = iter([False, False, True])
        ticks = iter(range(100))
        assert phone.wait_for_origin(
            8501, probe=lambda _p: next(answers), clock=lambda: next(ticks),
            sleep=lambda _s: None,
        ) is True

    def test_a_dead_dashboard_stops_the_wait_immediately(self):
        """Waiting out the full timeout on a corpse is 90 silent seconds."""
        probes: list[int] = []
        ticks = iter(range(100))
        result = phone.wait_for_origin(
            8501, probe=lambda _p: probes.append(1) or False,
            is_dead=lambda: True, clock=lambda: next(ticks), sleep=lambda _s: None,
        )
        assert result is False
        assert probes == [], "probed a dashboard already known dead"

    def test_times_out_rather_than_hanging_forever(self):
        clock_values = iter([0, 50, 100, 150])
        assert phone.wait_for_origin(
            8501, timeout=90, probe=lambda _p: False,
            clock=lambda: next(clock_values), sleep=lambda _s: None,
        ) is False
