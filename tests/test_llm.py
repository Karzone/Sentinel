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
        # The WIRED schema, not the raw one: the API rejects bound keywords
        # with a 400, so identity with CATALYST_SCHEMA was the live bug.
        assert fmt["type"] == "json_schema"
        assert fmt["schema"] == llm.wire_schema(CATALYST_SCHEMA)
        assert "minimum" not in str(fmt["schema"])

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


class TestUnavailabilityIsNamed:
    """`available()` collapsed three differently-actionable blockers into one
    silent False. The live failure that forced this: a valid key in .env, the
    SDK not installed (it is an optional extra), `sentinel health` saying
    "ready" because it checked only the key, and every long-term idea rejected
    for a missing invalidation with nothing anywhere naming the cause."""

    def _config(self, **overrides):
        from sentinel.config import LlmConfig
        return LlmConfig(**overrides)

    def test_a_missing_sdk_names_the_install_command(self, monkeypatch):
        from sentinel.llm.client import AnthropicClient
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        client = AnthropicClient(self._config())
        monkeypatch.setattr(client, "_import_sdk", lambda: None)
        reason = client.unavailable_reason()
        assert reason and "uv sync --extra llm" in reason
        assert client.available() is False

    def test_a_missing_key_names_the_key(self, monkeypatch):
        from sentinel.llm.client import AnthropicClient
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        client = AnthropicClient(self._config())
        reason = client.unavailable_reason()
        assert reason and "ANTHROPIC_API_KEY" in reason

    def test_disabled_in_config_names_the_config(self, monkeypatch):
        from sentinel.llm.client import AnthropicClient
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        client = AnthropicClient(self._config(enabled=False))
        reason = client.unavailable_reason()
        assert reason and "config" in reason

    def test_available_means_no_reason(self, monkeypatch):
        from sentinel.llm.client import AnthropicClient
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        client = AnthropicClient(self._config(), sdk=object())
        assert client.unavailable_reason() is None
        assert client.available() is True

    def test_the_key_check_comes_before_the_sdk_check(self, monkeypatch):
        """With neither key nor SDK, the key is the first thing to fix — an
        install hint would send the user to the wrong step."""
        from sentinel.llm.client import AnthropicClient
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        client = AnthropicClient(self._config())
        monkeypatch.setattr(client, "_import_sdk", lambda: None)
        assert "ANTHROPIC_API_KEY" in client.unavailable_reason()


