from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from sentinel.config import Config, _decimalise, STARTER_CONFIG
from sentinel.domain import Bar, Fundamentals, NewsItem
from sentinel.storage import init_db


@pytest.fixture()
def conn(tmp_path):
    c = init_db(tmp_path / "t.sqlite")
    yield c
    c.close()


@pytest.fixture()
def config(tmp_path) -> Config:
    import tomllib

    data = _decimalise(tomllib.loads(STARTER_CONFIG))
    data["paths"] = {"db": str(tmp_path / "t.sqlite"), "briefs": str(tmp_path / "briefs")}
    return Config.model_validate(data)


def make_bar(ticker="DEMO1.LSE", date=dt.date(2024, 1, 2), close="100", **kw) -> Bar:
    close_d = Decimal(str(close))
    defaults = dict(
        ticker=ticker, date=date, open=close_d, high=close_d, low=close_d,
        close=close_d, adjusted_close=close_d, volume=100_000, currency="GBP",
    )
    defaults.update(kw)
    return Bar(**defaults)


def series(ticker: str, closes, start=dt.date(2023, 1, 2), currency="GBP") -> list[Bar]:
    """A run of daily bars from a list of closes, weekdays only."""
    out: list[Bar] = []
    day = start
    for c in closes:
        while day.weekday() >= 5:
            day += dt.timedelta(days=1)
        price = Decimal(str(c))
        out.append(
            Bar(
                ticker=ticker, date=day, open=price, high=price * Decimal("1.01"),
                low=price * Decimal("0.99"), close=price, adjusted_close=price,
                volume=1_000_000, currency=currency,
            )
        )
        day += dt.timedelta(days=1)
    return out


def make_fundamentals(ticker="DEMO1.LSE", as_of=dt.date(2024, 1, 1), **kw) -> Fundamentals:
    defaults = dict(
        ticker=ticker, as_of=as_of, currency="GBP", sector="consumer",
        market_cap=Decimal("1000000000"),
        revenue_ttm=Decimal("500000000"), revenue_prior_ttm=Decimal("450000000"),
        eps_ttm=Decimal("2.50"), eps_prior_ttm=Decimal("2.00"),
        gross_margin=Decimal("0.42"), operating_margin=Decimal("0.18"),
        net_margin=Decimal("0.12"),
        free_cash_flow_ttm=Decimal("60000000"),
        operating_cash_flow_ttm=Decimal("80000000"),
        total_debt=Decimal("200000000"), total_equity=Decimal("400000000"),
        total_assets=Decimal("900000000"), total_assets_prior=Decimal("850000000"),
        current_assets=Decimal("300000000"), current_liabilities=Decimal("150000000"),
        current_assets_prior=Decimal("260000000"), current_liabilities_prior=Decimal("150000000"),
        shares_outstanding=Decimal("100000000"), shares_outstanding_prior=Decimal("100000000"),
        net_income_ttm=Decimal("60000000"), net_income_prior_ttm=Decimal("48000000"),
        pe_ratio=Decimal("14"), pe_5y_median=Decimal("18"), pe_sector_median=Decimal("17"),
        ev_ebitda=Decimal("9"), ev_ebitda_sector_median=Decimal("11"),
    )
    defaults.update(kw)
    return Fundamentals(**defaults)


def make_news(ticker="DEMO1.LSE", headline="Beat expectations", days_ago=1) -> NewsItem:
    return NewsItem(
        ticker=ticker,
        published_at=dt.datetime.now(dt.UTC) - dt.timedelta(days=days_ago),
        headline=headline, summary="Details.", source="test", url="https://example.test/1",
    )
