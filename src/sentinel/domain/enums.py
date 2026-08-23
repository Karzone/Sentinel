"""Closed vocabularies shared across modules.

Every enum here is stored in SQLite *as its string value* (same convention the
rest of the owner's stack uses) so an audit row stays readable years later
without the code that wrote it.
"""

from __future__ import annotations

from enum import Enum


class StrEnum(str, Enum):
    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.value


class IdeaClass(StrEnum):
    """The two idea classes the philosophy keeps deliberately separate."""

    LONG_TERM = "long_term"   # 6 months - 5 years, fundamentals-led
    SWING = "swing"           # 1 - 8 weeks, technicals + catalyst-led


class Conviction(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class Direction(StrEnum):
    LONG = "long"
    FLAT = "flat"           # explicitly "no position", not a weak long
    AVOID = "avoid"


class ModuleName(StrEnum):
    FUNDAMENTAL = "fundamental"
    TECHNICAL = "technical"
    NEWS = "news"
    SENTIMENT = "sentiment"
    SYNTHESIS = "synthesis"


class CatalystType(StrEnum):
    EARNINGS = "earnings"
    GUIDANCE = "guidance"
    MA = "m_and_a"
    REGULATORY = "regulatory"
    PRODUCT = "product"
    MANAGEMENT = "management"
    MACRO = "macro"
    LEGAL = "legal"
    CAPITAL = "capital"      # placings, buybacks, dividends, debt raises
    OTHER = "other"


class RiskCheckId(StrEnum):
    """One id per hard rule. These are the names that appear in the audit log,
    so renaming one is a breaking change to the eval history."""

    HAS_STOP = "has_stop"
    HAS_INVALIDATION = "has_invalidation"
    STOP_BELOW_ENTRY = "stop_below_entry"
    POSITION_SIZE_POSITIVE = "position_size_positive"
    MAX_SINGLE_POSITION = "max_single_position"
    MAX_SECTOR_CONCENTRATION = "max_sector_concentration"
    SWING_SUB_ALLOCATION = "swing_sub_allocation"
    DRAWDOWN_KILL_SWITCH = "drawdown_kill_switch"
    NO_DUPLICATE_POSITION = "no_duplicate_position"
    MAX_OPEN_POSITIONS = "max_open_positions"
    SUFFICIENT_CASH = "sufficient_cash"
    DATA_FRESHNESS = "data_freshness"


class Outcome(StrEnum):
    PASS = "pass"
    FAIL = "fail"


class Wrapper(StrEnum):
    """UK account wrapper eligibility. Informational flags only — not tax advice."""

    ISA_ELIGIBLE = "isa_eligible"
    GIA_ONLY = "gia_only"
    UNKNOWN = "unknown"


class PositionStatus(StrEnum):
    OPEN = "open"
    CLOSED_STOP = "closed_stop"
    CLOSED_INVALIDATED = "closed_invalidated"
    CLOSED_MANUAL = "closed_manual"
    CLOSED_TARGET = "closed_target"


class Severity(StrEnum):
    """Data-quality severities. CRITICAL is the Sev-1 in the spec: a signal
    generated from data this bad is a defect, so the pipeline refuses it."""

    INFO = "info"
    WARN = "warn"
    CRITICAL = "critical"


class NotifyEvent(StrEnum):
    """The *only* events allowed to push to a phone. The daily brief is not here
    on purpose — see notify/router.py."""

    STOP_TRIGGERED = "stop_triggered"
    INVALIDATION_HIT = "invalidation_hit"
    KILL_SWITCH = "kill_switch"
    PIPELINE_FAILURE = "pipeline_failure"
    EARNINGS_IMMINENT = "earnings_imminent"
