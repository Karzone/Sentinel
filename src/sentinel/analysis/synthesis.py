"""Synthesis module: the LLM writes the memo, the rules decide its fate.

The composite score is computed **before** the model is called and is never
shown to it as a target. If the memo could see "composite 82" it would write
towards it, and the memo would stop being independent evidence about whether the
score is reasonable.

What the model does get is every module's evidence, keyed. It must cite those
keys in ``claims``, and rules.R3 rejects any claim citing a key no module
produced — which turns "did it hallucinate?" into a string comparison rather
than a judgement call.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from decimal import Decimal
from typing import Sequence

from ..domain.enums import Conviction, Direction, IdeaClass, ModuleName
from ..domain.models import (
    CatalystRead, Evidence, Idea, IdeaMemo, SentimentRead, Signal, sequence_digest,
)
from ..llm.client import LlmClient, LlmUnavailable
from ..llm.schemas import MEMO_SCHEMA
from ..money import dec
from . import rules

SYNTHESIS_VERSION = "synthesis-v1"

#: Weight per module in the composite, renormalised over whichever modules ran.
#: Fundamentals lead because the philosophy says long-term ideas are
#: fundamentals-led; sentiment is last and smallest by design.
MODULE_WEIGHTS = {
    ModuleName.FUNDAMENTAL: Decimal("0.40"),
    ModuleName.TECHNICAL: Decimal("0.30"),
    ModuleName.NEWS: Decimal("0.20"),
    ModuleName.SENTIMENT: Decimal("0.10"),
}

SYSTEM = """You write short investment memos for a personal research system.

You will be given the scored output of several analysis modules, each with its
evidence. Write the memo those modules support — not the best case that could be
made for the company.

Hard requirements:
- thesis: at most three sentences. If you cannot say it in three, the modules do
  not agree enough for an idea.
- invalidation: a specific, checkable condition. It must name a number, a price,
  a date, or a named event — "if the thesis breaks" and "if fundamentals
  deteriorate" are rejected automatically by downstream code.
- claims: list the evidence keys you actually relied on. Every key must be one
  of the keys supplied to you. A key you invent causes the memo to be discarded.
- bear_case must be a real case. If you cannot construct one, the idea is not
  ready.

On conviction: "high" is expensive. Downstream evals check whether
high-conviction ideas actually outperform low-conviction ones, so an inflated
label does not flatter the system, it degrades a measurement. Use "high" only
when the modules agree strongly and their data coverage is good.

On idea_class: "long_term" is 6 months to 5 years and must be led by
fundamentals; "swing" is 1 to 8 weeks and must be led by a specific catalyst. A
swing idea with no catalyst is rejected.

