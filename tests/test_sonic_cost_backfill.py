"""Historical costing only touches verified usage and costing fields."""

from copy import deepcopy

import pytest

from scripts.backfill_sonic_costs import evidence_from_logs_insights, prepare_cost_patch

SID = "CA-cost-test"


def delta(text_in=0, audio_in=0, text_out=0, audio_out=0):
    return {
        "event_type": "usage", "call_sid": SID,
        "input_tokens": text_in + audio_in,
        "output_tokens": text_out + audio_out,
        "total_tokens": text_in + audio_in + text_out + audio_out,
        "modality_details": {
            "input": {"text": text_in, "audio": audio_in},
            "output": {"text": text_out, "audio": audio_out},
        },
    }


def record():
    return {
        "call_sid": SID,
        "termination": {"telephone_completion_confirmed": True, "telephone_status": "completed"},
        "events_log": [
            {"event_type": "connection_start", "model": "amazon.nova-2-5-sonic", "call_sid": SID},
            delta(text_in=10, text_out=20), delta(audio_in=10, audio_out=20),
        ],
        "usage": delta(audio_in=10, audio_out=20),
        "structured_data": {"payment_specifics": {"agreed_date": "2026-11-04"}},
        "intelligence_status": "ready", "ptp_sms_status": "sent", "triggers": ["ptp"],
    }


def evidence():
    return {
        "call_sid": SID,
        "usage_semantics": "delta",
        "semantics_source": "Verified deployed Strands Bedrock converter uses details.delta",
        "cloudwatch": {
            "call_sid": SID, "region": "us-east-1", "log_group": "/ecs/app",
            "query_id": "query-test", "event_count": "2",
            "input_audio": "10", "input_text": "10", "output_audio": "20", "output_text": "20",
        },
    }


def test_prepare_uses_deltas_once_and_preserves_intelligence():
    source, proof = record(), evidence()
    original = deepcopy(source)
    result = prepare_cost_patch(source, proof)
    assert result["status"] == "prepared"
    assert set(result["updates"]) == {"usage", "sonic_cost", "sonic_cost_backfill"}
    assert result["updates"]["usage"]["input_tokens"] == 20
    assert result["updates"]["usage"]["output_tokens"] == 40
    assert result["updates"]["sonic_cost"]["total_cost_usd"] == "0.0003283"
    assert result["updates"]["sonic_cost_backfill"]["cloudwatch_aggregate_matches"] is True
    assert result["updates"]["sonic_cost_backfill"]["pricing_basis"] == "current_public_on_demand_snapshot"
    assert result["expected_usage"] == original["usage"]
    assert result["expected_cost"] is None
    assert source == original
    assert proof == evidence()


def test_cloudwatch_recovery_replaces_store_events_instead_of_combining():
    source, proof = record(), evidence()
    proof["usage_events"] = source["events_log"][1:] + [delta(audio_out=20)]
    proof["cloudwatch"].update(event_count="3", output_audio="40")
    result = prepare_cost_patch(source, proof)
    assert result["updates"]["usage"]["event_count"] == 3
    assert result["updates"]["sonic_cost"]["total_cost_usd"] == "0.0005683"
    assert result["updates"]["sonic_cost"]["usage_source"] == "cloudwatch_usage_events"


@pytest.mark.parametrize("field,value,reason", [
    ("aws_region", "us-west-2", "region_evidence_conflict"),
    ("nova_model_id", "another-model", "model_evidence_conflict"),
])
def test_current_call_context_conflicts_cannot_be_overridden(field, value, reason):
    source = record()
    source[field] = value
    assert prepare_cost_patch(source, evidence())["reason"] == reason


def test_explicit_cumulative_usage_event_cannot_be_summed_as_delta():
    source = record()
    source["events_log"][1]["usage_accounting"] = "cumulative"
    assert prepare_cost_patch(source, evidence())["reason"] == "usage_event_semantics_conflict"


@pytest.mark.parametrize("field,value", [("event_count", "3"), ("output_audio", "40"), ("input_text", "11")])
def test_unreconciled_logs_do_not_claim_full_call_cost(field, value):
    proof = evidence()
    proof["cloudwatch"][field] = value
    result = prepare_cost_patch(record(), proof)
    assert result["updates"]["usage"]["complete"] is False
    assert result["updates"]["sonic_cost"]["status"] == "partial"
    assert result["updates"]["sonic_cost"]["total_cost_usd"] is None
    assert result["reason"] == "incomplete_usage_coverage"


