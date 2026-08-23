"""The hosted-deployment entry point and its requirements file.

Two failure modes are worth a test. A requirements.txt that has drifted from
pyproject.toml deploys code the suite was never green against, silently. And a
hosted instance rendering anything other than fabricated data would present
invented numbers as a track record to people who cannot check — the single most
damaging thing this repository can do.
"""

from __future__ import annotations

import re
import sqlite3
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ENTRY = ROOT / "streamlit_app.py"
REQUIREMENTS = ROOT / "requirements.txt"


def _pinned() -> dict[str, str]:
    pins: dict[str, str] = {}
    for line in REQUIREMENTS.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name, _, version = line.partition("==")
        pins[name.strip().lower().replace("_", "-")] = version.strip()
    return pins


def _declared() -> dict[str, str]:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text())
    project = data["project"]
    specs = list(project["dependencies"])
    specs += list(project["optional-dependencies"]["dashboard"])
    out: dict[str, str] = {}
    for spec in specs:
        match = re.match(r"^([A-Za-z0-9._-]+)\s*(.*)$", spec)
        assert match, spec
        out[match.group(1).lower().replace("_", "-")] = match.group(2).strip()
    return out


class TestRequirementsMirrorPyproject:
    def test_every_declared_dependency_is_pinned(self):
        missing = set(_declared()) - set(_pinned())
        assert not missing, (
            f"{sorted(missing)} are in pyproject.toml but not requirements.txt, so a "
            "Community Cloud deploy would be missing them"
        )

    def test_no_pin_is_invented(self):
        """A pin with no declaration is a dependency nothing else knows about."""
        extra = set(_pinned()) - set(_declared())
        assert not extra, f"{sorted(extra)} are pinned but not declared in pyproject.toml"

    def test_every_pin_satisfies_its_declared_floor(self):
        """`streamlit>=1.40` pinned to 1.39 would install, and break in ways the
        suite never sees because the suite runs the lockfile."""
        for name, constraint in _declared().items():
            floor = re.search(r">=\s*([0-9][0-9A-Za-z.]*)", constraint)
            if not floor:
                continue
            pinned = tuple(int(p) for p in re.findall(r"\d+", _pinned()[name])[:3])
            required = tuple(int(p) for p in re.findall(r"\d+", floor.group(1))[:3])
            assert pinned >= required, f"{name}: pinned {pinned} < declared floor {required}"


class TestHostedEntryPointServesOnlyFabricatedData:
    def test_it_refuses_a_database_without_the_demo_stamp(self, tmp_path):
        """A real database at the hosted path must stop the app, not render.
        A hosted deployment cannot verify who is looking at it."""
        real = tmp_path / "real.sqlite"
        with sqlite3.connect(real) as conn:
            conn.execute("CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT)")
            conn.execute("INSERT INTO schema_meta VALUES ('demo_data', 'false')")

        source = ENTRY.read_text()
        namespace = _run_entry_guard(source, real)
        assert namespace["stopped"], "a non-demo database was allowed through"
        assert not namespace["seeded"], "it must not overwrite an existing database"

    def test_it_accepts_the_seeder_s_own_output(self, tmp_path):
        db = tmp_path / "demo.sqlite"
        subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "seed_demo.py"),
             "--db", str(db), "--history", "60"],
            check=True, cwd=ROOT, capture_output=True,
        )
        namespace = _run_entry_guard(ENTRY.read_text(), db)
        assert not namespace["stopped"], "the seeder's own output was rejected"

    def test_it_never_marks_the_session_local(self):
        """SENTINEL_DASHBOARD_LOCAL is what lets the gate serve without a
        password. A hosted app setting it would be an open portfolio."""
        source = ENTRY.read_text()
        assert 'os.environ.pop("SENTINEL_DASHBOARD_LOCAL", None)' in source
        assert 'os.environ["SENTINEL_DASHBOARD_LOCAL"]' not in source


def _run_entry_guard(source: str, db: Path) -> dict:
    """Execute the entry point's guard in isolation.

    The file ends by importing and running the real app, which needs a live
    Streamlit session, so the guard is extracted and run against fakes instead
    of booting the whole thing.
    """
    guard = source.split("os.environ[\"SENTINEL_DB\"]")[0]
    guard = guard.replace("import streamlit as st", "")
    namespace: dict = {"stopped": False, "seeded": False}

    class _FakeSt:
        secrets: dict = {}

        def error(self, *_a, **_k) -> None:
            namespace["stopped"] = True

        def stop(self) -> None:
            namespace["stopped"] = True
            raise _Stop

        def spinner(self, *_a, **_k):
            return _Noop()

    class _Stop(Exception):
        pass

    class _Noop:
        def __enter__(self): return self
        def __exit__(self, *_a): return False

    namespace["st"] = _FakeSt()
    namespace["__file__"] = str(ENTRY)
    guard = guard.replace("DB = Path(os.environ.get(\"SENTINEL_DB\") or \"/tmp/sentinel-demo.sqlite\")",
                          f"DB = Path(r'{db}')")
    guard = guard.replace("    _seed()", "    namespace['seeded'] = True")
    namespace["namespace"] = namespace
    try:
        exec(compile(guard, "streamlit_app-guard", "exec"), namespace)   # noqa: S102
    except _Stop:
        pass
    return namespace
