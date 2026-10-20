"""Post-call intelligence: off-path disposition + structured-data extraction.

This module runs the EXPENSIVE part of FEAT-002 (an LLM classification +
structured extraction, relative-date resolution, and the downstream automation
triggers) AFTER the call has ended, as a fire-and-forget asyncio task launched
from :func:`app.main.media_stream`'s finally-block. It therefore never runs on
the real-time audio/turn path and provably cannot inflate call latency: the
metrics ``total_e2e_ms`` / ``nova_total_ms`` / ``ttft_ms`` are computed on the
audio path (``session_start``..``session_end``) in :class:`app.metrics.CallMetrics`
and are already frozen by the time :func:`extract_and_apply` runs.

Pipeline (:func:`extract_and_apply`):

1. Classify the Call Disposition and extract structured conversation data +
   a Next-Best-Action from the transcript via the Bedrock Converse API on a
   TEXT model (default ``amazon.nova-lite-v1:0``). The bedrock client is
   injectable so tests never hit the network, and the model JSON is parsed
   defensively.
2. Resolve any captured relative date phrase to a concrete ISO date using the
   SAME deterministic date-math as the in-call ``compute_future_date`` tool.
3. Apply downstream triggers (PTP -> pause collection + payment-link SMS;
   Dispute -> escalate to a human specialist).
4. Persist the intelligence onto the stored call record with
   ``intelligence_status`` ``ready`` (or ``failed`` on any error). The caller is
   never crashed.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import Any, Mapping

from app.collections_agent import _resolve_relative_date
from app.config import Settings, get_settings
from app.store import CALL_DISPOSITIONS, Store, blank_structured_data

logger = logging.getLogger(__name__)

# System prompt driving the Bedrock Converse structured extraction. The model
# is asked to return ONLY a strict JSON object; app._extract_json_object parses
# it defensively (tolerating any surrounding prose).
_EXTRACTION_SYSTEM = (
    "You are an Accounts Receivable collections analyst. Read the call "
    "transcript and classify the outcome. Respond with ONLY a single JSON "
    "object, no prose, matching exactly this schema: "
    '{"disposition": <one of ' + ", ".join(CALL_DISPOSITIONS) + ">, "
    '"payment_specifics": {"promised_amount": <number or null>, '
    '"agreed_date": <YYYY-MM-DD or a natural-language phrase or null>, '
    '"preferred_payment_method": <string or null>}, '
    '"contextual_data": {"reason_for_delinquency": <string or null>, '
    '"customer_sentiment": <string or null>}, '
    '"next_best_action": <string or null>}.'
)


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
    parts = [block.get("text", "") for block in content if isinstance(block, dict)]
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
    transcript: str, settings: Settings, bedrock_client: Any
) -> dict[str, Any]:
    """Call Bedrock Converse and return the parsed JSON object (or raise)."""
    response = bedrock_client.converse(
        modelId=settings.extraction_model_id,
        system=[{"text": _EXTRACTION_SYSTEM}],
        messages=[
            {
                "role": "user",
                "content": [
                    {"text": "Call transcript:\n" + (transcript or "(no transcript captured)")}
                ],
            }
        ],
        inferenceConfig={"maxTokens": 512, "temperature": 0.0},
    )
    parsed = _extract_json_object(_converse_text(response))
    if parsed is None:
        raise ValueError("model did not return a parseable JSON object")
    return parsed


def _build_structured_data(parsed: Mapping[str, Any], reference_date) -> dict[str, Any]:
    """Map the parsed model JSON to the stored structured_data shape.

    Resolves a relative ``agreed_date`` phrase via the same date math as the
    in-call ``compute_future_date`` tool.
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
        # Accept an already-ISO date, otherwise resolve the natural phrase.
        try:
            resolved_date = datetime.strptime(agreed.strip(), "%Y-%m-%d").date().isoformat()
        except ValueError:
            resolved = _resolve_relative_date(agreed, reference_date)
            resolved_date = resolved.isoformat() if resolved else None
    data["payment_specifics"]["agreed_date"] = resolved_date

    data["contextual_data"]["reason_for_delinquency"] = contextual.get(
        "reason_for_delinquency"
    )
    data["contextual_data"]["customer_sentiment"] = contextual.get("customer_sentiment")
    return data


def _emit(event_type: str, **data: Any) -> None:
    """Emit a structured JSON log line (CloudWatch-friendly)."""
    logger.info(json.dumps({"event_type": event_type, **data}, default=str))


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
            error=str(exc),
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
        "collection_paused": False,
        "escalated": False,
        "ptp_sms_status": None,
        "ptp_sms_to": None,
        "triggers": [],
    }

    try:
        if bedrock_client is None:  # pragma: no cover - requires AWS creds/network
            import boto3

            bedrock_client = boto3.client(
                "bedrock-runtime", region_name=settings.aws_region
            )

        transcript = _transcript_text(call_record)
        parsed = _classify(transcript, settings, bedrock_client)

        disposition = _normalize_disposition(parsed.get("disposition"))
        reference_date = datetime.now(timezone.utc).date()
        structured_data = _build_structured_data(parsed, reference_date)
        next_best_action = parsed.get("next_best_action")

        changes["disposition"] = disposition
        changes["structured_data"] = structured_data
        changes["next_best_action"] = (
            next_best_action if isinstance(next_best_action, str) else None
        )

        triggers: list[str] = []
        # -- downstream triggers ------------------------------------------
        if disposition == "promise_to_pay":
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
        elif disposition == "dispute_raised":
            _apply_dispute_trigger(account_id, store)
            changes["escalated"] = True
            triggers.append("escalation_to_human_specialist")
        elif disposition == "wrong_number":
            _emit("wrong_number_flagged", account_id=account_id)
            triggers.append("wrong_number_flagged")

        changes["triggers"] = triggers
        changes["intelligence_status"] = "ready"
        _emit(
            "postcall_intelligence_ready",
            call_sid=call_id,
            account_id=account_id,
            disposition=disposition,
            triggers=triggers,
        )
    except Exception as exc:  # noqa: BLE001 - must never crash the caller
        changes["intelligence_status"] = "failed"
        logger.exception(
            "call_sid=<%s> | post-call intelligence extraction failed: %s",
            call_id,
            exc,
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
