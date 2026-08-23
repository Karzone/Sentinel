"""Phase 2: the LLM seam — strict schema, one repair, then fail loudly."""

from __future__ import annotations

from typing import Any

import pytest

from sentinel.config import LlmConfig
from sentinel.llm import client as llm
from sentinel.llm.fake import FlakyClient, ScriptedClient
from sentinel.llm.schemas import CATALYST_SCHEMA, MEMO_SCHEMA, SENTIMENT_SCHEMA

GOOD_CATALYST: dict[str, Any] = {
    "catalyst_type": "earnings", "direction": "long", "materiality": 4,
    "horizon_days": 30, "summary": "Beat and raised.", "headline_refs": ["X beats"],
}


class TestValidator:
    def test_a_clean_payload_has_no_errors(self):
        assert llm.validate(GOOD_CATALYST, CATALYST_SCHEMA) == []

    def test_a_missing_required_field_is_reported_by_name(self):
        payload = {k: v for k, v in GOOD_CATALYST.items() if k != "direction"}
        errors = llm.validate(payload, CATALYST_SCHEMA)
        assert errors == ["$.direction: required field is missing"]

    def test_an_out_of_range_integer_names_the_bound(self):
        errors = llm.validate({**GOOD_CATALYST, "materiality": 9}, CATALYST_SCHEMA)
        assert errors == ["$.materiality: 9 is above the maximum 5"]

    def test_an_unknown_enum_value_lists_the_allowed_ones(self):
        errors = llm.validate({**GOOD_CATALYST, "direction": "buy"}, CATALYST_SCHEMA)
        assert "expected one of" in errors[0] and "'long'" in errors[0]

    def test_extra_fields_are_rejected(self):
        errors = llm.validate({**GOOD_CATALYST, "price_target": 100}, CATALYST_SCHEMA)
        assert errors == ["$.price_target: unexpected field"]

    def test_a_boolean_is_not_an_integer(self):
        """bool subclasses int in Python, so `materiality: true` would sail
        through a naive isinstance check and become materiality 1."""
        errors = llm.validate({**GOOD_CATALYST, "materiality": True}, CATALYST_SCHEMA)
        assert errors == ["$.materiality: expected an integer, got true"]

    def test_null_is_not_a_string(self):
        errors = llm.validate({**GOOD_CATALYST, "summary": None}, CATALYST_SCHEMA)
        assert errors == ["$.summary: expected a string, got null"]

    def test_array_items_are_validated_positionally(self):
        errors = llm.validate({**GOOD_CATALYST, "headline_refs": ["ok", 7]}, CATALYST_SCHEMA)
        assert errors == ["$.headline_refs[1]: expected a string, got 7"]

    def test_a_fractional_conviction_is_a_valid_number(self):
        payload = {"sentiment": 1, "conviction": 0.75, "herding_risk": False,
                   "rationale": "mixed", "sample_size": 12}
        assert llm.validate(payload, SENTIMENT_SCHEMA) == []

    def test_maxlength_is_enforced(self):
        payload = {**GOOD_CATALYST, "summary": "x" * 500}
        errors = llm.validate(payload, CATALYST_SCHEMA)
        assert "maximum is 400" in errors[0]


class TestJsonExtraction:
    def test_a_fenced_block_is_unwrapped(self):
        assert llm.extract_json('```json\n{"a": 1}\n```') == {"a": 1}

    def test_bare_json_parses(self):
        assert llm.extract_json('{"a": 1}') == {"a": 1}


class StubMessages:
    def __init__(self, responses: list[str]) -> None:
        self.responses = responses
        self.requests: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> Any:
        self.requests.append(kwargs)
        text = self.responses[min(len(self.requests) - 1, len(self.responses) - 1)]

        class Block:
            type = "text"

            def __init__(self, t: str) -> None:
                self.text = t

        class Response:
            content = [Block(text)]

        return Response()


class StubSdk:
    def __init__(self, responses: list[str]) -> None:
        self.messages = StubMessages(responses)


