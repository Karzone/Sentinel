"""The one narrow write path into ``sentinel.toml``, for the dashboard's Settings page.

The dashboard is read-only (CLAUDE.md rule 9) with two deliberate write seams:
recording a trade, and this. The difference from a generic config editor is that
**the set of editable fields is closed and lives in the signature of
``apply_changes``**: a watchlist, two notification addresses, and the schedule.
There is no parameter that reaches ``[risk]``, ``satellite_capital_gbp``, the
vendor providers, ``[sectors]`` or the LLM model, so a page cannot be talked
into editing them — the only way to change a risk limit is still to edit the
tracked file by hand.

Edits are text edits, not a parse-and-rewrite, because the starter file is
mostly comments explaining *why* each limit is what it is; round-tripping it
through a TOML writer would delete them. Every result is re-parsed and validated
as a full ``Config`` before anything touches disk, and the write is atomic, so a
bad edit leaves the old file in place rather than a half-written one.

Keys (vendor API keys, the dashboard password) are deliberately not editable
here. They live in the host's secret store; a secret typed into a web form is a
secret that has been in a browser.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import tomllib
from pathlib import Path
from typing import Iterable, Sequence

from .config import Config, _decimalise

MAX_WATCHLIST = 60

#: SYMBOL.EXCHANGE, the shape every vendor adapter here uses (NVDA.US, VWRP.LSE).
_TICKER = re.compile(r"^[A-Z0-9][A-Z0-9\-]{0,9}\.[A-Z]{2,4}$")
_SECTION = re.compile(r"^\[[A-Za-z0-9_.\"\-]+\]\s*(#.*)?$")
_NTFY_TOPIC = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")
_EMAIL = re.compile(r"^[^@\s,;<>\"']+@[^@\s,;<>\"']+\.[^@\s,;<>\"']+$")


class SettingsError(ValueError):
    """A rejected edit. The message is shown to the user as-is."""


# ------------------------------------------------------------------ validation


def parse_watchlist(raw: str | Iterable[str]) -> tuple[str, ...]:
    """Split on commas, semicolons and whitespace; upper-case; dedupe in order."""
    tokens = re.split(r"[,;\s]+", raw) if isinstance(raw, str) else list(raw)
    seen: dict[str, None] = {}
    bad: list[str] = []
    for token in tokens:
        token = token.strip().upper()
        if not token:
            continue
        if not _TICKER.match(token):
            bad.append(token)
        else:
            seen.setdefault(token)
    if bad:
        shown = ", ".join(bad[:5]) + (f" (+{len(bad) - 5} more)" if len(bad) > 5 else "")
        raise SettingsError(
            f"not a ticker in SYMBOL.EXCHANGE form (e.g. NVDA.US, VWRP.LSE): {shown}"
        )
    if len(seen) > MAX_WATCHLIST:
        raise SettingsError(
            f"{len(seen)} tickers; the limit is {MAX_WATCHLIST} — every ticker costs LLM "
            "calls on each run"
        )
    return tuple(seen)


def validate_ntfy_topic(topic: str) -> str:
    topic = topic.strip()
    if topic and not _NTFY_TOPIC.match(topic):
        raise SettingsError("ntfy topic may only contain letters, digits, '-' and '_'")
    if topic and len(topic) < 12:
        raise SettingsError(
            "ntfy topic is the only credential on the push channel — use at least 12 "
            "characters so it cannot be guessed"
        )
    return topic


def validate_email(address: str) -> str:
    address = address.strip()
    if address and not _EMAIL.match(address):
        raise SettingsError(f"{address!r} does not look like an email address")
    return address


# ---------------------------------------------------------------- text editing


def _toml_value(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return json.dumps(value)            # TOML basic strings accept JSON escapes
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_toml_value(v) for v in value) + "]"
    raise TypeError(f"unsupported TOML value {value!r}")


def _section_bounds(lines: Sequence[str], section: str | None) -> tuple[int, int] | None:
    """(first body line, end) of a section; None for a section that does not exist.

    Top-level (``section=None``) is everything before the first header. A header
    line is only ever at column 0 and never inside a multi-line array here, so a
    line-wise scan is exact for the files this module writes and the starter.
    """
    if section is None:
        for i, line in enumerate(lines):
            if _SECTION.match(line):
                return 0, i
        return 0, len(lines)
    header = f"[{section}]"
    start = None
    for i, line in enumerate(lines):
        if line.strip().split("#")[0].strip() == header:
            start = i + 1
            break
    if start is None:
        return None
    for j in range(start, len(lines)):
        if _SECTION.match(lines[j]):
            return start, j
    return start, len(lines)


def _key_span(lines: Sequence[str], start: int, end: int, key: str) -> tuple[int, int] | None:
    """Half-open line span of ``key = value``, following a multi-line array."""
    pattern = re.compile(rf"^\s*{re.escape(key)}\s*=")
    for i in range(start, end):
        if pattern.match(lines[i]):
            value = lines[i].split("=", 1)[1]
            depth = value.count("[") - value.count("]")
            j = i + 1
            while depth > 0 and j < end:
                depth += lines[j].count("[") - lines[j].count("]")
                j += 1
            return i, j
    return None


def set_key(text: str, section: str | None, key: str, value: object) -> str:
    """Set ``key`` in ``section`` (None = top level), preserving everything else."""
    # Already that value? Return the text untouched. Re-rendering would still
    # change whitespace ("daily_at  =" -> "daily_at =") and report an edit that
    # did not happen.
    try:
        node = tomllib.loads(text)
        for part in (section.split(".") if section else []):
            node = node[part]
        if key in node and node[key] == (list(value) if isinstance(value, tuple) else value):
            return text
    except (tomllib.TOMLDecodeError, KeyError, TypeError):
        pass

    lines = text.split("\n")
    rendered = f"{key} = {_toml_value(value)}"
    bounds = _section_bounds(lines, section)
    if bounds is None:
        tail = "" if text.endswith("\n") else "\n"
        return f"{text}{tail}\n[{section}]\n{rendered}\n"
    start, end = bounds
    span = _key_span(lines, start, end, key)
    if span is not None:
        i, j = span
        trailing = ""
        if j == i + 1 and "#" in lines[i]:
            # Keep an inline comment ("daily_at = ... # Mon-Fri") when the value
            # is on one line; a comment inside a multi-line array is dropped.
            before_hash, comment = lines[i].split("#", 1)
            if before_hash.count('"') % 2 == 0:   # the '#' is not inside a string
                trailing = "  #" + comment
        lines[i:j] = [rendered + trailing]
    elif section is None:
        lines.insert(end, rendered)
        if end < len(lines) - 1 and lines[end + 1].strip():
            lines.insert(end + 1, "")
    else:
        lines.insert(start, rendered)
    return "\n".join(lines)


def apply_changes(
    text: str,
    *,
    watchlist: Sequence[str] | None = None,
    ntfy_topic: str | None = None,
    email_to: str | None = None,
    autopilot_enabled: bool | None = None,
    daily_at: str | None = None,
    weekly_at: str | None = None,
    universe: str | None = None,
) -> str:
    """Return ``text`` with the requested edits, or raise ``SettingsError``.

    ``None`` means "leave alone". The result is validated as a whole ``Config``
    so an edit that would make the file unloadable never gets out of this
    function.
    """
    new = text
    if watchlist is not None:
        new = set_key(new, None, "watchlist", list(parse_watchlist(watchlist)))
    if ntfy_topic is not None:
        new = set_key(new, "notify", "ntfy_topic", validate_ntfy_topic(ntfy_topic))
    if email_to is not None:
        new = set_key(new, "notify", "email_to", validate_email(email_to))
    if autopilot_enabled is not None:
        new = set_key(new, "autopilot", "enabled", bool(autopilot_enabled))
    if daily_at is not None:
        new = set_key(new, "autopilot", "daily_at", daily_at)
    if weekly_at is not None:
        new = set_key(new, "autopilot", "weekly_at", weekly_at)
    if universe is not None:
        new = set_key(new, "autopilot", "universe", universe)

    try:
        config = Config.model_validate(_decimalise(tomllib.loads(new)))
    except (tomllib.TOMLDecodeError, ValueError) as exc:
        raise SettingsError(f"that change would make sentinel.toml invalid: {exc}") from exc
    if universe and universe not in config.universes:
        known = ", ".join(sorted(config.universes)) or "(none)"
        raise SettingsError(f"unknown universe {universe!r}; known: {known}")
    return new


# ------------------------------------------------------------------------ disk


def write_atomic(path: Path, text: str) -> None:
    """Write via a sibling temp file and ``os.replace``; keep one ``.bak``.

    A reader (the scheduler re-reads this file every tick) never sees a partial
    file, and the previous version survives one edit.
    """
    path = Path(path)
    if path.exists():
        path.with_suffix(path.suffix + ".bak").write_text(
            path.read_text(encoding="utf-8"), encoding="utf-8"
        )
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def save(path: Path, **changes: object) -> list[str]:
    """Apply ``changes`` to the file at ``path``; return human-readable diff lines."""
    path = Path(path)
    before = path.read_text(encoding="utf-8")
    after = apply_changes(before, **changes)  # type: ignore[arg-type]
    if after == before:
        return []
    write_atomic(path, after)
    summary = [f"{key} updated" for key, value in changes.items() if value is not None]
    _log(path.parent, summary)
    return summary


def _log(home: Path, summary: Sequence[str]) -> None:
    """An append-only record of who-changed-what-when. The database audit trail
    is append-only by trigger and the dashboard's connection is read-only, so a
    settings change is recorded beside the config instead."""
    stamp = dt.datetime.now(dt.UTC).isoformat(timespec="seconds")
    with open(home / "settings-changes.log", "a", encoding="utf-8") as handle:
        handle.write(f"{stamp}  {'; '.join(summary)}\n")
