"""`sentinel phone` — the dashboard on your phone, one command.

Runs the two processes the bash tunnel script ran (Streamlit on loopback,
cloudflared republishing it at a public URL), but cross-platform, because the
operator's machine is Windows and a .sh file is not a phone mode there.

The invariants are the script's, restated once:

- **A password before anything serves.** The public URL is reachable by anyone
  who has it; the password is the only gate. Checked here, before a single
  process starts — and the dashboard is started with ``--tunnel``, so its own
  auth layer refuses to treat the loopback bind as "local, no password needed".
- **Origin before tunnel.** cloudflared happily serves 502s from a live URL
  forever, so the dashboard must answer on loopback before the tunnel opens.
- **Shared fate.** A tunnel that outlives its origin is a public 502; an
  origin that outlives its tunnel is an invisible server someone forgets is
  running. Either process dying takes the other down.

The pure decisions (password policy, URL parsing, argv construction, the
who-died-first arbitration) are module functions so the tests can hold them
without spawning anything.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

#: Anything shorter is a gesture, not a gate, on a URL anyone can reach.
MIN_PASSWORD_LEN = 8

#: Quick-tunnel URLs look like https://<words>.trycloudflare.com — cloudflared
#: prints one inside a box of punctuation, so parse, never split.
_URL = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")

INSTALL_HINTS = """cloudflared is not installed (or not on PATH).

  Windows:   winget install --id Cloudflare.cloudflared
  macOS:     brew install cloudflared
  Linux:     https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/

Then run `sentinel phone` again. (Or set CLOUDFLARED_BIN to its full path.)"""


def password_problem(password: str | None) -> str | None:
    """None means acceptable. The rule errs strict: this password fronts a
    public URL, and the difference between '' and 'abc' is cosmetic there."""
    if not password or not password.strip():
        return "a password is required — the URL is reachable by anyone who has it"
    if len(password) < MIN_PASSWORD_LEN:
        return (f"password is {len(password)} characters; phone mode requires at "
                f"least {MIN_PASSWORD_LEN} — it is the only gate on a public URL")
    return None


def find_cloudflared(env: dict[str, str] | None = None) -> str | None:
    env = env if env is not None else dict(os.environ)
    override = env.get("CLOUDFLARED_BIN")
    if override:
        # An explicit path that does not exist must fail here, not after the
        # dashboard is already serving (the bash script learned this one).
        return override if Path(override).exists() else None
    return shutil.which("cloudflared", path=env.get("PATH"))


def dashboard_argv(port: int, theme: str) -> list[str]:
    """--tunnel is the load-bearing flag: it stops the loopback bind counting
    as a local session, which is what makes the password mandatory."""
    return [sys.executable, "-m", "sentinel", "dashboard",
            "--port", str(port), "--tunnel", "--theme", theme]


def cloudflared_argv(binary: str, port: int) -> list[str]:
    return [binary, "tunnel", "--url", f"http://127.0.0.1:{port}",
            "--no-autoupdate"]


def parse_tunnel_url(text: str) -> str | None:
    match = _URL.search(text)
    return match.group(0) if match else None


@dataclass(slots=True)
class Verdict:
    """Who died, so the operator's error message names the right half."""
    failed: str | None      # "dashboard" | "tunnel" | None (operator stopped it)


def arbitrate(dashboard_dead: bool, tunnel_dead: bool) -> Verdict | None:
    """None = both alive, keep watching."""
    if dashboard_dead:
        return Verdict(failed="dashboard")
    if tunnel_dead:
        return Verdict(failed="tunnel")
    return None


def wait_for_origin(port: int, *, timeout: float = 90.0,
                    probe=None, is_dead=None, clock=time.monotonic,
                    sleep=time.sleep) -> bool:
    """True once the dashboard answers on loopback; False on timeout or death.

    Health first, tunnel second — the one ordering that can never publish a
    502. ``probe``/``is_dead`` are injectable for tests.
    """
    probe = probe or _health_probe
    deadline = clock() + timeout
    while clock() < deadline:
        if is_dead is not None and is_dead():
            return False
        if probe(port):
            return True
        sleep(0.5)
    return False


def _health_probe(port: int) -> bool:
    import urllib.error
    import urllib.request

    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/_stcore/health", timeout=2
        ) as response:
            return response.status == 200
    except (urllib.error.URLError, OSError):
        return False


def spawn(argv: list[str], *, log_file, env: dict[str, str]) -> subprocess.Popen:
    """One process, its output to one log, stoppable as a group.

    POSIX gets a new session (killpg reaches the grandchildren — `python -m
    sentinel dashboard` runs streamlit as a child of a child). Windows gets a
    new process group for the same reason: terminate() alone would stop the
    wrapper and leave Streamlit serving.
    """
    kwargs: dict[str, object] = {"stdout": log_file, "stderr": subprocess.STDOUT}
    if os.name == "posix":
        kwargs["start_new_session"] = True
    else:  # pragma: no cover - Windows path, exercised on the owner's machine
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    return subprocess.Popen(argv, env=env, **kwargs)


def stop(process: subprocess.Popen) -> None:
    """Best-effort group stop; escalates once; never raises."""
    if process.poll() is not None:
        return
    try:
        if os.name == "posix":
            import signal

            os.killpg(process.pid, signal.SIGTERM)
        else:  # pragma: no cover - Windows path
            process.terminate()
    except (ProcessLookupError, PermissionError, OSError):
        pass
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        try:
            if os.name == "posix":
                import signal

                os.killpg(process.pid, signal.SIGKILL)
            else:  # pragma: no cover
                process.kill()
        except (ProcessLookupError, PermissionError, OSError):
            pass
