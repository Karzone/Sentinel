"""The fundamental module — deterministic, no LLM.

Five components, each scored 0-100 and then weighted. The part worth reading
carefully is how *missing* data is handled: a component with no inputs is
dropped and the remaining weights are renormalised, and `confidence` falls to
the fraction of weight that was actually available.

The alternative — scoring a missing input as 50, or as 0 — is what makes a
thinly-covered small cap look like a considered neutral instead of what it is,
which is unknown. §5.2 asks whether high-conviction ideas outperform
low-conviction ones; that question only means something if 'we could not see' is
distinguishable from 'we looked and it was average'.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Callable

from ..domain.enums import ModuleName
from ..domain.models import Evidence, Fundamentals, Signal
from ..money import dec

FUNDAMENTAL_VERSION = "fundamental-v1"

WEIGHTS = {
    "growth": Decimal("0.25"),
    "profitability": Decimal("0.20"),
    "balance_sheet": Decimal("0.15"),
    "valuation": Decimal("0.25"),
    "quality": Decimal("0.15"),
}

Component = tuple[Decimal | None, str]

#: Ceiling on the balance-sheet component when book equity is negative.
NEGATIVE_EQUITY_CAP = Decimal("35")


def _band(value: Decimal, bands: list[tuple[Decimal, Decimal]]) -> Decimal:
    """Map a value through ascending (threshold, score) bands."""
    result = bands[0][1]
    for threshold, score in bands:
        if value >= threshold:
            result = score
    return result


def _ratio(numerator: Decimal | None, denominator: Decimal | None) -> Decimal | None:
    if numerator is None or denominator is None or denominator == 0:
        return None
    return numerator / denominator


def _growth(f: Fundamentals) -> Component:
    parts: list[Decimal] = []
    labels: list[str] = []
    if f.revenue_ttm is not None and f.revenue_prior_ttm not in (None, 0):
        rev = (f.revenue_ttm - f.revenue_prior_ttm) / abs(f.revenue_prior_ttm)
        parts.append(_band(rev, [
            (Decimal("-1"), Decimal("5")), (Decimal("-0.10"), Decimal("20")),
            (Decimal("0"), Decimal("45")), (Decimal("0.05"), Decimal("60")),
            (Decimal("0.10"), Decimal("72")), (Decimal("0.20"), Decimal("85")),
            (Decimal("0.35"), Decimal("95")),
        ]))
        labels.append(f"revenue {rev:+.1%} YoY")
    if f.eps_ttm is not None and f.eps_prior_ttm not in (None, 0):
        # A swing through zero makes percentage growth meaningless — report the
        # direction instead of a number that reads as +400%.
        if f.eps_prior_ttm < 0 < f.eps_ttm:
            parts.append(Decimal("80"))
            labels.append("EPS turned positive")
        elif f.eps_ttm < 0 <= f.eps_prior_ttm:
            parts.append(Decimal("10"))
            labels.append("EPS turned negative")
        else:
            eps = (f.eps_ttm - f.eps_prior_ttm) / abs(f.eps_prior_ttm)
            parts.append(_band(eps, [
                (Decimal("-1"), Decimal("5")), (Decimal("-0.10"), Decimal("22")),
                (Decimal("0"), Decimal("45")), (Decimal("0.08"), Decimal("62")),
                (Decimal("0.15"), Decimal("75")), (Decimal("0.30"), Decimal("90")),
            ]))
            labels.append(f"EPS {eps:+.1%} YoY")
    if not parts:
        return None, "no growth data"
    return sum(parts) / len(parts), "; ".join(labels)


def _profitability(f: Fundamentals) -> Component:
    parts: list[Decimal] = []
    labels: list[str] = []
    if f.net_margin is not None:
        parts.append(_band(f.net_margin, [
            (Decimal("-1"), Decimal("5")), (Decimal("0"), Decimal("40")),
            (Decimal("0.05"), Decimal("55")), (Decimal("0.10"), Decimal("70")),
            (Decimal("0.18"), Decimal("85")), (Decimal("0.28"), Decimal("95")),
        ]))
        labels.append(f"net margin {f.net_margin:.1%}")
    if f.operating_margin is not None:
        parts.append(_band(f.operating_margin, [
            (Decimal("-1"), Decimal("5")), (Decimal("0"), Decimal("42")),
            (Decimal("0.08"), Decimal("60")), (Decimal("0.15"), Decimal("75")),
            (Decimal("0.25"), Decimal("90")),
        ]))
        labels.append(f"operating margin {f.operating_margin:.1%}")
    fcf_yield = _ratio(f.free_cash_flow_ttm, f.market_cap)
    if fcf_yield is not None:
        parts.append(_band(fcf_yield, [
            (Decimal("-1"), Decimal("10")), (Decimal("0"), Decimal("45")),
            (Decimal("0.03"), Decimal("62")), (Decimal("0.05"), Decimal("75")),
            (Decimal("0.08"), Decimal("90")),
        ]))
        labels.append(f"FCF yield {fcf_yield:.1%}")
    if not parts:
        return None, "no profitability data"
    return sum(parts) / len(parts), "; ".join(labels)


def _balance_sheet(f: Fundamentals) -> Component:
    parts: list[Decimal] = []
    labels: list[str] = []
    debt_equity = _ratio(f.total_debt, f.total_equity)
    if debt_equity is not None:
        if f.total_equity is not None and f.total_equity < 0:
            parts.append(Decimal("5"))
            labels.append("negative shareholders' equity")
        else:
            # Descending: less leverage scores higher, so the bands invert.
            parts.append(_band(-debt_equity, [
                (Decimal("-10"), Decimal("5")), (Decimal("-2"), Decimal("25")),
                (Decimal("-1.5"), Decimal("40")), (Decimal("-1"), Decimal("55")),
                (Decimal("-0.5"), Decimal("72")), (Decimal("-0.25"), Decimal("85")),
                (Decimal("0"), Decimal("92")),
            ]))
            labels.append(f"debt/equity {debt_equity:.2f}")
    current_ratio = _ratio(f.current_assets, f.current_liabilities)
    if current_ratio is not None:
        parts.append(_band(current_ratio, [
            (Decimal("0"), Decimal("10")), (Decimal("0.8"), Decimal("30")),
            (Decimal("1"), Decimal("50")), (Decimal("1.3"), Decimal("68")),
            (Decimal("1.8"), Decimal("82")), (Decimal("2.5"), Decimal("88")),
        ]))
        labels.append(f"current ratio {current_ratio:.2f}")
    if not parts:
        return None, "no balance-sheet data"
    value = sum(parts) / len(parts)
    if f.total_equity is not None and f.total_equity < 0:
        # A healthy current ratio must not average away negative book equity.
        # Not zero either: buyback-driven negative equity (McDonald's, Starbucks)
        # is a capital-structure choice, not insolvency. The cap says "the
        # balance sheet is not a strength here" and leaves the verdict to the
        # other four components.
        value = min(value, NEGATIVE_EQUITY_CAP)
    return value, "; ".join(labels)


def _valuation(f: Fundamentals) -> Component:
    """Valuation is always *relative* — to the company's own history and to its
    sector. An absolute P/E band would score every utility cheap and every
    software name expensive, which tells you about the sector, not the idea."""
    parts: list[Decimal] = []
    labels: list[str] = []
    if f.pe_ratio is not None and f.pe_ratio > 0:
        if f.pe_5y_median not in (None, 0):
            rel = f.pe_ratio / f.pe_5y_median
            parts.append(_band(-rel, [
                (Decimal("-10"), Decimal("8")), (Decimal("-1.5"), Decimal("25")),
                (Decimal("-1.2"), Decimal("40")), (Decimal("-1"), Decimal("55")),
                (Decimal("-0.85"), Decimal("72")), (Decimal("-0.7"), Decimal("88")),
            ]))
            labels.append(f"P/E {f.pe_ratio:.1f} vs 5y median {f.pe_5y_median:.1f}")
        if f.pe_sector_median not in (None, 0):
            rel = f.pe_ratio / f.pe_sector_median
            parts.append(_band(-rel, [
                (Decimal("-10"), Decimal("10")), (Decimal("-1.4"), Decimal("28")),
                (Decimal("-1.1"), Decimal("45")), (Decimal("-0.95"), Decimal("62")),
                (Decimal("-0.8"), Decimal("78")), (Decimal("-0.65"), Decimal("90")),
            ]))
            labels.append(f"vs sector median {f.pe_sector_median:.1f}")
    elif f.pe_ratio is not None and f.pe_ratio <= 0:
        parts.append(Decimal("25"))
        labels.append("loss-making on a trailing basis")
    if f.ev_ebitda is not None and f.ev_ebitda_sector_median not in (None, 0) and f.ev_ebitda > 0:
        rel = f.ev_ebitda / f.ev_ebitda_sector_median
        parts.append(_band(-rel, [
            (Decimal("-10"), Decimal("12")), (Decimal("-1.4"), Decimal("30")),
            (Decimal("-1.1"), Decimal("48")), (Decimal("-0.9"), Decimal("68")),
            (Decimal("-0.75"), Decimal("85")),
        ]))
        labels.append(f"EV/EBITDA {f.ev_ebitda:.1f} vs sector {f.ev_ebitda_sector_median:.1f}")
    if not parts:
        return None, "no valuation data"
    return sum(parts) / len(parts), "; ".join(labels)


def piotroski_f_score(f: Fundamentals) -> tuple[int, int, list[str]]:
    """Piotroski's nine binary tests.

    Returns ``(passed, available, notes)``. ``available`` matters: an F-score of
    5 out of 9 tests and an F-score of 5 out of 5 *computable* tests are
    different claims, and reporting only the numerator would flatter every
    company with patchy data.
    """
    passed = 0
    available = 0
    notes: list[str] = []

    def test(name: str, condition: Callable[[], bool | None]) -> None:
        nonlocal passed, available
        try:
            result = condition()
        except (TypeError, ZeroDivisionError, ArithmeticError):
            result = None
        if result is None:
            return
        available += 1
        if result:
            passed += 1
            notes.append(f"+{name}")

    roa = _ratio(f.net_income_ttm, f.total_assets)
    roa_prior = _ratio(f.net_income_prior_ttm, f.total_assets_prior)

    test("ROA positive", lambda: None if roa is None else roa > 0)
    test("operating cash flow positive",
         lambda: None if f.operating_cash_flow_ttm is None else f.operating_cash_flow_ttm > 0)
    test("ROA improving", lambda: None if roa is None or roa_prior is None else roa > roa_prior)
    test("cash flow exceeds earnings",
         lambda: None if f.operating_cash_flow_ttm is None or f.net_income_ttm is None
         else f.operating_cash_flow_ttm > f.net_income_ttm)

    leverage = _ratio(f.total_debt, f.total_assets)
    leverage_prior = _ratio(f.total_debt_prior, f.total_assets_prior)
    test("leverage falling",
         lambda: None if leverage is None or leverage_prior is None else leverage < leverage_prior)

    current = _ratio(f.current_assets, f.current_liabilities)
    current_prior = _ratio(f.current_assets_prior, f.current_liabilities_prior)
    test("current ratio improving",
         lambda: None if current is None or current_prior is None else current > current_prior)

    test("no share issuance",
         lambda: None if f.shares_outstanding is None or f.shares_outstanding_prior is None
         else f.shares_outstanding <= f.shares_outstanding_prior)

    test("gross margin improving",
         lambda: None if f.gross_margin is None or f.gross_margin_prior is None
         else f.gross_margin > f.gross_margin_prior)

    turnover = _ratio(f.revenue_ttm, f.total_assets)
    turnover_prior = _ratio(f.revenue_prior_ttm, f.total_assets_prior)
    test("asset turnover improving",
         lambda: None if turnover is None or turnover_prior is None else turnover > turnover_prior)

    return passed, available, notes


def _quality(f: Fundamentals) -> Component:
    passed, available, notes = piotroski_f_score(f)
    if available < 4:
        # Fewer than four computable tests is not an F-score, it is a rumour.
        return None, f"F-score not computable ({available} of 9 tests available)"
    score = dec(passed) / dec(available) * Decimal("100")
    return score, f"Piotroski {passed}/{available} computable tests" + (
        f" ({', '.join(notes)})" if notes else ""
    )


def score(f: Fundamentals, *, as_of: dt.date | None = None) -> Signal:
    components: dict[str, Component] = {
        "growth": _growth(f),
        "profitability": _profitability(f),
        "balance_sheet": _balance_sheet(f),
        "valuation": _valuation(f),
        "quality": _quality(f),
    }
    available = {k: v for k, v in components.items() if v[0] is not None}
    available_weight = sum(WEIGHTS[k] for k in available)

    if not available:
        return Signal(
            module=ModuleName.FUNDAMENTAL, module_version=FUNDAMENTAL_VERSION,
            ticker=f.ticker, as_of=as_of or f.as_of, score=Decimal("50"),
            confidence=Decimal("0"),
            evidence=(Evidence(key="coverage", value="no fundamental data at all", source="fundamental"),),
            notes="no fundamental data",
        )

    composite = sum(v[0] * WEIGHTS[k] for k, v in available.items()) / available_weight
    composite = max(Decimal("0"), min(Decimal("100"), dec(composite).quantize(Decimal("0.01"))))

    evidence = tuple(
        Evidence(
            key=key, value=label, source="fundamental",
            weight=((value * WEIGHTS[key] / available_weight).quantize(Decimal("0.01"))
                    if value is not None else Decimal("0")),
        )
        for key, (value, label) in components.items()
    )
    return Signal(
        module=ModuleName.FUNDAMENTAL, module_version=FUNDAMENTAL_VERSION, ticker=f.ticker,
        as_of=as_of or f.as_of, score=composite,
        confidence=available_weight.quantize(Decimal("0.01")),
        evidence=evidence,
        notes=f"{len(available)}/5 components available",
    )
