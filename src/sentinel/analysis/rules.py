"""The deterministic vetting layer: the LLM proposes, the rules dispose.

Every rule here rejects a memo the synthesis model was perfectly happy with.
They exist because a language model asked to write an investment case will
always write one — fluency is not a filter — and the failure modes are
predictable enough to encode.

A rejection is not a bug and is not silent: the memo is stored with its
``rejected_by_rules`` reasons and shows up in the brief's rejected section and
in the audit trail, which is what makes rule R3 (hallucinated claims) an
observable rate rather than an anecdote.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Sequence

from ..domain.enums import Conviction, Direction, IdeaClass
from ..domain.models import CatalystRead, Evidence, IdeaMemo, SentimentRead, Signal

RULES_VERSION = "rules-v1"

#: A high-conviction label has to be paid for. §5.3 checks that high-conviction
#: ideas actually outperform low-conviction ones; letting the model award the
#: label freely would guarantee that eval comes back as noise.
HIGH_CONVICTION_MIN_SCORE = Decimal("70")
HIGH_CONVICTION_MIN_CONFIDENCE = Decimal("0.7")
#: A long idea needs the deterministic modules on side, whatever the prose says.
LONG_MIN_COMPOSITE = Decimal("50")
#: Swing ideas are catalyst-led by definition, so a swing with no material
#: catalyst is a momentum guess wearing a thesis.
SWING_MIN_MATERIALITY = 3

HORIZON_BANDS = {
    IdeaClass.SWING: (7, 56),          # 1-8 weeks
    IdeaClass.LONG_TERM: (180, 1825),  # 6 months - 5 years
}

#: An invalidation must be checkable. These are the shapes a checkable one takes:
#: a number, a percentage, a price, a date, a quarter, or a named event.
_FALSIFIABLE = re.compile(
    r"(\d+(\.\d+)?\s*%)|([£$€]\s?\d)|(\b\d{4}-\d{2}-\d{2}\b)|(\bQ[1-4]\b)"
    r"|(\bbelow\b|\babove\b|\bfalls?\b|\brises?\b|\bmisses\b|\bbreaches\b|\bloses\b)\s+[^.]*\d"
    r"|(\bH[12]\b)|(\bFY\d{2,4}\b)",
    re.IGNORECASE,
)
_VAGUE = re.compile(
    r"\b(if the (thesis|story) (breaks|changes)|things (get|go) worse|sentiment (turns|sours)"
    r"|fundamentals deteriorate|the outlook (worsens|deteriorates)|management disappoints)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class Rejection:
    rule: str
    reason: str

    def __str__(self) -> str:
        return f"{self.rule}: {self.reason}"


def _sentence_count(text: str) -> int:
    return len([s for s in re.split(r"[.!?]+", text) if s.strip()])


def vet(
    memo: IdeaMemo,
    *,
    signals: Sequence[Signal],
    composite: Decimal,
    catalyst: CatalystRead | None = None,
    sentiment: SentimentRead | None = None,
    evidence: Sequence[Evidence] = (),
) -> list[Rejection]:
    """Return every reason this memo must not become an idea. Empty means pass."""
    rejections: list[Rejection] = []
    by_module = {s.module.value: s for s in signals}

    # R1 — sentiment is a contrarian or confirming input, never a primary buy
    # reason. The check is structural: strip sentiment out and see whether
    # anything is left standing.
    if sentiment is not None and sentiment.sentiment > 0:
        deterministic = [
            by_module[name].score for name in ("fundamental", "technical") if name in by_module
        ]
        if deterministic and max(deterministic) < LONG_MIN_COMPOSITE:
            rejections.append(Rejection(
                "R1", "positive sentiment is the only thing supporting this — neither the "
                      "fundamental nor the technical module is above neutral",
            ))

    # R1b — a crowded name is exactly where sentiment inverts.
    if sentiment is not None and sentiment.herding_risk and memo.conviction is Conviction.HIGH:
        rejections.append(Rejection(
            "R1b", "high conviction on a name flagged as crowded; herding risk caps conviction "
                   "at medium",
        ))

    # R2 — the invalidation must be falsifiable. §5.2 scores this with an LLM
    # judge; this is the deterministic floor beneath that judgement.
    if not _FALSIFIABLE.search(memo.invalidation):
        rejections.append(Rejection(
            "R2", f"invalidation is not checkable — it names no number, price, date or "
                  f"specific event: {memo.invalidation!r}",
        ))
    elif _VAGUE.search(memo.invalidation):
        rejections.append(Rejection(
            "R2b", f"invalidation restates the thesis rather than falsifying it: "
                   f"{memo.invalidation!r}",
        ))

    # R3 — every claim must trace to a module output. This is the hallucination
    # guard, and it is the reason `claims` is in the memo schema at all.
    known = {e.key for e in evidence}
    unknown = [c for c in memo.claims if c not in known]
    if unknown:
        rejections.append(Rejection(
            "R3", f"claims cite evidence that no module produced: {unknown}",
        ))
    if not memo.claims:
        rejections.append(Rejection("R3b", "memo cites no module evidence at all"))

    # R4 — a swing idea is catalyst-led by definition.
    if memo.idea_class is IdeaClass.SWING:
        if catalyst is None:
            rejections.append(Rejection("R4", "swing idea with no catalyst"))
        elif catalyst.materiality < SWING_MIN_MATERIALITY:
            rejections.append(Rejection(
                "R4", f"swing idea on a materiality-{catalyst.materiality} catalyst "
                      f"(needs {SWING_MIN_MATERIALITY}+)",
            ))
        elif catalyst.direction is Direction.AVOID:
            rejections.append(Rejection(
                "R4b", "swing idea built on a catalyst the news module reads as negative",
            ))

    # R5 — three sentences, as specified. A memo that sprawls has not been
    # forced to decide what the thesis actually is.
    if _sentence_count(memo.thesis) > 3:
        rejections.append(Rejection(
            "R5", f"thesis is {_sentence_count(memo.thesis)} sentences; the limit is 3",
        ))

    # R6 — conviction must be earned from the deterministic scores.
    if memo.conviction is Conviction.HIGH:
        if composite < HIGH_CONVICTION_MIN_SCORE:
            rejections.append(Rejection(
                "R6", f"high conviction on a composite of {composite} "
                      f"(needs {HIGH_CONVICTION_MIN_SCORE})",
            ))
        weakest = min((s.confidence for s in signals), default=Decimal("0"))
        if weakest < HIGH_CONVICTION_MIN_CONFIDENCE:
            rejections.append(Rejection(
                "R6b", f"high conviction while a module is only {weakest} confident "
                       f"(needs {HIGH_CONVICTION_MIN_CONFIDENCE})",
            ))

    # R7 — the class and the horizon must agree, or the risk layer applies the
    # wrong sub-allocation cap to it.
    low, high = HORIZON_BANDS[memo.idea_class]
    if not (low <= memo.horizon_days <= high):
        rejections.append(Rejection(
            "R7", f"{memo.idea_class.value} idea with a {memo.horizon_days}-day horizon "
                  f"(expected {low}-{high})",
        ))

    # R8 — the prose cannot outvote the numbers.
    if composite < LONG_MIN_COMPOSITE:
        rejections.append(Rejection(
            "R8", f"composite score {composite} is below neutral; no long idea from a "
                  "below-average setup",
        ))

    return rejections


def conviction_ceiling(
    composite: Decimal, signals: Sequence[Signal], sentiment: SentimentRead | None
) -> Conviction:
    """The highest conviction the numbers would support.

    Used to *report* what the model over-claimed, so a rejection can say what it
    should have said rather than only that it was wrong.
    """
    if sentiment is not None and sentiment.herding_risk:
        return Conviction.MEDIUM
    weakest = min((s.confidence for s in signals), default=Decimal("0"))
    if composite >= HIGH_CONVICTION_MIN_SCORE and weakest >= HIGH_CONVICTION_MIN_CONFIDENCE:
        return Conviction.HIGH
    if composite >= LONG_MIN_COMPOSITE:
        return Conviction.MEDIUM
    return Conviction.LOW
