"""The single LLM seam.

Rule 3 of the build instructions: *every LLM call — temperature 0 where
scoring, strict JSON schema, retry-with-repair once, then fail loudly.* All
four are implemented here and nowhere else, so no module can quietly opt out.

**One documented deviation, and it is the API's, not ours.** ``temperature`` was
removed from the Claude 4.6+ and 5-series models — sending it returns a 400.
Determinism on those models comes from the constrained decode of
``output_config.format`` plus a low ``effort`` setting, not from a sampling
knob. ``_accepts_sampling`` still sends ``temperature=0`` to older models that
do accept it, so the rule is honoured wherever the API can honour it. §5.2's
inter-run consistency eval measures what we actually get rather than assuming
the knob did its job — which is the right way round in any case.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any, Protocol

from ..config import LlmConfig, api_key
from ..logging_setup import get_logger

log = get_logger("llm")
LLM_CLIENT_VERSION = "llm-client-v1"

#: Model families that removed the sampling parameters. Sending `temperature`
#: to one of these is a 400, not a no-op.
_NO_SAMPLING = re.compile(
    r"^claude-(fable-5|mythos-5|opus-5|opus-4-[678]|sonnet-5|sonnet-4-6)", re.IGNORECASE
)


def accepts_sampling(model: str) -> bool:
    return not _NO_SAMPLING.match(model or "")


class LlmError(RuntimeError):
    """Base for LLM failures. Never caught inside a module — the point of
    'fail loudly' is that a brief is not generated from a guess."""


class LlmUnavailable(LlmError):
    """No API key, or the SDK is not installed. Distinct from a call failing:
    the pipeline degrades to deterministic-only rather than erroring."""


class SchemaViolation(LlmError):
    def __init__(self, module: str, errors: list[str], raw: str) -> None:
        super().__init__(f"{module}: output failed schema validation after repair: {errors}")
        self.module = module
        self.errors = errors
        self.raw = raw


@dataclass(slots=True)
class LlmResult:
    data: dict[str, Any]
    model: str
    attempts: int
    repaired: bool
    prompt_hash: str
    raw: str = ""


@dataclass(slots=True)
class LlmCallRecord:
    module: str
    model: str
    prompt_hash: str
    attempts: int
    schema_ok: bool
    repaired: bool
    error: str | None = None


class LlmClient(Protocol):
    def available(self) -> bool: ...

    def complete_json(
        self, *, module: str, system: str, prompt: str, schema: dict[str, Any],
        model: str | None = None,
    ) -> LlmResult: ...


# ---------------------------------------------------------------- validation


def validate(data: Any, schema: dict[str, Any], path: str = "$") -> list[str]:
    """A small JSON Schema subset validator.

    Deliberately not a dependency. The four schemas in schemas.py use exactly
    these keywords, and a local validator means the error strings fed back into
    the repair turn are ones we control and can make actionable — a generic
    library message like "None is not of type 'string'" tells the model far less
    than "$.direction: expected one of ['long','flat','avoid'], got null".
    """
    errors: list[str] = []
    expected = schema.get("type")

    if expected == "object":
        if not isinstance(data, dict):
            return [f"{path}: expected an object, got {type(data).__name__}"]
        properties = schema.get("properties", {})
        for key in schema.get("required", []):
            if key not in data:
                errors.append(f"{path}.{key}: required field is missing")
        if schema.get("additionalProperties") is False:
            for key in data:
                if key not in properties:
                    errors.append(f"{path}.{key}: unexpected field")
        for key, value in data.items():
            if key in properties:
                errors += validate(value, properties[key], f"{path}.{key}")
        return errors

    if expected == "array":
        if not isinstance(data, list):
            return [f"{path}: expected an array, got {type(data).__name__}"]
        item_schema = schema.get("items")
        if item_schema:
            for i, item in enumerate(data):
                errors += validate(item, item_schema, f"{path}[{i}]")
        return errors

    if expected == "string":
        if not isinstance(data, str):
            return [f"{path}: expected a string, got {json.dumps(data)}"]
        if (allowed := schema.get("enum")) and data not in allowed:
            errors.append(f"{path}: expected one of {allowed}, got {json.dumps(data)}")
        if (cap := schema.get("maxLength")) and len(data) > cap:
            errors.append(f"{path}: {len(data)} characters, maximum is {cap}")
        return errors

    if expected == "integer":
        # bool is a subclass of int in Python; True is not a valid integer here.
        if not isinstance(data, int) or isinstance(data, bool):
            return [f"{path}: expected an integer, got {json.dumps(data)}"]
    elif expected == "number":
        if isinstance(data, bool) or not isinstance(data, (int, float)):
            return [f"{path}: expected a number, got {json.dumps(data)}"]
    elif expected == "boolean":
        if not isinstance(data, bool):
            return [f"{path}: expected a boolean, got {json.dumps(data)}"]
        return errors
    else:
        return errors

    if (low := schema.get("minimum")) is not None and data < low:
        errors.append(f"{path}: {data} is below the minimum {low}")
    if (high := schema.get("maximum")) is not None and data > high:
        errors.append(f"{path}: {data} is above the maximum {high}")
    if (allowed := schema.get("enum")) and data not in allowed:
        errors.append(f"{path}: expected one of {allowed}, got {json.dumps(data)}")
    return errors


def extract_json(text: str) -> Any:
    """Parse the model's text as JSON, tolerating a fenced block.

    ``output_config.format`` makes bare JSON the norm, so the fence handling is
    for the older-model and fake-client paths rather than the happy one.
    """
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*", "", stripped)
        stripped = re.sub(r"\s*```$", "", stripped)
    return json.loads(stripped)


def prompt_hash(system: str, prompt: str) -> str:
    return hashlib.sha256(f"{system}\x1f{prompt}".encode()).hexdigest()[:16]


REPAIR_TEMPLATE = """Your previous response did not satisfy the required schema.

