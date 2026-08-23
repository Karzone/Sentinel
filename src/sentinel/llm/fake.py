"""Deterministic stand-ins for the LLM, for tests and offline runs.

``ScriptedClient`` returns canned payloads. ``FlakyClient`` returns a
schema-violating payload first and a good one after, which is the only way to
test that the repair turn actually happens rather than that it exists.
"""

from __future__ import annotations

import json
from typing import Any, Callable, Iterable

from .client import (
    LlmCallRecord, LlmResult, SchemaViolation, prompt_hash, validate,
)


class ScriptedClient:
    """Returns the next payload for a module each time it is asked."""

    def __init__(self, payloads: dict[str, Any] | None = None, *, model: str = "fake-model") -> None:
        self.payloads: dict[str, list[Any]] = {
            module: list(values) if isinstance(values, list) else [values]
            for module, values in (payloads or {}).items()
        }
        self.model = model
        self.calls: list[LlmCallRecord] = []
        self.prompts: list[tuple[str, str]] = []

    def available(self) -> bool:
        return True

    def complete_json(
        self, *, module: str, system: str, prompt: str, schema: dict[str, Any],
        model: str | None = None,
    ) -> LlmResult:
        self.prompts.append((module, prompt))
        queue = self.payloads.get(module)
        if not queue:
            raise AssertionError(f"ScriptedClient has no payload left for module {module!r}")
        data = queue.pop(0) if len(queue) > 1 else queue[0]
        digest = prompt_hash(system, prompt)
        errors = validate(data, schema)
        if errors:
            self.calls.append(LlmCallRecord(module, self.model, digest, 1, False, False, str(errors)))
            raise SchemaViolation(module, errors, json.dumps(data))
        self.calls.append(LlmCallRecord(module, self.model, digest, 1, True, False))
        return LlmResult(data=data, model=self.model, attempts=1, repaired=False,
                         prompt_hash=digest, raw=json.dumps(data))


class FlakyClient:
    """Fails the schema `failures` times, then succeeds — exercising the repair
    path end to end rather than asserting the code for it exists."""

    def __init__(self, good: Any, bad: Any, *, failures: int = 1, repair_attempts: int = 1) -> None:
        self.good = good
        self.bad = bad
        self.failures = failures
        self.repair_attempts = repair_attempts
        self.calls: list[LlmCallRecord] = []
        self.requests = 0

    def available(self) -> bool:
        return True

    def complete_json(
        self, *, module: str, system: str, prompt: str, schema: dict[str, Any],
        model: str | None = None,
    ) -> LlmResult:
        digest = prompt_hash(system, prompt)
        attempts = 0
        errors: list[str] = []
        for attempt in range(self.repair_attempts + 1):
            attempts += 1
            self.requests += 1
            data = self.bad if attempt < self.failures else self.good
            errors = validate(data, schema)
            if not errors:
                self.calls.append(
                    LlmCallRecord(module, "flaky", digest, attempts, True, attempt > 0)
                )
                return LlmResult(data=data, model="flaky", attempts=attempts,
                                 repaired=attempt > 0, prompt_hash=digest, raw=json.dumps(data))
        self.calls.append(LlmCallRecord(module, "flaky", digest, attempts, False, True, str(errors)))
        raise SchemaViolation(module, errors, json.dumps(self.bad))


class CallableClient:
    """Builds a payload from the prompt, for consistency and judge tests."""

    def __init__(self, fn: Callable[[str, str], Any]) -> None:
        self.fn = fn
        self.calls: list[LlmCallRecord] = []

    def available(self) -> bool:
        return True

    def complete_json(
        self, *, module: str, system: str, prompt: str, schema: dict[str, Any],
        model: str | None = None,
    ) -> LlmResult:
        data = self.fn(module, prompt)
        digest = prompt_hash(system, prompt)
        errors = validate(data, schema)
        if errors:
            raise SchemaViolation(module, errors, json.dumps(data))
        self.calls.append(LlmCallRecord(module, "callable", digest, 1, True, False))
        return LlmResult(data=data, model="callable", attempts=1, repaired=False,
                         prompt_hash=digest, raw=json.dumps(data))


class UnavailableClient:
    """What the pipeline sees with no ANTHROPIC_API_KEY: deterministic modules
    still run, LLM modules are simply skipped."""

    def available(self) -> bool:
        return False

    def complete_json(self, **_: Any) -> LlmResult:
        from .client import LlmUnavailable

        raise LlmUnavailable("no LLM configured")


def iter_payloads(values: Iterable[Any]) -> list[Any]:
    return list(values)
