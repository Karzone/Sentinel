"""Per-module signal-quality evals (§5.2).

The rule this module exists to serve: a hit rate without a confidence interval
is not a result. Sixty percent on ten samples and sixty percent on four hundred
are the same number and completely different evidence, and only one of them
justifies letting a module keep its directional field.

Intervals are Wilson, not the normal approximation. At the sample sizes a
personal system actually reaches — thirty or forty catalyst calls in a quarter —
the normal approximation is badly wrong near the ends and can produce a lower
bound below zero, which reads as reassuring nonsense.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable, Mapping, Sequence

SIGNAL_QUALITY_VERSION = "signal-quality-v1"

#: Two-sided 95%.
Z_95 = 1.959964


@dataclass(frozen=True, slots=True)
class Interval:
    low: float
    high: float

    def excludes(self, value: float) -> bool:
        return value < self.low or value > self.high

    def __str__(self) -> str:
        return f"[{self.low:.1%}, {self.high:.1%}]"


def wilson_interval(successes: int, trials: int, *, z: float = Z_95) -> Interval | None:
    """Wilson score interval for a binomial proportion."""
    if trials <= 0:
        return None
    p = successes / trials
    denominator = 1 + z * z / trials
    centre = (p + z * z / (2 * trials)) / denominator
    margin = (z * math.sqrt(p * (1 - p) / trials + z * z / (4 * trials * trials))) / denominator
    return Interval(max(0.0, centre - margin), min(1.0, centre + margin))


@dataclass(frozen=True, slots=True)
class DirectionalCall:
    """One catalyst call and what the market did over its stated horizon."""

    ticker: str
    predicted: str            # "long" | "avoid" | "flat"
    realised_return: float    # over horizon_days
    materiality: int = 3
    horizon_days: int = 30

    @property
    def scoreable(self) -> bool:
        """"flat" makes no directional claim, so scoring it as right or wrong
        would let a module farm accuracy by never committing."""
        return self.predicted in ("long", "avoid")

    @property
    def correct(self) -> bool:
        if self.predicted == "long":
            return self.realised_return > 0
        return self.realised_return < 0


@dataclass(frozen=True, slots=True)
class DirectionAccuracy:
    calls: int
    scoreable: int
    correct: int
    hit_rate: float | None
    interval: Interval | None
    #: True when the interval excludes 50% — i.e. distinguishable from a coin.
    beats_coin_flip: bool | None
    abstention_rate: float | None

    def verdict(self) -> str:
        if self.hit_rate is None:
            return "no directional calls to score"
        if self.scoreable < 100:
            return (
                f"{self.hit_rate:.0%} on {self.scoreable} calls {self.interval} — "
                f"below the 100-sample threshold the kill criteria specify, so no verdict yet"
            )
        if self.beats_coin_flip and self.hit_rate > 0.5:
            return f"{self.hit_rate:.0%} on {self.scoreable} calls {self.interval} — better than chance"
        if self.beats_coin_flip:
            return (
                f"{self.hit_rate:.0%} on {self.scoreable} calls {self.interval} — "
                f"significantly WORSE than a coin flip"
            )
        return (
            f"{self.hit_rate:.0%} on {self.scoreable} calls {self.interval} — statistically "
            f"indistinguishable from a coin flip. Per the kill criteria this module becomes "
            f"summary-only and loses its directional field."
        )


def direction_accuracy(calls: Sequence[DirectionalCall], *, z: float = Z_95) -> DirectionAccuracy:
    scoreable = [c for c in calls if c.scoreable]
    if not scoreable:
        return DirectionAccuracy(len(calls), 0, 0, None, None, None,
                                 (1.0 if calls else None))
    correct = sum(1 for c in scoreable if c.correct)
    interval = wilson_interval(correct, len(scoreable), z=z)
    return DirectionAccuracy(
        calls=len(calls), scoreable=len(scoreable), correct=correct,
        hit_rate=correct / len(scoreable), interval=interval,
        beats_coin_flip=interval.excludes(0.5) if interval else None,
        abstention_rate=1.0 - len(scoreable) / len(calls),
    )


@dataclass(frozen=True, slots=True)
class BucketStat:
    bucket: int
    samples: int
    mean_abs_move: float


@dataclass(frozen=True, slots=True)
class MaterialityCalibration:
    buckets: list[BucketStat]
    monotonic: bool | None
    spread: float | None

    def verdict(self) -> str:
        if self.monotonic is None:
            return "not enough buckets populated to judge materiality calibration"
        if self.monotonic:
            return "materiality is calibrated: higher ratings do move price more"
        return (
            "materiality is NOT calibrated — higher ratings do not move price more than "
            "lower ones. The scale is decorative until the prompt anchors are re-tuned."
        )


def materiality_calibration(calls: Sequence[DirectionalCall]) -> MaterialityCalibration:
    """§5.2's quarterly bucket analysis: do 5s move price more than 1s?

    Absolute move, because materiality is a claim about *magnitude*, not
    direction — direction is scored separately and mixing the two would let a
    well-calibrated magnitude scale look broken because of a directional miss.
    """
    grouped: dict[int, list[float]] = {}
    for call in calls:
        grouped.setdefault(call.materiality, []).append(abs(call.realised_return))
    buckets = [
        BucketStat(bucket=level, samples=len(values), mean_abs_move=sum(values) / len(values))
        for level, values in sorted(grouped.items())
    ]
    if len(buckets) < 2:
        return MaterialityCalibration(buckets, None, None)
    means = [b.mean_abs_move for b in buckets]
    monotonic = all(earlier <= later for earlier, later in zip(means, means[1:]))
    return MaterialityCalibration(buckets, monotonic, means[-1] - means[0])


@dataclass(frozen=True, slots=True)
class ConsistencyResult:
    runs: int
    identical: int
    rate: float | None
    #: §5.2 target for the sentiment module at temperature 0.
    meets_target: bool | None

    def verdict(self) -> str:
        if self.rate is None:
            return "need at least two runs to measure consistency"
        note = "" if self.meets_target else " — below the 90% target"
        return f"{self.rate:.0%} of repeat runs returned the same score{note}"


def inter_run_consistency(runs: Sequence[Mapping[str, object]], *, key: str,
                          target: float = 0.90) -> ConsistencyResult:
    """Same inputs, repeated runs: how often is the scored field identical?

    This is the measurement that replaces trusting `temperature=0` — which the
    current model family does not accept as a parameter at all. Measuring what
    the pipeline actually produces is the right way round regardless.
    """
    if len(runs) < 2:
        return ConsistencyResult(len(runs), 0, None, None)
    baseline = runs[0].get(key)
    identical = sum(1 for run in runs[1:] if run.get(key) == baseline)
    rate = identical / (len(runs) - 1)
    return ConsistencyResult(len(runs), identical, rate, rate >= target)


def schema_compliance_verdict(stats: Mapping[str, object], *, target: float = 0.99) -> str:
    """§5.2's >= 99% after retry."""
    rate = stats.get("rate")
    if rate is None:
        return "no LLM calls recorded yet"
    first = stats.get("first_pass_rate")
    first_text = f", {first:.0%} first-pass" if isinstance(first, float) else ""
    status = "meets" if float(rate) >= target else "BELOW"
    return f"{float(rate):.1%} schema compliance after retry{first_text} — {status} the {target:.0%} target"


def realised_return(prices: Sequence[Decimal | float], *, horizon: int) -> float | None:
    """Return over the next ``horizon`` observations, for scoring a call."""
    if len(prices) <= horizon:
        return None
    start = float(prices[0])
    if start <= 0:
        return None
    return float(prices[horizon]) / start - 1.0


def build_calls(
    records: Iterable[Mapping[str, object]],
) -> list[DirectionalCall]:
    """Turn stored catalyst reads plus realised moves into scoreable calls."""
    out: list[DirectionalCall] = []
    for record in records:
        move = record.get("realised_return")
        if move is None:
            continue
        out.append(DirectionalCall(
            ticker=str(record.get("ticker", "?")),
            predicted=str(record.get("direction", "flat")),
            realised_return=float(move),
            materiality=int(record.get("materiality", 3)),
            horizon_days=int(record.get("horizon_days", 30)),
        ))
    return out
