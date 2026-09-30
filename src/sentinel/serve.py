"""`sentinel serve` — the hosted, self-running mode.

Everything the README tells you to type by hand (`init`, edit `.env`, edit the
provider lines, `ingest`, `brief`, `weekly`, `dashboard`, `phone`) becomes the
container's job, so the operator's whole interaction is: set secrets once on the
host, then open a URL.

What it deliberately does NOT do, because each would be a silent bypass:

- **It never serves without a password.** The dashboard's own gate is fail-closed
  already; this refuses earlier and louder, before a port is opened.
- **It never edits a risk limit.** Bootstrap writes the *starter* file only when
  none exists; after that the only automated edits are (a) upgrading a vendor
  line from ``fixture`` to a real vendor once that vendor's key appears, never
  the reverse, and (b) the Settings page's closed field set (`settings.py`).
- **It never places an order.** A scheduled run is ingest + brief + (Sundays) the
  weekly review: research output only.

The pure decisions (which providers a set of keys implies, what is due right now,
what a run's exit codes mean) are module functions so the tests hold them without
starting a process or waiting for a clock.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .config import AutopilotConfig, Config, STARTER_CONFIG, _decimalise, load_config
from .logging_setup import get_logger

log = get_logger("serve")

STATE_FILE = "autopilot-state.json"

#: A full ingest + brief is ~2 + ~20 minutes for 25 tickers; allow for a slow vendor.
JOB_TIMEOUT_SECONDS = 3 * 60 * 60

#: How long the scheduler waits for someone else's job (the dashboard's Run
#: button, or a run that outlived a restart) before giving up for this tick.
LOCK_WAIT_SECONDS = 60 * 60


# ---------------------------------------------------------------- providers


def detect_providers(env: Mapping[str, str]) -> dict[str, str]:
    """The vendor lines a set of keys implies. Pure; ``fixture`` when no key."""
    has = lambda name: bool(env.get(name))  # noqa: E731
    fundamentals = [name for name, key in (("fmp", "FMP_API_KEY"), ("eodhd", "EODHD_API_KEY"))
                    if has(key)]
    return {
        "price_provider": "eodhd" if has("EODHD_API_KEY") else "fixture",
        "fundamentals_provider": ",".join(fundamentals) or "fixture",
        "news_provider": "finnhub" if has("FINNHUB_API_KEY") else "fixture",
    }


def upgrade_providers(text: str, env: Mapping[str, str]) -> tuple[str, list[str]]:
    """Move ``fixture`` provider lines to the real vendor a key now implies.

    One direction only. A line already set to anything other than ``fixture`` is
    the operator's decision and is never touched, and a removed key never
    downgrades a real line back to generated data — that would quietly turn a
    real brief into a fabricated one.
    """
    changes: list[str] = []
    wanted = detect_providers(env)
    for key, value in wanted.items():
        if value == "fixture":
            continue
        pattern = re.compile(rf'^(\s*{key}\s*=\s*)"fixture"', re.MULTILINE)
        if pattern.search(text):
            text = pattern.sub(rf'\g<1>"{value}"', text, count=1)
            changes.append(f"{key}: fixture -> {value}")
    return text, changes


# ---------------------------------------------------------------- bootstrap


@dataclass(slots=True)
class BootstrapReport:
    actions: list[str] = field(default_factory=list)


def bootstrap(home: Path, env: Mapping[str, str]) -> BootstrapReport:
    """Make ``home`` a runnable Sentinel directory. Idempotent; safe every boot."""
    from .storage import connect, migrate

    report = BootstrapReport()
    home.mkdir(parents=True, exist_ok=True)
    (home / "data").mkdir(exist_ok=True)

    toml_path = home / "sentinel.toml"
    if not toml_path.exists():
        text, _ = upgrade_providers(STARTER_CONFIG, env)
        toml_path.write_text(text, encoding="utf-8")
        report.actions.append("wrote sentinel.toml")
    else:
        current = toml_path.read_text(encoding="utf-8")
        text, changes = upgrade_providers(current, env)
        if changes:
            from .settings import write_atomic

            write_atomic(toml_path, text)
            report.actions += [f"upgraded {c}" for c in changes]

    config = load_config(toml_path)
    db_path = home / config.paths.db if not Path(config.paths.db).is_absolute() else Path(config.paths.db)
    existed = db_path.exists()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(db_path)
    migrate(conn)
    conn.close()
    if not existed:
        report.actions.append(f"created database {db_path}")
    return report


# ---------------------------------------------------------------- what to score


def scoring_tickers(config: Config, env: Mapping[str, str]) -> list[str]:
    """The universe the schedule scores, plus the watchlist, deduplicated in order.

    ``autopilot.universe`` wins; otherwise ``ai`` once a real price vendor is
    configured (``demo`` before that, so a keyless first boot still produces a
    working, honestly-labelled page). Watchlist names ride along on every run:
    they are the ones the owner said they care about.
    """
    name = config.autopilot.universe
    if not name:
        real = config.data.price_provider != "fixture"
        for candidate in (("ai", "demo") if real else ("demo",)):
            if candidate in config.universes:
                name = candidate
                break
    tickers = list(config.universes.get(name, ())) if name else []
    for extra in config.watchlist:
        if extra not in tickers:
            tickers.append(extra)
    return tickers


# ---------------------------------------------------------------- the clock


def local_now(config: AutopilotConfig, now_utc: dt.datetime) -> dt.datetime:
    try:
        zone = ZoneInfo(config.timezone)
    except (ZoneInfoNotFoundError, ValueError):
        log.warning("unknown timezone %r — falling back to UTC", config.timezone)
        zone = ZoneInfo("UTC")
    return now_utc.astimezone(zone)


def _at(clock: str) -> dt.time:
    hour, minute = clock.split(":")
    return dt.time(int(hour), int(minute))


def due(now_local: dt.datetime, state: Mapping[str, object], config: AutopilotConfig) -> str | None:
    """Which job, if any, should start right now. ``None`` means nothing.

    Catch-up rather than exact-minute matching: the container may be restarted or
    asleep at 07:00, and "the scheduler happened to be running in that minute" is
    not a property worth depending on. A job is due once its time has passed
    *today* and it has not yet been attempted today — the same semantics as
    systemd's ``Persistent=true``. Yesterday's missed run is not replayed.

    The first boot is the exception: with no history at all the daily job is due
    immediately, weekend or not, so the page is not empty until Monday.
    """
    if not config.enabled:
        return None
    today = now_local.date().isoformat()
    if "daily_attempted" not in state:
        return "daily"
    weekday = now_local.weekday()                  # Mon=0 ... Sun=6
    if weekday < 5 and now_local.time() >= _at(config.daily_at) \
            and state.get("daily_attempted") != today:
        return "daily"
    if weekday == 6 and now_local.time() >= _at(config.weekly_at) \
            and state.get("weekly_attempted") != today:
        return "weekly"
    return None


def load_state(home: Path) -> dict[str, object]:
    try:
        return json.loads((home / "data" / STATE_FILE).read_text())
    except (FileNotFoundError, ValueError):
        return {}


def save_state(home: Path, state: Mapping[str, object]) -> None:
    path = home / "data" / STATE_FILE
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(dict(state), indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


# ---------------------------------------------------------------- running jobs


@dataclass(frozen=True, slots=True)
class StepResult:
    name: str
    code: int | None       # None = timed out / unknowable


def classify(step: str, code: int | None) -> str:
    """``ok`` | ``degraded`` | ``failed`` — the runner's exit-code contract.

    Mirrors deploy/sentinel-daily.sh and deliberately does not flatten exit 2:
    a run that completed but had a ticker blocked by the quality layer is not a
    success, and not a reason to stop either.
    """
    if code is None:
        return "failed"
    if code == 0:
        return "ok"
    if code == 2:
        return "degraded"
    return "failed"


def _alert(message: str, *, argv_prefix: list[str]) -> None:
    try:
        subprocess.run([*argv_prefix, "notify", "failure", message],
                       check=False, timeout=120, capture_output=True)
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.warning("could not send the failure alert either: %s", exc)


def run_steps(
    kind: str, config: Config, home: Path, env: Mapping[str, str],
    *, argv_prefix: list[str] | None = None,
) -> list[StepResult]:
    """Run one scheduled job (``daily`` = ingest then brief; ``weekly``) to the end.

    Goes through ``dashboard.jobs`` so the scheduler and the dashboard's Run
    buttons share ONE lock: two writers on one SQLite file is the failure that
    lock exists for.
    """
    from .dashboard import jobs

    prefix = argv_prefix or [sys.executable, "-m", "sentinel"]
    db_path = home / config.paths.db if not Path(config.paths.db).is_absolute() else Path(config.paths.db)
    tickers = ",".join(scoring_tickers(config, env))

    if kind == "daily":
        plan = [
            ("ingest", ["--tickers", tickers, "--history", str(config.autopilot.history_days)]),
            ("brief", ["--tickers", tickers, "--send"]),
        ]
    elif kind == "weekly":
        plan = [("weekly", ["--send"])]
    else:
        raise ValueError(f"unknown job kind {kind!r}")

    results: list[StepResult] = []
    for name, extra in plan:
        if name in ("ingest", "brief") and not tickers:
            log.error("%s: nothing to score — no universe or watchlist", name)
            results.append(StepResult(name, 1))
            _alert(f"Scheduled {name} skipped: there is no universe or watchlist to score.",
                   argv_prefix=prefix)
            break
        job = _start_when_free(name, db_path, extra, prefix)
        code = jobs.wait(job, db_path=db_path, timeout=JOB_TIMEOUT_SECONDS) if job else None
        results.append(StepResult(name, code))
        verdict = classify(name, code)
        log.info("%s finished: exit %s (%s)", name, code, verdict)
        if verdict == "failed":
            _alert(f"Scheduled {name} failed (exit {code}). No new brief was produced.",
                   argv_prefix=prefix)
            break
        if name == "brief" and verdict == "degraded":
            _alert("Today's brief went out flagged as incomplete: a data-quality check "
                   "blocked at least one ticker.", argv_prefix=prefix)
    return results


def _start_when_free(name, db_path, extra, prefix, *, sleep=time.sleep, clock=time.monotonic):
    from .dashboard import jobs

    deadline = clock() + LOCK_WAIT_SECONDS
    while True:
        try:
            return jobs.start(name, db_path=db_path, extra_args=extra, argv_prefix=prefix)
        except jobs.JobRefused:
            if clock() >= deadline:
                log.error("%s: another job held the lock for %ds — skipping", name,
                          LOCK_WAIT_SECONDS)
                return None
            sleep(15)


def scheduler_tick(
    home: Path, env: Mapping[str, str], *, now_utc: dt.datetime | None = None,
    runner: Callable[..., list[StepResult]] = run_steps,
) -> str | None:
    """One look at the clock. Returns the job it ran, if any.

    Re-reads ``sentinel.toml`` every tick, so a change made on the Settings page
    takes effect on the next one with no restart. The attempt is recorded BEFORE
    the run: a job that crashes the process must not be retried in a loop every
    30 seconds forever — it is retried tomorrow, and the failure alert says so.
    """
    config = load_config(home / "sentinel.toml")
    now = local_now(config.autopilot, now_utc or dt.datetime.now(dt.UTC))
    state = load_state(home)
    kind = due(now, state, config.autopilot)
    if kind is None:
        return None

    today = now.date().isoformat()
    state["daily_attempted" if kind == "daily" else "weekly_attempted"] = today
    if kind == "daily":
        state.setdefault("weekly_attempted", "")
    state[f"{kind}_started_at"] = dt.datetime.now(dt.UTC).isoformat(timespec="seconds")
    save_state(home, state)

    log.info("scheduler: starting %s run (%s %s)", kind, today, config.autopilot.timezone)
    results = runner(kind, config, home, env)
    state[f"{kind}_result"] = {r.name: r.code for r in results}
    state[f"{kind}_finished_at"] = dt.datetime.now(dt.UTC).isoformat(timespec="seconds")
    save_state(home, state)
    return kind


def scheduler_loop(home: Path, env: Mapping[str, str], stop: threading.Event,
                   *, interval: float = 30.0) -> None:
    """Tick until told to stop. A failing tick is logged and retried next time:
    the scheduler thread dying silently is the failure this loop exists to
    prevent, because a dead scheduler looks exactly like a quiet market."""
    while not stop.is_set():
        try:
            scheduler_tick(home, env)
        except Exception:  # noqa: BLE001 - see docstring
            log.exception("scheduler tick failed")
        stop.wait(interval)


# ---------------------------------------------------------------- the supervisor


def password_problem(env: Mapping[str, str]) -> str | None:
    from .dashboard import auth
    from .phone import password_problem as check

    return check(env.get(auth.PASSWORD_ENV))


def dashboard_argv(port: int, theme: str = "light") -> list[str]:
    """Bind all interfaces: in a container the platform's proxy is not loopback.
    A non-loopback bind is exactly what makes the dashboard demand its password."""
    return [sys.executable, "-m", "sentinel", "dashboard", "--address", "0.0.0.0",
            "--port", str(port), "--theme", theme]


def serve(home: Path, port: int, env: Mapping[str, str] | None = None) -> int:
    """Run the dashboard and the scheduler until one of them should stop."""
    from . import phone as ph

    env = dict(os.environ if env is None else env)
    problem = password_problem(env)
    if problem:
        print(f"refusing to start: {problem}\n"
              "Set SENTINEL_DASHBOARD_PASSWORD in the host's secrets.", file=sys.stderr)
        return 1

    os.chdir(home := home.resolve())
    env["SENTINEL_HOME"] = str(home)
    report = bootstrap(home, env)
    for action in report.actions:
        log.info("bootstrap: %s", action)

    config = load_config(home / "sentinel.toml")
    log.info("providers: price=%s fundamentals=%s news=%s | universe=%s (%d tickers) | LLM key: %s",
             config.data.price_provider, config.data.fundamentals_provider,
             config.data.news_provider, config.autopilot.universe or "automatic",
             len(scoring_tickers(config, env)), "yes" if env.get("ANTHROPIC_API_KEY") else "NO")

    stop = threading.Event()
    threading.Thread(target=scheduler_loop, args=(home, env, stop),
                     name="sentinel-scheduler", daemon=True).start()

    dashboard = ph.spawn(dashboard_argv(port), log_file=None, env=env)
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    healthy = ph.wait_for_origin(port, is_dead=lambda: dashboard.poll() is not None)
    if healthy:
        log.info("ready: dashboard answering on :%d", port)
    else:
        log.error("the dashboard never became healthy")
        stop.set()
    exit_code = 0 if healthy else 1
    try:
        while not stop.is_set():
            if dashboard.poll() is not None:
                log.error("dashboard exited (%s) — stopping so the host restarts us",
                          dashboard.returncode)
                exit_code = 1
                stop.set()
            else:
                stop.wait(2)
    except KeyboardInterrupt:
        stop.set()
    finally:
        ph.stop(dashboard)
    return exit_code