You are not being asked whether to buy, and nothing you write executes a trade.
A human reads this memo and decides."""


def composite_score(signals: Sequence[Signal]) -> Decimal:
    """Confidence-weighted mean of the module scores.

    Weighting by confidence as well as by module means a fundamental read with
    only two of five components available cannot dominate a full technical read
    — which is how "we could not see much" stops masquerading as conviction.
    """
    weighted = Decimal("0")
    total = Decimal("0")
    for signal in signals:
        weight = MODULE_WEIGHTS.get(signal.module, Decimal("0")) * signal.confidence
        weighted += signal.score * weight
        total += weight
    if total == 0:
        return Decimal("50")
    return (weighted / total).quantize(Decimal("0.01"))


def collect_evidence(signals: Sequence[Signal]) -> tuple[Evidence, ...]:
    return tuple(e for signal in signals for e in signal.evidence)


def build_prompt(
    ticker: str, signals: Sequence[Signal], as_of: dt.date,
    *, catalyst: CatalystRead | None, sentiment: SentimentRead | None,
) -> str:
    lines = [f"Company: {ticker}", f"Date: {as_of.isoformat()}", "", "MODULE OUTPUTS", ""]
    for signal in signals:
        lines.append(
            f"[{signal.module.value}] score {signal.score}/100, "
            f"confidence {signal.confidence} ({signal.module_version})"
        )
        for item in signal.evidence:
            lines.append(f"  - {item.key}: {item.value}")
        lines.append("")
    if catalyst:
        lines.append(
            f"Catalyst: {catalyst.catalyst_type.value}, direction {catalyst.direction.value}, "
            f"materiality {catalyst.materiality}/5, horizon {catalyst.horizon_days}d"
        )
    if sentiment:
        lines.append(
            f"Sentiment: {sentiment.sentiment:+d} over {sentiment.sample_size} items, "
            f"crowded={sentiment.herding_risk}"
        )
    lines += [
        "",
        "AVAILABLE EVIDENCE KEYS (claims must use these exact keys):",
        ", ".join(sorted({e.key for e in collect_evidence(signals)})) or "(none)",
        "",
        "Write the memo.",
    ]
    return "\n".join(lines)


def write_memo(
    client: LlmClient, ticker: str, signals: Sequence[Signal], as_of: dt.date,
    *, catalyst: CatalystRead | None = None, sentiment: SentimentRead | None = None,
) -> IdeaMemo:
    if not client.available():
        raise LlmUnavailable("synthesis module requires an LLM")
    result = client.complete_json(
        module="synthesis", system=SYSTEM,
        prompt=build_prompt(ticker, signals, as_of, catalyst=catalyst, sentiment=sentiment),
        schema=MEMO_SCHEMA,
    )
    data = result.data
    return IdeaMemo(
        ticker=ticker, thesis=data["thesis"], bull_case=data["bull_case"],
        bear_case=data["bear_case"], invalidation=data["invalidation"],
        idea_class=IdeaClass(data["idea_class"]), conviction=Conviction(data["conviction"]),
        horizon_days=data["horizon_days"], claims=tuple(data["claims"]),
    )


def idea_id(ticker: str, as_of: dt.date, digest: str) -> str:
    return hashlib.sha256(f"{ticker}|{as_of.isoformat()}|{digest}".encode()).hexdigest()[:20]


def inputs_digest(signals: Sequence[Signal], catalyst: CatalystRead | None,
                  sentiment: SentimentRead | None) -> str:
    """§5.4: reproducibility needs the *inputs* pinned, not just the output.

    Sorted inside ``sequence_digest``, so module execution order cannot change
    the digest — two runs that saw the same facts get the same fingerprint.
    """
    parts = [
        json.dumps(signal.model_dump(mode="json"), sort_keys=True, default=str)
        for signal in signals
    ]
    if catalyst:
        parts.append(json.dumps(catalyst.model_dump(mode="json"), sort_keys=True, default=str))
    if sentiment:
        parts.append(json.dumps(sentiment.model_dump(mode="json"), sort_keys=True, default=str))
    return sequence_digest(parts)


def build_idea(
    ticker: str,
    signals: Sequence[Signal],
    as_of: dt.date,
    *,
    memo: IdeaMemo | None = None,
    catalyst: CatalystRead | None = None,
    sentiment: SentimentRead | None = None,
    created_at: dt.datetime | None = None,
) -> Idea:
    """Assemble the immutable record, running the rules over the memo.

    A rejected memo still produces an Idea — stored, with its reasons. Deleting
    rejections would make the rejection rate unmeasurable, and the rejection
    rate is the main evidence that the rules layer is doing anything.
    """
    composite = composite_score(signals)
    evidence = collect_evidence(signals)
    rejections: list[rules.Rejection] = []
    if memo is not None:
        rejections = rules.vet(
            memo, signals=signals, composite=composite, catalyst=catalyst,
            sentiment=sentiment, evidence=evidence,
        )

    digest = inputs_digest(signals, catalyst, sentiment)
    versions = {s.module.value: s.module_version for s in signals}
    versions["synthesis"] = SYNTHESIS_VERSION
    versions["rules"] = rules.RULES_VERSION

    conviction = memo.conviction if memo else rules.conviction_ceiling(composite, signals, sentiment)
    idea_class = memo.idea_class if memo else IdeaClass.LONG_TERM
    direction = Direction.LONG if composite >= rules.LONG_MIN_COMPOSITE else Direction.FLAT
    if rejections:
        direction = Direction.FLAT

    return Idea(
        id=idea_id(ticker, as_of, digest),
        created_at=created_at or dt.datetime.now(dt.UTC),
        as_of=as_of, ticker=ticker, idea_class=idea_class, conviction=conviction,
        direction=direction, signals=tuple(signals), memo=memo, catalyst=catalyst,
        sentiment=sentiment, composite_score=composite,
        rejected_by_rules=tuple(str(r) for r in rejections),
        inputs_digest=digest, model_versions=versions,
    )
