"""The domain objects. Every one of these is persisted and replayed, so treat
field names as a wire contract: renaming one invalidates archived audit rows.

Scoring convention, used identically by every module: **0-100, where 50 is
neutral**, higher is more attractive. Not -100..+100, because a signal's
*absence* and a signal's *negativity* are different things and a midpoint makes
that legible on a chart.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Any, Mapping, Sequence

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..money import GBP, dec
from .enums import (
    CatalystType,
    Conviction,
    Direction,
    IdeaClass,
    ModuleName,
    Outcome,
    PositionStatus,
    RiskCheckId,
    Severity,
    Wrapper,
)

NEUTRAL_SCORE = Decimal("50")


class Frozen(BaseModel):
    """Base for value objects. Frozen because audit rows must not be mutated
    after the fact — an idea is a photograph of a moment, not a working copy."""

    model_config = ConfigDict(frozen=True, extra="forbid", ser_json_timedelta="float")


# --------------------------------------------------------------------------
# Market data
# --------------------------------------------------------------------------


class Bar(Frozen):
    """One OHLCV bar. ``adjusted_close`` is the split- and dividend-adjusted
    close; ``close`` is the raw print. Keeping both is what lets the quality
    layer *verify* the adjustment instead of trusting the vendor."""

    ticker: str
    date: dt.date
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    adjusted_close: Decimal
    volume: int
    currency: str = GBP

    @field_validator("open", "high", "low", "close", "adjusted_close", mode="before")
    @classmethod
    def _to_decimal(cls, v: Any) -> Decimal:
        return dec(v)

    @model_validator(mode="after")
    def _ohlc_coherent(self) -> "Bar":
        if self.high < self.low:
            raise ValueError(f"{self.ticker} {self.date}: high {self.high} < low {self.low}")
        if not (self.low <= self.open <= self.high):
            raise ValueError(f"{self.ticker} {self.date}: open {self.open} outside [{self.low}, {self.high}]")
        if not (self.low <= self.close <= self.high):
            raise ValueError(f"{self.ticker} {self.date}: close {self.close} outside [{self.low}, {self.high}]")
        if self.volume < 0:
            raise ValueError(f"{self.ticker} {self.date}: negative volume")
        return self


class Fundamentals(Frozen):
    """A point-in-time fundamentals snapshot.

    ``as_of`` is the date the *filing* became public, not the date we fetched
    it — backtests that use the fetch date leak the future.
    """

    ticker: str
    as_of: dt.date
    currency: str = GBP
    #: The company's display name ("Arista Networks"). None on rows ingested
    #: before this field existed; every surface falls back to the ticker.
    company_name: str | None = None
    sector: str | None = None
    market_cap: Decimal | None = None
    revenue_ttm: Decimal | None = None
    revenue_prior_ttm: Decimal | None = None
    eps_ttm: Decimal | None = None
    eps_prior_ttm: Decimal | None = None
    gross_margin: Decimal | None = None          # fraction, 0.42 == 42%
    operating_margin: Decimal | None = None
    net_margin: Decimal | None = None
    free_cash_flow_ttm: Decimal | None = None
    operating_cash_flow_ttm: Decimal | None = None
    total_debt: Decimal | None = None
    total_debt_prior: Decimal | None = None
    gross_margin_prior: Decimal | None = None
    total_equity: Decimal | None = None
    total_assets: Decimal | None = None
    total_assets_prior: Decimal | None = None
    current_assets: Decimal | None = None
    current_liabilities: Decimal | None = None
    current_assets_prior: Decimal | None = None
    current_liabilities_prior: Decimal | None = None
    shares_outstanding: Decimal | None = None
    shares_outstanding_prior: Decimal | None = None
    net_income_ttm: Decimal | None = None
    net_income_prior_ttm: Decimal | None = None
    pe_ratio: Decimal | None = None
    pe_5y_median: Decimal | None = None
    pe_sector_median: Decimal | None = None
    ev_ebitda: Decimal | None = None
    ev_ebitda_sector_median: Decimal | None = None
    next_earnings_date: dt.date | None = None
    wrapper: Wrapper = Wrapper.UNKNOWN


class NewsItem(Frozen):
    ticker: str
    published_at: dt.datetime
    headline: str
    summary: str = ""
    source: str = ""
    url: str = ""

    @property
    def digest_text(self) -> str:
        return f"{self.headline}. {self.summary}".strip()


# --------------------------------------------------------------------------
# Module outputs
# --------------------------------------------------------------------------


class Evidence(Frozen):
    """One traceable fact behind a score.

    The synthesis judge scores 'evidence grounding: does every claim trace to a
    module output?' — this is the thing it traces *to*. A claim with no matching
    Evidence is a hallucination by definition.
    """

    key: str                 # e.g. "revenue_growth_yoy"
    value: str               # rendered for the prompt and the memo footnote
    source: str              # module name, or vendor for raw facts
    weight: Decimal = Decimal("0")   # points this fact contributed to the score


class Signal(Frozen):
    """A scored, structured module output with its evidence attached."""

    module: ModuleName
    module_version: str
    ticker: str
    as_of: dt.date
    score: Decimal = Field(ge=0, le=100)
    confidence: Decimal = Field(default=Decimal("1"), ge=0, le=1)
    evidence: tuple[Evidence, ...] = ()
    notes: str = ""

    @field_validator("score", "confidence", mode="before")
    @classmethod
    def _to_decimal(cls, v: Any) -> Decimal:
        return dec(v)

    @property
    def is_neutral(self) -> bool:
        return self.score == NEUTRAL_SCORE


class CatalystRead(Frozen):
    """News/catalyst LLM output. Strict schema — see llm/schemas.py."""

    ticker: str
    as_of: dt.date
    catalyst_type: CatalystType
    direction: Direction
    materiality: int = Field(ge=1, le=5)
    horizon_days: int = Field(ge=1, le=365)
    summary: str
    headline_refs: tuple[str, ...] = ()


class SentimentRead(Frozen):
    """Sentiment LLM output.

    ``sentiment`` is -2..+2 as specified. This is a *contrarian or confirming*
    input only — analysis/rules.py enforces that it can never be a primary buy
    reason, which is why there is no `direction` field here to be tempted by.
    """

    ticker: str
    as_of: dt.date
    sentiment: int = Field(ge=-2, le=2)
    conviction: Decimal = Field(ge=0, le=1)
    herding_risk: bool
    rationale: str
    sample_size: int = Field(ge=0)


class IdeaMemo(Frozen):
    """Synthesis LLM output — the prose half of an Idea. The rules layer vets
    this before it is allowed anywhere near a brief."""

    ticker: str
    thesis: str
    bull_case: str
    bear_case: str
    invalidation: str
    idea_class: IdeaClass
    conviction: Conviction
    horizon_days: int = Field(ge=1)
    claims: tuple[str, ...] = ()   # each must map to an Evidence key


# --------------------------------------------------------------------------
# Risk
# --------------------------------------------------------------------------


class RiskCheckResult(Frozen):
    check: RiskCheckId
    outcome: Outcome
    reason: str
    detail: Mapping[str, str] = Field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return self.outcome is Outcome.PASS


class PositionPlan(Frozen):
    """What the risk layer says you may buy, in GBP, if anything at all."""

    ticker: str
    entry: Decimal                  # in the instrument's own currency
    currency: str = GBP
    stop: Decimal | None = None
    shares: int = 0
    gbp_exposure: Decimal = Decimal("0")
    gbp_risk: Decimal = Decimal("0")      # loss if the stop fills exactly
    fraction_of_satellite: Decimal = Decimal("0")
    fx_rate_used: Decimal = Decimal("1")


class RiskVerdict(Frozen):
    approved: bool
    checks: tuple[RiskCheckResult, ...]
    plan: PositionPlan | None = None

    @property
    def failures(self) -> tuple[RiskCheckResult, ...]:
        return tuple(c for c in self.checks if not c.passed)

    @property
    def failure_reasons(self) -> tuple[str, ...]:
        return tuple(f"{c.check.value}: {c.reason}" for c in self.failures)


# --------------------------------------------------------------------------
# Ideas, positions, briefs
# --------------------------------------------------------------------------


class Idea(Frozen):
    """The immutable audit record. Stored once, never updated.

    ``inputs_digest`` is a hash of every input that fed the idea and
    ``model_versions`` records what produced it, so §5.4 reproducibility is a
    query rather than an archaeology project.
    """

    id: str
    created_at: dt.datetime
    as_of: dt.date
    ticker: str
    idea_class: IdeaClass
    conviction: Conviction
    direction: Direction
    signals: tuple[Signal, ...]
    memo: IdeaMemo | None = None
    catalyst: CatalystRead | None = None
    sentiment: SentimentRead | None = None
    composite_score: Decimal = Field(default=NEUTRAL_SCORE, ge=0, le=100)
    risk: RiskVerdict | None = None
    rejected_by_rules: tuple[str, ...] = ()
    inputs_digest: str = ""
    model_versions: Mapping[str, str] = Field(default_factory=dict)

    @property
    def accepted(self) -> bool:
        return (
            not self.rejected_by_rules
            and self.risk is not None
            and self.risk.approved
        )


class Fill(Frozen):
    """One executed (paper) transaction."""

    ticker: str
    date: dt.date
    shares: int                  # +buy / -sell
    price: Decimal               # instrument currency, before costs
    currency: str = GBP
    fx_rate: Decimal = Decimal("1")
    commission_gbp: Decimal = Decimal("0")
    stamp_duty_gbp: Decimal = Decimal("0")
    slippage_gbp: Decimal = Decimal("0")

    @property
    def costs_gbp(self) -> Decimal:
        return self.commission_gbp + self.stamp_duty_gbp + self.slippage_gbp


class Position(Frozen):
    ticker: str
    idea_id: str
    idea_class: IdeaClass
    sector: str
    opened_on: dt.date
    shares: int
    entry: Decimal
    currency: str = GBP
    fx_rate_at_entry: Decimal = Decimal("1")
    stop: Decimal | None = None
    invalidation: str = ""
    status: PositionStatus = PositionStatus.OPEN
    closed_on: dt.date | None = None
    exit_price: Decimal | None = None

    @property
    def is_open(self) -> bool:
        return self.status is PositionStatus.OPEN

    def gbp_cost_basis(self) -> Decimal:
        return self.entry * Decimal(self.shares) * self.fx_rate_at_entry


class DataQualityIssue(Frozen):
    check: str
    severity: Severity
    ticker: str | None
    detail: str
    as_of: dt.date


class Brief(Frozen):
    """A day's output. Rendered to markdown/HTML by brief/render.py."""

    id: str
    generated_at: dt.datetime
    as_of: dt.date
    ideas: tuple[Idea, ...] = ()
    rejected: tuple[Idea, ...] = ()
    portfolio_lines: tuple[str, ...] = ()
    risk_lines: tuple[str, ...] = ()
    triggered: tuple[str, ...] = ()
    data_issues: tuple[DataQualityIssue, ...] = ()
    stale: bool = False
    kill_switch_active: bool = False
    what_we_got_wrong: tuple[str, ...] = ()

    @property
    def has_critical_data_issue(self) -> bool:
        return any(i.severity is Severity.CRITICAL for i in self.data_issues)


def sequence_digest(parts: Sequence[str]) -> str:
    """Stable digest of the inputs behind an idea. sha256 over sorted parts —
    order of module execution must not change the digest."""
    import hashlib

    h = hashlib.sha256()
    for part in sorted(parts):
        h.update(part.encode("utf-8"))
        h.update(b"\x1f")
    return h.hexdigest()[:32]
