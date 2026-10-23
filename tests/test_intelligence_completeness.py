"""Synthetic post-call extraction regressions; no storage or network side effects."""

import json
import logging

import pytest

from app import code_interpreter, postcall
from app.config import DEFAULT_EXTRACTION_MODEL_ID
from app.store import InMemoryStore
from tests.test_postcall import _settings


LUNA = "us.openai.gpt-6-luna"
SENSITIVE_PAYLOAD = "SYNTHETIC_PRIVATE_PROVIDER_PAYLOAD"


def complete_payload():
    return {
        "disposition": "undetermined",
        "payment_specifics": {
            "promised_amount": None, "agreed_date": None,
            "preferred_payment_method": None,
        },
        "contextual_data": {"reason_for_delinquency": None, "customer_sentiment": None},
        "next_best_action": "Clarify whether the caller can commit to a payment amount and date.",
    }


def response(payload, *, stop_reason="end_turn"):
    return {
        "output": {"message": {"content": [
            {"reasoningContent": {"redactedContent": SENSITIVE_PAYLOAD}},
            {"text": json.dumps(payload) if isinstance(payload, dict) else payload},
        ]}},
        "stopReason": stop_reason,
        "usage": {"inputTokens": 100, "outputTokens": 150, "totalTokens": 250},
    }


class SequenceModel:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def converse(self, **kwargs):
        self.calls.append(kwargs)
        result = self.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def classify(model, metadata):
    return postcall._classify(
        "assistant: Can you confirm a payment date?\nuser: I am not sure yet.",
        _settings(extraction_model_id=LUNA), model,
        reference_date="2026-10-22", reference_timezone="Asia/Kolkata", metadata=metadata,
    )


def test_complete_luna_result_keeps_unknown_facts_null_and_records_safe_provenance():
    model = SequenceModel(response(complete_payload()))
    metadata = {}
    result = classify(model, metadata)
    assert result == complete_payload()
    assert len(model.calls) == 1
    assert model.calls[0]["modelId"] == LUNA
    assert model.calls[0]["inferenceConfig"]["maxTokens"] >= 1024
    assert "temperature" not in model.calls[0]["inferenceConfig"]
    assert metadata["model_id"] == LUNA and metadata["api"] == "bedrock_converse"
    assert metadata["status"] == "ready" and metadata["issues"] == []
    assert metadata["attempts"] == metadata["result_attempt"] == 1
    assert metadata["field_status"]["contextual_data.reason_for_delinquency"] == "not_discussed"
    assert metadata["field_status"]["contextual_data.customer_sentiment"] == "uncertain"
    assert metadata["field_status"]["next_best_action"] == "recommended"
    assert metadata["usage"] == {"input_tokens": 100, "output_tokens": 150, "total_tokens": 250}
    assert metadata["duration_ms"] >= 0 and metadata["stop_reason"] == "end_turn"
    assert SENSITIVE_PAYLOAD not in json.dumps(metadata) + json.dumps(result)


def test_missing_recommendation_receives_one_bounded_repair_with_luna():
    incomplete = complete_payload()
    incomplete.pop("next_best_action")
    model = SequenceModel(response(incomplete), response(complete_payload()))
    metadata = {}
    assert classify(model, metadata)["next_best_action"]
    assert len(model.calls) == 2
    assert all(call["modelId"] == LUNA for call in model.calls)
    assert "missing:next_best_action" in model.calls[1]["messages"][0]["content"][0]["text"]
    assert metadata["status"] == "ready" and metadata["result_attempt"] == 2
    assert metadata["usage"]["total_tokens"] == 500


def test_explicitly_null_recommendation_stays_partial_without_fabricating_context():
    incomplete = complete_payload()
    incomplete["next_best_action"] = None
    model = SequenceModel(response(incomplete), response(incomplete))
    metadata = {}
    result = classify(model, metadata)
    assert len(model.calls) == 2
    assert metadata["status"] == "partial"
    assert metadata["issues"] == ["missing:next_best_action"]
    assert result["next_best_action"] is None
    assert result["contextual_data"]["reason_for_delinquency"] is None
    assert metadata["field_status"]["next_best_action"] == "missing"


