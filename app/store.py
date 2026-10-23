"""Pluggable persistence for due/account records and call analytics.

Defines the :class:`Store` protocol with in-memory and DynamoDB implementations.
Both provide the same interface to the business logic.

Entities
--------
Due/Account record::

    {
        "account_id": str,
        "debtor_name": str,
        "amount_due": float,
        "currency": str,            # e.g. "USD"
        "due_date": str,            # ISO date, e.g. "2024-05-01"
        "status": str,              # open|promised_to_pay|paid|disputed|callback
        "notes": str,
        "phone": str | None,        # debtor's E.164 number (PTP SMS destination)
        "collection_paused": bool,  # true when a PTP pauses auto-collection
        "escalated": bool,          # true when a dispute escalates to a human
        "last_updated": str,        # ISO timestamp
    }

CallAnalytics record (produced by :mod:`app.metrics`, enriched post-call by
:mod:`app.postcall`)::

    {
        "call_sid": str,
        "account_id": str,
        ...metrics fields..., "turns": [...], "events_log": [...],
        # Post-call intelligence, blank at call end, filled async:
        "disposition": str | None,          # one of CALL_DISPOSITIONS
        "structured_data": {
            "payment_specifics": {
                "promised_amount": float | None,
                "agreed_date": str | None,       # resolved ISO date
                "preferred_payment_method": str | None,
            },
            "contextual_data": {
                "reason_for_delinquency": str | None,
                "customer_sentiment": str | None,
            },
        },
        "next_best_action": str | None,
        "intelligence_status": str,          # pending|ready|partial|failed
        "intelligence_metadata": dict,      # optional model/API, validation, field status, usage
        "collection_paused": bool,           # downstream trigger result (PTP)
        "escalated": bool,                   # downstream trigger result (dispute)
        "ptp_sms_status": str | None,        # sent|blocked_trial|failed
        "ptp_sms_to": str | None,            # masked PTP SMS destination
        "triggers": [str, ...],              # which downstream triggers ran
    }

``promised_amount`` is numeric and is stored as ``Decimal`` in DynamoDB via
:func:`_to_dynamo` (and converted back by :func:`_from_dynamo`).
"""

from __future__ import annotations

import copy
import json
import logging
import os
import threading
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Mapping, Protocol, runtime_checkable

logger = logging.getLogger(__name__)

# Allowed due-record statuses (documented for the collections tool + UI).
DUE_STATUSES = (
    "open",
    "promised_to_pay",
    "paid",
    "disputed",
    "callback",
)

# Standardized Call Disposition taxonomy (the "what happened" outcome tag).
#
# A disposition is a richer, post-call outcome tag than DUE_STATUSES; it drives
# downstream automation (CRM update, workflow routing, compliance). Each tag's
# downstream directive is documented inline:
#
# - promise_to_pay (PTP): debtor committed to a date/amount. Directive: send a
#   payment-link SMS and PAUSE further automated collection attempts
#   (collection_paused=true on the account).
# - refusal_to_pay: debtor explicitly refuses. Directive: route to a human
#   collections strategy queue; do not keep auto-dialing on the same cadence.
# - dispute_raised: debtor disputes the balance. Directive: ESCALATE the account
#   to a human specialist immediately (escalated=true) and emit an escalation
#   event; compliance-sensitive.
# - inbound_callback_requested: debtor asked to be called back / will call back.
#   Directive: schedule a follow-up touchpoint.
# - wrong_number: reached the wrong party. Directive: flag the account so the
#   number is not re-dialed; suppress further contact to that number.
# - no_answer: nobody picked up. Directive: reschedule per retry policy.
# - left_voicemail: voicemail left. Directive: reschedule and wait for callback.
# - already_paid: debtor states the balance is already paid. Directive: verify
#   and reconcile; pause collection.
# - undetermined: outcome could not be classified from the transcript.
#   Directive: leave for human review; no automated trigger.
CALL_DISPOSITIONS = (
    "promise_to_pay",
    "refusal_to_pay",
    "dispute_raised",
    "inbound_callback_requested",
    "wrong_number",
    "no_answer",
    "left_voicemail",
    "already_paid",
    "undetermined",
)


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def blank_structured_data() -> dict[str, Any]:
    """Return an empty structured-conversation-data skeleton.

    The UI renders blank placeholders from this shape before the post-call
    intelligence has run; :mod:`app.postcall` fills it in afterwards.
    """
    return {
        "payment_specifics": {
            "promised_amount": None,
            "agreed_date": None,
            "preferred_payment_method": None,
        },
        "contextual_data": {
            "reason_for_delinquency": None,
            "customer_sentiment": None,
        },
    }


