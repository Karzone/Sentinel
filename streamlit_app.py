"""Entry point for Streamlit Community Cloud.

Community Cloud runs a script from the repository root, has no persistent disk
and no `.env`, so this file supplies the three things the deployed app needs and
the local CLI already provides: a database, a password, and the demo stamp.

**This deployment is demonstration-only, and enforced as such rather than
documented as such.** A hosted instance has no vendor keys and no way to ingest,
so the only database it can ever have is a fabricated one — and the failure that
matters is the reverse of the usual: not "the demo looks broken" but "fabricated
numbers get read as a track record". So the database is seeded here, from
`scripts/seed_demo.py`, on every cold start (~2.5s), and the demo stamp it
writes is asserted before the app is allowed to render. If a real database ever
appeared at this path, this script would refuse to serve it.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import streamlit as st

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

# Community Cloud exposes secrets through st.secrets, while the app reads
# os.environ — it has no idea it is on Streamlit and should keep it that way.
# Copying here is the whole adapter.
for key in ("SENTINEL_DASHBOARD_PASSWORD", "SENTINEL_DASHBOARD_THEME"):
    try:
        if key in st.secrets:
            os.environ[key] = str(st.secrets[key])
    except Exception:                      # no secrets.toml at all
        pass

# NOT loopback-local: SENTINEL_DASHBOARD_LOCAL is deliberately never set here, so
# auth.decide takes the fail-closed branch and a missing password refuses to
# serve rather than serving openly. See specs/sentinel.md.
os.environ.pop("SENTINEL_DASHBOARD_LOCAL", None)

DB = Path(os.environ.get("SENTINEL_DB") or "/tmp/sentinel-demo.sqlite")


def _is_demo(path: Path) -> bool:
    """Whether the database carries `scripts/seed_demo.py`'s fabrication stamp."""
    try:
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as conn:
            row = conn.execute(
                "SELECT value FROM schema_meta WHERE key = 'demo_data'"
            ).fetchone()
    except sqlite3.Error:
        return False
    return bool(row) and str(row[0]).lower() == "true"


def _seed() -> None:
    subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "seed_demo.py"), "--db", str(DB)],
        check=True, cwd=ROOT,
    )


if not DB.exists():
    with st.spinner("Fabricating a demonstration database…"):
        _seed()

if not _is_demo(DB):
    # Reached only if something put a real database at this path. Refusing is
    # the only safe move: a hosted deployment cannot verify who is looking.
    st.error(
        f"{DB} is not a demonstration database. This deployment serves fabricated "
        "data only and will not render anything else.",
        icon="🛑",
    )
    st.stop()

os.environ["SENTINEL_DB"] = str(DB)

from sentinel.dashboard.app import main  # noqa: E402  (after sys.path + env setup)

main()
