"""Post-call intelligence: off-path disposition + structured-data extraction.

Runs LLM classification, structured extraction, relative-date resolution, and
downstream automation as a retained background task launched
after confirmed telephone completion. Blocking provider and storage operations
run in a worker thread. Call analytics are already finalized before
:func:`extract_and_apply` runs, so this work does not change their measured
timings. Background work still shares process and service capacity with live
calls.

Pipeline (:func:`extract_and_apply`):

1. Classify the Call Disposition and extract structured conversation data +
   a Next-Best-Action from the transcript via the Bedrock Converse API on a
   TEXT model (OpenAI GPT-6 Luna). The bedrock client is
   injectable so tests never hit the network, and the model JSON is parsed
   defensively.
2. Resolve the captured relative date against the call-local reference date
   with deterministic validation. For supported fallback cases, the same Luna
   model writes a Python program and AgentCore Code Interpreter executes it.
3. Apply downstream triggers only for a complete extraction (PTP -> pause
   collection + payment-link SMS; Dispute -> escalate to a human specialist).
4. Persist the intelligence onto the stored call record with
   ``intelligence_status`` ``ready``, ``partial`` for incomplete output after
   one repair, or ``failed`` on an extraction error. The caller is never crashed.
"""

from __future__ import annotations

import json
import logging
import math
import re
import time
from datetime import datetime, timezone
from typing import Any, Mapping
from zoneinfo import ZoneInfo

from app import date_resolver
from app.config import Settings, get_settings
from app.postcall_workflow import run_postcall_blocking
from app.store import CALL_DISPOSITIONS, Store, blank_structured_data

logger = logging.getLogger(__name__)

# System prompt driving the Bedrock Converse structured extraction. The model
# is asked to return ONLY a strict JSON object; app._extract_json_object parses
# it defensively (tolerating any surrounding prose).
_EXTRACTION_SYSTEM = (
    "You are an Accounts Receivable collections analyst. Read the call "
    "transcript and classify the outcome. The conversation may be in English, "
    "French, Spanish, or a mixture of those languages. Interpret all three, "
    "including later corrections. Keep schema keys and analyst descriptions "
    "and recommendations in English. Keep the caller's exact payment-date "
    "phrase in its original language; do not translate that phrase. "
    "Respond with ONLY a single JSON "
    "object, no prose, matching exactly this schema: "
    '{"disposition": <one of ' + ", ".join(CALL_DISPOSITIONS) + ">, "
    '"payment_specifics": {"promised_amount": <number or null>, '
    '"agreed_date": <the caller\'s exact payment-date phrase or null>, '
    '"preferred_payment_method": <string or null>}, '
    '"contextual_data": {"reason_for_delinquency": <string or null>, '
    '"customer_sentiment": <string or null>}, '
    '"next_best_action": <nonempty string>}.'
    " Copy the caller's date phrase; do not compute a calendar date or infer "
    "today from an overdue invoice date. Relative dates will be resolved "
    "separately against the supplied call-local reference date. An ISO date "
    "may be returned only when the caller explicitly used that ISO date. "
    "If no date was agreed, return null. Include EVERY key in the schema. "
    "For the reason for delinquency and payment method, use null when the "
    "caller did not state them; never invent a hardship, motive, or method. "
    "Assess customer sentiment only from the conversation; use null if there "
    "is insufficient evidence. The next-best-action is your recommendation, "
    "not a fact the caller must have spoken: give one short, practical next "
    "step supported by the final agreement and outcome. When the agreement "
    "is unclear, recommend the specific clarification needed. Do not claim "
    "a payment was received, an SMS was sent, or another action already "
    "happened unless the transcript establishes it. If no follow-up is "
    "appropriate, explain that briefly. Respect later corrections and the "
    "final agreed amount/date. Do not invent missing information."
)

_TEXT_FIELDS = (
    "payment_specifics.agreed_date",
    "payment_specifics.preferred_payment_method",
    "contextual_data.reason_for_delinquency",
    "contextual_data.customer_sentiment",
)
_INCOMPLETE_STOPS = {
    "max_tokens", "guardrail_intervened", "content_filtered",
    "malformed_model_output", "malformed_tool_use", "model_context_window_exceeded",
}