@runtime_checkable
class Store(Protocol):
    """Storage interface for due records and call analytics.

    A DynamoDB implementation only needs to satisfy these methods.
    """

    def get_due(self, account_id: str) -> dict[str, Any] | None:  # pragma: no cover - protocol
        ...

    def update_due(self, account_id: str, **changes: Any) -> dict[str, Any]:  # pragma: no cover
        ...

    def update_promised_date_if_current(
        self, account_id: str, *, phrase: str, call_sid: str | None, promised_date: str,
    ) -> bool:  # pragma: no cover - protocol
        ...

    def list_dues(self) -> list[dict[str, Any]]:  # pragma: no cover - protocol
        ...

    def save_call_analytics(self, record: Mapping[str, Any]) -> dict[str, Any]:  # pragma: no cover
        ...

    def update_call_analytics(self, call_id: str, **changes: Any) -> dict[str, Any]:  # pragma: no cover
        ...

    def list_calls(self) -> list[dict[str, Any]]:  # pragma: no cover - protocol
        ...

    def get_call(self, call_id: str) -> dict[str, Any] | None:  # pragma: no cover - protocol
        ...


def _seed_dues() -> dict[str, dict[str, Any]]:
    """Fictional demo records for local use and tests."""
    now = _utcnow_iso()
    records = [
        {
            "account_id": "ACC-1001",
            "debtor_name": "Jordan Rivera",
            "amount_due": 428.57,
            "currency": "USD",
            "due_date": "2024-05-15",
            "status": "open",
            "notes": "",
            # Debtor contact number. On a Twilio trial the PTP SMS can only go
            # to the verified number, so these demo records carry None and the
            # trial path falls back to TWILIO_VERIFIED_CALLER_ID; populate with
            # a real E.164 number for production/off-trial sends.
            "phone": None,
            "collection_paused": False,
            "escalated": False,
            "last_updated": now,
        },
        {
            "account_id": "ACC-1002",
            "debtor_name": "Alex Morgan",
            "amount_due": 1250.00,
            "currency": "USD",
            "due_date": "2024-06-01",
            "status": "open",
            "notes": "",
            "phone": None,
            "collection_paused": False,
            "escalated": False,
            "last_updated": now,
        },
    ]
    return {r["account_id"]: r for r in records}


