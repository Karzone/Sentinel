"""Sentiment module (LLM).

The philosophy line this module exists to obey: *sentiment is a contrarian or
confirmation input, never a primary buy reason.* That is enforced in two places
and both matter.

Here, in ``to_signal``: extreme positive sentiment on a crowded name scores
*worse*, not better, because that is what the input is actually for. Nothing in
the model's output has to cooperate for that to hold — it is arithmetic.

And in rules.R1, which rejects any memo where positive sentiment is the only
thing standing up. Two independent mechanisms, because a single one placed
inside a prompt is a request, not a constraint.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Sequence

from ..domain.enums import ModuleName
from ..domain.models import Evidence, NewsItem, SentimentRead, Signal
from ..llm.client import LlmClient, LlmUnavailable
from ..llm.schemas import SENTIMENT_SCHEMA
from ..money import dec

SENTIMENT_VERSION = "sentiment-v1"

#: How much a crowded-name flag pulls positive sentiment down.
HERDING_PENALTY = Decimal("18")

SYSTEM = """You score the tone of retail and press commentary on one company.

Output a sentiment from -2 to +2:
  -2  Uniformly negative; capitulation, anger, "get out".
  -1  Leaning negative; scepticism, disappointment.
   0  Mixed or neutral; genuine disagreement, or nothing much being said.
  +1  Leaning positive; quiet optimism.
  +2  Uniformly positive; euphoria, price targets with no downside case.

conviction is how *consistent* the tone is across the sources, from 0 to 1. It
is not how bullish the tone is: uniform pessimism is high conviction.

herding_risk is true when the name looks crowded — a retail consensus, a meme,
uniform euphoria with nobody arguing the other side, or commentary that is
mostly about the share price rather than the business.

Two things to keep in mind:
- Downstream code treats extreme positive sentiment on a crowded name as a
  reason for caution, not enthusiasm. Score what is there; do not try to be
  helpful by softening it.
- Judge only the text supplied. If there is very little of it, say so with a
  low conviction and a small sample_size rather than extrapolating."""


def build_prompt(ticker: str, texts: Sequence[str], as_of: dt.date) -> str:
    lines = [f"Company: {ticker}", f"Today: {as_of.isoformat()}", "",
             f"Commentary and headlines ({len(texts)} items):"]
    lines += [f"- {text}" for text in texts]
    lines.append("")
    lines.append("Score the aggregate tone.")
    return "\n".join(lines)


def read_sentiment(
    client: LlmClient, ticker: str, items: Sequence[NewsItem], as_of: dt.date,
    *, extra_texts: Sequence[str] = (),
) -> SentimentRead | None:
    texts = [item.digest_text for item in items] + list(extra_texts)
    if not texts:
        return None
    if not client.available():
        raise LlmUnavailable("sentiment module requires an LLM")
    result = client.complete_json(
        module="sentiment", system=SYSTEM, prompt=build_prompt(ticker, texts, as_of),
        schema=SENTIMENT_SCHEMA,
    )
    data = result.data
    return SentimentRead(
        ticker=ticker, as_of=as_of, sentiment=data["sentiment"],
        conviction=dec(data["conviction"]), herding_risk=data["herding_risk"],
        rationale=data["rationale"], sample_size=data["sample_size"],
    )


def to_signal(read: SentimentRead) -> Signal:
    score = Decimal("50") + Decimal(read.sentiment) * Decimal("10")
    note = read.rationale
    if read.herding_risk and read.sentiment > 0:
        # The whole point of the herding flag: enthusiasm on a crowded name is a
        # warning, so it moves the score the other way.
        score -= HERDING_PENALTY
        note = f"crowded name — positive tone discounted. {note}"
    score = max(Decimal("0"), min(Decimal("100"), score))
    return Signal(
        module=ModuleName.SENTIMENT, module_version=SENTIMENT_VERSION, ticker=read.ticker,
        as_of=read.as_of, score=score, confidence=read.conviction,
        evidence=(
            Evidence(key="sentiment", value=f"tone {read.sentiment:+d} over {read.sample_size} items",
                     source="sentiment"),
            Evidence(key="herding_risk", value="crowded" if read.herding_risk else "not crowded",
                     source="sentiment"),
        ),
        notes=note,
    )
