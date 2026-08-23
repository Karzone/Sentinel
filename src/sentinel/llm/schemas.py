"""JSON schemas for every LLM call.

All four are ``additionalProperties: false`` with every field required. That is
not pedantry — a model that omits ``direction`` and a model that returns
``direction: null`` are both schema failures we want to *see*, because §5.2
measures schema-compliance rate and a lenient schema would report 100% while
the downstream code quietly coped with holes.

Enum values are duplicated here as literals rather than generated from the
enums in domain/enums.py. Generating them would couple the wire contract to a
refactor: renaming a Python enum member would silently change the schema an
archived call was validated against, and the eval history would stop meaning
one thing.
"""

from __future__ import annotations

from typing import Any

CATALYST_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "catalyst_type": {
            "type": "string",
            "enum": ["earnings", "guidance", "m_and_a", "regulatory", "product",
                     "management", "macro", "legal", "capital", "other"],
        },
        "direction": {
            "type": "string",
            "enum": ["long", "flat", "avoid"],
            "description": "Expected direction of the effect on the share price. "
                           "'flat' when the news is real but directionally ambiguous.",
        },
        "materiality": {
            "type": "integer",
            "minimum": 1,
            "maximum": 5,
            "description": "1 = routine noise, 5 = changes the investment case.",
        },
        "horizon_days": {
            "type": "integer",
            "minimum": 1,
            "maximum": 365,
            "description": "Over how many days the effect should express itself.",
        },
        "summary": {"type": "string", "maxLength": 400},
        "headline_refs": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Verbatim headlines this read is based on.",
        },
    },
    "required": ["catalyst_type", "direction", "materiality", "horizon_days",
                 "summary", "headline_refs"],
    "additionalProperties": False,
}

SENTIMENT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "sentiment": {"type": "integer", "minimum": -2, "maximum": 2},
        "conviction": {
            "type": "number", "minimum": 0, "maximum": 1,
            "description": "How consistent the tone is across sources, not how bullish.",
        },
        "herding_risk": {
            "type": "boolean",
            "description": "True when the name is crowded — a retail consensus, a "
                           "meme, or uniform euphoria with no dissent.",
        },
        "rationale": {"type": "string", "maxLength": 400},
        "sample_size": {"type": "integer", "minimum": 0},
    },
    "required": ["sentiment", "conviction", "herding_risk", "rationale", "sample_size"],
    "additionalProperties": False,
}

MEMO_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "thesis": {"type": "string", "maxLength": 600,
                   "description": "At most three sentences."},
        "bull_case": {"type": "string", "maxLength": 700},
        "bear_case": {"type": "string", "maxLength": 700},
        "invalidation": {
            "type": "string", "maxLength": 400,
            "description": "A specific, checkable condition that would prove the "
                           "thesis wrong. Must name a number, a date or an event.",
        },
        "idea_class": {"type": "string", "enum": ["long_term", "swing"]},
        "conviction": {"type": "string", "enum": ["low", "medium", "high"]},
        "horizon_days": {"type": "integer", "minimum": 1, "maximum": 1825},
        "claims": {
            "type": "array",
            "items": {"type": "string"},
            "description": "The evidence keys this memo relies on. Every key must "
                           "appear in the module evidence supplied in the prompt.",
        },
    },
    "required": ["thesis", "bull_case", "bear_case", "invalidation", "idea_class",
                 "conviction", "horizon_days", "claims"],
    "additionalProperties": False,
}

JUDGE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "thesis_clarity": {"type": "integer", "minimum": 1, "maximum": 5},
        "evidence_grounding": {"type": "integer", "minimum": 1, "maximum": 5},
        "falsifiability": {"type": "integer", "minimum": 1, "maximum": 5},
        "hallucinated_claims": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Claims in the memo with no supporting module evidence. "
                           "Any entry here is an automatic fail regardless of scores.",
        },
        "comment": {"type": "string", "maxLength": 500},
    },
    "required": ["thesis_clarity", "evidence_grounding", "falsifiability",
                 "hallucinated_claims", "comment"],
    "additionalProperties": False,
}