def test_legacy_telephone_needs_real_completion_evidence():
    source = record()
    del source["termination"]
    proof = evidence()
    assert prepare_cost_patch(source, proof)["reason"] == "telephone_completion_unconfirmed"
    proof["telephone"] = {"call_sid": SID, "status": "completed", "source": "Twilio Calls API", "checked_at": "2026-10-22T17:00:00Z"}
    assert prepare_cost_patch(source, proof)["status"] == "prepared"
    proof["telephone"]["call_sid"] = "CA-another-call"
    assert prepare_cost_patch(source, proof)["reason"] == "telephone_completion_unconfirmed"


def test_stored_twilio_completion_can_confirm_legacy_call():
    source = record()
    del source["termination"]
    source["twilio_insights"] = {"source": "twilio_call_resource", "fields": {"status": "completed"}}
    assert prepare_cost_patch(source, evidence())["status"] == "prepared"
    source["twilio_insights"]["source"] = "assumed"
    assert prepare_cost_patch(source, evidence())["reason"] == "telephone_completion_unconfirmed"


def test_new_nonterminal_twilio_evidence_overrides_old_completion_claim():
    proof = evidence()
    proof["telephone"] = {"call_sid": SID, "status": "in-progress", "source": "Twilio Calls API", "checked_at": "2026-10-22T17:00:00Z"}
    assert prepare_cost_patch(record(), proof)["reason"] == "telephone_completion_unconfirmed"


@pytest.mark.parametrize("field,value,reason", [
    ("call_sid", "CA-other", "call_identity_mismatch"),
    ("usage_semantics", "cumulative", "historical_usage_semantics_unverified"),
    ("semantics_source", None, "historical_usage_semantics_unverified"),
    ("cloudwatch", {}, "cloudwatch_region_provenance_missing"),
])
def test_missing_or_conflicting_evidence_skips_patch(field, value, reason):
    proof = evidence()
    proof[field] = value
    result = prepare_cost_patch(record(), proof)
    assert result["reason"] == reason
    assert result["updates"] == {}


def test_cloudwatch_wrong_call_is_rejected():
    proof = evidence()
    proof["cloudwatch"]["call_sid"] = "CA-other"
    assert prepare_cost_patch(record(), proof)["reason"] == "cloudwatch_call_identity_mismatch"


def test_mixed_models_across_restarts_do_not_get_sonic_only_cost():
    source = record()
    source["events_log"].append({"event_type": "connection_start", "model": "different-model"})
    assert prepare_cost_patch(source, evidence())["reason"] == "connection_model_unverified"


def test_region_conflict_never_falls_back_to_deployment_default():
    source = record()
    source["sonic_context"] = {"region": "us-west-2"}
    assert prepare_cost_patch(source, evidence())["reason"] == "region_evidence_conflict"


def test_wrong_call_usage_event_rejected():
    source = record()
    source["events_log"][1]["call_sid"] = "CA-other"
    assert prepare_cost_patch(source, evidence())["reason"] == "usage_event_call_identity_mismatch"


def test_cumulative_event_marker_conflicts_with_delta_contract():
    source = record()
    source["events_log"][1]["usage_semantics"] = "cumulative"
    assert prepare_cost_patch(source, evidence())["reason"] == "usage_event_semantics_conflict"


def test_equal_audio_deltas_are_not_content_deduplicated():
    source = record()
    source["events_log"].append(delta(audio_in=10, audio_out=20))
    proof = evidence()
    proof["cloudwatch"].update(event_count="3", input_audio="20", output_audio="40")
    result = prepare_cost_patch(source, proof)
    assert result["updates"]["usage"]["event_count"] == 3
    assert result["updates"]["sonic_cost"]["total_cost_usd"] == "0.0005983"


def test_insights_normalizer_preserves_strings_and_does_not_invent_semantics():
    output = evidence_from_logs_insights({
        "region": "us-east-1", "log_group": "/ecs/app", "query_id": "q-1",
        "results": [[{"field": "call_sid", "value": SID}, {"field": "input_text", "value": "1234"}]],
    })
    assert output[SID]["cloudwatch"]["input_text"] == "1234"
    assert output[SID]["cloudwatch"]["query_id"] == "q-1"
    assert "usage_semantics" not in output[SID]