def _validated_extraction(parsed: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, str], list[str]]:
    """Keep valid fields while identifying missing or wrongly typed output.

    An explicit null for a fact that was not discussed is different from an
    omitted field. A recommendation is required because it is the analyst's
    conclusion, rather than a fact that must have been spoken by the caller.
    """
    normalized = {"disposition": "undetermined", **blank_structured_data(), "next_best_action": None}
    statuses: dict[str, str] = {}
    issues: list[str] = []
    disposition = parsed.get("disposition")
    if isinstance(disposition, str) and disposition in CALL_DISPOSITIONS:
        normalized["disposition"] = disposition
        statuses["disposition"] = "extracted"
    else:
        statuses["disposition"] = "missing" if "disposition" not in parsed else "invalid"
        issues.append(f"{statuses['disposition']}:disposition")

    fields = ("payment_specifics.promised_amount", *_TEXT_FIELDS)
    for path in fields:
        section, key = path.split(".")
        source = parsed.get(section)
        if not isinstance(source, Mapping) or key not in source:
            status = "invalid" if section in parsed and not isinstance(source, Mapping) else "missing"
            statuses[path] = status
            issues.append(f"{status}:{path}")
            continue
        value = source[key]
        if value is None:
            statuses[path] = "uncertain" if key == "customer_sentiment" else "not_discussed"
        elif key == "promised_amount":
            if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0:
                normalized[section][key] = float(value)
                statuses[path] = "extracted"
            else:
                statuses[path] = "invalid"
                issues.append(f"invalid:{path}")
        elif isinstance(value, str) and value.strip():
            normalized[section][key] = value.strip()
            statuses[path] = "extracted"
        else:
            statuses[path] = "invalid"
            issues.append(f"invalid:{path}")

    action = parsed.get("next_best_action")
    if isinstance(action, str) and action.strip():
        normalized["next_best_action"] = action.strip()
        statuses["next_best_action"] = "recommended"
    else:
        statuses["next_best_action"] = "missing" if action is None or action == "" else "invalid"
        issues.append(f"{statuses['next_best_action']}:next_best_action")
    return normalized, statuses, issues


def _transcript_text(call_record: Mapping[str, Any]) -> str:
    """Flatten the stored transcript into a plain ``role: text`` dialogue.

    Prefers the chat-style ``conversation_turns`` and falls back to the raw
    ``transcripts`` list.
    """
    turns = call_record.get("conversation_turns") or []
    lines: list[str] = []
    for turn in turns:
        speaker = turn.get("speaker") or turn.get("role") or "unknown"
        text = (turn.get("text") or "").strip()
        if text:
            lines.append(f"{speaker}: {text}")
    if lines:
        return "\n".join(lines)

    for t in call_record.get("transcripts") or []:
        if t.get("is_final") and (t.get("text") or "").strip():
            lines.append(f"{t.get('role', 'unknown')}: {t['text'].strip()}")
    return "\n".join(lines)


def _caller_text(call_record: Mapping[str, Any]) -> str:
    """Only caller speech can corroborate a legacy capture or explicit date."""
    turns = call_record.get("conversation_turns") or call_record.get("transcripts") or []
    return " ".join(
        str(turn.get("text") or "")
        for turn in turns
        if (turn.get("speaker") or turn.get("role")) == "user"
    )


def _captured_payment_phrase(
    call_record: Mapping[str, Any], due: Mapping[str, Any] | None,
) -> tuple[dict[str, Any] | None, str | None]:
    """Use call-bound capture; require corroboration for pre-provenance records."""
    call_id = call_record.get("call_sid") or call_record.get("call_id")
    capture = call_record.get("payment_date_capture")
    if (
        isinstance(capture, Mapping)
        and capture.get("call_sid") in (None, call_id)
        and isinstance(capture.get("phrase"), str)
        and capture["phrase"].strip()
    ):
        return dict(capture), "call_capture"
    due = due or {}
    phrase = due.get("promised_date_phrase")
    if not isinstance(phrase, str) or not phrase.strip():
        return None, None
    due_call = due.get("promised_date_phrase_call_sid")
    if call_id and due_call == call_id:
        return {
            "phrase": phrase,
            "call_sid": call_id,
            "reference_date": due.get("promised_date_reference_date") or due.get("promised_date_phrase_reference_date"),
            "reference_timezone": due.get("promised_date_reference_timezone") or due.get("promised_date_phrase_reference_timezone"),
        }, "due_capture_same_call"
    # A mutable account's phrase from a newer call must not rewrite this call.
    if due_call:
        return None, None
    recorded_tool = any(
        event.get("tool") == "capture_payment_date_phrase"
        for event in call_record.get("events_log") or []
    )
    normalize = lambda value: re.sub(r"\W+", " ", value.lower()).strip()
    if recorded_tool and normalize(phrase) in normalize(_caller_text(call_record)):
        return {"phrase": phrase, "call_sid": call_id}, "legacy_capture_confirmed_in_call"
    return None, None


