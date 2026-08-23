"""LLM-as-judge for the synthesis module (§5.2).

Two design choices worth defending.

**The judge runs on a different model than the author** (``LlmConfig.judge_model``).
A model marking its own homework agrees with itself; the disagreement is the
signal.

**Hallucinated claims are an automatic fail regardless of the 1-5 scores.** The
spec says so, and it is right: a memo can be clear, well-argued and falsifiable
while resting on a fact no module produced, and averaging that into a 4.2 buries
the only finding that mattered. ``passed`` is therefore not a function of the
scores alone.

The judge is given the module evidence, not the market. It is scoring whether
the memo is *supported*, not whether it was right — being right is what the
performance evals measure, and conflating the two makes a lucky memo look
well-argued.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from ..config import LlmConfig
from ..domain.models import Evidence, Idea, IdeaMemo
from ..llm.client import LlmClient, LlmUnavailable
from ..llm.schemas import JUDGE_SCHEMA

JUDGE_VERSION = "judge-v1"

#: Below this mean, a memo fails even with no hallucinations.
PASS_MARK = 3.0

SYSTEM = """You are marking an investment memo written by another system.

You will see the memo and the module evidence that was available when it was
written. Score three things from 1 to 5:

  thesis_clarity      Is the argument stated plainly and specifically, or is it
                      hedged prose that could describe any company?
  evidence_grounding  Does every factual claim trace to the supplied evidence?
  falsifiability      Is the invalidation condition something you could check on
                      a given day and get an unambiguous yes or no?

Then list hallucinated_claims: any claim in the memo with no support in the
evidence supplied. Quote the claim. Be strict — a number that does not appear in
the evidence is a hallucination even if it sounds plausible, and especially if
it sounds plausible.

You are not judging whether the investment is a good one, and you have no
information about what happened next. A memo that is well-argued from weak
evidence should score well on clarity and poorly on grounding. Say so rather
than splitting the difference."""


@dataclass(frozen=True, slots=True)
class JudgeVerdict:
    thesis_clarity: int
    evidence_grounding: int
    falsifiability: int
    hallucinated_claims: tuple[str, ...]
    comment: str
    model: str = ""

    @property
    def mean_score(self) -> float:
        return (self.thesis_clarity + self.evidence_grounding + self.falsifiability) / 3

    @property
    def passed(self) -> bool:
        """Hallucinations fail the memo outright, whatever the scores."""
        if self.hallucinated_claims:
            return False
        return self.mean_score >= PASS_MARK

    def summary(self) -> str:
        if self.hallucinated_claims:
            return (f"FAIL — {len(self.hallucinated_claims)} unsupported claim(s): "
                    f"{'; '.join(self.hallucinated_claims)}")
        return (f"{'PASS' if self.passed else 'FAIL'} — clarity {self.thesis_clarity}/5, "
                f"grounding {self.evidence_grounding}/5, falsifiability {self.falsifiability}/5")


def build_prompt(memo: IdeaMemo, evidence: Sequence[Evidence]) -> str:
    lines = [
        f"Company: {memo.ticker}", "",
        "MEMO", f"Thesis: {memo.thesis}", f"Bull case: {memo.bull_case}",
        f"Bear case: {memo.bear_case}", f"Invalidation: {memo.invalidation}",
        f"Class: {memo.idea_class.value}, conviction: {memo.conviction.value}, "
        f"horizon: {memo.horizon_days} days",
        f"Claims cited: {', '.join(memo.claims) or '(none)'}",
        "", "EVIDENCE AVAILABLE TO THE AUTHOR",
    ]
    lines += [f"- {e.key} ({e.source}): {e.value}" for e in evidence] or ["(none)"]
    lines += ["", "Mark the memo."]
    return "\n".join(lines)


def judge_memo(
    client: LlmClient, memo: IdeaMemo, evidence: Sequence[Evidence],
    *, config: LlmConfig | None = None,
) -> JudgeVerdict:
    if not client.available():
        raise LlmUnavailable("the judge requires an LLM")
    config = config or LlmConfig()
    result = client.complete_json(
        module="judge", system=SYSTEM, prompt=build_prompt(memo, evidence),
        schema=JUDGE_SCHEMA, model=config.judge_model,
    )
    data = result.data
    return JudgeVerdict(
        thesis_clarity=data["thesis_clarity"],
        evidence_grounding=data["evidence_grounding"],
        falsifiability=data["falsifiability"],
        hallucinated_claims=tuple(data["hallucinated_claims"]),
        comment=data["comment"], model=result.model,
    )


def judge_idea(client: LlmClient, idea: Idea, *, config: LlmConfig | None = None) -> JudgeVerdict | None:
    if idea.memo is None:
        return None
    evidence = tuple(e for signal in idea.signals for e in signal.evidence)
    return judge_memo(client, idea.memo, evidence, config=config)


def sampling_rate(month: int) -> float:
    """§5.2's cadence: 100% in month 1, 20% thereafter."""
    return 1.0 if month <= 1 else 0.2


def should_judge(index: int, month: int) -> bool:
    """Deterministic sampling, so a rerun judges the same memos.

    Random sampling would make the judge scores in two runs of the same month
    incomparable, and §5.4 wants a replayed brief to reproduce.
    """
    rate = sampling_rate(month)
    if rate >= 1.0:
        return True
    stride = max(1, round(1 / rate))
    return index % stride == 0
