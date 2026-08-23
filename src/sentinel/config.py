"""Configuration: one TOML file for everything you would ever want to tune, and
``.env`` for the things that must never be committed.

The split is deliberate. Risk limits belong in a tracked file so a change to
them shows up in a diff and a code review; API keys belong nowhere near git.
"""

from __future__ import annotations

import os
import tomllib
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .money import dec

DEFAULT_CONFIG_NAME = "sentinel.toml"


class RiskLimits(BaseModel):
    """Phase 3's numbers. Changing one of these changes what the system will
    ever let you buy, so they live here rather than scattered as literals."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    max_single_position_pct: Decimal = Decimal("10")
    max_sector_pct: Decimal = Decimal("30")
    risk_per_trade_pct: Decimal = Decimal("1")
    swing_max_pct: Decimal = Decimal("25")
    drawdown_kill_pct: Decimal = Decimal("15")
    min_position_gbp: Decimal = Decimal("250")
    max_open_positions: int = 12

    _PCT_FIELDS = (
        "max_single_position_pct", "max_sector_pct", "risk_per_trade_pct",
        "swing_max_pct", "drawdown_kill_pct",
    )

    @field_validator(
        "max_single_position_pct", "max_sector_pct", "risk_per_trade_pct",
        "swing_max_pct", "drawdown_kill_pct", "min_position_gbp", mode="before",
    )
    @classmethod
    def _dec(cls, v: Any) -> Decimal:
        return dec(v)

    @field_validator(
        "max_single_position_pct", "max_sector_pct", "risk_per_trade_pct",
        "swing_max_pct", "drawdown_kill_pct",
    )
    @classmethod
    def _sane_pct(cls, v: Decimal) -> Decimal:
        if not (Decimal("0") < v <= Decimal("100")):
            raise ValueError(f"percentage limit must be in (0, 100], got {v}")
        return v

    @field_validator("max_open_positions")
    @classmethod
    def _positive_int(cls, v: int) -> int:
        if v < 1:
            raise ValueError("max_open_positions must be at least 1")
        return v


class DataConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    price_provider: str = "fixture"
    fundamentals_provider: str = "fixture"
    news_provider: str = "fixture"
    #: Beyond this, a brief carries a loud banner (spec §5.4 data-freshness SLA).
    staleness_hours: int = 24
    #: Below this many bars a technical score is refused rather than guessed.
    min_history_bars: int = 250
    news_lookback_days: int = 14


class LlmConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    model: str = "claude-sonnet-5"
    #: Judge runs on a different model than the author, so a memo is not marked
    #: by the same hand that wrote it.
    judge_model: str = "claude-opus-5"
    temperature: float = 0.0
    max_tokens: int = 2000
    #: Rule 3: retry-with-repair *once*, then fail loudly.
    repair_attempts: int = 1
    enabled: bool = True


class NotifyConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    email_to: str = ""
    email_from: str = ""
    ntfy_topic: str = ""
    ntfy_server: str = "https://ntfy.sh"
    #: Push when a held position reports earnings inside this window.
    earnings_warning_hours: int = 48


class PathsConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    db: Path = Path("data/sentinel.sqlite")
    briefs: Path = Path("data/briefs")
    fixtures: Path = Path("data/fixtures")


class Config(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    satellite_capital_gbp: Decimal = Decimal("10000")
    base_currency: str = "GBP"
    risk: RiskLimits = Field(default_factory=RiskLimits)
    data: DataConfig = Field(default_factory=DataConfig)
    llm: LlmConfig = Field(default_factory=LlmConfig)
    notify: NotifyConfig = Field(default_factory=NotifyConfig)
    paths: PathsConfig = Field(default_factory=PathsConfig)
    universes: Mapping[str, tuple[str, ...]] = Field(default_factory=dict)
    watchlist: tuple[str, ...] = ()
    sectors: Mapping[str, str] = Field(default_factory=dict)
    benchmarks: Mapping[str, str] = Field(
        default_factory=lambda: {
            "B1": "VWRP.LSE",
            "B2": "SPY.US",
            "B3": "CASH",
            "B4": "RANDOM",
        }
    )
    #: Where the config was loaded from, so `sentinel health` can say so.
    source_path: Path | None = None

    @field_validator("satellite_capital_gbp", mode="before")
    @classmethod
    def _dec(cls, v: Any) -> Decimal:
        return dec(v)

    @field_validator("satellite_capital_gbp")
    @classmethod
    def _positive(cls, v: Decimal) -> Decimal:
        if v <= 0:
            raise ValueError("satellite capital must be positive")
        return v

    def universe(self, name: str) -> tuple[str, ...]:
        try:
            return tuple(self.universes[name])
        except KeyError as exc:
            known = ", ".join(sorted(self.universes)) or "(none configured)"
            raise KeyError(f"unknown universe {name!r}; known: {known}") from exc

    def sector_of(self, ticker: str) -> str:
        """Sector lookup for the concentration limit.

        Falls back to ``"unknown"`` rather than raising — but note the risk layer
        treats every ``unknown`` ticker as *one shared bucket*, so an unmapped
        sector concentrates rather than escapes the limit. That is the safe
        direction, and it makes a missing mapping visible as a rejection.
        """
        return self.sectors.get(ticker.upper(), "unknown")


def _decimalise(node: Any) -> Any:
    """TOML gives floats; money and limits must be Decimal before pydantic sees
    them, or 0.1 arrives as 0.1000000000000000055."""
    if isinstance(node, dict):
        return {k: _decimalise(v) for k, v in node.items()}
    if isinstance(node, list):
        return [_decimalise(v) for v in node]
    if isinstance(node, float):
        return dec(node)
    return node


def load_config(path: str | Path | None = None) -> Config:
    """Load config from TOML, falling back to defaults when the file is absent.

    Environment overrides are limited on purpose: only ``SENTINEL_SATELLITE_GBP``
    and ``SENTINEL_DB``, because a risk limit silently overridden by an env var
    is exactly the kind of bypass §5.5 suspends real-money usage over.
    """
    load_dotenv_if_present()
    candidate = Path(path) if path else Path(os.environ.get("SENTINEL_CONFIG", DEFAULT_CONFIG_NAME))
    data: dict[str, Any] = {}
    if candidate.exists():
        data = _decimalise(tomllib.loads(candidate.read_text(encoding="utf-8")))
        data["source_path"] = candidate

    if (cap := os.environ.get("SENTINEL_SATELLITE_GBP")):
        data["satellite_capital_gbp"] = dec(cap)
    if (db := os.environ.get("SENTINEL_DB")):
        paths = dict(data.get("paths") or {})
        paths["db"] = Path(db)
        data["paths"] = paths
    return Config.model_validate(data)


def load_dotenv_if_present(path: str | Path = ".env") -> None:
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def api_key(name: str) -> str | None:
    """Vendor keys are read here and nowhere else, so `sentinel health` can
    report which vendors are dormant without every adapter re-reading os.environ."""
    return os.environ.get(name) or None


STARTER_CONFIG = """\
# Sentinel configuration.
#
# Research output, not financial advice.
#
# Satellite capital ONLY — the 10-20% of investable capital this system manages.
# The passive core in your ISA is deliberately outside its scope, and every
# position-sizing calculation below is a fraction of THIS number, not of your
# net worth.
satellite_capital_gbp = 10000

