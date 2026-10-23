"""Regional provider-token accounting, including missing historical evidence."""

from copy import deepcopy
from decimal import Decimal, localcontext

import pytest

from app.sonic_pricing import MODEL_ID, calculate_sonic_cost


def usage(text_in=10, audio_in=0, text_out=20, audio_out=0, *, complete=True):
    return {
        "accounting": "cumulative_deltas",
        "event_count": 2,
        "complete": complete,
        "input_tokens": text_in + audio_in,
        "output_tokens": text_out + audio_out,
        "total_tokens": text_in + audio_in + text_out + audio_out,
        "modality_details": {
            "input": {"text": text_in, "audio": audio_in},
            "output": {"text": text_out, "audio": audio_out},
        },
    }


def price(record=None, **kwargs):
    return calculate_sonic_cost(
        usage() if record is None else record,
        model_id=kwargs.pop("model_id", MODEL_ID),
        region=kwargs.pop("region", "us-east-1"),
        **kwargs,
    )


def test_text_example_has_subcent_precision_and_auditable_rates():
    result = price()
    # (10 * $0.33 + 20 * $2.75) / 1,000,000, not a flat speech rate.
    assert result["status"] == "estimated"
    assert result["total_cost_usd"] == "0.0000583"
    assert result["observed_cost_usd"] == "0.0000583"
    assert result["input_tokens"] == 10
    assert result["output_tokens"] == 20
    assert result["total_tokens"] == 30
    assert result["pricing"]["catalog_version"] == "20261016210806"
    assert result["pricing"]["effective_at"] == "2026-10-01T00:00:00Z"
    assert result["pricing"]["basis"] == "public_on_demand_snapshot"
    assert result["breakdown"][0]["sku"] == "FS6KSC7AWUD3CMSS"
    assert result["breakdown"][0]["rate_code"].endswith(".JRTCKXETXF.6YS6EN2CT7")
    assert sum(Decimal(line["cost_usd"]) for line in result["breakdown"]) == Decimal(result["total_cost_usd"])


def test_speech_uses_its_own_rates_not_text_rates():
    result = price(usage(0, 10, 0, 20))
    assert result["total_cost_usd"] == "0.00027"
    assert [line["rate_per_million_usd"] for line in result["breakdown"]] == ["0.33", "3", "2.75", "12"]


@pytest.mark.parametrize("region,expected", [
    ("us-east-1", "0.01808"),
    ("us-west-2", "0.0188861"),
    ("eu-north-1", "0.0199882"),
    ("ap-northeast-1", "0.0224124"),
])
def test_region_prices_are_not_interchanged(region, expected):
    result = price(usage(1000, 1000, 1000, 1000), region=region)
    assert result["total_cost_usd"] == expected
    assert result["region"] == region


def test_mixed_usage_not_rounded_per_category():
    assert price(usage(100, 200, 50, 300))["total_cost_usd"] == "0.0043705"


def test_partial_usage_has_observed_subtotal_but_no_call_total():
    result = price(usage(complete=False), usage_source="cloudwatch_recovered_deltas")
    assert result["status"] == "partial"
    assert result["reason"] == "incomplete_usage_coverage"
    assert result["observed_cost_usd"] == "0.0000583"
    assert result["total_cost_usd"] is None
    assert result["usage_source"] == "cloudwatch_recovered_deltas"


def test_missing_completion_flag_cannot_claim_complete_coverage():
    record = usage()
    del record["complete"]
    assert price(record)["status"] == "partial"


@pytest.mark.parametrize("kwargs,reason", [
    ({"region": "ap-south-1"}, "region_not_priced"),
    ({"region": None}, "region_not_priced"),
    ({"model_id": "amazon.nova-2-sonic-v1:0"}, "model_not_priced"),
    ({"model_id": "us.openai.gpt-6-luna"}, "model_not_priced"),
    ({"model_id": None}, "model_not_priced"),
])
def test_no_silent_model_or_region_fallback(kwargs, reason):
    result = price(**kwargs)
    assert result["status"] == "unavailable"
    assert result["reason"] == reason
    assert result["total_cost_usd"] is None
    assert result["breakdown"] == []


def test_legacy_last_delta_is_not_mistaken_for_full_call():
    record = usage()
    del record["accounting"]
    assert price(record)["reason"] == "usage_not_accumulated"


def test_aggregate_counts_cannot_be_priced_without_modality_breakdown():
    record = usage()
    del record["modality_details"]
    assert price(record)["reason"] == "modality_counts_missing"


def test_missing_modality_does_not_imply_zero():
    record = usage()
    del record["modality_details"]["output"]["audio"]
    assert price(record)["reason"] == "modality_counts_missing_or_invalid"


def test_no_events_does_not_imply_zero_cost():
    record = usage(0, 0, 0, 0)
    record["event_count"] = 0
    assert price(record)["reason"] == "usage_not_recorded"


def test_explicit_zero_usage_is_priced_as_zero():
    result = price(usage(0, 0, 0, 0))
    assert result["status"] == "estimated"
    assert result["total_cost_usd"] == "0"


@pytest.mark.parametrize("bad", [-1, 1.5, True, "10", float("nan"), float("inf"), Decimal("NaN")])
def test_invalid_counts_not_coerced_or_rounded(bad):
    record = usage()
    record["input_tokens"] = bad
    assert price(record)["reason"] == "invalid_usage_totals"


def test_inconsistent_totals_withhold_cost():
    record = usage()
    record["total_tokens"] += 1
    assert price(record)["reason"] == "usage_totals_mismatch"
    record["total_tokens"] -= 1
    record["modality_details"]["input"]["text"] += 1
    assert price(record)["reason"] == "modality_totals_mismatch"


@pytest.mark.parametrize("where,key", [
    ("top", "cache_read_input_tokens"),
    ("top", "cache_write_input_tokens"),
    ("input", "cache_read"),
    ("output", "reasoning"),
])
def test_unpriced_nonzero_token_category_is_not_ignored(where, key):
    record = usage()
    target = record if where == "top" else record["modality_details"][where]
    target[key] = 1
    assert price(record)["reason"] == "unpriced_token_category"
    target[key] = 0
    assert price(record)["status"] == "estimated"
    target[key] = None
    assert price(record)["status"] == "estimated"


def test_dynamodb_decimals_and_ambient_precision_do_not_change_cost():
    record = usage(Decimal(100), Decimal(200), Decimal(50), Decimal(300))
    record["event_count"] = Decimal(2)
    with localcontext() as context:
        context.prec = 2
        result = price(record)
    assert result["total_cost_usd"] == "0.0043705"
    assert isinstance(result["input_tokens"], int)


def test_function_does_not_mutate_usage():
    record = usage()
    before = deepcopy(record)
    price(record)
    assert record == before