class InMemoryStore:
    """Thread-safe in-memory :class:`Store` implementation.

    Suitable for local development, tests, and the demo. State is seeded with a
    couple of due records and is not persisted across process restarts.
    """

    def __init__(self, *, seed: bool = True) -> None:
        self._lock = threading.RLock()
        self._dues: dict[str, dict[str, Any]] = _seed_dues() if seed else {}
        self._calls: dict[str, dict[str, Any]] = {}

    # -- Due / account records ------------------------------------------------

    def get_due(self, account_id: str) -> dict[str, Any] | None:
        with self._lock:
            rec = self._dues.get(account_id)
            return copy.deepcopy(rec) if rec is not None else None

    def add_due(self, record: Mapping[str, Any]) -> dict[str, Any]:
        """Insert or replace a due record (helper for seeding/tests)."""
        rec = dict(record)
        account_id = str(rec["account_id"])
        rec.setdefault("currency", "USD")
        rec.setdefault("status", "open")
        rec.setdefault("notes", "")
        rec.setdefault("phone", None)
        rec.setdefault("collection_paused", False)
        rec.setdefault("escalated", False)
        rec["account_id"] = account_id
        rec["last_updated"] = _utcnow_iso()
        with self._lock:
            self._dues[account_id] = rec
            return copy.deepcopy(rec)

    def update_due(self, account_id: str, **changes: Any) -> dict[str, Any]:
        """Apply ``changes`` to a due record and bump ``last_updated``.

        Raises:
            KeyError: If the account id is unknown.
        """
        with self._lock:
            rec = self._dues.get(account_id)
            if rec is None:
                raise KeyError(f"unknown account_id: {account_id}")
            for key, value in changes.items():
                if value is not None:
                    rec[key] = value
            rec["last_updated"] = _utcnow_iso()
            return copy.deepcopy(rec)

    def update_promised_date_if_current(
        self, account_id: str, *, phrase: str, call_sid: str | None, promised_date: str,
    ) -> bool:
        """Commit a resolved date only while its original capture still owns it."""
        with self._lock:
            rec = self._dues.get(account_id)
            if rec is None or rec.get("promised_date_phrase") != phrase:
                return False
            if rec.get("promised_date_phrase_call_sid") not in (None, call_sid):
                return False
            rec["promised_date"] = promised_date
            rec["last_updated"] = _utcnow_iso()
            return True

    def list_dues(self) -> list[dict[str, Any]]:
        with self._lock:
            return [copy.deepcopy(r) for r in self._dues.values()]

    # -- Call analytics -------------------------------------------------------

    def save_call_analytics(self, record: Mapping[str, Any]) -> dict[str, Any]:
        """Persist a CallAnalytics record keyed by ``call_sid``.

        Falls back to ``account_id`` or a synthetic key when ``call_sid`` is
        missing so analytics are never silently dropped.
        """
        rec = copy.deepcopy(dict(record))
        call_id = rec.get("call_sid") or rec.get("call_id") or rec.get("account_id")
        if not call_id:
            call_id = f"call-{len(self._calls) + 1}"
        rec.setdefault("call_sid", call_id)
        with self._lock:
            self._calls[str(call_id)] = rec
            return copy.deepcopy(rec)

    def update_call_analytics(self, call_id: str, **changes: Any) -> dict[str, Any]:
        """Merge ``changes`` onto a persisted CallAnalytics record.

        Used by :mod:`app.postcall` to write post-call intelligence onto the
        record saved at call end. Raises ``KeyError`` if the call is unknown.
        """
        with self._lock:
            rec = self._calls.get(call_id)
            if rec is None:
                raise KeyError(f"unknown call_id: {call_id}")
            for key, value in changes.items():
                rec[key] = value
            return copy.deepcopy(rec)

    def list_calls(self) -> list[dict[str, Any]]:
        with self._lock:
            return [copy.deepcopy(r) for r in self._calls.values()]

    def get_call(self, call_id: str) -> dict[str, Any] | None:
        with self._lock:
            rec = self._calls.get(call_id)
            return copy.deepcopy(rec) if rec is not None else None


# -- DynamoDB-backed store ----------------------------------------------------


