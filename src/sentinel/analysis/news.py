"""News & catalyst module (LLM).

The model's job here is narrow and it is stated narrowly: read company-tagged
headlines and say what *kind* of event this is, which way it points, how much it
matters and over what horizon. It is not asked whether to buy.

``materiality`` is the field §5.2 calibrates quarterly — do materiality-5 events
actually move price more than materiality-1 events? — so the prompt anchors the
scale with concrete examples rather than leaving "5" to mean whatever the model
feels. An uncalibrated scale would make that eval unfalsifiable.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Sequence

from ..domain.enums import CatalystType, Direction, ModuleName
from ..domain.models import CatalystRead, Evidence, NewsItem, Signal
from ..llm.client import LlmClient, LlmUnavailable
from ..llm.schemas import CATALYST_SCHEMA

NEWS_VERSION = "news-v1"

SYSTEM = """You classify company news for an investment research system.

You are reading headlines that are already tagged to one company. Your job is to
say what kind of event this is, which way it points for the share price, how
material it is, and over what horizon — nothing else. You are not deciding
whether to buy, and no downstream code will read your output as a
recommendation.

The materiality scale is fixed. Anchor to these, do not drift:
  1  Routine. Broker note, minor contract, personnel below board level.
  2  Notable but not case-changing. Small acquisition, regional expansion.
  3  Moves estimates. In-line results with a changed outlook, mid-size contract.
  4  Changes the investment case for a year. Guidance cut or raise, CEO change,
     a failed trial, a large acquisition.
  5  Changes what the company is. Takeover approach, going-concern doubt,
     regulatory action that removes a licence to operate, an emergency placing.

Rules:
- Base your read only on the headlines given. Do not use anything you recall
  about this company from elsewhere; if the headlines do not support a read,
  return materiality 1 and direction "flat".
- "flat" is a real answer. Use it when the news is genuine but the direction is
  genuinely ambiguous. Do not manufacture a direction to seem useful.
- horizon_days is when the effect should express itself, not how long you would
  hold. An earnings surprise is days; a regulatory review is months.
- headline_refs must quote the headlines you actually used, verbatim."""


def build_prompt(ticker: str, items: Sequence[NewsItem], as_of: dt.date) -> str:
    lines = [f"Company: {ticker}", f"Today: {as_of.isoformat()}", "", "Headlines (newest first):"]
    for item in items:
        age = (as_of - item.published_at.date()).days
        lines.append(f"- [{age}d ago] {item.headline}")
        if item.summary:
            lines.append(f"    {item.summary}")
    lines.append("")
    lines.append("Classify the single most important catalyst in this set.")
    return "\n".join(lines)


def read_catalyst(
    client: LlmClient, ticker: str, items: Sequence[NewsItem], as_of: dt.date
) -> CatalystRead | None:
    """None when there is no news or no LLM — never a fabricated neutral read."""
    if not items:
        return None
    if not client.available():
        raise LlmUnavailable("news module requires an LLM")
    result = client.complete_json(
        module="news", system=SYSTEM, prompt=build_prompt(ticker, items, as_of),
        schema=CATALYST_SCHEMA,
    )
    data = result.data
    return CatalystRead(
        ticker=ticker, as_of=as_of,
        catalyst_type=CatalystType(data["catalyst_type"]),
        direction=Direction(data["direction"]),
        materiality=data["materiality"],
        horizon_days=data["horizon_days"],
        summary=data["summary"],
        headline_refs=tuple(data["headline_refs"]),
    )


def to_signal(catalyst: CatalystRead) -> Signal:
    """Catalyst -> 0-100, deterministically.

    Kept out of the model on purpose: the LLM supplies direction and
    materiality, and the *arithmetic* that turns them into a score is code, so
    the mapping cannot drift between runs or between model versions.
    """
    step = Decimal(catalyst.materiality) * Decimal("8")
    if catalyst.direction is Direction.LONG:
        score = Decimal("50") + step
    elif catalyst.direction is Direction.AVOID:
        score = Decimal("50") - step
    else:
        score = Decimal("50")
    score = max(Decimal("0"), min(Decimal("100"), score))
    # A materiality-1 read is nearly no information, so it should not carry the
    # same weight in the composite as a materiality-5 one.
    confidence = (Decimal(catalyst.materiality) / Decimal("5")).quantize(Decimal("0.01"))
    return Signal(
        module=ModuleName.NEWS, module_version=NEWS_VERSION, ticker=catalyst.ticker,
        as_of=catalyst.as_of, score=score, confidence=confidence,
        evidence=(
            Evidence(key="catalyst", value=f"{catalyst.catalyst_type.value}: {catalyst.summary}",
                     source="news"),
            Evidence(key="catalyst_materiality",
                     value=f"materiality {catalyst.materiality}/5 over {catalyst.horizon_days} days",
                     source="news"),
        ),
        notes=catalyst.summary,
    )
