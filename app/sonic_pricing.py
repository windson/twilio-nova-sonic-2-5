"""Pure, post-call Nova 2.5 Sonic token costing against a reviewed price snapshot.

This module performs no network or model calls. The post-call owner must invoke
it only after telephone completion. Token counts come from provider usage, never
from transcript length, audio duration, or another model's estimates.

Rates were retrieved on 2026-10-22 with AWS Price List API GetProducts for
AmazonBedrock, model="Nova 2.5 Sonic", across all regions. The 16 returned
OnDemand products cover four disjoint token categories in four regions; no
Sonic 2.5 cache product was present. Unknown categories remain unpriced.

The pinned Strands 1.62 BidiUsageEvent contains *deltas*, converted from Nova's
usageEvent.details.delta. CallMetrics must add these once across all sessions.
https://docs.aws.amazon.com/nova/latest/nova2-userguide/sonic-output-events.html
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation, localcontext
from typing import Any, Mapping

MODEL_ID = "amazon.nova-2-5-sonic"
PRICING_VERSION = "20261016210806"
PRICING_EFFECTIVE_AT = "2026-10-01T00:00:00Z"
PRICING_PUBLISHED_AT = "2026-10-16T21:08:06Z"
PRICING_CHECKED_AT = "2026-10-22T17:21:53Z"

# (USD per 1,000 tokens, AWS product SKU). Preserve the API's decimal values;
# these are genuinely region-specific, including text input/output pricing.
_RATES: dict[str, dict[str, tuple[str, str]]] = {
    "us-east-1": {
        "text_input": ("0.0003300000", "FS6KSC7AWUD3CMSS"),
        "audio_input": ("0.0030000000", "545D9MCNGJ6YH7ZP"),
        "text_output": ("0.0027500000", "XC39NVNGRPCQNSAZ"),
        "audio_output": ("0.0120000000", "VMV2TR4RQK8R628X"),
    },
    "us-west-2": {
        "text_input": ("0.0003190000", "5F5CCRPH6JN253TV"),
        "audio_input": ("0.0031832000", "D7G79NRRX9W9P7PM"),
        "text_output": ("0.0026510000", "QSNPZ3QHXEJNHH5H"),
        "audio_output": ("0.0127329000", "HZ6434XKD2F4X33C"),
    },
    "eu-north-1": {
        "text_input": ("0.0003630000", "U4GFVHGM7YNVGTDU"),
        "audio_input": ("0.0033266000", "HQTRQYX55SHX6WGS"),
        "text_output": ("0.0029920000", "NYFZ4JBG8NYZ63P2"),
        "audio_output": ("0.0133066000", "77889UDCUYRXV7BU"),
    },
    "ap-northeast-1": {
        "text_input": ("0.0003960000", "QDDTQZZXVD8D5CGX"),
        "audio_input": ("0.0037411000", "WU8X77CQZ2HUX7DZ"),
        "text_output": ("0.0033110000", "PVBG46QV35ND7CF6"),
        "audio_output": ("0.0149643000", "46V5YESFBWBG6R9B"),
    },
}


def _count(value: Any) -> int | None:
    """Accept exact nonnegative numeric counts, including DynamoDB Decimals."""
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        return None
    try:
        decimal = Decimal(str(value))
        if not decimal.is_finite() or decimal < 0 or decimal != decimal.to_integral_value():
            return None
        return int(decimal)
    except (InvalidOperation, ValueError, OverflowError):
        return None


def _money(value: Decimal) -> str:
    # Strings preserve sub-cent precision in JSON and DynamoDB. The UI decides
    # display precision; rounding every token category would lose real usage.
    rendered = format(value, "f")
    return rendered.rstrip("0").rstrip(".") if "." in rendered else rendered


def calculate_sonic_cost(
    usage: Mapping[str, Any] | None,
    *,
    model_id: str | None,
    region: str | None,
    usage_source: str = "bidi_usage",
) -> dict[str, Any]:
    """Price validated cumulative provider usage, without guessing missing data.

    ``usage`` must carry ``accounting='cumulative_deltas'``, an ``event_count``,
    explicit ``complete`` coverage, totals and disjoint audio/text counts in
    ``modality_details.input/output``. Legacy last-event snapshots must first
    be reconstructed from usage events; aggregate counts alone cannot be priced
    because speech and text have different rates.

    ``estimated`` means a public-list-price estimate for complete recorded
    usage, not an AWS invoice. For incomplete coverage, only
    ``observed_cost_usd`` is populated and ``status`` is ``partial``. The caller
    must not present that subtotal as the full call's cost. Historical calls
    are explicitly repriced at this snapshot rather than claiming historical
    invoice reconciliation.
    """
    usage = usage if isinstance(usage, Mapping) else {}
    result: dict[str, Any] = {
        "schema_version": 1,
        "status": "unavailable",
        "reason": None,
        "model_id": model_id,
        "model_name": "Nova 2.5 Sonic",
        "region": region,
        "currency": "USD",
        "total_cost_usd": None,
        "observed_cost_usd": None,
        "input_tokens": _count(usage.get("input_tokens")),
        "output_tokens": _count(usage.get("output_tokens")),
        "total_tokens": _count(usage.get("total_tokens")),
        "usage_source": usage_source,
        "usage_complete": usage.get("complete") is True,
        "usage_event_count": _count(usage.get("event_count")),
        "breakdown": [],
        "pricing": {
            "source": "AWS Price List API",
            "service_code": "AmazonBedrock",
            "model_filter": "Nova 2.5 Sonic",
            "source_url": "https://aws.amazon.com/nova/pricing/",
            "catalog_version": PRICING_VERSION,
            "effective_at": PRICING_EFFECTIVE_AT,
            "published_at": PRICING_PUBLISHED_AT,
            "checked_at": PRICING_CHECKED_AT,
            "basis": "public_on_demand_snapshot",
        },
        "exclusions": ["Other models", "Twilio", "AWS infrastructure", "Discounts", "Taxes"],
    }

    def unavailable(reason: str) -> dict[str, Any]:
        result["reason"] = reason
        return result

    if model_id != MODEL_ID:
        return unavailable("model_not_priced")
    if region not in _RATES:
        return unavailable("region_not_priced")
    if usage.get("accounting") != "cumulative_deltas":
        return unavailable("usage_not_accumulated")
    if not result["usage_event_count"]:
        return unavailable("usage_not_recorded")
    if any(result[name] is None for name in ("input_tokens", "output_tokens", "total_tokens")):
        return unavailable("invalid_usage_totals")
    if result["input_tokens"] + result["output_tokens"] != result["total_tokens"]:
        return unavailable("usage_totals_mismatch")

    details = usage.get("modality_details")
    if not isinstance(details, Mapping):
        return unavailable("modality_counts_missing")
    counts: dict[str, int] = {}
    for direction in ("input", "output"):
        values = details.get(direction)
        if not isinstance(values, Mapping):
            return unavailable("modality_counts_missing")
        for modality in ("text", "audio"):
            count = _count(values.get(modality))
            if count is None:
                return unavailable("modality_counts_missing_or_invalid")
            counts[f"{modality}_{direction}"] = count
        if sum(counts[f"{modality}_{direction}"] for modality in ("text", "audio")) != result[f"{direction}_tokens"]:
            return unavailable("modality_totals_mismatch")
        # Never subtract or double-charge a cache/reasoning category whose
        # billing semantics are absent from this model's reviewed snapshot.
        for key, value in values.items():
            if key not in {"text", "audio"} and value is not None and _count(value) != 0:
                return unavailable("unpriced_token_category")
    for key in ("cache_read_input_tokens", "cache_write_input_tokens"):
        value = usage.get(key)
        if value is not None and _count(value) != 0:
            return unavailable("unpriced_token_category")

    with localcontext() as context:
        # Independent from an application's ambient Decimal context, including
        # unusually large historical totals. A rate has at most ten decimals.
        context.prec = max(40, len(str(result["total_tokens"])) + 20)
        total = Decimal(0)
        for category, (raw_rate, sku) in _RATES[region].items():
            modality, direction = category.split("_")
            rate = Decimal(raw_rate)
            cost = Decimal(counts[category]) * rate / Decimal(1000)
            total += cost
            result["breakdown"].append({
                "direction": direction,
                "modality": modality,
                "tokens": counts[category],
                "rate_per_million_usd": _money(rate * 1000),
                "cost_usd": _money(cost),
                "sku": sku,
                "rate_code": f"{sku}.JRTCKXETXF.6YS6EN2CT7",
            })
        result["observed_cost_usd"] = _money(total)
        if result["usage_complete"]:
            result["status"] = "estimated"
            result["total_cost_usd"] = _money(total)
        else:
            result["status"] = "partial"
            result["reason"] = "incomplete_usage_coverage"
    return result
