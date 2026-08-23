"""Prove the hosted entry point actually boots, in an environment that mimics one.

Run this with an interpreter that has `requirements.txt` installed and the
`sentinel` package NOT installed — the Community Cloud condition. It is the only
check that can see a bug in `streamlit_app.py`'s child processes, because every
development machine has the package installed and an editable install makes it
importable whatever the environment says.

**Health is not enough, and neither is a running server.** Streamlit binds its
HTTP port and answers ``/_stcore/health`` long before it executes the app script:
the script runs when a CLIENT SESSION opens. A check that stops at health passes
against an app that would throw on its first visitor — which is exactly how the
first version of this check reported a pass while the deploy was broken. So this
opens the stream socket and sends a ``rerun_script`` BackMsg, which is what a
browser does, then waits for the side effect only a real run can produce: a
seeded database carrying the demo stamp.
"""

from __future__ import annotations

import argparse
import asyncio
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path


def _wait_for_health(port: int, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    url = f"http://127.0.0.1:{port}/_stcore/health"
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as response:
                if response.status == 200:
                    return
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(1)
    raise SystemExit(f"the server never answered {url}")


async def _run_one_session(port: int, timeout: float) -> None:
    import websockets                                   # a uvicorn dependency
    from streamlit.proto.BackMsg_pb2 import BackMsg

    url = f"ws://127.0.0.1:{port}/_stcore/stream"
    async with websockets.connect(url, open_timeout=timeout) as socket:
        message = BackMsg()
        message.rerun_script.is_auto_rerun = False
        await socket.send(message.SerializeToString())
        # Drain a few frames so the run has actually started before we return.
        for _ in range(3):
            try:
                await asyncio.wait_for(socket.recv(), timeout=timeout)
            except asyncio.TimeoutError:
                raise SystemExit("the app sent nothing back — the script never ran")


def _wait_for_stamp(db: Path, timeout: float) -> None:
    """Wait for the STAMP, not for the file.

    The seeder creates the database and then spends a couple of seconds filling
    it, so a check that returns the moment the path exists reads schema_meta
    before it has been written and reports "not stamped as demo data: None" for
    a run that was merely still going. Waiting on the value is waiting on the
    thing actually being asserted.
    """
    deadline = time.monotonic() + timeout
    last: object = None
    while time.monotonic() < deadline:
        if db.exists() and db.stat().st_size > 0:
            try:
                with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as conn:
                    row = conn.execute(
                        "SELECT value FROM schema_meta WHERE key = 'demo_data'").fetchone()
            except sqlite3.Error as exc:      # mid-write: no table yet, or locked
                last = exc
            else:
                if row and row[0] == "true":
                    return
                last = row
        time.sleep(1)
    raise SystemExit(
        f"{db} never carried a demo_data stamp within {timeout:.0f}s (last saw {last!r}) — "
        "the entry point did not complete a run"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=60.0)
    args = parser.parse_args()

    # If the package is importable, this check proves nothing about the deployed
    # environment: the bug it exists to catch is invisible when `import sentinel`
    # works regardless of PYTHONPATH.
    try:
        import sentinel  # noqa: F401
    except ImportError:
        pass
    else:
        raise SystemExit(
            "`sentinel` is importable here, so this check would be vacuous. "
            "Run it with a venv built from requirements.txt alone."
        )

    _wait_for_health(args.port, args.timeout)
    print("server healthy — opening a client session")
    asyncio.run(_run_one_session(args.port, args.timeout))
    _wait_for_stamp(args.db, args.timeout)

    print(f"hosted entry point booted, ran, and seeded {args.db} "
          f"({args.db.stat().st_size:,} bytes, stamped demo_data=true)")


if __name__ == "__main__":
    sys.exit(main())