def _payment_reference(
    call_record: Mapping[str, Any], settings: Settings,
    capture: Mapping[str, Any] | None, insights: Mapping[str, Any],
) -> dict[str, str]:
    """Freeze a call-local calendar anchor so delayed processing is repeatable."""
    capture = capture or {}
    prior = call_record.get("payment_date_resolution") or {}
    timezone_name = (
        capture.get("reference_timezone")
        or prior.get("reference_timezone")
        or settings.payment_timezone
    )
    local_timezone = ZoneInfo(str(timezone_name))
    result = {"reference_timezone": str(timezone_name)}
    # The tool records the call-local day at invocation, including midnight
    # crossings. Retain that explicit date across retries/configuration changes.
    for source, value in (
        ("payment_date_capture.reference_date", capture.get("reference_date")),
        ("payment_date_resolution.reference_date", prior.get("reference_date")),
    ):
        if isinstance(value, str):
            try:
                resolved = datetime.strptime(value, "%Y-%m-%d").date()
            except ValueError:
                continue
            return {**result, "reference_date": resolved.isoformat(), "reference_source": source}
    fields = insights.get("fields") or {}
    for source, value in (
        ("payment_date_capture.captured_at", capture.get("captured_at")),
        ("call.created_at", call_record.get("created_at")),
        ("twilio.start_time", fields.get("start_time")),
    ):
        if not isinstance(value, str):
            continue
        try:
            instant = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            continue
        if instant.tzinfo is None:
            # Earlier records used naive UTC timestamps. Make that assumption
            # explicit in the provenance instead of using the host timezone.
            instant = instant.replace(tzinfo=timezone.utc)
            source += ".assumed_utc"
        return {
            **result,
            "reference_date": instant.astimezone(local_timezone).date().isoformat(),
            "reference_source": source,
        }
    return {
        **result,
        "reference_date": datetime.now(timezone.utc).astimezone(local_timezone).date().isoformat(),
        "reference_source": "processing_time_fallback",
    }


def _extract_json_object(text: str) -> dict[str, Any] | None:
    """Defensively pull the first JSON object out of model output."""
    if not text:
        return None
    # Fast path: whole thing is JSON.
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            return obj
    except (ValueError, TypeError):
        pass
    # Fallback: grab the outermost {...} block.
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return None
    try:
        obj = json.loads(match.group(0))
    except (ValueError, TypeError):
        return None
    return obj if isinstance(obj, dict) else None


def _converse_text(response: Mapping[str, Any]) -> str:
    """Pull the assistant text out of a Bedrock Converse response."""
    try:
        content = response["output"]["message"]["content"]
    except (KeyError, TypeError):
        return ""
    if not isinstance(content, list):
        return ""
    # OpenAI responses can also contain encrypted reasoning blocks. They are
    # neither the extraction answer nor content to persist in call analytics.
    parts = [block["text"] for block in content if isinstance(block, dict) and isinstance(block.get("text"), str)]
    return "".join(parts).strip()


def _normalize_disposition(value: Any) -> str:
    """Coerce the model's disposition to a known tag (else 'undetermined')."""
    if not isinstance(value, str):
        return "undetermined"
    tag = value.strip().lower().replace(" ", "_").replace("-", "_")
    if tag in ("ptp",):
        return "promise_to_pay"
    return tag if tag in CALL_DISPOSITIONS else "undetermined"