def _to_dynamo(value: Any) -> Any:
    """Recursively convert Python values to DynamoDB-friendly types.

    DynamoDB rejects ``float``; numbers must be ``Decimal``. We round-trip
    floats through ``str`` so values like ``428.57`` are stored exactly.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, Mapping):
        return {k: _to_dynamo(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_dynamo(v) for v in value]
    return value


def _from_dynamo(value: Any) -> Any:
    """Recursively convert DynamoDB types back to plain Python values."""
    if isinstance(value, Decimal):
        # Preserve integers as int, everything else as float.
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, Mapping):
        return {k: _from_dynamo(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_from_dynamo(v) for v in value]
    return value


class DynamoDbStore:
    """DynamoDB-backed :class:`Store` implementation.

    Selected automatically on AWS when ``DUES_TABLE`` / ``CALLS_TABLE`` env
    vars are present (see :func:`get_default_store`). Uses two tables:

    - dues table: partition key ``account_id`` (String)
    - calls table: partition key ``call_sid`` (String)

    Call-analytics records contain nested ``turns`` / ``events_log`` lists; the
    whole record is stored as a single item. A boto3 resource is created lazily
    so importing this module never touches the network, which keeps tests fast
    and lets boto3 be monkeypatched.
    """

    def __init__(
        self,
        dues_table: str,
        calls_table: str,
        *,
        region_name: str | None = None,
        resource_factory: Any | None = None,
    ) -> None:
        self._dues_table_name = dues_table
        self._calls_table_name = calls_table
        self._region_name = region_name or os.environ.get("AWS_REGION") or os.environ.get(
            "AWS_DEFAULT_REGION"
        )
        self._resource_factory = resource_factory
        self._lock = threading.Lock()
        # boto3 Resource and Table objects are not documented thread-safe.
        # Each SDK worker thread owns its session/resource/table handles.
        self._handles = threading.local()

    # -- lazy boto3 wiring ----------------------------------------------------

    def _resource_handle(self) -> Any:
        resource = getattr(self._handles, "resource", None)
        if resource is None:
            if self._resource_factory is not None:
                resource = self._resource_factory()
            else:  # pragma: no cover - requires AWS creds/network
                import boto3
                from botocore.config import Config

                resource = boto3.Session(region_name=self._region_name).resource(
                    "dynamodb",
                    config=Config(connect_timeout=3, read_timeout=10,
                                  retries={"mode": "standard", "max_attempts": 2}),
                )
            self._handles.resource = resource
        return resource

    def _dues_table(self) -> Any:
        table = getattr(self._handles, "dues", None)
        if table is None:
            table = self._resource_handle().Table(self._dues_table_name)
            self._handles.dues = table
        return table

    def _calls_table(self) -> Any:
        table = getattr(self._handles, "calls", None)
        if table is None:
            table = self._resource_handle().Table(self._calls_table_name)
            self._handles.calls = table
        return table

    # -- Due / account records ------------------------------------------------

    def get_due(self, account_id: str) -> dict[str, Any] | None:
        resp = self._dues_table().get_item(Key={"account_id": account_id})
        item = resp.get("Item")
        return _from_dynamo(item) if item is not None else None

    def add_due(self, record: Mapping[str, Any]) -> dict[str, Any]:
        rec = dict(record)
        rec["account_id"] = str(rec["account_id"])
        rec.setdefault("currency", "USD")
        rec.setdefault("status", "open")
        rec.setdefault("notes", "")
        rec.setdefault("phone", None)
        rec.setdefault("collection_paused", False)
        rec.setdefault("escalated", False)
        rec["last_updated"] = _utcnow_iso()
        self._dues_table().put_item(Item=_to_dynamo(rec))
        return rec

    def update_due(self, account_id: str, **changes: Any) -> dict[str, Any]:
        """Atomically update a due record with a conditional UpdateExpression.

        Uses a single ``update_item`` call with ``attribute_exists(account_id)``
        as the condition, so the record must already exist and concurrent
        updates to different attributes do not clobber each other (unlike a
        read-then-full-put). Raises ``KeyError`` when the account is unknown.
        """
        applied = {k: v for k, v in changes.items() if v is not None}
        applied["last_updated"] = _utcnow_iso()

        # Build a SET expression with placeholders so reserved words / dotted
        # names are always safe.
        set_parts: list[str] = []
        expr_names: dict[str, str] = {"#pk": "account_id"}
        expr_values: dict[str, Any] = {}
        for i, (key, value) in enumerate(applied.items()):
            name_ph = f"#n{i}"
            value_ph = f":v{i}"
            set_parts.append(f"{name_ph} = {value_ph}")
            expr_names[name_ph] = key
            expr_values[value_ph] = _to_dynamo(value)
        update_expr = "SET " + ", ".join(set_parts)

        try:
            resp = self._dues_table().update_item(
                Key={"account_id": account_id},
                UpdateExpression=update_expr,
                ConditionExpression="attribute_exists(#pk)",
                ExpressionAttributeNames=expr_names,
                ExpressionAttributeValues=expr_values,
                ReturnValues="ALL_NEW",
            )
        except Exception as exc:  # noqa: BLE001 - map the conditional failure
            if type(exc).__name__ == "ConditionalCheckFailedException":
                raise KeyError(f"unknown account_id: {account_id}") from exc
            raise
        return _from_dynamo(resp.get("Attributes", {}))

    def update_promised_date_if_current(
        self, account_id: str, *, phrase: str, call_sid: str | None, promised_date: str,
    ) -> bool:
        """Atomically prevent an older call from replacing a newer promise."""
        try:
            self._dues_table().update_item(
                Key={"account_id": account_id},
                UpdateExpression="SET #date = :date, #updated = :updated",
                ConditionExpression=("attribute_exists(#pk) AND #phrase = :phrase AND "
                                     "(attribute_not_exists(#call) OR #call = :call)"),
                ExpressionAttributeNames={"#pk": "account_id", "#phrase": "promised_date_phrase",
                    "#call": "promised_date_phrase_call_sid", "#date": "promised_date",
                    "#updated": "last_updated"},
                ExpressionAttributeValues={":phrase": phrase, ":call": call_sid or "",
                    ":date": promised_date, ":updated": _utcnow_iso()},
            )
            return True
        except Exception as exc:
            if type(exc).__name__ == "ConditionalCheckFailedException":
                return False
            raise

    def list_dues(self) -> list[dict[str, Any]]:
        resp = self._dues_table().scan()
        items = resp.get("Items", [])
        return [_from_dynamo(i) for i in items]

    # -- Call analytics -------------------------------------------------------

    def save_call_analytics(self, record: Mapping[str, Any]) -> dict[str, Any]:
        rec = dict(record)
        call_id = rec.get("call_sid") or rec.get("call_id") or rec.get("account_id")
        if not call_id:
            call_id = f"call-{_utcnow_iso()}"
        rec["call_sid"] = str(call_id)
        self._calls_table().put_item(Item=_to_dynamo(rec))
        return rec

    def update_call_analytics(self, call_id: str, **changes: Any) -> dict[str, Any]:
        """Merge ``changes`` onto a persisted CallAnalytics item.

        Uses a single conditional ``update_item`` (``attribute_exists(call_sid)``)
        so post-call intelligence is written without clobbering the metrics
        fields. ``promised_amount`` and other numerics are converted to
        ``Decimal`` via :func:`_to_dynamo`. Raises ``KeyError`` when unknown.
        """
        set_parts: list[str] = []
        expr_names: dict[str, str] = {"#pk": "call_sid"}
        expr_values: dict[str, Any] = {}
        for i, (key, value) in enumerate(changes.items()):
            name_ph = f"#n{i}"
            value_ph = f":v{i}"
            set_parts.append(f"{name_ph} = {value_ph}")
            expr_names[name_ph] = key
            expr_values[value_ph] = _to_dynamo(value)
        update_expr = "SET " + ", ".join(set_parts)

        try:
            resp = self._calls_table().update_item(
                Key={"call_sid": call_id},
                UpdateExpression=update_expr,
                ConditionExpression="attribute_exists(#pk)",
                ExpressionAttributeNames=expr_names,
                ExpressionAttributeValues=expr_values,
                ReturnValues="ALL_NEW",
            )
        except Exception as exc:  # noqa: BLE001 - map the conditional failure
            if type(exc).__name__ == "ConditionalCheckFailedException":
                raise KeyError(f"unknown call_id: {call_id}") from exc
            raise
        return _from_dynamo(resp.get("Attributes", {}))

    def list_calls(self) -> list[dict[str, Any]]:
        table = self._calls_table()
        resp = table.scan()
        items = resp.get("Items", [])
        while resp.get("LastEvaluatedKey"):
            resp = table.scan(ExclusiveStartKey=resp["LastEvaluatedKey"])
            items.extend(resp.get("Items", []))
        return [_from_dynamo(i) for i in items]

    def get_call(self, call_id: str) -> dict[str, Any] | None:
        resp = self._calls_table().get_item(Key={"call_sid": call_id})
        item = resp.get("Item")
        return _from_dynamo(item) if item is not None else None


# Process-wide default store, configurable through set_default_store.
_default_store: Store | None = None
_default_lock = threading.Lock()


def _build_store_from_env() -> Store:
    """Pick the store backend from the environment.

    When both ``DUES_TABLE`` and ``CALLS_TABLE`` are set, use a DynamoDB-backed
    store (the AWS deployment). Otherwise fall back to the in-memory store used
    for local development and tests.
    """
    dues_table = os.environ.get("DUES_TABLE")
    calls_table = os.environ.get("CALLS_TABLE")
    if dues_table and calls_table:
        logger.info(
            json.dumps(
                {
                    "event_type": "store_backend_selected",
                    "backend": "dynamodb",
                    "dues_table": dues_table,
                    "calls_table": calls_table,
                }
            )
        )
        return DynamoDbStore(dues_table, calls_table)
    logger.info(
        json.dumps({"event_type": "store_backend_selected", "backend": "in_memory"})
    )
    return InMemoryStore()


def get_default_store() -> Store:
    """Return the process-wide default store, building it lazily.

    The backend is chosen from the environment the first time this is called:
    DynamoDB when ``DUES_TABLE``/``CALLS_TABLE`` are set, in-memory otherwise.
    """
    global _default_store
    if _default_store is None:
        with _default_lock:
            if _default_store is None:
                _default_store = _build_store_from_env()
    return _default_store


def set_default_store(store: Store) -> None:
    """Override the process-wide default store (used by application setup and tests)."""
    global _default_store
    with _default_lock:
        _default_store = store
