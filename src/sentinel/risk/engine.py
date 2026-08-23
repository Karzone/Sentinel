"""The risk layer. Hard-coded, no LLM, non-negotiable.

Read the imports: nothing from ``analysis/`` or ``llm/`` appears in this module,
and nothing ever should. The engine is handed an ``Idea`` but reads only its
ticker, its class and its invalidation text — never its score, never its
conviction. Two tests exist purely to keep it that way, because the failure this
prevents is not a crash but a slow drift where a "high conviction" idea starts
being allowed a slightly bigger position.

Every check runs. No short-circuiting on the first failure: the spec says
failed checks are logged with reasons, plural, and hiding a second breach behind
the first makes the next run look like a new problem.

§5.5 pre-commits that any month with a risk-layer bypass bug suspends real-money
usage until root cause plus a regression test. That is the standard this file is
held to.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Mapping, Sequence

from ..config import RiskLimits
from ..domain.enums import IdeaClass, Outcome, RiskCheckId
from ..domain.models import Idea, Position, PositionPlan, RiskCheckResult, RiskVerdict
from ..money import GBP, FxRates
from .sizing import Sizing, SizingCap, size

RISK_VERSION = "risk-v1"


@dataclass(slots=True)
class PortfolioState:
    """Everything the engine needs to know about where the money currently is.

    ``satellite_capital`` is the 10-20% satellite allocation, never total net
    worth — every percentage limit below is a fraction of this number. The
    passive core is deliberately outside this system's scope, so a limit that
    accidentally sized against the whole portfolio would be roughly 5-10x too
    permissive.
    """

    satellite_capital: Decimal
    cash: Decimal
    positions: list[Position] = field(default_factory=list)
    nav: Decimal = Decimal("0")
    high_water_mark: Decimal = Decimal("0")
    fx: FxRates = field(default_factory=FxRates.identity)
    #: Optional current prices, instrument currency. Absent -> cost basis.
    marks: Mapping[str, Decimal] = field(default_factory=dict)

    @property
    def open_positions(self) -> list[Position]:
        return [p for p in self.positions if p.is_open]

    def exposure_gbp(self, position: Position) -> Decimal:
        mark = self.marks.get(position.ticker)
        price = mark if mark is not None else position.entry
        return price * Decimal(position.shares) * position.fx_rate_at_entry

    def sector_exposure_gbp(self, sector: str) -> Decimal:
        return sum(
            (self.exposure_gbp(p) for p in self.open_positions if p.sector == sector),
            Decimal("0"),
        )

    def class_exposure_gbp(self, idea_class: IdeaClass) -> Decimal:
        return sum(
            (self.exposure_gbp(p) for p in self.open_positions if p.idea_class is idea_class),
            Decimal("0"),
        )

    def drawdown(self) -> Decimal:
        if self.high_water_mark <= 0:
            return Decimal("0")
        return (self.high_water_mark - self.nav) / self.high_water_mark


class RiskEngine:
    def __init__(self, limits: RiskLimits, *, sectors: Mapping[str, str] | None = None) -> None:
        self.limits = limits
        self.sectors = {k.upper(): v for k, v in (sectors or {}).items()}

    # ---------------------------------------------------------------- helpers
    def sector_of(self, ticker: str) -> str:
        return self.sectors.get(ticker.upper(), "unknown")

    def kill_switch_active(self, state: PortfolioState) -> bool:
        """True once drawdown from the high-water mark *exceeds* the limit.

        Strictly greater than, so a portfolio sitting exactly on the line is not
        halted by a rounding difference. Measured from the high-water mark, not
        from the starting capital: up 50% then down 16% from the peak is a
        kill-switch event even though the account is well ahead.
        """
        return state.drawdown() > self.limits.drawdown_kill_pct / Decimal("100")

    def _pct(self, pct: Decimal, capital: Decimal) -> Decimal:
        return capital * pct / Decimal("100")

    # ---------------------------------------------------------------- checks
    def evaluate(
        self,
        idea: Idea,
        *,
        entry: Decimal,
        stop: Decimal | None,
        state: PortfolioState,
        as_of: dt.date,
        data_stale: bool = False,
        currency: str = GBP,
    ) -> RiskVerdict:
        checks: list[RiskCheckResult] = []
        capital = state.satellite_capital
        sector = self.sector_of(idea.ticker)
        is_swing = idea.idea_class is IdeaClass.SWING

        def add(check: RiskCheckId, ok: bool, reason: str, **detail: object) -> None:
            checks.append(RiskCheckResult(
                check=check, outcome=Outcome.PASS if ok else Outcome.FAIL,
                reason=reason, detail={k: str(v) for k, v in detail.items()},
            ))

        # -- 1. data quality gates everything downstream
        add(RiskCheckId.DATA_FRESHNESS, not data_stale,
            "data is fresh" if not data_stale else
            "the price or fundamentals feeding this idea failed a quality check",
            as_of=as_of)

        # -- 2. mandatory levels
        has_stop = stop is not None
        if is_swing:
            add(RiskCheckId.HAS_STOP, has_stop,
                "stop level present" if has_stop else
                "a short-term idea must carry a stop level")
        else:
            # A long-term idea still needs a level to size against; it is a
            # sizing input rather than a hard exit, and the invalidation is the
            # thing that actually ends the position.
            add(RiskCheckId.HAS_STOP, has_stop,
                "sizing level present" if has_stop else
                "no stop or sizing level supplied, so the position cannot be sized")

        invalidation = (idea.memo.invalidation.strip() if idea.memo else "")
        if is_swing:
            add(RiskCheckId.HAS_INVALIDATION, True,
                "short-term ideas are governed by their stop")
        else:
            add(RiskCheckId.HAS_INVALIDATION, bool(invalidation),
                "invalidation condition written" if invalidation else
                "a long-term idea must carry a written invalidation condition")

        stop_ok = has_stop and stop < entry and stop >= 0
        add(RiskCheckId.STOP_BELOW_ENTRY, stop_ok,
            "stop is below the entry" if stop_ok else
            f"stop {stop} is not below the entry {entry}; the sizing formula needs a "
            "positive stop distance",
            entry=entry, stop=stop)

        # -- 3. portfolio-level gates
        already_held = any(p.ticker == idea.ticker for p in state.open_positions)
        add(RiskCheckId.NO_DUPLICATE_POSITION, not already_held,
            "no open position in this name" if not already_held else
            f"already holding {idea.ticker}; adding to a position is a separate decision")

        open_count = len(state.open_positions)
        room = open_count < self.limits.max_open_positions
        add(RiskCheckId.MAX_OPEN_POSITIONS, room,
            f"{open_count} of {self.limits.max_open_positions} positions open" if room else
            f"{open_count} positions already open, the limit is {self.limits.max_open_positions}")

        kill = self.kill_switch_active(state)
        if is_swing:
            add(RiskCheckId.DRAWDOWN_KILL_SWITCH, not kill,
                f"drawdown {state.drawdown():.1%} is within the "
                f"{self.limits.drawdown_kill_pct}% limit" if not kill else
                f"drawdown {state.drawdown():.1%} exceeds the "
                f"{self.limits.drawdown_kill_pct}% limit — no new short-term ideas until "
                "the portfolio is reviewed and de-risked",
                drawdown=state.drawdown())
        else:
            add(RiskCheckId.DRAWDOWN_KILL_SWITCH, True,
                "the kill switch halts short-term ideas only; a long-term thesis is not "
                "invalidated by the portfolio being down",
                drawdown=state.drawdown())

        # -- 4. sizing, and the caps that bind it
        single_cap = self._pct(self.limits.max_single_position_pct, capital)
        sector_headroom = self._pct(self.limits.max_sector_pct, capital) - state.sector_exposure_gbp(sector)
        swing_headroom = (
            self._pct(self.limits.swing_max_pct, capital) - state.class_exposure_gbp(IdeaClass.SWING)
        )

        caps = [SizingCap("max_single_position", single_cap),
                SizingCap("max_sector_concentration", sector_headroom),
                SizingCap("sufficient_cash", state.cash)]
        if is_swing:
            caps.append(SizingCap("swing_sub_allocation", swing_headroom))

        fx_rate = Decimal("1")
        if currency != GBP:
            fx_rate = Decimal(str(state.fx.rates.get(currency, Decimal("1"))))
        elif idea.ticker.upper().endswith((".US", ".NYSE", ".NASDAQ")):
            # The ticker suffix is the fallback when the caller did not say.
            fx_rate = Decimal(str(state.fx.rates.get("USD", Decimal("1"))))

        sizing: Sizing = size(
            entry=entry, stop=stop if stop is not None else Decimal("-1"),
            satellite_capital=capital, risk_per_trade_pct=self.limits.risk_per_trade_pct,
            fx_rate=fx_rate, caps=tuple(caps),
        )

        minimum = self.limits.min_position_gbp
        add(RiskCheckId.MAX_SINGLE_POSITION, sizing.gbp_exposure <= single_cap,
            f"{sizing.gbp_exposure} is within the {self.limits.max_single_position_pct}% "
            f"single-position cap of {single_cap}",
            exposure=sizing.gbp_exposure, cap=single_cap)

        sector_ok = sector_headroom >= minimum
        add(RiskCheckId.MAX_SECTOR_CONCENTRATION, sector_ok,
            f"sector '{sector}' has {sector_headroom} of headroom" if sector_ok else
            f"sector '{sector}' is at {state.sector_exposure_gbp(sector)} against a "
            f"{self.limits.max_sector_pct}% cap of "
            f"{self._pct(self.limits.max_sector_pct, capital)}; no room for a viable position",
            sector=sector, headroom=sector_headroom)

        if is_swing:
            swing_ok = swing_headroom >= minimum
            add(RiskCheckId.SWING_SUB_ALLOCATION, swing_ok,
                f"short-term book has {swing_headroom} of headroom" if swing_ok else
                f"short-term book is at {state.class_exposure_gbp(IdeaClass.SWING)} against a "
                f"{self.limits.swing_max_pct}% cap; short-term ideas are capped well below "
                "long-term ones because that is where retail losses concentrate",
                headroom=swing_headroom)

        cash_ok = state.cash >= minimum
        add(RiskCheckId.SUFFICIENT_CASH, cash_ok,
            f"{state.cash} available" if cash_ok else
            f"{state.cash} available, below the {minimum} minimum position size",
            cash=state.cash)

        size_ok = sizing.shares > 0 and sizing.gbp_exposure >= minimum
        add(RiskCheckId.POSITION_SIZE_POSITIVE, size_ok,
            f"{sizing.shares} shares, {sizing.gbp_exposure} exposure, "
            f"{sizing.gbp_risk} at risk" if size_ok else
            f"sized position is {sizing.gbp_exposure}, below the {minimum} minimum "
            f"(binding constraint: {sizing.binding_cap or 'the risk budget'})",
            shares=sizing.shares, exposure=sizing.gbp_exposure,
            binding=sizing.binding_cap or "risk_budget")

        approved = all(c.passed for c in checks)
        plan = None
        if approved:
            plan = PositionPlan(
                ticker=idea.ticker, entry=entry, currency=currency,
                stop=stop, shares=sizing.shares, gbp_exposure=sizing.gbp_exposure,
                gbp_risk=sizing.gbp_risk,
                fraction_of_satellite=(sizing.gbp_exposure / capital).quantize(Decimal("0.0001"))
                if capital else Decimal("0"),
                fx_rate_used=fx_rate,
            )
        return RiskVerdict(approved=approved, checks=tuple(checks), plan=plan)

    def review_required(self, state: PortfolioState) -> str | None:
        """The 'review and de-risk' brief the kill switch is supposed to issue."""
        if not self.kill_switch_active(state):
            return None
        return (
            f"Satellite drawdown is {state.drawdown():.1%} from a high-water mark of "
            f"{state.high_water_mark}, past the {self.limits.drawdown_kill_pct}% limit. "
            "No new short-term ideas will be issued. Review every open position against "
            "its invalidation condition and de-risk deliberately rather than waiting."
        )

    def stops_hit(
        self, state: PortfolioState, marks: Mapping[str, Decimal]
    ) -> list[tuple[Position, Decimal]]:
        """Open positions whose stop has been breached at the supplied marks."""
        hit: list[tuple[Position, Decimal]] = []
        for position in state.open_positions:
            mark = marks.get(position.ticker)
            if mark is not None and position.stop is not None and mark <= position.stop:
                hit.append((position, mark))
        return hit

    def approaching_stop(
        self, state: PortfolioState, marks: Mapping[str, Decimal], *, within: Decimal = Decimal("0.05")
    ) -> list[tuple[Position, Decimal]]:
        """Positions within ``within`` of their stop — the brief's watch list."""
        close: list[tuple[Position, Decimal]] = []
        for position in state.open_positions:
            mark = marks.get(position.ticker)
            if mark is None or position.stop is None or mark <= position.stop:
                continue
            distance = (mark - position.stop) / mark
            if distance <= within:
                close.append((position, distance))
        return close


def sector_allocation(state: PortfolioState) -> dict[str, Decimal]:
    """Current sector weights as fractions of satellite capital, for the brief."""
    if state.satellite_capital <= 0:
        return {}
    out: dict[str, Decimal] = {}
    for position in state.open_positions:
        out[position.sector] = out.get(position.sector, Decimal("0")) + state.exposure_gbp(position)
    return {
        sector: (value / state.satellite_capital).quantize(Decimal("0.0001"))
        for sector, value in sorted(out.items())
    }
