"""The fundamentals fallback chain.

The live case: FMP's free plan answered 9 of 25 AI tickers and returned
`402 Payment Required — Special Endpoint: This value set for 'symbol' is not
available under your current subscription` for the other 16. The plan gates
ROWS, not the endpoint, so the adapter was correct and no retry would help.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from sentinel.config import Config
from sentinel.data import registry
from sentinel.data.base import ProviderError
from sentinel.data.chain import FundamentalsChain
from sentinel.domain.models import Fundamentals


def _snapshot(ticker: str) -> Fundamentals:
    return Fundamentals(ticker=ticker, as_of=dt.date(2026, 8, 1), currency="USD",
                        revenue_ttm=Decimal("1"))


class _Vendor:
    """Answers for `covers`; 402s for anything else — FMP's free plan, exactly."""

    def __init__(self, name: str, covers: set[str] | None = None, *,
                 available: bool = True, empty: bool = False):
        self.name = name
        self._covers = covers if covers is not None else set()
        self._available = available
        self._empty = empty
        self.asked: list[str] = []

    def available(self) -> bool:
        return self._available

    def fetch_fundamentals(self, ticker: str):
        self.asked.append(ticker)
        if self._empty:
            return None
        if ticker in self._covers:
            return _snapshot(ticker)
        raise ProviderError(
            f"402 Payment Required: 'symbol' {ticker} is not available under "
            f"your current subscription ({self.name})"
        )


class TestTheChainWidensCoverage:
    def test_the_second_vendor_covers_what_the_first_refuses(self):
        """The whole point: two partial free tiers make one wider one."""
        first, second = _Vendor("fmp", {"NVDA"}), _Vendor("eodhd", {"ARM"})
        chain = FundamentalsChain([first, second])

        assert chain.fetch_fundamentals("NVDA") is not None
        assert chain.fetch_fundamentals("ARM") is not None
        # NVDA never reaches the second vendor; ARM is the fallback that
        # turns a 402 into a row.
        assert second.asked == ["ARM"], "second vendor was not consulted"

    def test_the_first_vendor_wins_and_the_second_is_never_asked(self):
        """Order is a preference, not a race — a covered ticker must not spend
        a second vendor's quota."""
        first, second = _Vendor("fmp", {"NVDA"}), _Vendor("eodhd", {"NVDA"})
        FundamentalsChain([first, second]).fetch_fundamentals("NVDA")
        assert second.asked == []

    def test_the_source_recorded_is_the_vendor_that_answered(self):
        """Provenance has to survive the fallback: a row fetched from EODHD
        recorded as `fmp+eodhd` is a provenance nobody can be held to."""
        chain = FundamentalsChain([_Vendor("fmp", {"NVDA"}), _Vendor("eodhd", {"ARM"})])
        chain.fetch_fundamentals("NVDA")
        assert chain.answered_by == "fmp"
        chain.fetch_fundamentals("ARM")
        assert chain.answered_by == "eodhd"

    def test_a_failure_does_not_leave_the_previous_vendors_name_behind(self):
        """Otherwise the next row is filed under whoever answered last."""
        chain = FundamentalsChain([_Vendor("fmp", {"NVDA"})])
        chain.fetch_fundamentals("NVDA")
        with pytest.raises(ProviderError):
            chain.fetch_fundamentals("ARM")
        assert chain.answered_by is None


class TestWhenEveryoneRefuses:
    def test_each_vendors_own_reason_survives(self):
        """`402 not in your plan` and `403 endpoint not in your plan` need
        different actions. Collapsing them to "no fundamentals" is the bug this
        module exists to avoid."""
        chain = FundamentalsChain([_Vendor("fmp"), _Vendor("eodhd")])
        with pytest.raises(ProviderError) as caught:
            chain.fetch_fundamentals("ARM")
        message = str(caught.value)
        assert "fmp" in message and "eodhd" in message
        assert message.count("402") == 2, message

    def test_an_empty_answer_is_named_rather_than_swallowed(self):
        """A vendor with no row for a ticker raises nothing. A chain of those
        would return None with no explanation at all."""
        chain = FundamentalsChain([_Vendor("fmp", empty=True), _Vendor("eodhd", empty=True)])
        with pytest.raises(ProviderError) as caught:
            chain.fetch_fundamentals("ARM")
        assert "no data for ARM" in str(caught.value)

    def test_a_dormant_vendor_is_skipped_not_failed(self):
        """A chain configured with a key you have not set yet must still use
        the key you have."""
        dormant = _Vendor("eodhd", {"ARM"}, available=False)
        chain = FundamentalsChain([_Vendor("fmp", {"NVDA"}), dormant])
        assert chain.fetch_fundamentals("NVDA") is not None
        assert dormant.asked == []

    def test_the_chain_is_available_when_any_member_is(self):
        chain = FundamentalsChain([_Vendor("fmp", available=False), _Vendor("eodhd")])
        assert chain.available() is True
        assert FundamentalsChain([_Vendor("fmp", available=False)]).available() is False

    def test_an_empty_chain_is_a_configuration_error_not_a_silent_pass(self):
        with pytest.raises(ValueError):
            FundamentalsChain([])


class TestConfiguration:
    def _config(self, spec: str) -> Config:
        base = Config()
        return base.model_copy(
            update={"data": base.data.model_copy(update={"fundamentals_provider": spec})}
        )

    def test_a_single_name_still_builds_a_single_provider(self):
        provider = registry.fundamentals_provider(self._config("fixture"))
        assert not isinstance(provider, FundamentalsChain)
        assert provider.name == "fixture"

    def test_a_comma_separated_list_builds_a_chain_in_order(self):
        chain = registry.fundamentals_provider(self._config("fmp,eodhd"))
        assert isinstance(chain, FundamentalsChain)
        assert [p.name for p in chain.members] == ["fmp", "eodhd"]

    def test_whitespace_and_a_trailing_comma_are_tolerated(self):
        chain = registry.fundamentals_provider(self._config(" fmp , eodhd , "))
        assert [p.name for p in chain.members] == ["fmp", "eodhd"]

    def test_an_unknown_name_in_a_chain_still_names_itself(self):
        with pytest.raises(registry.UnknownProvider) as caught:
            registry.fundamentals_provider(self._config("fmp,nope"))
        assert "nope" in str(caught.value)

    def test_an_empty_setting_is_rejected(self):
        with pytest.raises(registry.UnknownProvider):
            registry.fundamentals_provider(self._config(" , "))


class TestHealthReportsEachVendorSeparately:
    def test_a_chain_with_one_missing_key_does_not_read_as_ready(self):
        """Collapsed to one row, a half-dormant chain looks healthy — and
        naming the absent key is the entire job of `sentinel health`."""
        base = Config()
        config = base.model_copy(
            update={"data": base.data.model_copy(
                update={"fundamentals_provider": "fmp,eodhd"})}
        )
        rows = [r for r in registry.describe(config) if r["kind"] == "fundamentals"]
        assert [r["provider"] for r in rows] == ["fmp", "eodhd"]
        # Neither key is set in the test environment, so both must read dormant
        # rather than one aggregate "ready".
        assert all(r["available"] is False for r in rows)
