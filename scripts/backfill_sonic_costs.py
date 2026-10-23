#!/usr/bin/env python3
"""Prepare cost-only historical patches from exported records and evidence.

No AWS clients, writes, model calls, intelligence extraction, or messaging are
performed. The output is a review artifact, not an executable deployment. Run
from the repository with:

    python scripts/backfill_sonic_costs.py --records-dir /private/calls \
        --evidence /private/evidence.json --output /private/cost-patches.json

Evidence is a JSON object keyed by call SID. Each entry needs verified delta
semantics, CloudWatch region/query provenance and aggregate usage. An optional
``usage_events`` list replaces (never supplements) the record's events_log
when CloudWatch retained events that were absent from the saved snapshot.
"""

from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys
from typing import Any, Mapping

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.sonic_pricing import MODEL_ID, calculate_sonic_cost
from app.sonic_usage import UsageAccumulator

_TERMINAL_STATUSES = {"completed", "canceled", "failed", "busy", "no-answer"}
_UPDATE_KEYS = {"usage", "sonic_cost", "sonic_cost_backfill"}


def _hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _reported_integer(value: Any) -> int | None:
    # Logs Insights emits numeric aggregate values as strings. Do not truncate
    # fractions or accept booleans as counts.
    if isinstance(value, int) and not isinstance(value, bool):
        return value if value >= 0 else None
    if isinstance(value, str) and value.isascii() and value.isdigit():
        return int(value)
    return None


def _telephone_evidence(record: Mapping[str, Any], evidence: Mapping[str, Any]) -> dict[str, Any] | None:
    supplied = evidence.get("telephone")
    if isinstance(supplied, Mapping):
        if (
            supplied.get("status") in _TERMINAL_STATUSES
            and supplied.get("source") == "Twilio Calls API"
            and supplied.get("call_sid") == record.get("call_sid")
            and supplied.get("checked_at")
        ):
            return dict(supplied)
        # An explicit newer nonterminal or conflicting response takes priority
        # over a stale stored claim that the telephone completed.
        return None
    termination = record.get("termination")
    if isinstance(termination, Mapping) and termination.get("telephone_completion_confirmed") is True:
        return {
            "source": "call_lifecycle_controller",
            "status": termination.get("telephone_status"),
            "confirmed_at": (termination.get("timestamps") or {}).get("telephone_completed"),
        }
    insights = record.get("twilio_insights")
    if isinstance(insights, Mapping) and insights.get("source") == "twilio_call_resource":
        fields = insights.get("fields")
        if isinstance(fields, Mapping) and fields.get("status") in _TERMINAL_STATUSES:
            return {"source": "stored_twilio_call_resource", "status": fields["status"]}
    return None


