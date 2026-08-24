"""Long-running CLI jobs, started from the dashboard.

Running `sentinel ingest` from a web page has two failure modes a terminal
never has, and this module exists to close them rather than hope:

- **The tab is not the process.** A job that runs inside the Streamlit script
  dies when the browser tab closes, half-done. So a job is a DETACHED
  subprocess (`start_new_session=True`): once started it belongs to the
  operating system, and closing the page, or the dashboard itself, does not
  touch it. Output goes to a log file next to the database.

- **A button can be clicked twice.** Two concurrent ingests contend for the
  SQLite write lock and interleave their audit trails. So starting takes a
  lock file (atomic `O_EXCL` create) holding the pid; while that pid is alive
  no second job starts. A lock whose pid is dead is stale — the machine
  rebooted mid-run — and is cleared on the next look.

Only names in ``COMMANDS`` can run: the page hands over a choice, never a
command line.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

#: The whole surface. A job is (argv suffix, human label); nothing else runs.
COMMANDS: dict[str, list[str]] = {
    "ingest": ["ingest"],
    "brief": ["brief"],     # the SCORING run: pipeline over the universe + today's brief
    "weekly": ["weekly"],   # the retrospective REVIEW: performance, evals, kill criteria
}


class JobRefused(RuntimeError):
    """Raised instead of starting a second concurrent job."""


@dataclass(frozen=True, slots=True)
class RunningJob:
    name: str
    pid: int
    started_at: str
    log_path: str

    @property
    def alive(self) -> bool:
        return _pid_alive(self.pid)


#: Popen handles for jobs this process started. Needed for liveness, not just
#: bookkeeping: an exited child is a ZOMBIE until its parent reaps it, and
#: `os.kill(pid, 0)` calls a zombie alive — without the poll() below, a
#: finished ingest would read as "running" until the dashboard restarted.
#: A lock written by a *previous* dashboard process has no handle here, and
#: needs none: when that parent died, init inherited and reaped the child, so
#: the plain pid probe is truthful again.
_handles: dict[int, subprocess.Popen] = {}


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    handle = _handles.get(pid)
    if handle is not None:
        if handle.poll() is None:
            return True
        _handles.pop(pid, None)
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def lock_path(db_path: str | Path) -> Path:
    return Path(str(db_path) + ".job-lock")


def log_path(db_path: str | Path) -> Path:
    return Path(str(db_path) + ".job-log")


def running(db_path: str | Path) -> RunningJob | None:
    """The job currently holding the lock, or None. Clears a stale lock."""
    lock = lock_path(db_path)
    try:
        payload = json.loads(lock.read_text())
    except (FileNotFoundError, ValueError):
        return None
    job = RunningJob(
        name=str(payload.get("name", "?")), pid=int(payload.get("pid", -1)),
        started_at=str(payload.get("started_at", "")),
        log_path=str(payload.get("log_path", "")),
    )
    if not job.alive:
        # The pid is gone: finished (it removes its own lock, so this is rare)
        # or died with the machine. Either way nothing is running.
        lock.unlink(missing_ok=True)
        return None
    return job


def start(name: str, *, db_path: str | Path, extra_args: list[str] | None = None,
          argv_prefix: list[str] | None = None) -> RunningJob:
    """Start one allow-listed job, detached, under the lock.

    ``argv_prefix`` exists for tests; real callers get
    ``[sys.executable, "-m", "sentinel"]`` — the dashboard's own interpreter,
    which is the one place the package is known to be importable.
    """
    if name not in COMMANDS:
        raise JobRefused(f"unknown job {name!r}; allowed: {', '.join(sorted(COMMANDS))}")
    current = running(db_path)
    if current is not None:
        raise JobRefused(
            f"{current.name} has been running since {current.started_at} "
            f"(pid {current.pid}) — one job at a time"
        )

    lock = lock_path(db_path)
    log = log_path(db_path)
    argv = (argv_prefix or [sys.executable, "-m", "sentinel"]) + COMMANDS[name] + (extra_args or [])
    started = dt.datetime.now(dt.UTC).isoformat(timespec="seconds")

    # O_EXCL: if two sessions race the button, exactly one creates the lock.
    fd = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    try:
        with open(log, "ab") as sink:
            sink.write(f"\n=== {name} started {started} ===\n".encode())
            sink.flush()
            detach: dict[str, object] = (
                {"start_new_session": True} if os.name == "posix"
                # Windows has no setsid; these flags are its detach.
                else {"creationflags": (subprocess.CREATE_NEW_PROCESS_GROUP
                                        | subprocess.DETACHED_PROCESS)}
            )
            process = subprocess.Popen(
                argv, stdout=sink, stderr=subprocess.STDOUT,
                env={**os.environ, "SENTINEL_DB": str(db_path)},
                **detach,  # survives the tab, and the dashboard
            )
        _handles[process.pid] = process
        job = RunningJob(name=name, pid=process.pid, started_at=started,
                         log_path=str(log))
        os.write(fd, json.dumps({
            "name": name, "pid": process.pid, "started_at": started,
            "log_path": str(log),
        }).encode())
    except BaseException:
        os.close(fd)
        lock.unlink(missing_ok=True)
        raise
    os.close(fd)

    # The lock must not outlive the job by more than a `running()` call, so the
    # job removes it itself on exit — via a tiny watcher would be more moving
    # parts than the stale-pid sweep in `running()` already gives us. Nothing
    # to do here: `running()` clears it the first time it looks after exit.
    return job


def tail(db_path: str | Path, *, lines: int = 40) -> str:
    try:
        text = log_path(db_path).read_text(errors="replace")
    except FileNotFoundError:
        return ""
    return "\n".join(text.splitlines()[-lines:])