def _coerce_amount(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        m = re.search(r"-?\d+(?:\.\d+)?", value.replace(",", ""))
        if m:
            try:
                return float(m.group(0))
            except ValueError:
                return None
    return None


def _classify(
    transcript: str, settings: Settings, bedrock_client: Any,
    *, reference_date: str, reference_timezone: str,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Validate the extraction and allow one repair of incomplete output.

    SDK failures are not retried here. If a repair request fails, retain an
    already valid partial result and report that limitation. No raw model
    response, transcript, encrypted reasoning, or exception text is logged.
    """
    metadata = metadata if metadata is not None else {}
    metadata.update({"model_id": settings.extraction_model_id, "api": "bedrock_converse",
                     "status": "failed", "attempts": 0, "issues": [], "field_status": {}, "usage": {}})
    started = time.perf_counter()
    prompt = (
        f"Call-local reference date: {reference_date}\n"
        f"Calendar timezone: {reference_timezone}\n"
        "This reference describes the call, not processing time or invoice due date.\n"
        "Call transcript:\n" + (transcript or "(no transcript captured)")
    )
    best = None
    stop_reasons: dict[int, str] = {}
    issues: list[str] = []
    try:
        for attempt in (1, 2):
            metadata["attempts"] = attempt
            repair = (
                "\n\nYour previous response failed these checks: " + ", ".join(issues)
                + ". Re-read the transcript and return the COMPLETE JSON schema. "
                "Do not invent a reason, payment method, or sentiment to fill an unknown field; "
                "explicit nulls are allowed for those fields. Provide a grounded next-best-action."
            ) if attempt == 2 else ""
            try:
                response = bedrock_client.converse(
                    modelId=settings.extraction_model_id,
                    system=[{"text": _EXTRACTION_SYSTEM}],
                    messages=[{"role": "user", "content": [{"text": prompt + repair}]}],
                    # Luna rejects temperature; its default sampling policy
                    # applies while the explicit cap bounds this background job.
                    inferenceConfig={"maxTokens": 1536 if attempt == 1 else 2048},
                )
            except Exception as exc:
                metadata["error_type"] = type(exc).__name__
                if best is None:
                    metadata["issues"] = ["model_request_failed"]
                    raise
                issues = [*best[2], "repair_request_failed"]
                best = (*best[:2], issues, best[3])
                break

            usage = response.get("usage") or {}
            if isinstance(usage, Mapping):
                for source, target in (("inputTokens", "input_tokens"), ("outputTokens", "output_tokens"),
                                       ("totalTokens", "total_tokens")):
                    value = usage.get(source)
                    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                        metadata["usage"][target] = metadata["usage"].get(target, 0) + value
            stop_reason = response.get("stopReason")
            if not isinstance(stop_reason, str):
                stop_reason = None
            metadata["stop_reason"] = stop_reason if stop_reason in _INCOMPLETE_STOPS | {"end_turn", "stop_sequence", "tool_use"} else "not_reported"
            stop_reasons[attempt] = metadata["stop_reason"]
            parsed = _extract_json_object(_converse_text(response))
            if parsed is None:
                issues = ["invalid_model_json"]
            else:
                normalized, field_status, issues = _validated_extraction(parsed)
                if stop_reason in _INCOMPLETE_STOPS or stop_reason == "tool_use":
                    issues.append("incomplete_model_response")
                if best is None or len(issues) < len(best[2]):
                    best = (normalized, field_status, issues, attempt)
                if not issues:
                    break

        if best is None:
            metadata["issues"] = issues
            raise ValueError("model did not return a parseable extraction object")
        normalized, field_status, issues, used_attempt = best
        metadata.update({"field_status": field_status, "issues": issues,
                         "status": "partial" if issues else "ready", "result_attempt": used_attempt,
                         "stop_reason": stop_reasons[used_attempt]})
        return normalized
    finally:
        metadata["duration_ms"] = round((time.perf_counter() - started) * 1000, 3)


def _resolve_date_phrase(
    phrase: str,
    reference_date,
    settings: Settings,
    code_interpreter_factory: Any | None,
    bedrock_client: Any | None,
    async_tool_calls: list[dict[str, Any]] | None = None,
    diagnostics: dict[str, Any] | None = None,
) -> str | None:
    """Resolve a date ``phrase`` to a concrete ISO date via the HYBRID resolver.

    Routes through :func:`app.date_resolver.resolve_date_hybrid`, which is a
    two-tier resolver (both tiers OFF the real-time audio path):

    * TIER 1 - a pure, sub-millisecond deterministic resolver
      (:func:`app.date_resolver.resolve_deterministic`) covering the common/
      enumerable grammar ("second Tuesday of next month", "next Friday", "in
      two weeks", "end of next month", ...). It uses NO network and NO AWS, and
      an already-ISO phrase short-circuits here too.
    * TIER 2 - ONLY on unsupported grammar, the model-authored AWS
      Bedrock AgentCore Code Interpreter (:func:`app.code_interpreter.
      resolve_date_phrase`), where a model WRITES a Python program on the fly
      and the sandbox EXECUTES it.

    When a tool actually ran (i.e. the phrase was not already ISO), the
    invocation is recorded as an ASYNC (off-path) tool call in
    ``async_tool_calls`` (when a list is provided), carrying a ``resolved_via``
    label (``'deterministic'`` | ``'code_interpreter'`` | ``'unresolved'``) so
    the dashboard can surface which tier resolved it, without
    counting it toward call latency.
    """
    text = (phrase or "").strip()
    if not text:
        date_resolver.record_diagnostic(diagnostics, "empty_phrase", "input", fallback_attempted=False)
        return None
    # Exact dates obey the same future/calendar guard as relative phrases.
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        date_resolver.record_diagnostic(diagnostics, date_resolver.payment_date_rejection(text, reference_date),
                                        "input", fallback_attempted=False)
        return date_resolver.validate_payment_date(text, reference_date)

    started_at = datetime.now(timezone.utc).isoformat()
    started = time.perf_counter()
    resolved, resolved_via = date_resolver.resolve_date_hybrid(
        text,
        reference_date,
        region=settings.aws_region,
        model_id=settings.extraction_model_id,
        bedrock_client=bedrock_client,
        code_interpreter_factory=code_interpreter_factory,
        diagnostics=diagnostics,
    )
    if async_tool_calls is not None:
        async_tool_calls.append(
            {
                "tool": "resolve_date_phrase",
                "kind": "async",
                "t_ms": None,
                "off_path": True,
                "resolved_via": resolved_via,
                "phase": "post_call",
                "execution_mode": "thread_offload",
                "source": "measured_execution",
                "started_at": started_at,
                "duration_ms": round((time.perf_counter() - started) * 1000, 3),
                "reference_date": str(reference_date),
                "resolution_reason": (diagnostics or {}).get("reason"),
                "resolution_stage": (diagnostics or {}).get("stage"),
            }
        )
    return resolved


def _enrich_twilio_insights(
    call_record: Mapping[str, Any],
    settings: Settings,
    twilio_client_factory: Any | None,
) -> dict[str, Any]:
    """Fetch the REAL Twilio Call resource OFF-PATH, never raising.

    Delegates to :func:`app.twilio_client.fetch_call_insights`, which already
    degrades gracefully. This wrapper adds a belt-and-braces guard so that even
    an unexpected error during enrichment can never crash the post-call task;
    it returns an explicit unavailable marker instead of fabricating data.
    """
    call_sid = call_record.get("call_sid") or call_record.get("call_id")
    try:
        from app.twilio_client import fetch_call_insights

        return fetch_call_insights(
            str(call_sid) if call_sid else None,
            settings=settings,
            client_factory=twilio_client_factory,
        )
    except Exception as exc:  # noqa: BLE001 - enrichment must never crash
        logger.warning(
            "call_sid=<%s> | twilio insights enrichment errored (%s)",
            call_sid,
            type(exc).__name__,
        )
        return {
            "available": False,
            "source": "unavailable",
            "advanced_qos_available": False,
            "advanced_qos_reason": None,
            "fields": {},
            "error": type(exc).__name__,
        }


def _build_structured_data(
    parsed: Mapping[str, Any],
    reference_date,
    settings: Settings,
    code_interpreter_factory: Any | None,
    bedrock_client: Any | None,
    async_tool_calls: list[dict[str, Any]] | None = None,
    date_diagnostics: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Map the parsed model JSON to the stored structured_data shape.

    Resolves confirmed phrases through the off-path hybrid resolver, keeping
    the already-ISO fast path and the actual outcome diagnostic.
    """
    data = blank_structured_data()
    payment = parsed.get("payment_specifics") or {}
    contextual = parsed.get("contextual_data") or {}

    data["payment_specifics"]["promised_amount"] = _coerce_amount(
        payment.get("promised_amount")
    )
    data["payment_specifics"]["preferred_payment_method"] = payment.get(
        "preferred_payment_method"
    )

    agreed = payment.get("agreed_date")
    resolved_date = None
    if isinstance(agreed, str) and agreed.strip():
        resolved_date = _resolve_date_phrase(
            agreed,
            reference_date,
            settings,
            code_interpreter_factory,
            bedrock_client,
            async_tool_calls=async_tool_calls,
            diagnostics=date_diagnostics,
        )
    else:
        date_resolver.record_diagnostic(date_diagnostics, "no_payment_date", "input", fallback_attempted=False)
    data["payment_specifics"]["agreed_date"] = resolved_date

    data["contextual_data"]["reason_for_delinquency"] = contextual.get(
        "reason_for_delinquency"
    )
    data["contextual_data"]["customer_sentiment"] = contextual.get("customer_sentiment")
    return data


def _emit(event_type: str, **data: Any) -> None:
    """Emit a structured JSON log line (CloudWatch-friendly)."""
    # This logger already has the application's JSON handler. The module's
    # default INFO logger otherwise drops success/completeness diagnostics.
    logging.getLogger("ar_collections.metrics").info(
        event_type, extra={"data": {"event_type": event_type, **data}},
    )


def _mask_number(number: str | None) -> str | None:
    """Mask a phone number for logging/record-keeping (keep last 4 digits).

    We record WHICH number the PTP SMS targeted on the call record, but never
    store the full E.164 destination as plaintext. e.g. "+15551234567" ->
    "+1******4567".
    """
    if not number:
        return None
    digits = [c for c in number if c.isdigit()]
    if len(digits) <= 4:
        return "*" * len(number)
    keep_tail = "".join(digits[-4:])
    prefix = number[0] if number[0] == "+" else ""
    return f"{prefix}{'*' * 6}{keep_tail}"


def _ptp_sms_destination(
    due_record: Mapping[str, Any] | None, settings: Settings
) -> str | None:
    """Pick the PTP payment-link SMS destination (trial-aware, explicit).

    - TRIAL account (``settings.twilio_trial_mode`` true, the POC default): the
      verified caller id is the ONLY number a trial account may legally message,
      so the SMS is pinned to ``TWILIO_VERIFIED_CALLER_ID`` regardless of the
      debtor. This is intentional and documented; sending to any other
      destination would raise on a trial account.
    - FULL account (trial mode off): prefer the debtor's own ``phone`` from the
      due/account record so the real debtor receives their payment link, and
      fall back to the verified caller id only when the record carries no phone.
    """
    verified = settings.twilio_verified_caller_id
    if settings.twilio_trial_mode:
        return verified
    debtor_phone = (due_record or {}).get("phone")
    if isinstance(debtor_phone, str) and debtor_phone.strip():
        return debtor_phone.strip()
    return verified


def _apply_ptp_trigger(
    account_id: str | None,
    structured_data: Mapping[str, Any],
    store: Store,
    settings: Settings,
    twilio_client_factory: Any | None,
) -> dict[str, Any]:
    """PTP downstream trigger: pause collection + send a payment-link SMS.

    Returns a dict with ``status`` ('sent'|'blocked_trial'|'failed' or ``None``
    when no destination/config is available) and ``to_masked`` (the masked
    destination the SMS targeted, or ``None``).
    """
    # Pause further automated collection on the account and read the debtor's
    # contact number for the (trial-aware) SMS destination.
    due_record: Mapping[str, Any] | None = None
    if account_id:
        try:
            due_record = store.update_due(account_id, collection_paused=True)
        except KeyError:
            logger.warning("account_id=<%s> | PTP pause: unknown account", account_id)
            try:
                due_record = store.get_due(account_id)
            except Exception:  # noqa: BLE001 - best effort
                due_record = None

    # Build the payment link and attempt the REAL SMS (do not fake success).
    base = (settings.payment_link_base_url or "").rstrip("/")
    link = f"{base}/{account_id}" if account_id else base
    body = (
        "Thank you for your promise to pay. You can complete your payment "
        f"securely here: {link}"
    )

    to = _ptp_sms_destination(due_record, settings)
    to_masked = _mask_number(to)
    if not (settings.twilio_configured and settings.twilio_from_number and to):
        _emit(
            "ptp_sms_skipped_unconfigured",
            account_id=account_id,
            reason="twilio_not_configured_or_no_destination",
        )
        return {"status": None, "to_masked": to_masked}

    from app.twilio_client import send_sms

    try:
        result = send_sms(
            to, body, settings=settings, client_factory=twilio_client_factory
        )
        _emit(
            "ptp_sms_sent",
            account_id=account_id,
            message_sid=result.message_sid,
            payment_link=link,
            to_masked=to_masked,
            trial_mode=settings.twilio_trial_mode,
        )
        return {"status": "sent", "to_masked": to_masked}
    except Exception as exc:  # noqa: BLE001 - trial restriction / API error
        # Twilio trial accounts can only message the verified number; a send to
        # any other destination raises. On a trial account we classify this as
        # 'blocked_trial'; off trial a send failure is a genuine 'failed'. Flag
        # it honestly, never fake success.
        status = "blocked_trial" if settings.twilio_trial_mode else "failed"
        _emit(
            f"ptp_sms_{status}",
            account_id=account_id,
            # SDK exception messages can contain credentials or request URLs.
            error_type=type(exc).__name__,
            to_masked=to_masked,
            trial_mode=settings.twilio_trial_mode,
        )
        return {"status": status, "to_masked": to_masked}


def _apply_dispute_trigger(account_id: str | None, store: Store) -> None:
    """Dispute downstream trigger: escalate the account to a human specialist."""
    if account_id:
        try:
            store.update_due(account_id, escalated=True)
        except KeyError:
            logger.warning("account_id=<%s> | dispute escalate: unknown account", account_id)
    _emit("escalation_to_human_specialist", account_id=account_id)


async def extract_and_apply(
    call_record: Mapping[str, Any],
    store: Store,
    settings: Settings | None = None,
    *,
    bedrock_client: Any | None = None,
    twilio_client_factory: Any | None = None,
    code_interpreter_factory: Any | None = None,
) -> dict[str, Any]:
    """Run blocking SDK/persistence work in a worker thread, preserving async API.

    The complete pipeline runs in the dedicated, bounded post-call pool rather
    than competing with live call I/O for the default executor's workers.
    """
    return await run_postcall_blocking(
        _extract_and_apply_sync,
        call_record,
        store,
        settings,
        bedrock_client=bedrock_client,
        twilio_client_factory=twilio_client_factory,
        code_interpreter_factory=code_interpreter_factory,
    )


def _extract_and_apply_sync(
    call_record: Mapping[str, Any],
    store: Store,
    settings: Settings | None = None,
    *,
    bedrock_client: Any | None = None,
    twilio_client_factory: Any | None = None,
    code_interpreter_factory: Any | None = None,
) -> dict[str, Any]:
    """Classify disposition, extract structured data + NBA, run triggers, persist.

    Runs OFF the real-time call path (fire-and-forget task). Never raises into
    the caller: on any failure it logs and persists ``intelligence_status =
    'failed'``.

    Args:
        call_record: The CallAnalytics record saved at call end (read-only).
        store: The store to persist intelligence back onto.
        settings: Settings override (defaults to :func:`get_settings`).
        bedrock_client: Injectable ``bedrock-runtime`` client (stubbed in
            tests); built lazily from boto3 when ``None``.
        twilio_client_factory: Injectable Twilio ``Client`` factory for the SMS
            trigger (stubbed in tests).
        code_interpreter_factory: Injectable ``(region) -> client`` factory for
            The AgentCore Code Interpreter used to resolve natural-language
            dates (stubbed in tests so no AWS call happens). When ``None`` the
            real AgentCore client is built lazily by :mod:`app.code_interpreter`.

    Returns:
        The changes dict that was merged onto the stored call record.
    """
    settings = settings or get_settings()
    call_id = call_record.get("call_sid") or call_record.get("call_id")
    account_id = call_record.get("account_id")

    changes: dict[str, Any] = {
        "disposition": None,
        "structured_data": blank_structured_data(),
        "next_best_action": None,
        "intelligence_status": "failed",
        "intelligence_metadata": {"model_id": settings.extraction_model_id,
                                  "api": "bedrock_converse", "status": "failed",
                                  "attempts": 0, "issues": [], "field_status": {}, "usage": {}},
        "collection_paused": False,
        "escalated": False,
        "ptp_sms_status": None,
        "ptp_sms_to": None,
        "triggers": [],
    }

    # Fetch the REAL Twilio Call resource OFF-PATH (post-call only).
    # This is intentionally done up front and independently of the LLM
    # extraction so that even if extraction fails the authentic Twilio facts
    # are still persisted. It degrades gracefully (never raises) and never
    # fabricates data. ``call_sid`` keyword matches the fetch signature.
    changes["twilio_insights"] = _enrich_twilio_insights(
        call_record, settings, twilio_client_factory
    )

    try:
        if bedrock_client is None:  # pragma: no cover - requires AWS creds/network
            import boto3

            # Session construction is per worker: boto3's shared default
            # session is not safe to initialize concurrently across threads.
            bedrock_client = boto3.session.Session(
                region_name=settings.aws_region
            ).client("bedrock-runtime")

        # Collect ASYNC (post-call, off-path) tool-call entries. The
        # AgentCore Code Interpreter date resolution runs here, after the call,
        # so it is classified kind='async' with t_ms=None (NOT part of call
        # latency). We record one entry each time a natural-language phrase is
        # actually resolved (the already-ISO fast path invokes no tool).
        async_tool_calls: list[dict[str, Any]] = []

        due = None
        if account_id:
            try:
                due = store.get_due(account_id)
            except Exception:  # noqa: BLE001 - a call capture still suffices
                pass
        capture, phrase_source = _captured_payment_phrase(call_record, due)
        reference = _payment_reference(call_record, settings, capture, changes["twilio_insights"])
        reference_date = reference["reference_date"]
        transcript = _transcript_text(call_record)
        parsed = _classify(
            transcript, settings, bedrock_client,
            reference_date=reference_date,
            reference_timezone=reference["reference_timezone"],
            metadata=changes["intelligence_metadata"],
        )

        disposition = _normalize_disposition(parsed.get("disposition"))
        payment = dict(parsed.get("payment_specifics") or {})
        extracted_date = payment.get("agreed_date")
        rejection_reason = None
        if capture:
            # The explicit call capture is authoritative even if the extractor
            # invented a nonempty ISO date from an old invoice date.
            payment["agreed_date"] = capture["phrase"]
        elif isinstance(extracted_date, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", extracted_date):
            if extracted_date not in _caller_text(call_record):
                payment["agreed_date"] = None
                rejection_reason = "unconfirmed_model_iso_date"
            phrase_source = "extraction"
        else:
            phrase_source = "extraction" if extracted_date else None
        parsed = {**parsed, "payment_specifics": payment}
        date_diagnostics: dict[str, Any] = {}
        structured_data = _build_structured_data(
            parsed,
            reference_date,
            settings,
            code_interpreter_factory,
            bedrock_client,
            async_tool_calls=async_tool_calls,
            date_diagnostics=date_diagnostics,
        )
        next_best_action = parsed.get("next_best_action")

        agreed_date = structured_data["payment_specifics"].get("agreed_date")
        resolution_via = (
            async_tool_calls[-1]["resolved_via"] if async_tool_calls
            else "explicit_date" if agreed_date else "unresolved"
        )
        changes["payment_date_resolution"] = {
            **reference,
            "phrase": payment.get("agreed_date"),
            "phrase_source": phrase_source,
            "resolved_via": resolution_via,
            "resolved_date": agreed_date,
            "status": "resolved" if agreed_date else "unresolved",
            "requires_review": agreed_date is None and disposition == "promise_to_pay",
            "reason": rejection_reason or date_diagnostics.get("reason"),
            "diagnostics": date_diagnostics,
            "model_date_overridden": bool(capture and extracted_date and extracted_date != capture["phrase"]),
        }
        # This logger is configured by the app's existing JSON handler. Emit
        # controlled diagnostic fields only: never the phrase, generated code,
        # sandbox stdout, transcript, or raw SDK exception payload.
        logging.getLogger("ar_collections.metrics").info("payment_date_resolution", extra={"data": {
            "event_type": "payment_date_resolution", "call_sid": call_id,
            "status": changes["payment_date_resolution"]["status"],
            "reason": changes["payment_date_resolution"]["reason"],
            "resolved_via": resolution_via, **reference, **{
                key: date_diagnostics[key] for key in (
                    "stage", "fallback_attempted", "deterministic_miss", "phrase_issues", "error_type",
                ) if key in date_diagnostics
            },
        }})
        # Resolve exactly once, then use the same result for both surfaces.
        # A later call's capture must not be overwritten when replaying this one.
        extraction_ready = changes["intelligence_metadata"]["status"] == "ready"
        if account_id and capture and agreed_date and extraction_ready:
            updated = store.update_promised_date_if_current(
                account_id, phrase=capture["phrase"], call_sid=call_id,
                promised_date=agreed_date,
            )
            changes["payment_date_resolution"]["due_date_updated"] = updated

        changes["disposition"] = disposition
        changes["structured_data"] = structured_data
        changes["next_best_action"] = (
            next_best_action if isinstance(next_best_action, str) else None
        )
        field_status = changes["intelligence_metadata"]["field_status"]
        if agreed_date:
            field_status["payment_specifics.agreed_date"] = "resolved"
        elif payment.get("agreed_date") or rejection_reason:
            field_status["payment_specifics.agreed_date"] = "needs_review"
        # Persist the call-level async (off-path) tool calls.
        changes["async_tool_calls"] = async_tool_calls

        triggers: list[str] = []
        # -- downstream triggers ------------------------------------------
        if extraction_ready and disposition == "promise_to_pay":
            ptp = _apply_ptp_trigger(
                account_id, structured_data, store, settings, twilio_client_factory
            )
            ptp_status = ptp["status"]
            changes["collection_paused"] = True
            changes["ptp_sms_status"] = ptp_status
            # Record (masked) which number the payment link was sent to so an
            # auditor can see the destination without the full number leaking.
            changes["ptp_sms_to"] = ptp["to_masked"]
            triggers.append("ptp_pause_collection")
            if ptp_status is not None:
                triggers.append(f"ptp_sms_{ptp_status}")
        elif extraction_ready and disposition == "dispute_raised":
            _apply_dispute_trigger(account_id, store)
            changes["escalated"] = True
            triggers.append("escalation_to_human_specialist")
        elif extraction_ready and disposition == "wrong_number":
            _emit("wrong_number_flagged", account_id=account_id)
            triggers.append("wrong_number_flagged")

        changes["triggers"] = triggers
        changes["intelligence_status"] = changes["intelligence_metadata"]["status"]
        _emit(
            "postcall_intelligence_" + changes["intelligence_status"],
            call_sid=call_id,
            account_id=account_id,
            disposition=disposition,
            triggers=triggers,
            intelligence_metadata=changes["intelligence_metadata"],
        )
    except Exception as exc:  # noqa: BLE001 - must never crash the caller
        changes["intelligence_status"] = "failed"
        metadata = changes["intelligence_metadata"]
        metadata["status"] = "failed"
        metadata["error_type"] = type(exc).__name__
        if not metadata["issues"]:
            metadata["issues"] = ["extraction_failed"]
        _emit("postcall_intelligence_failed", call_sid=call_id,
              intelligence_metadata=metadata)
        logger.warning(
            "call_sid=<%s> error_type=<%s> | post-call intelligence extraction failed",
            call_id,
            type(exc).__name__,
        )

    # Persist onto the stored call record (best-effort; never raise).
    if call_id:
        try:
            store.update_call_analytics(str(call_id), **changes)
        except Exception:  # noqa: BLE001 - persistence must not crash the task
            logger.exception(
                "call_sid=<%s> | failed to persist post-call intelligence", call_id
            )
    return changes
