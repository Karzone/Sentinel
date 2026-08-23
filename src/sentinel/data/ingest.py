"""The daily ingestion job.

Order matters here and is not arbitrary: **fetch, persist, then check**. The
quality checks read from the database rather than from what we just fetched,
so they validate the series a brief will actually be scored on — including the
bars ingested last week — rather than only today's delta.

A vendor failure for one ticker never takes the universe down. It is recorded
as a CRITICAL issue for that ticker, which is what stops it being scored;
everything else proceeds.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Sequence

from ..config import Config
from ..domain.enums import Severity
from ..domain.models import DataQualityIssue
from ..logging_setup import get_logger
from ..storage import audit, repo
from . import quality, registry
from .base import ProviderError

log = get_logger("ingest")
INGEST_VERSION = "ingest-v1"


@dataclass(slots=True)
class IngestResult:
    as_of: dt.date
    tickers: tuple[str, ...]
    bars_written: int = 0
    fundamentals_written: int = 0
    news_written: int = 0
    report: quality.QualityReport = field(default_factory=lambda: quality.QualityReport(dt.date.today()))
    vendor_failures: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.report.blocking and not self.vendor_failures

    def summary(self) -> str:
        return (
            f"{len(self.tickers)} tickers · {self.bars_written} bars · "
            f"{self.fundamentals_written} fundamentals · {self.news_written} news · "
            f"{len(self.report.critical)} critical, {len(self.report.warnings)} warnings"
        )


def ingest(
    conn,
    config: Config,
    tickers: Sequence[str],
    *,
    as_of: dt.date | None = None,
    history_days: int = 800,
    with_news: bool = True,
) -> IngestResult:
    as_of = as_of or dt.date.today()
    start = as_of - dt.timedelta(days=history_days)
    result = IngestResult(as_of=as_of, tickers=tuple(tickers))
    result.report = quality.QualityReport(as_of)
    first_bars: dict[str, dt.date] = {}

    prices = registry.price_provider(config)
    fundamentals = registry.fundamentals_provider(config)
    news = registry.news_provider(config)

    audit.record(
        conn, audit.AuditEvent.INGEST_STARTED,
        payload={"tickers": list(tickers), "as_of": as_of.isoformat(),
                 "price_provider": prices.name, "version": INGEST_VERSION},
    )

    for ticker in tickers:
        # -- prices
        if prices.available():
            try:
                bars = prices.fetch_bars(ticker, start, as_of)
                result.bars_written += repo.save_bars(conn, bars, source=prices.name)
            except ProviderError as exc:
                result.vendor_failures.append(f"{ticker} prices: {exc}")
                result.report.extend([
                    DataQualityIssue(
                        check="vendor", severity=Severity.CRITICAL, ticker=ticker,
                        detail=f"price vendor {prices.name} failed: {exc}", as_of=as_of,
                    )
                ])
        else:
            result.report.extend([
                DataQualityIssue(
                    check="vendor", severity=Severity.CRITICAL, ticker=ticker,
                    detail=f"price provider {prices.name} is not configured", as_of=as_of,
                )
            ])

        # -- fundamentals
        if fundamentals.available():
            try:
                snapshot = fundamentals.fetch_fundamentals(ticker)
                if snapshot is not None:
                    # quality.check_fundamentals has a CRITICAL branch for a
                    # snapshot dated after the as-of date, and it is UNREACHABLE
                    # from the pipeline: repo.get_fundamentals filters such a row
                    # out point-in-time, so the pipeline sees None and reports the
                    # much milder "no fundamentals snapshot". The precise error
                    # existed and could never fire. Ingest is the one place
                    # holding the record before the filter, so it checks here.
                    if snapshot.as_of > as_of:
                        result.report.extend(quality.check_fundamentals(
                            ticker, snapshot, as_of))
                    # A chain answers for one ticker with FMP and the next
                    # with EODHD, so the source has to come from whoever
                    # actually answered — naming the chain would record a
                    # provenance that no single vendor can be held to.
                    source = getattr(fundamentals, "answered_by", None) or fundamentals.name
                    result.fundamentals_written += repo.save_fundamentals(
                        conn, [snapshot], source=source
                    )
            except ProviderError as exc:
                # Not fatal: a ticker with prices but no fundamentals can still
                # be scored on trend, it just cannot be a long-term idea. But it
                # must still be VISIBLE — this used to append to vendor_failures
                # and nothing else, and nothing printed that list, so a failing
                # vendor was indistinguishable from one with nothing to say.
                result.vendor_failures.append(f"{ticker} fundamentals: {exc}")
                result.report.extend([
                    DataQualityIssue(
                        check="vendor", severity=Severity.WARN, ticker=ticker,
                        detail=f"fundamentals vendor {fundamentals.name} failed: {exc}",
                        as_of=as_of,
                    )
                ])

        # -- news
        if with_news and news.available():
            try:
                since = dt.datetime.combine(
                    as_of - dt.timedelta(days=config.data.news_lookback_days), dt.time.min, dt.UTC
                )
                result.news_written += repo.save_news(conn, news.fetch_news(ticker, since))
            except ProviderError as exc:
                result.vendor_failures.append(f"{ticker} news: {exc}")

    # -- checks, against what is now in the database
    for ticker in tickers:
        stored_bars = repo.get_bars(conn, ticker, end=as_of)
        stored_fundamentals = repo.get_fundamentals(conn, ticker, as_of=as_of)
        if stored_bars:
            first_bars[ticker] = stored_bars[0].date
        result.report.extend(
            quality.run_all(
                ticker, stored_bars, stored_fundamentals, as_of,
                staleness_hours=config.data.staleness_hours,
                min_history_bars=config.data.min_history_bars,
                requested_start=start,
            )
        )

    # Run-level, after every ticker: a shared start date is only visible across
    # the universe. One ticker short is a listing; twenty-five short to the same
    # week is a plan.
    result.report.extend(quality.check_history_cap(first_bars, start, as_of))

    repo.save_quality_issues(conn, result.report.issues)
    audit.record(
        conn, audit.AuditEvent.INGEST_COMPLETED,
        payload={
            "as_of": as_of.isoformat(), "bars": result.bars_written,
            "fundamentals": result.fundamentals_written, "news": result.news_written,
            "critical": len(result.report.critical), "warnings": len(result.report.warnings),
            "vendor_failures": result.vendor_failures,
            # The requested window and what each series actually starts at.
            # Without these, comparing two runs of different depth is guesswork
            # — which is exactly how a silent truncation stayed unexplained.
            "history_days": history_days,
            "requested_start": start.isoformat(),
            "first_bars": {t: d.isoformat() for t, d in sorted(first_bars.items())},
        },
    )
    log.info("ingest complete: %s", result.summary())
    return result