class TestWireSchema:
    """The structured-outputs API rejects numeric/string/array constraint
    keywords with a 400 — live case: every schema here carries integer bounds,
    so the FIRST real LLM call of the project failed on schema, not content.
    The bounds are not dropped from the contract: the full schema still
    validates every response locally, with the repair turn behind it."""

    def test_the_rejected_keywords_are_stripped_at_every_depth(self):
        from sentinel.llm.client import wire_schema

        schema = {
            "type": "object", "additionalProperties": False,
            "properties": {
                "horizon_days": {"type": "integer", "minimum": 1, "maximum": 1825},
                "thesis": {"type": "string", "maxLength": 600},
                "tags": {"type": "array", "minItems": 1, "maxItems": 5,
                         "items": {"type": "string", "pattern": "^[a-z]+$"}},
                "nested": {"type": "object", "additionalProperties": False,
                           "properties": {"p": {"type": "number", "minimum": 0,
                                                "exclusiveMaximum": 1}}},
            },
            "required": ["horizon_days"],
        }
        wired = wire_schema(schema)
        text = str(wired)
        for keyword in ("minimum", "maximum", "maxLength", "minItems",
                        "maxItems", "pattern", "exclusiveMaximum"):
            assert keyword not in text, keyword

    def test_everything_the_api_supports_survives(self):
        from sentinel.llm.client import wire_schema

        schema = {"type": "object", "additionalProperties": False,
                  "properties": {"kind": {"type": "string", "enum": ["a", "b"],
                                          "description": "which"}},
                  "required": ["kind"]}
        assert wire_schema(schema) == schema

    def test_the_original_schema_is_not_mutated(self):
        """The full schema is still the local validation contract — stripping
        in place would silently weaken the repair loop too."""
        from sentinel.llm.client import wire_schema

        schema = {"type": "integer", "minimum": 1}
        wire_schema(schema)
        assert schema == {"type": "integer", "minimum": 1}

    def test_every_shipped_schema_is_wire_clean(self):
        """Not just the one that failed live: any schema added later with a
        bound would 400 on its first real call, which is exactly the class of
        bug that only shows up on the owner's machine."""
        from sentinel.llm import schemas
        from sentinel.llm.client import wire_schema

        shipped = {name: value for name, value in vars(schemas).items()
                   if name.endswith("_SCHEMA") and isinstance(value, dict)}
        assert shipped, "no schemas found — the discovery convention changed"
        for name, schema in shipped.items():
            text = str(wire_schema(schema))
            for keyword in ("minimum", "maximum", "minLength", "maxLength",
                            "minItems", "maxItems", "pattern"):
                assert keyword not in text, f"{name} still carries {keyword}"

    def test_the_request_path_sends_the_wired_schema(self):
        """Stripping that exists but is not on the request path is the guard
        that runs second all over again."""
        from sentinel.config import LlmConfig
        from sentinel.llm.client import AnthropicClient

        sent = {}

        class _Messages:
            def create(self, **kwargs):
                sent.update(kwargs)

                class _Response:
                    content = [type("B", (), {"type": "text",
                                              "text": '{"ok": true}'})()]
                return _Response()

        class _Sdk:
            messages = _Messages()

        client = AnthropicClient(LlmConfig(), sdk=_Sdk())
        client.complete_json(
            module="test", system="s", prompt="p",
            schema={"type": "object", "additionalProperties": False,
                    "properties": {"ok": {"type": "boolean"},
                                   "n": {"type": "integer", "minimum": 1}},
                    "required": ["ok"]},
        )
        wire = str(sent["output_config"])
        assert "minimum" not in wire
        assert "json_schema" in wire


class TestStrippedBoundsBecomeProse:
    """A silently-dropped bound costs a full repair call every time the model
    overruns it — live, the very first run repaired twice (`summary` 480/400
    chars, `rationale` 669/400). The model cannot honour a limit it was never
    told, so the wire copy restates each stripped bound in the description."""

    def test_a_max_length_is_restated_in_words(self):
        from sentinel.llm.client import wire_schema

        wired = wire_schema({"type": "string", "maxLength": 400})
        assert wired == {"type": "string", "description": "at most 400 characters"}

    def test_numeric_bounds_are_restated(self):
        from sentinel.llm.client import wire_schema

        wired = wire_schema({"type": "integer", "minimum": 1, "maximum": 1825})
        assert wired["description"] == "value between 1 and 1825"

    def test_an_existing_description_is_kept_and_extended(self):
        from sentinel.llm.client import wire_schema

        wired = wire_schema({"type": "string", "maxLength": 600,
                             "description": "One-paragraph thesis."})
        assert wired["description"] == "One-paragraph thesis. (at most 600 characters)"

    def test_the_hint_never_uses_schema_keyword_names(self):
        """So a hint can never be mistaken for a live constraint — and the
        wire-clean assertions elsewhere stay meaningful."""
        from sentinel.llm.client import wire_schema

        wired = wire_schema({"type": "integer", "minimum": 1, "maximum": 5,
                             "description": "clarity"})
        for keyword in ("minimum", "maximum", "maxLength", "minItems"):
            assert keyword not in str(wired)

    def test_a_bound_free_schema_gains_no_description(self):
        from sentinel.llm.client import wire_schema

        assert wire_schema({"type": "boolean"}) == {"type": "boolean"}