Validation errors:
{errors}

Your previous response was:
{previous}

Return the corrected JSON object only. Do not explain the correction."""


# ---------------------------------------------------------------- Anthropic


#: JSON Schema keywords the structured-outputs endpoint REJECTS with a 400
#: ("For 'integer' type, properties maximum, minimum are not supported").
#: The official SDK helpers handle this by stripping them from the wire schema
#: and validating client-side; this client already validates the full schema
#: locally (with a repair turn on violation), so stripping loses nothing —
#: the bounds are still enforced, just by us instead of by the API.
_WIRE_UNSUPPORTED = frozenset({
    "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf",
    "minLength", "maxLength", "pattern",
    "minItems", "maxItems", "uniqueItems",
})


def wire_schema(schema: Any) -> Any:
    """A deep copy of `schema` with the keywords the API rejects removed —
    and restated as PROSE in the description, so the model still sees them.

    The FULL schema stays the contract: `validate()` runs it against every
    response, and a bound the API never saw still triggers the repair turn.
    But a silently-dropped bound costs a full extra API call every time the
    model overruns it (live: `summary` came back 480/400 characters and
    `rationale` 669/400 on the FIRST real run) — the model cannot honour a
    limit it was never told. The hint makes attempt 1 usually pass; the
    repair turn stays as the backstop, not the norm.
    """
    if isinstance(schema, dict):
        wired = {key: wire_schema(value) for key, value in schema.items()
                 if key not in _WIRE_UNSUPPORTED}
        hint = _bounds_hint(schema)
        if hint:
            existing = wired.get("description", "")
            wired["description"] = f"{existing} ({hint})".strip() if existing else hint
        return wired
    if isinstance(schema, list):
        return [wire_schema(item) for item in schema]
    return schema


def _bounds_hint(node: dict[str, Any]) -> str:
    """The stripped constraints, as words. Phrased without the JSON-Schema
    keyword names so a hint is never mistaken for a live constraint."""
    hints: list[str] = []
    low, high = node.get("minimum"), node.get("maximum")
    if low is not None and high is not None:
        hints.append(f"value between {low} and {high}")
    elif low is not None:
        hints.append(f"value at least {low}")
    elif high is not None:
        hints.append(f"value at most {high}")
    short, long = node.get("minLength"), node.get("maxLength")
    if long is not None:
        hints.append(f"at most {long} characters"
                     + (f", at least {short}" if short is not None else ""))
    elif short is not None:
        hints.append(f"at least {short} characters")
    few, many = node.get("minItems"), node.get("maxItems")
    if few is not None and many is not None:
        hints.append(f"{few} to {many} items")
    elif many is not None:
        hints.append(f"at most {many} items")
    elif few is not None:
        hints.append(f"at least {few} items")
    return "; ".join(hints)


class AnthropicClient:
    """The real client. Structured output via ``output_config.format``."""

    def __init__(self, config: LlmConfig, *, sdk: Any = None) -> None:
        self.config = config
        self._sdk = sdk
        self._client: Any = None
        self.calls: list[LlmCallRecord] = []

    def available(self) -> bool:
        return self.unavailable_reason() is None

    def unavailable_reason(self) -> str | None:
        """None when the client can run; otherwise the SPECIFIC blocker.

        There are three distinct ways to be unavailable and they need three
        different actions. `available()` collapsed them to one silent False,
        which produced the worst live failure shape this repo knows: a valid
        key in .env, the SDK not installed, `sentinel health` saying "ready"
        (it only checked the key), and every long-term idea rejected for a
        missing invalidation with nothing anywhere naming the cause.
        """
        if not self.config.enabled:
            return "disabled in config ([llm] enabled = false)"
        if self._sdk is not None:
            return None
        if not api_key("ANTHROPIC_API_KEY"):
            return "no ANTHROPIC_API_KEY (set it in .env)"
        if self._import_sdk() is None:
            return ("the anthropic SDK is not installed — run "
                    "`uv sync --extra llm` (add --extra dashboard to keep the "
                    "dashboard) and re-run")
        return None

    def _import_sdk(self) -> Any:
        try:
            import anthropic  # noqa: PLC0415 - optional dependency, imported lazily
        except ImportError:
            return None
        return anthropic

    def _ensure_client(self) -> Any:
        if self._client is not None:
            return self._client
        if self._sdk is not None:
            self._client = self._sdk
            return self._client
        anthropic = self._import_sdk()
        if anthropic is None:
            raise LlmUnavailable("the `anthropic` package is not installed (`uv sync --extra llm`)")
        if not api_key("ANTHROPIC_API_KEY"):
            raise LlmUnavailable("ANTHROPIC_API_KEY is not set")
        self._client = anthropic.Anthropic()
        return self._client

    def _request(self, *, model: str, system: str, messages: list[dict[str, Any]],
                 schema: dict[str, Any]) -> str:
        kwargs: dict[str, Any] = {
            "model": model,
            "max_tokens": self.config.max_tokens,
            "system": system,
            "messages": messages,
            "output_config": {"format": {"type": "json_schema",
                                          "schema": wire_schema(schema)}},
        }
        if accepts_sampling(model):
            kwargs["temperature"] = self.config.temperature
        response = self._ensure_client().messages.create(**kwargs)
        return "".join(
            block.text for block in response.content if getattr(block, "type", None) == "text"
        )

    def complete_json(
        self, *, module: str, system: str, prompt: str, schema: dict[str, Any],
        model: str | None = None,
    ) -> LlmResult:
        model = model or self.config.model
        digest = prompt_hash(system, prompt)
        messages: list[dict[str, Any]] = [{"role": "user", "content": prompt}]
        attempts = 0
        last_errors: list[str] = []
        raw = ""

        # One initial attempt plus `repair_attempts` repairs, then fail loudly.
        for attempt in range(self.config.repair_attempts + 1):
            attempts += 1
            raw = self._request(model=model, system=system, messages=messages, schema=schema)
            try:
                data = extract_json(raw)
            except json.JSONDecodeError as exc:
                last_errors = [f"$: response was not valid JSON ({exc})"]
                data = None
            else:
                last_errors = validate(data, schema)

            if data is not None and not last_errors:
                record = LlmCallRecord(module, model, digest, attempts, True, attempt > 0)
                self.calls.append(record)
                return LlmResult(data=data, model=model, attempts=attempts,
                                 repaired=attempt > 0, prompt_hash=digest, raw=raw)

            log.warning("%s: schema violation on attempt %d: %s", module, attempts, last_errors)
            messages = [
                {"role": "user", "content": prompt},
                {"role": "assistant", "content": raw},
                {"role": "user", "content": REPAIR_TEMPLATE.format(
                    errors="\n".join(f"- {e}" for e in last_errors), previous=raw
                )},
            ]

        self.calls.append(
            LlmCallRecord(module, model, digest, attempts, False, True, "; ".join(last_errors))
        )
        raise SchemaViolation(module, last_errors, raw)
