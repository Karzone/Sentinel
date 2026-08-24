"""`python -m sentinel` — how the dashboard's job runner invokes the CLI.

The console script (`sentinel`) only exists on PATH inside an activated venv;
the dashboard process knows its own interpreter, so `sys.executable -m
sentinel` works from anywhere the package is importable.
"""

from .cli import app

app()
