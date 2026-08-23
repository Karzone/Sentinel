"""Phase 1: vendor parsing, tested without a key or a network round-trip.

The payload shapes below are recorded from each vendor's documented response.
They cannot prove the endpoint URL is right — only a live call does that, and
`sentinel health` is where you find out — but they do prove the mapping, which
is the half of an adapter that actually harbours bugs.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from sentinel.data import eodhd, finnhub, fmp
from sentinel.data.base import ProviderError, currency_for

EOD_PAYLOAD = [
    {"date": "2024-01-02", "open": 100.5, "high": 102.0, "low": 99.25, "close": 101.0,
     "adjusted_close": 99.5, "volume": 1234567},
    {"date": "2024-01-03", "open": 101.0, "high": 103.5, "low": 100.5, "close": 103.0,
     "adjusted_close": 103.0, "volume": 987654},
]


class TestEodhd:
    def test_maps_rows_to_bars_in_date_order(self):
        bars = eodhd.parse_eod("VOD.LSE", list(reversed(EOD_PAYLOAD)))
        assert [b.date for b in bars] == [dt.date(2024, 1, 2), dt.date(2024, 1, 3)]
        assert bars[0].currency == "GBP"

    def test_prices_arrive_as_decimal_not_float(self):
        # Vendor JSON is float. If any of it reaches a position size as a float,
        # rule 5 is broken silently — so assert the type, not just the value.
        bar = eodhd.parse_eod("VOD.LSE", EOD_PAYLOAD)[0]
        assert isinstance(bar.close, Decimal)
        assert bar.close == Decimal("101.0")
        assert bar.adjusted_close == Decimal("99.5")

    def test_rows_with_a_null_price_are_dropped_not_zero_filled(self):
        payload = EOD_PAYLOAD + [{"date": "2024-01-04", "open": None, "high": None,
                                  "low": None, "close": None, "volume": 0}]
        assert len(eodhd.parse_eod("VOD.LSE", payload)) == 2

    def test_a_missing_adjusted_close_falls_back_to_close(self):
        payload = [{"date": "2024-01-02", "open": 10, "high": 10, "low": 10,
                    "close": 10, "volume": 1}]
        assert eodhd.parse_eod("X.LSE", payload)[0].adjusted_close == Decimal("10")

    def test_an_unparseable_row_raises_rather_than_returning_short(self):
        # Silently returning fewer bars would look like a market holiday to the
        # missing-bars check instead of like a vendor fault.
        with pytest.raises(ProviderError):
            eodhd.parse_eod("X.LSE", [{"date": "not-a-date", "open": 1, "high": 1,
                                       "low": 1, "close": 1, "volume": 1}])

    def test_dormant_without_a_key(self):
        assert eodhd.EodhdProvider(token=None).available() is False


class TestFmp:
    INCOME = [
        {"date": "2024-06-30", "fillingDate": "2024-08-02", "revenue": 500_000_000,
         "grossProfit": 210_000_000, "operatingIncome": 90_000_000, "netIncome": 60_000_000,
         "epsdiluted": 0.6, "weightedAverageShsOutDil": 100_000_000},
        {"date": "2023-06-30", "fillingDate": "2023-08-02", "revenue": 450_000_000,
         "grossProfit": 180_000_000, "operatingIncome": 70_000_000, "netIncome": 48_000_000,
         "epsdiluted": 0.48, "weightedAverageShsOutDil": 100_000_000},
    ]
    BALANCE = [
        {"date": "2024-06-30", "totalAssets": 900_000_000, "totalCurrentAssets": 300_000_000,
         "totalCurrentLiabilities": 150_000_000, "totalDebt": 200_000_000,
         "totalStockholdersEquity": 400_000_000},
        {"date": "2023-06-30", "totalAssets": 850_000_000, "totalCurrentAssets": 260_000_000,
         "totalCurrentLiabilities": 150_000_000, "totalDebt": 220_000_000,
         "totalStockholdersEquity": 350_000_000},
    ]
    CASHFLOW = [{"date": "2024-06-30", "operatingCashFlow": 80_000_000, "freeCashFlow": 60_000_000}]
    RATIOS = [{"peRatioTTM": 14.2, "enterpriseValueMultipleTTM": 9.1}]
    PROFILE = [{"currency": "USD", "sector": "Technology", "mktCap": 1_000_000_000}]

    def _assemble(self):
        return fmp.assemble("DEMO.US", self.INCOME, self.BALANCE, self.CASHFLOW,
                            self.RATIOS, profile=self.PROFILE)

    def test_as_of_is_the_filing_date_not_the_period_end(self):
        """Scoring a period on the day it ended is lookahead: the numbers were
        not public for another five weeks."""
        assert self._assemble().as_of == dt.date(2024, 8, 2)

    def test_prior_period_balance_sheet_lines_are_carried(self):
        # Piotroski needs prior-period assets and working capital. Getting the
        # index wrong here yields a plausible-looking but wrong F-score.
        f = self._assemble()
        assert f.total_assets_prior == Decimal("850000000")
        assert f.current_assets_prior == Decimal("260000000")

    def test_margins_are_derived_not_guessed(self):
        f = self._assemble()
        assert f.gross_margin == Decimal("0.4200")
        assert f.net_margin == Decimal("0.1200")

    def test_a_zero_revenue_period_yields_none_margin_not_a_crash(self):
        income = [{**self.INCOME[0], "revenue": 0}]
        f = fmp.assemble("DEMO.US", income, self.BALANCE, self.CASHFLOW, self.RATIOS)
        assert f is not None and f.net_margin is None

    def test_no_statements_means_no_snapshot(self):
        assert fmp.assemble("DEMO.US", [], [], [], []) is None


class TestFinnhub:
    PAYLOAD = [
        {"datetime": 1704200000, "headline": "Company beats", "summary": "Good.",
         "source": "Reuters", "url": "https://x.test/1"},
        {"datetime": 1704100000, "headline": "", "summary": "no headline", "url": ""},
        {"datetime": 1704300000, "headline": "Company guides up", "summary": "", "url": ""},
    ]

    def test_untitled_rows_are_dropped_and_the_rest_sort_newest_first(self):
        items = finnhub.parse_news("DEMO.US", self.PAYLOAD)
        assert [i.headline for i in items] == ["Company guides up", "Company beats"]

    def test_since_filters_server_slop(self):
        since = dt.datetime.fromtimestamp(1704250000, dt.UTC)
        items = finnhub.parse_news("DEMO.US", self.PAYLOAD, since=since)
        assert [i.headline for i in items] == ["Company guides up"]

    def test_a_broken_timestamp_skips_the_row(self):
        items = finnhub.parse_news("DEMO.US", [{"datetime": "nope", "headline": "x"}])
        assert items == []


class TestCurrencyInference:
    @pytest.mark.parametrize(
        "ticker,expected",
        [("VOD.LSE", "GBP"), ("BP.L", "GBP"), ("AAPL.US", "USD"),
         ("MC.PA", "EUR"), ("NESN.SW", "CHF"), ("SHOP.TO", "CAD"), ("AAPL", "GBP")],
    )
    def test_suffix_drives_currency(self, ticker, expected):
        assert currency_for(ticker) == expected


class TestEodhdFundamentalsDate:
    """`as_of` on a fundamentals snapshot is the PERIOD the numbers describe.

    It was EODHD's `General.UpdatedAt` — the vendor's record-update timestamp —
    which broke two things at once against a live account:

    * it is stamped in EODHD's timezone, so on a UK clock every snapshot landed
      dated TOMORROW, and `repo.get_fundamentals` reads point-in-time
      (`as_of <= ?`). All 25 rows were written and then filtered out; the brief
      reported "no fundamentals snapshot" for a database that had them;
    * a snapshot "filed" today is never stale, so the staleness check would have
      passed two-year-old financials as current.
    """

    PAYLOAD = {
        "General": {"UpdatedAt": "2026-08-24", "CurrencyCode": "USD", "Sector": "Technology"},
        "Highlights": {"MostRecentQuarter": "2026-06-30", "EarningsShare": "3.10",
                       "ProfitMargin": "0.55"},
        "Valuation": {"EnterpriseValueEbitda": "40.1"},
    }

    def test_the_period_end_is_used_not_the_vendor_update_stamp(self):
        from sentinel.data.eodhd import parse_fundamentals

        snapshot = parse_fundamentals("NVDA.US", self.PAYLOAD)
        assert snapshot is not None
        assert snapshot.as_of == dt.date(2026, 6, 30), (
            "UpdatedAt is when EODHD touched the row, not the period the numbers cover"
        )

    def test_a_point_in_time_read_can_then_find_it(self, conn):
        """The actual failure: written, then invisible."""
        from sentinel.data.eodhd import parse_fundamentals
        from sentinel.storage import repo

        snapshot = parse_fundamentals("NVDA.US", self.PAYLOAD)
        repo.save_fundamentals(conn, [snapshot], source="eodhd")
        # The day the vendor stamp claimed, and the day before it.
        found = repo.get_fundamentals(conn, "NVDA.US", as_of=dt.date(2026, 8, 23))
        assert found is not None, "the snapshot was saved and then filtered out again"
        assert found.as_of == dt.date(2026, 6, 30)

    def test_the_update_stamp_is_still_the_fallback(self):
        from sentinel.data.eodhd import parse_fundamentals

        payload = {"General": {"UpdatedAt": "2026-05-01"}, "Highlights": {"EarningsShare": "1"}}
        snapshot = parse_fundamentals("X.US", payload)
        assert snapshot is not None and snapshot.as_of == dt.date(2026, 5, 1)

    def test_the_quarter_that_ended_is_not_the_next_earnings_date(self):
        """Assigning MostRecentQuarter to next_earnings_date asserted the next
        earnings were in the past."""
        from sentinel.data.eodhd import parse_fundamentals

        snapshot = parse_fundamentals("NVDA.US", self.PAYLOAD)
        assert snapshot.next_earnings_date != dt.date(2026, 6, 30)
        assert snapshot.next_earnings_date is None


class TestFutureDatedFundamentalsAreReported:
    """quality.check_fundamentals has a CRITICAL branch for a snapshot dated
    after the as-of date. It was unreachable: get_fundamentals filters such a
    row out point-in-time, so the pipeline saw None and reported the much milder
    "no fundamentals snapshot". Ingest holds the record before the filter."""

    def test_ingest_reports_a_future_dated_snapshot(self, conn, config, monkeypatch):
        import datetime as _dt
        from sentinel.data import ingest as ingest_mod, registry
        from sentinel.domain.models import Fundamentals

        as_of = _dt.date(2026, 8, 23)
        future = Fundamentals(ticker="X.US", as_of=_dt.date(2026, 8, 24), currency="USD")

        class _Vendor:
            name = "stub"
            def available(self): return True
            def fetch_fundamentals(self, ticker): return future

        monkeypatch.setattr(registry, "fundamentals_provider", lambda _c: _Vendor())
        result = ingest_mod.ingest(conn, config, ["X.US"], as_of=as_of, with_news=False)

        critical = [i for i in result.report.issues if "after the as-of date" in i.detail]
        assert critical, "a snapshot dated in the future was stored with no complaint"