class TestAnthropicClient:
    def _client(self, responses: list[str], **cfg: Any) -> tuple[llm.AnthropicClient, StubSdk]:
        sdk = StubSdk(responses)
        return llm.AnthropicClient(LlmConfig(**cfg), sdk=sdk), sdk

    def test_a_valid_first_response_needs_no_repair(self):
        import json

        client, sdk = self._client([json.dumps(GOOD_CATALYST)])
        result = client.complete_json(module="news", system="s", prompt="p", schema=CATALYST_SCHEMA)
        assert result.attempts == 1 and result.repaired is False
        assert len(sdk.messages.requests) == 1

    def test_an_invalid_response_is_repaired_once_and_succeeds(self):
        import json

        client, sdk = self._client([json.dumps({**GOOD_CATALYST, "materiality": 9}),
                                    json.dumps(GOOD_CATALYST)])
        result = client.complete_json(module="news", system="s", prompt="p", schema=CATALYST_SCHEMA)
        assert result.attempts == 2 and result.repaired is True
        # The repair turn must carry the errors AND the previous answer, or the
        # model is being asked to fix something it cannot see.
        repair = sdk.messages.requests[1]["messages"][-1]["content"]
        assert "above the maximum 5" in repair and "materiality" in repair

    def test_a_second_failure_raises_rather_than_returning_a_guess(self):
        import json

        bad = json.dumps({**GOOD_CATALYST, "materiality": 9})
        client, _ = self._client([bad, bad])
        with pytest.raises(llm.SchemaViolation) as exc:
            client.complete_json(module="news", system="s", prompt="p", schema=CATALYST_SCHEMA)
        assert exc.value.module == "news"
        assert client.calls[-1].schema_ok is False

    def test_unparseable_text_is_treated_as_a_schema_failure_not_a_crash(self):
        import json

        client, _ = self._client(["I'm afraid I can't do that", json.dumps(GOOD_CATALYST)])
        result = client.complete_json(module="news", system="s", prompt="p", schema=CATALYST_SCHEMA)
        assert result.repaired is True

    def test_temperature_is_omitted_on_models_that_reject_it(self):
        """The 4.6+/5-series removed the sampling parameters — sending
        temperature is a 400, not a no-op. Determinism there comes from the
        constrained decode, and the consistency eval measures what we get."""
        import json

        client, sdk = self._client([json.dumps(GOOD_CATALYST)], model="claude-opus-5")
        client.complete_json(module="news", system="s", prompt="p", schema=CATALYST_SCHEMA)
        assert "temperature" not in sdk.messages.requests[0]

    def test_temperature_zero_is_still_sent_where_the_api_accepts_it(self):
        import json

        client, sdk = self._client([json.dumps(GOOD_CATALYST)], model="claude-haiku-4-5")
        client.complete_json(module="news", system="s", prompt="p", schema=CATALYST_SCHEMA)
        assert sdk.messages.requests[0]["temperature"] == 0.0

    def test_the_schema_is_sent_as_a_constrained_output_format(self):
        import json

        client, sdk = self._client([json.dumps(GOOD_CATALYST)])
        client.complete_json(module="news", system="s", prompt="p", schema=CATALYST_SCHEMA)
        fmt = sdk.messages.requests[0]["output_config"]["format"]
        assert fmt["type"] == "json_schema" and fmt["schema"] is CATALYST_SCHEMA

    def test_dormant_without_a_key(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        assert llm.AnthropicClient(LlmConfig()).available() is False

    def test_disabled_in_config_means_unavailable_even_with_a_key(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
        assert llm.AnthropicClient(LlmConfig(enabled=False)).available() is False


class TestFakes:
    def test_the_flaky_client_exercises_the_repair_path(self):
        bad = {**GOOD_CATALYST, "direction": "moon"}
        client = FlakyClient(good=GOOD_CATALYST, bad=bad, failures=1, repair_attempts=1)
        result = client.complete_json(module="news", system="s", prompt="p", schema=CATALYST_SCHEMA)
        assert result.repaired is True and client.requests == 2

    def test_the_flaky_client_gives_up_after_the_allowed_repairs(self):
        bad = {**GOOD_CATALYST, "direction": "moon"}
        client = FlakyClient(good=GOOD_CATALYST, bad=bad, failures=5, repair_attempts=1)
        with pytest.raises(llm.SchemaViolation):
            client.complete_json(module="news", system="s", prompt="p", schema=CATALYST_SCHEMA)

    def test_the_scripted_client_rejects_payloads_that_break_the_schema(self):
        client = ScriptedClient({"synthesis": {"thesis": "too thin"}})
        with pytest.raises(llm.SchemaViolation):
            client.complete_json(module="synthesis", system="s", prompt="p", schema=MEMO_SCHEMA)
