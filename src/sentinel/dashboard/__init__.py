"""The read-only Streamlit dashboard (Phase 6).

Launch with ``sentinel dashboard``. It reads the same SQLite database the CLI
writes and never writes to it — see ``queries.read_only_connect``.
"""

from . import palette, queries

__all__ = ["palette", "queries"]