def prepare_cost_patch(record: Mapping[str, Any], evidence: Mapping[str, Any]) -> dict[str, Any]:
    """Return a reviewable patch or a reason to leave this record untouched.

    Required evidence shape::

        {"call_sid": "CA...", "usage_semantics": "delta",
         "semantics_source": "verified deployment/SDK source reference",
         "cloudwatch": {"region": "us-east-1", "log_group": "/ecs/...",
             "query_id": "...", "event_count": 99,
             "input_audio": 482, "input_text": 1501,
             "output_audio": 1289, "output_text": 352},
         "telephone": {"call_sid": "CA...", "status": "completed",
             "source": "Twilio Calls API", "checked_at": "..."}}

    The telephone entry can be omitted when stored lifecycle/Twilio evidence
    already confirms completion. ``usage_events`` is an optional full list of
    CloudWatch usage events; its event count and four sums are still checked
    independently against the CloudWatch aggregate. A mismatch produces an
    explicitly partial cost, never a claimed complete call total.
    """
    sid = record.get("call_sid")
    result: dict[str, Any] = {"call_sid": sid, "status": "skipped", "reason": None, "updates": {}}

    def skipped(reason: str) -> dict[str, Any]:
        result["reason"] = reason
        return result

    if not isinstance(sid, str) or not sid or evidence.get("call_sid") != sid:
        return skipped("call_identity_mismatch")
    telephone = _telephone_evidence(record, evidence)
    if telephone is None:
        return skipped("telephone_completion_unconfirmed")
    if evidence.get("usage_semantics") != "delta" or not evidence.get("semantics_source"):
        return skipped("historical_usage_semantics_unverified")
    cloudwatch = evidence.get("cloudwatch")
    if not isinstance(cloudwatch, Mapping) or not all(
        isinstance(cloudwatch.get(key), str) and cloudwatch[key].strip()
        for key in ("region", "log_group", "query_id")
    ):
        return skipped("cloudwatch_region_provenance_missing")
    if cloudwatch.get("call_sid", sid) != sid:
        return skipped("cloudwatch_call_identity_mismatch")

    recorded_events = record.get("events_log")
    if not isinstance(recorded_events, list):
        return skipped("event_log_missing")
    connection_events = [event for event in recorded_events if isinstance(event, Mapping) and event.get("event_type") == "connection_start"]
    if not connection_events or any(event.get("model") != MODEL_ID for event in connection_events):
        return skipped("connection_model_unverified")
    if record.get("nova_model_id") not in (None, MODEL_ID):
        return skipped("model_evidence_conflict")
    if record.get("aws_region") not in (None, cloudwatch["region"]):
        return skipped("region_evidence_conflict")
    recorded_context = record.get("sonic_context")
    if isinstance(recorded_context, Mapping) and recorded_context.get("region") not in (None, cloudwatch["region"]):
        return skipped("region_evidence_conflict")

    from_cloudwatch = "usage_events" in evidence
    if from_cloudwatch:
        events = evidence["usage_events"]
        if not isinstance(events, list) or any(not isinstance(event, Mapping) for event in events):
            return skipped("invalid_cloudwatch_usage_events")
    else:
        events = [event for event in recorded_events if isinstance(event, Mapping) and event.get("event_type") == "usage"]
    if not events:
        return skipped("usage_events_missing")
    if any(event.get("call_sid") != sid for event in events):
        return skipped("usage_event_call_identity_mismatch")
    if any(event.get("usage_semantics", "delta") != "delta"
           or event.get("usage_accounting", "delta") != "delta"
           or event.get("event_type") != "usage" for event in events):
        return skipped("usage_event_semantics_conflict")

    accumulator = UsageAccumulator()
    for event in events:
        accumulator.add(event)
    usage = accumulator.snapshot()
    observed = usage.get("modality_details") or {}
    checks = {
        "event_count": usage.get("event_count"),
        **{
            f"{direction}_{modality}": (observed.get(direction) or {}).get(modality)
            for direction in ("input", "output")
            for modality in ("audio", "text")
        },
    }
    matches = all(
        _reported_integer(cloudwatch.get(key)) is not None
        and _reported_integer(cloudwatch.get(key)) == observed_count
        for key, observed_count in checks.items()
    )
    # Accumulator completeness describes event validity; coverage additionally
    # requires agreement with the independently queried persisted log stream.
    usage["complete"] = usage.get("complete") is True and matches
    source = "cloudwatch_usage_events" if from_cloudwatch else "events_log_verified_against_cloudwatch"
    cost = calculate_sonic_cost(usage, model_id=MODEL_ID, region=cloudwatch["region"], usage_source=source)
    patch_evidence = {
        "schema_version": 1,
        "scope": "sonic_cost_only",
        "usage_source": source,
        "usage_semantics": "delta",
        "semantics_source": evidence["semantics_source"],
        "cloudwatch": {key: deepcopy(value) for key, value in cloudwatch.items() if key != "usage_events"},
        "cloudwatch_aggregate_matches": matches,
        "telephone": telephone,
        "source_events_sha256": _hash(events),
        "source_record_sha256": _hash(record),
        "previous_usage_sha256": _hash(record.get("usage")),
        "pricing_basis": "current_public_on_demand_snapshot",
    }
    result.update({
        "status": "prepared",
        "reason": None if usage["complete"] else "incomplete_usage_coverage",
        # Apply only if the record still has this value; a concurrent new
        # analytics writer must not have its result overwritten by this patch.
        "expected_usage": deepcopy(record.get("usage")),
        "expected_cost": deepcopy(record.get("sonic_cost")),
        "updates": {"usage": usage, "sonic_cost": cost, "sonic_cost_backfill": patch_evidence},
    })
    assert set(result["updates"]) == _UPDATE_KEYS
    return result


def evidence_from_logs_insights(document: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """Normalize the root's aggregate export without asserting SDK semantics."""
    normalized = {}
    for row in document.get("results", []):
        fields = {field["field"]: field["value"] for field in row}
        sid = fields.get("call_sid")
        if not isinstance(sid, str) or not sid:
            continue
        normalized[sid] = {
            "call_sid": sid,
            "cloudwatch": {
                **{key: document.get(key) for key in ("region", "log_group", "query_id")},
                **fields,
            },
        }
    return normalized


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--records-dir", required=True, type=Path)
    parser.add_argument("--evidence", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    if args.output.resolve().is_relative_to(Path(__file__).resolve().parents[1]):
        parser.error("Keep the private backfill review artifact outside the Git repository.")
    evidence_by_sid = json.loads(args.evidence.read_text())
    patches = []
    for file in sorted(args.records_dir.glob("CA*.json")):
        record = json.loads(file.read_text())
        patches.append(prepare_cost_patch(record, evidence_by_sid.get(record.get("call_sid"), {})))
    document = {"mode": "prepare_only", "patches": patches, "prepared": sum(patch["status"] == "prepared" for patch in patches)}
    args.output.write_text(json.dumps(document, indent=2) + "\n")
    args.output.chmod(0o600)
    print(f"Prepared {document['prepared']} of {len(patches)} cost-only patches; no records were modified.")


if __name__ == "__main__":
    main()
