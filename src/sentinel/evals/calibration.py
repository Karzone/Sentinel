"""Calibration evals (§5.3) — "the most important ones", per the spec.

Everything here answers a variant of one question: *do the system's own labels
mean anything?* A conviction scale where high-conviction ideas do not outperform
low-conviction ones is not a weak signal, it is a broken instrument, and every
downstream decision made using it — including position sizing, if conviction
were ever allowed to touch it — is uninformed.

These evals are written to be able to return a negative verdict in plain
language. That is the whole point of pre-committing to them.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Mapping, Sequence

from .signal_quality import Interval, wilson_interval

CALIBRATION_VERSION = "calibration-v1"


# ---------------------------------------------------------------- Brier


@dataclass(frozen=True, slots=True)
class BrierResult:
    samples: int
    score: float | None
    #: Brier of always predicting the base rate — the bar to beat.
    baseline: float | None
    skill: float | None          # 1 - score/baseline; > 0 is informative

    def verdict(self) -> str:
        if self.score is None:
            return "no probabilistic calls to score"
        if self.skill is None or self.baseline == 0:
            return f"Brier {self.score:.3f} on {self.samples} calls"
        if self.skill > 0:
            return (f"Brier {self.score:.3f} vs {self.baseline:.3f} for always guessing the "
                    f"base rate — skill score {self.skill:+.2f}, informative")
        return (f"Brier {self.score:.3f} vs {self.baseline:.3f} for always guessing the base "
                f"rate — skill score {self.skill:+.2f}. The stated probabilities carry no "
                f"information beyond the base rate.")


def brier_score(calls: Sequence[tuple[float, bool]]) -> BrierResult:
    """Mean squared error of stated probabilities against outcomes.

    Reported against a base-rate baseline rather than alone. A raw Brier of 0.20
    sounds respectable and can still be *worse* than always predicting the base
    rate, which is the comparison that matters.
    """
    if not calls:
        return BrierResult(0, None, None, None)
    score = sum((p - (1.0 if outcome else 0.0)) ** 2 for p, outcome in calls) / len(calls)
    base_rate = sum(1 for _, outcome in calls if outcome) / len(calls)
    baseline = sum((base_rate - (1.0 if outcome else 0.0)) ** 2 for _, outcome in calls) / len(calls)
    skill = None if baseline == 0 else 1.0 - score / baseline
    return BrierResult(len(calls), score, baseline, skill)


# ---------------------------------------------------------------- conviction


@dataclass(frozen=True, slots=True)
class ConvictionBand:
    label: str
    samples: int
    mean_return: float
    win_rate: float | None
    interval: Interval | None


@dataclass(frozen=True, slots=True)
class ConvictionCalibration:
    bands: list[ConvictionBand]
    ordered: bool | None
    high_minus_low: float | None

    def verdict(self) -> str:
        if self.ordered is None:
            return "not enough conviction bands populated to judge"
        if self.ordered:
            return (f"conviction is calibrated: high-conviction ideas returned "
                    f"{self.high_minus_low:+.1%} more than low-conviction ones")
        return (
            "conviction labels are NOT calibrated — high-conviction ideas did not outperform "
            "low-conviction ones. Per §5.3 the labels are noise until the prompts are re-tuned; "
            "do not read them as information in the meantime."
        )


_ORDER = {"low": 0, "medium": 1, "high": 2}


def conviction_calibration(
    outcomes: Sequence[tuple[str, float]],
) -> ConvictionCalibration:
    """(conviction label, realised return) -> is the ordering real?

    Rolling 3-month windows are the spec's cadence; this function scores one
    window and the report chains them.
    """
    grouped: dict[str, list[float]] = {}
    for label, value in outcomes:
        grouped.setdefault(label.lower(), []).append(value)

    bands: list[ConvictionBand] = []
    for label in sorted(grouped, key=lambda k: _ORDER.get(k, 99)):
        values = grouped[label]
        wins = sum(1 for v in values if v > 0)
        bands.append(ConvictionBand(
            label=label, samples=len(values),
            mean_return=sum(values) / len(values),
            win_rate=wins / len(values),
            interval=wilson_interval(wins, len(values)),
        ))

    known = [b for b in bands if b.label in _ORDER]
    if len(known) < 2:
        return ConvictionCalibration(bands, None, None)
    means = [b.mean_return for b in known]
    ordered = all(earlier <= later for earlier, later in zip(means, means[1:]))
    return ConvictionCalibration(bands, ordered, means[-1] - means[0])


# ---------------------------------------------------------------- stops


@dataclass(frozen=True, slots=True)
class StopQuality:
    stopped: int
    recovered: int
    kept_falling: int
    recovery_rate: float | None
    interval: Interval | None

    def verdict(self) -> str:
        if self.recovery_rate is None:
            return "no stopped-out positions to judge"
        if self.recovery_rate > 0.6:
            return (
                f"{self.recovery_rate:.0%} of stopped positions recovered past the stop within "
                f"the review window {self.interval} — the stops are harvesting noise rather than "
                f"cutting losses. Widen them or size smaller."
            )
        if self.recovery_rate < 0.35:
            return (f"{self.recovery_rate:.0%} of stopped positions recovered {self.interval} — "
                    f"the stops are mostly catching real breaks")
        return (f"{self.recovery_rate:.0%} of stopped positions recovered {self.interval} — "
                f"no clear signal either way at this sample size")


def stop_quality(
    outcomes: Sequence[tuple[Decimal, Decimal]],
) -> StopQuality:
    """(stop level, best price reached after the stop) -> were the stops noise?

    §5.3's question. A high recovery rate is not a vindication of the stop, it is
    evidence the stop was too tight: the position was closed on volatility that
    the thesis would have survived, and the transaction cost was paid for
    nothing.
    """
    if not outcomes:
        return StopQuality(0, 0, 0, None, None)
    recovered = sum(1 for stop, best in outcomes if best > stop)
    total = len(outcomes)
    return StopQuality(
        stopped=total, recovered=recovered, kept_falling=total - recovered,
        recovery_rate=recovered / total, interval=wilson_interval(recovered, total),
    )


# ---------------------------------------------------------------- kill criteria


@dataclass(frozen=True, slots=True)
class KillCriteria:
    """§5.5, evaluated mechanically so future-you cannot rationalise past it."""

    paper_months: float
    strategy_sharpe: float | None
    benchmark_sharpe: float | None
    strategy_return: float | None
    benchmark_return: float | None
    catalyst_samples: int
    catalyst_beats_coin_flip: bool | None
    risk_bypass_bugs: int = 0

    @property
    def short_term_module_demoted(self) -> bool | None:
        """Sharpe below B1's AND total return below B1's, after 6 months."""
        if self.paper_months < 6:
            return None
        if None in (self.strategy_sharpe, self.benchmark_sharpe,
                    self.strategy_return, self.benchmark_return):
            return None
        return (self.strategy_sharpe < self.benchmark_sharpe
                and self.strategy_return < self.benchmark_return)

    @property
    def catalyst_module_demoted(self) -> bool | None:
        if self.catalyst_samples < 100:
            return None
        return self.catalyst_beats_coin_flip is False

    @property
    def real_money_suspended(self) -> bool:
        return self.risk_bypass_bugs > 0

    def verdicts(self) -> list[str]:
        out: list[str] = []
        if self.paper_months < 6:
            out.append(
                f"Paper trading is {self.paper_months:.1f} months in; the gate is 6. No "
                f"real-money use of short-term signals before then, whatever the numbers say."
            )
        elif self.short_term_module_demoted:
            out.append(
                "KILL CRITERION MET: after 6 months of paper trading the strategy's Sharpe and "
                "total return are both below the benchmark's. The short-term module is demoted "
                "to market insights only and the satellite capital stays indexed."
            )
        elif self.short_term_module_demoted is False:
            out.append("Six-month paper gate passed: risk-adjusted return is not below the benchmark.")
        else:
            # Past six months, but one of the four inputs is missing. Saying
            # nothing here is the worst option: it reads identically to "the gate
            # passed" while actually meaning the gate was never evaluated.
            missing = [
                name for name, value in (
                    ("strategy Sharpe", self.strategy_sharpe),
                    ("benchmark Sharpe", self.benchmark_sharpe),
                    ("strategy return", self.strategy_return),
                    ("benchmark return", self.benchmark_return),
                ) if value is None
            ]
            out.append(
                f"Paper trading is {self.paper_months:.1f} months in, past the 6-month gate, "
                f"but the comparison CANNOT be evaluated: {', '.join(missing)} unavailable. "
                f"The gate is untested, which is not the same as passed."
            )

        if self.catalyst_module_demoted:
            out.append(
                f"KILL CRITERION MET: catalyst direction accuracy is indistinguishable from a "
                f"coin flip over {self.catalyst_samples} samples. The module becomes "
                f"summary-only and loses its directional field."
            )
        elif self.catalyst_samples < 100:
            out.append(
                f"Catalyst module has {self.catalyst_samples} of the 100 samples needed for a verdict."
            )

        if self.real_money_suspended:
            out.append(
                f"KILL CRITERION MET: {self.risk_bypass_bugs} risk-layer bypass bug(s) this month. "
                f"Real-money usage is suspended until root cause plus a regression test ship."
            )
        return out


def evaluate_kill_criteria(**kwargs: object) -> KillCriteria:
    return KillCriteria(**kwargs)  # type: ignore[arg-type]


def summarise_calibration(
    *,
    conviction: ConvictionCalibration | None = None,
    brier: BrierResult | None = None,
    stops: StopQuality | None = None,
) -> Mapping[str, str]:
    out: dict[str, str] = {}
    if conviction:
        out["conviction"] = conviction.verdict()
    if brier:
        out["brier"] = brier.verdict()
    if stops:
        out["stops"] = stops.verdict()
    return out