[risk]
# These are hard limits. Nothing — no signal, no conviction level, no LLM —
# can override them. Editing this block is the only way they change, which is
# why it lives in a tracked file.
max_single_position_pct = 10    # of satellite capital
max_sector_pct          = 30    # of satellite capital
risk_per_trade_pct      = 1     # loss taken if a stop fills exactly
swing_max_pct           = 25    # short-term ideas capped well below long-term
drawdown_kill_pct       = 15    # from high-water mark -> no new swing ideas
min_position_gbp        = 250   # below this, costs eat the edge
max_open_positions      = 12

[data]
# "fixture" runs the whole pipeline offline from generated series — no keys, no
# network. Swap to a real vendor once its key is in .env.
price_provider        = "fixture"   # fixture | eodhd
fundamentals_provider = "fixture"   # fixture | fmp | eodhd
news_provider         = "fixture"   # fixture | finnhub
staleness_hours       = 24
min_history_bars      = 250

[llm]
model         = "claude-sonnet-5"
judge_model   = "claude-opus-5"
temperature   = 0.0
enabled       = true

[notify]
# Push is for "act or review now" only. The daily brief goes by email.
email_to    = ""
ntfy_topic  = ""

[paths]
db     = "data/sentinel.sqlite"
briefs = "data/briefs"

[universes]
demo = ["DEMO1.LSE", "DEMO2.LSE", "DEMO3.US", "DEMO4.US"]

[sectors]
# Ticker -> sector, for the 30% concentration limit. An unmapped ticker falls
# into a shared "unknown" bucket, which concentrates rather than escapes.
"DEMO1.LSE" = "consumer"
"DEMO2.LSE" = "industrials"
"DEMO3.US"  = "technology"
"DEMO4.US"  = "healthcare"

[benchmarks]
B1 = "VWRP.LSE"   # Vanguard FTSE All-World — the "just index it" question
B2 = "SPY.US"     # S&P 500, GBP-adjusted
B3 = "CASH"       # money-market floor
B4 = "RANDOM"     # Monte Carlo random portfolios — skill vs luck
"""

ENV_EXAMPLE = """\
# Vendor keys. Every one is optional; an absent key makes its adapter dormant
# rather than fatal, so the fixture pipeline always runs.
ANTHROPIC_API_KEY=
EODHD_API_KEY=
FMP_API_KEY=
FINNHUB_API_KEY=

# Notifications
SENTINEL_SMTP_HOST=
SENTINEL_SMTP_PORT=587
SENTINEL_SMTP_USER=
SENTINEL_SMTP_PASSWORD=
RESEND_API_KEY=
"""