@pytest.mark.parametrize("bad_amount", [True, -10, "50", [], float("nan"), float("inf")])
def test_invalid_amount_is_discarded_even_when_json_parses(bad_amount):
    payload = complete_payload()
    payload["payment_specifics"]["promised_amount"] = bad_amount
    model = SequenceModel(response(payload), response(payload))
    metadata = {}
    result = classify(model, metadata)
    assert result["payment_specifics"]["promised_amount"] is None
    assert metadata["status"] == "partial"
    assert metadata["field_status"]["payment_specifics.promised_amount"] == "invalid"


def test_wrong_section_types_and_missing_fields_are_not_reported_as_not_discussed():
    payload = {"disposition": "unrecognized", "payment_specifics": "unknown", "contextual_data": {}}
    model = SequenceModel(response(payload), response(payload))
    metadata = {}
    result = classify(model, metadata)
    assert result["disposition"] == "undetermined"
    assert metadata["status"] == "partial"
    assert metadata["field_status"]["payment_specifics.agreed_date"] == "invalid"
    assert metadata["field_status"]["contextual_data.reason_for_delinquency"] == "missing"
    assert metadata["field_status"]["disposition"] == "invalid"


def test_malformed_json_fails_after_one_repair_without_saving_provider_payload():
    model = SequenceModel(response(SENSITIVE_PAYLOAD), response(SENSITIVE_PAYLOAD))
    metadata = {}
    with pytest.raises(ValueError, match="parseable extraction"):
        classify(model, metadata)
    assert len(model.calls) == 2 and metadata["status"] == "failed"
    assert metadata["issues"] == ["invalid_model_json"]
    assert SENSITIVE_PAYLOAD not in json.dumps(metadata)


def test_failed_repair_preserves_partial_data_and_redacts_provider_failure():
    incomplete = complete_payload()
    incomplete["next_best_action"] = None
    model = SequenceModel(response(incomplete), RuntimeError(SENSITIVE_PAYLOAD))
    metadata = {}
    result = classify(model, metadata)
    assert result["contextual_data"] == complete_payload()["contextual_data"]
    assert metadata["status"] == "partial" and metadata["result_attempt"] == 1
    assert metadata["error_type"] == "RuntimeError"
    assert metadata["issues"] == ["missing:next_best_action", "repair_request_failed"]
    assert SENSITIVE_PAYLOAD not in json.dumps(metadata)


def test_initial_provider_failure_does_not_create_an_application_retry_loop():
    model = SequenceModel(RuntimeError(SENSITIVE_PAYLOAD))
    metadata = {}
    with pytest.raises(RuntimeError):
        classify(model, metadata)
    assert len(model.calls) == 1
    assert metadata["status"] == "failed" and metadata["issues"] == ["model_request_failed"]
    assert SENSITIVE_PAYLOAD not in json.dumps(metadata)


@pytest.mark.parametrize("stop_reason", ["max_tokens", "content_filtered", "tool_use"])
def test_parseable_but_incomplete_provider_response_is_not_ready(stop_reason):
    model = SequenceModel(response(complete_payload(), stop_reason=stop_reason),
                          response(complete_payload(), stop_reason=stop_reason))
    metadata = {}
    classify(model, metadata)
    assert len(model.calls) == 2 and metadata["status"] == "partial"
    assert metadata["issues"] == ["incomplete_model_response"]
    assert metadata["stop_reason"] == stop_reason


