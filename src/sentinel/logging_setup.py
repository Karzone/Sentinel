"""Structured logging.

JSON lines to a file (machine-readable, greppable, survives a crash) and a
readable line to stderr. Every log record carries ``run_id`` so a brief's whole
pipeline can be pulled out of a week of logs with one grep.
"""

from __future__ import annotations

import json
import logging
import sys
import uuid
from pathlib import Path
from typing import Any

RUN_ID = uuid.uuid4().hex[:12]


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "run_id": getattr(record, "run_id", RUN_ID),
            "msg": record.getMessage(),
        }
        for key, value in getattr(record, "extra_fields", {}).items():
            payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure(level: int = logging.INFO, log_file: Path | None = None) -> None:
    root = logging.getLogger("sentinel")
    root.setLevel(level)
    root.handlers.clear()
    root.propagate = False

    stderr = logging.StreamHandler(sys.stderr)
    stderr.setFormatter(logging.Formatter("%(levelname)-7s %(name)s: %(message)s"))
    root.addHandler(stderr)

    if log_file:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(log_file, encoding="utf-8")
        handler.setFormatter(JsonFormatter())
        root.addHandler(handler)


def get_logger(name: str) -> logging.LoggerAdapter:
    logger = logging.getLogger(f"sentinel.{name}")
    return logging.LoggerAdapter(logger, {"run_id": RUN_ID})
