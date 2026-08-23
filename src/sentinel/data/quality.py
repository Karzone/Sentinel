"""Data-quality checks.

The Phase 1 line that governs this file: **a signal generated from bad data is a
Sev-1 defect.** So these checks are not a dashboard nicety — a ``CRITICAL``
issue stops the pipeline scoring that ticker at all, and the brief renders a
banner instead of pretending.

The load-bearing check is ``adjustment_integrity``. Split and dividend
adjustment is the single most common way vendor price data is silently wrong,
and it is invisible to eyeballing: a 2:1 split that never made it into
``adjusted_close`` looks exactly like a 50% crash to every momentum indicator in
Phase 2. It is caught here by an invariant rather than a threshold —
``close / adjusted_close`` is a cumulative adjustment factor, so it must be
non-increasing as you move forward in time and must land at 1.0 on the most
recent bar. A vendor that breaks that has corrupted the series, full stop.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Sequence

from ..domain.enums import Severity
from ..domain.models import Bar, DataQualityIssue, Fundamentals

QUALITY_VERSION = "quality-v1"

#: A single-day adjusted move beyond this is either a real event or an
#: unadjusted corporate action. We cannot tell which, so it is a WARN and the
#: human decides — never a silent pass, never a silent block.
EXTREME_DAILY_MOVE = Decimal("0.35")
#: Adjustment factors are floats at the vendor; allow a hair of drift before
#: calling a series corrupt.
FACTOR_TOLERANCE = Decimal("0.005")
#: A gap longer than this many consecutive weekdays is not a bank holiday.
MAX_HOLIDAY_RUN = 5
#: Above this fraction of weekdays missing, the series is too holey to score.
MISSING_FRACTION_CRITICAL = Decimal("0.10")
#: Fundamentals older than this are almost certainly a dead feed, not a slow filer.
FUNDAMENTALS_MAX_AGE_DAYS = 400


@dataclass(slots=True)
class QualityReport:
    as_of: dt.date
    issues: list[DataQualityIssue] = field(default_factory=list)

    @property
    def critical(self) -> list[DataQualityIssue]:
        return [i for i in self.issues if i.severity is Severity.CRITICAL]

    @property
    def warnings(self) -> list[DataQualityIssue]:
        return [i for i in self.issues if i.severity is Severity.WARN]

    @property
    def blocking(self) -> bool:
        return bool(self.critical)

    def blocked_tickers(self) -> set[str]:
        """Tickers that must not be scored this run."""
        return {i.ticker for i in self.critical if i.ticker}

    def extend(self, more: Sequence[DataQualityIssue]) -> None:
        self.issues.extend(more)


def _issue(check: str, severity: Severity, ticker: str | None, detail: str, as_of: dt.date) -> DataQualityIssue:
    return DataQualityIssue(check=check, severity=severity, ticker=ticker, detail=detail, as_of=as_of)


def business_days_between(start: dt.date, end: dt.date) -> int:
    """Weekdays strictly after ``start``, up to and including ``end``.

    No exchange calendar, so bank holidays inflate this by a day or two — which
    is why the thresholds below are in *days of slack*, not exact counts.
    """
    if end <= start:
        return 0
    days = 0
    day = start + dt.timedelta(days=1)
    while day <= end:
        if day.weekday() < 5:
            days += 1
        day += dt.timedelta(days=1)
    return days


# ---------------------------------------------------------------- checks


def check_freshness(
    ticker: str, bars: Sequence[Bar], as_of: dt.date, *, staleness_hours: int = 24
) -> list[DataQualityIssue]:
    """§5.4's data-freshness SLA. Absent data is worse than stale data, so an
    empty series is CRITICAL rather than 'nothing to report'."""
    if not bars:
        return [_issue("freshness", Severity.CRITICAL, ticker, "no price history at all", as_of)]
    age = business_days_between(bars[-1].date, as_of)
    allowance = max(1, staleness_hours // 24)
    if age > allowance + 3:
        return [_issue(
            "freshness", Severity.CRITICAL, ticker,
            f"last bar {bars[-1].date} is {age} business days before {as_of}", as_of,
        )]
    if age > allowance:
        return [_issue(
            "freshness", Severity.WARN, ticker,
            f"last bar {bars[-1].date} is {age} business days before {as_of}", as_of,
        )]
    return []


def check_adjustment_integrity(ticker: str, bars: Sequence[Bar], as_of: dt.date) -> list[DataQualityIssue]:
    """Verify split/dividend adjustment rather than trusting it.

    ``factor = close / adjusted_close`` accumulates every split and dividend
    between a bar and today. It must therefore:

    * never *rise* as you move forward in time (a rise means an adjustment was
      un-applied, i.e. the series has been corrupted mid-history), and
    * equal 1.0 on the latest bar (the vendor normalises to the present).

    Both violations are CRITICAL: every technical indicator downstream reads
    adjusted closes, so a broken factor poisons momentum, trend and ATR at once.
    """
    issues: list[DataQualityIssue] = []
    if not bars:
        return issues

    factors: list[tuple[dt.date, Decimal]] = []
    for bar in bars:
        if bar.adjusted_close <= 0:
            issues.append(_issue(
                "adjustment_integrity", Severity.CRITICAL, ticker,
                f"non-positive adjusted_close {bar.adjusted_close} on {bar.date}", as_of,
            ))
            continue
        factors.append((bar.date, bar.close / bar.adjusted_close))

    if not factors:
        return issues

    last_date, last_factor = factors[-1]
    if abs(last_factor - Decimal("1")) > FACTOR_TOLERANCE:
        issues.append(_issue(
            "adjustment_integrity", Severity.CRITICAL, ticker,
            f"latest bar {last_date} has adjustment factor {last_factor:.4f}, expected 1.0 — "
            "the series is not normalised to the present",
            as_of,
        ))

    for (prev_date, prev_factor), (curr_date, curr_factor) in zip(factors, factors[1:]):
        if curr_factor > prev_factor + FACTOR_TOLERANCE:
            issues.append(_issue(
                "adjustment_integrity", Severity.CRITICAL, ticker,
                f"adjustment factor rose from {prev_factor:.4f} ({prev_date}) to "
                f"{curr_factor:.4f} ({curr_date}) — an adjustment was un-applied",
                as_of,
            ))
            break  # one report per series; the whole thing is suspect
    return issues


def check_missing_bars(ticker: str, bars: Sequence[Bar], as_of: dt.date) -> list[DataQualityIssue]:
    if len(bars) < 2:
        return []
    issues: list[DataQualityIssue] = []
    expected = business_days_between(bars[0].date, bars[-1].date) + 1
    missing = expected - len(bars)
    fraction = Decimal(missing) / Decimal(expected) if expected else Decimal("0")

    if fraction > MISSING_FRACTION_CRITICAL:
        issues.append(_issue(
            "missing_bars", Severity.CRITICAL, ticker,
            f"{missing}/{expected} weekdays missing ({fraction:.1%}) — series too incomplete to score",
            as_of,
        ))
    elif missing > 0:
        worst_gap, worst_at = 0, bars[0].date
        for prev, curr in zip(bars, bars[1:]):
            gap = business_days_between(prev.date, curr.date) - 1
            if gap > worst_gap:
                worst_gap, worst_at = gap, curr.date
        severity = Severity.WARN if worst_gap > MAX_HOLIDAY_RUN else Severity.INFO
        issues.append(_issue(
            "missing_bars", severity, ticker,
            f"{missing} weekday bars missing; longest gap {worst_gap} days before {worst_at}",
            as_of,
        ))
    return issues


def check_price_sanity(ticker: str, bars: Sequence[Bar], as_of: dt.date) -> list[DataQualityIssue]:
    """OHLC coherence is enforced by the Bar model itself, so what is left here
    is what a *valid* bar can still get wrong: zero prices, dead volume, and
    moves large enough to be an unadjusted corporate action."""
    issues: list[DataQualityIssue] = []
    zero_volume_run = 0
    for bar in bars:
        if bar.close <= 0 or bar.open <= 0:
            issues.append(_issue(
                "price_sanity", Severity.CRITICAL, ticker,
                f"non-positive price on {bar.date}", as_of,
            ))
        zero_volume_run = zero_volume_run + 1 if bar.volume == 0 else 0
        if zero_volume_run == MAX_HOLIDAY_RUN:
            issues.append(_issue(
                "price_sanity", Severity.WARN, ticker,
                f"{MAX_HOLIDAY_RUN} consecutive zero-volume bars ending {bar.date} — "
                "possible delisting or a stale feed printing the last price",
                as_of,
            ))
    for prev, curr in zip(bars, bars[1:]):
        if prev.adjusted_close <= 0:
            continue
        move = (curr.adjusted_close - prev.adjusted_close) / prev.adjusted_close
        if abs(move) > EXTREME_DAILY_MOVE:
            issues.append(_issue(
                "price_sanity", Severity.WARN, ticker,
                f"{move:+.1%} adjusted move on {curr.date} — verify it is a real event "
                "and not an unadjusted split",
                as_of,
            ))
    return issues


def check_history_depth(
    ticker: str, bars: Sequence[Bar], as_of: dt.date, *, minimum: int
) -> list[DataQualityIssue]:
    """The technical module needs a 200-day SMA before it can speak. Short
    history is a WARN, not a CRITICAL: the fundamental module is still valid,
    so the ticker stays scoreable — just not on trend."""
    if 0 < len(bars) < minimum:
        return [_issue(
            "history_depth", Severity.WARN, ticker,
            f"only {len(bars)} bars, need {minimum} for a full trend read", as_of,
        )]
    return []


def check_fundamentals(
    ticker: str, fundamentals: Fundamentals | None, as_of: dt.date
) -> list[DataQualityIssue]:
    if fundamentals is None:
        return [_issue("fundamentals", Severity.WARN, ticker, "no fundamentals snapshot", as_of)]
    issues: list[DataQualityIssue] = []
    age = (as_of - fundamentals.as_of).days
    if age > FUNDAMENTALS_MAX_AGE_DAYS:
        issues.append(_issue(
            "fundamentals", Severity.WARN, ticker,
            f"snapshot is {age} days old (filed {fundamentals.as_of})", as_of,
        ))
    if age < 0:
        # A filing dated in the future is a vendor bug, and it is the exact bug
        # that leaks lookahead into a backtest.
        issues.append(_issue(
            "fundamentals", Severity.CRITICAL, ticker,
            f"snapshot dated {fundamentals.as_of}, after the as-of date {as_of}", as_of,
        ))
    if fundamentals.sector in (None, ""):
        issues.append(_issue(
            "fundamentals", Severity.WARN, ticker,
            "no sector — the concentration limit will treat it as 'unknown'", as_of,
        ))
    return issues


def run_all(
    ticker: str,
    bars: Sequence[Bar],
    fundamentals: Fundamentals | None,
    as_of: dt.date,
    *,
    staleness_hours: int = 24,
    min_history_bars: int = 250,
) -> list[DataQualityIssue]:
    issues: list[DataQualityIssue] = []
    issues += check_freshness(ticker, bars, as_of, staleness_hours=staleness_hours)
    issues += check_adjustment_integrity(ticker, bars, as_of)
    issues += check_missing_bars(ticker, bars, as_of)
    issues += check_price_sanity(ticker, bars, as_of)
    issues += check_history_depth(ticker, bars, as_of, minimum=min_history_bars)
    issues += check_fundamentals(ticker, fundamentals, as_of)
    return issues