@pytest.mark.asyncio
async def test_pipeline_persists_partial_status_and_emits_safe_completeness_event(monkeypatch):
    incomplete = complete_payload()
    incomplete["next_best_action"] = None
    model = SequenceModel(response(incomplete), response(incomplete))
    record = {"call_sid": "CA_synthetic_completeness", "account_id": "ACC-1001",
              "created_at": "2026-10-22T08:00:00+00:00",
              "conversation_turns": [{"speaker": "user", "text": "I am not sure yet."}]}
    store = InMemoryStore()
    store.save_call_analytics(record)
    logged = []
    monkeypatch.setattr(logging.getLogger("ar_collections.metrics"), "info",
                        lambda message, **kwargs: logged.append(kwargs["extra"]["data"]))
    changes = await postcall.extract_and_apply(
        record, store, _settings(extraction_model_id=LUNA, twilio_account_sid=None, twilio_auth_token=None),
        bedrock_client=model,
    )
    persisted = store.get_call(record["call_sid"])
    assert changes["intelligence_status"] == persisted["intelligence_status"] == "partial"
    assert persisted["intelligence_metadata"] == changes["intelligence_metadata"]
    assert changes["next_best_action"] is None and changes["triggers"] == []
    assert changes["ptp_sms_status"] is None
    event = next(event for event in logged if event["event_type"] == "postcall_intelligence_partial")
    assert event["intelligence_metadata"]["model_id"] == LUNA
    assert SENSITIVE_PAYLOAD not in json.dumps(logged)


@pytest.mark.asyncio
@pytest.mark.parametrize("disposition", ["promise_to_pay", "dispute_raised"])
async def test_partial_extraction_withholds_downstream_business_effects(disposition):
    incomplete = complete_payload()
    incomplete["disposition"] = disposition
    incomplete["payment_specifics"].update(promised_amount=60, agreed_date="tomorrow")
    incomplete["next_best_action"] = None
    model = SequenceModel(response(incomplete), response(incomplete))
    record = {"call_sid": "CA_synthetic_partial", "account_id": "ACC-1001",
              "created_at": "2026-10-22T08:00:00+00:00",
              "conversation_turns": [{"speaker": "user", "text": "I will pay $60 tomorrow."}],
              "payment_date_capture": {"phrase": "tomorrow", "call_sid": "CA_synthetic_partial",
                                       "reference_date": "2026-10-22", "reference_timezone": "Asia/Kolkata"}}
    store = InMemoryStore()
    store.save_call_analytics(record)
    store.update_due("ACC-1001", promised_date_phrase="tomorrow", promised_date_phrase_call_sid=record["call_sid"])
    original_due = store.get_due("ACC-1001")
    changes = await postcall.extract_and_apply(
        record, store, _settings(extraction_model_id=LUNA, twilio_account_sid=None, twilio_auth_token=None),
        bedrock_client=model,
    )
    assert changes["intelligence_status"] == "partial"
    assert changes["structured_data"]["payment_specifics"]["agreed_date"] == "2026-10-23"
    assert store.get_due("ACC-1001") == original_due
    assert changes["collection_paused"] is changes["escalated"] is False
    assert changes["ptp_sms_status"] is None and changes["triggers"] == []


def test_luna_date_program_ignores_encrypted_reasoning_and_uses_shared_model_default():
    program = "print('2026-11-11')"
    model = SequenceModel(response(program))
    diagnostics = {}
    result = code_interpreter._generate_program(
        phrase="the day after a regional holiday", reference_date="2026-10-22",
        bedrock_client=model, model_id=LUNA, diagnostics=diagnostics,
    )
    assert result == program
    assert model.calls[0]["modelId"] == LUNA
    assert code_interpreter.DEFAULT_CODE_GEN_MODEL_ID == DEFAULT_EXTRACTION_MODEL_ID == LUNA
    assert "temperature" not in model.calls[0]["inferenceConfig"]
    assert SENSITIVE_PAYLOAD not in json.dumps(diagnostics)


def test_incomplete_luna_date_program_does_not_start_a_sandbox():
    model = SequenceModel(response("print('2026-11-11')", stop_reason="max_tokens"))
    diagnostics = {}

    def forbidden_factory(_region):
        pytest.fail("an incomplete model program must never start a sandbox")

    assert code_interpreter.resolve_date_phrase(
        "the day after the regional holiday", reference_date="2026-10-22", region="us-east-1",
        bedrock_client=model, client_factory=forbidden_factory, model_id=LUNA, diagnostics=diagnostics,
    ) is None
    assert diagnostics["reason"] == "incomplete_model_program"
