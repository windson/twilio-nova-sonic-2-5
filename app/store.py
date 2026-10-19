"""Pluggable persistence for due/account records and call analytics.

Defines a small storage interface plus an in-memory default implementation.
FEAT-005 can add a DynamoDB-backed store that satisfies the same
:class:`Store` protocol without any changes to the business logic.

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
        "last_updated": str,        # ISO timestamp
    }

CallAnalytics record (produced by :mod:`app.metrics`)::

    {
        "call_sid": str,
        "account_id": str,
        ...metrics fields..., "turns": [...], "events_log": [...]
    }
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


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@runtime_checkable
class Store(Protocol):
    """Storage interface for due records and call analytics.

    A DynamoDB implementation (FEAT-005) only needs to satisfy these methods.
    """

    def get_due(self, account_id: str) -> dict[str, Any] | None:  # pragma: no cover - protocol
        ...

    def update_due(self, account_id: str, **changes: Any) -> dict[str, Any]:  # pragma: no cover
        ...

    def list_dues(self) -> list[dict[str, Any]]:  # pragma: no cover - protocol
        ...

    def save_call_analytics(self, record: Mapping[str, Any]) -> dict[str, Any]:  # pragma: no cover
        ...

    def list_calls(self) -> list[dict[str, Any]]:  # pragma: no cover - protocol
        ...

    def get_call(self, call_id: str) -> dict[str, Any] | None:  # pragma: no cover - protocol
        ...


def _seed_dues() -> dict[str, dict[str, Any]]:
    """A couple of demo due records so the agent/UI have data out of the box."""
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
            "last_updated": now,
        },
        {
            "account_id": "ACC-1002",
            "debtor_name": "Priya Narayan",
            "amount_due": 1250.00,
            "currency": "USD",
            "due_date": "2024-06-01",
            "status": "open",
            "notes": "",
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
        self._resource: Any | None = None
        self._dues: Any | None = None
        self._calls: Any | None = None

    # -- lazy boto3 wiring ----------------------------------------------------

    def _resource_handle(self) -> Any:
        if self._resource is None:
            with self._lock:
                if self._resource is None:
                    if self._resource_factory is not None:
                        self._resource = self._resource_factory()
                    else:  # pragma: no cover - requires AWS creds/network
                        import boto3

                        self._resource = boto3.resource(
                            "dynamodb", region_name=self._region_name
                        )
        return self._resource

    def _dues_table(self) -> Any:
        if self._dues is None:
            self._dues = self._resource_handle().Table(self._dues_table_name)
        return self._dues

    def _calls_table(self) -> Any:
        if self._calls is None:
            self._calls = self._resource_handle().Table(self._calls_table_name)
        return self._calls

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
        rec["last_updated"] = _utcnow_iso()
        self._dues_table().put_item(Item=_to_dynamo(rec))
        return rec

    def update_due(self, account_id: str, **changes: Any) -> dict[str, Any]:
        current = self.get_due(account_id)
        if current is None:
            raise KeyError(f"unknown account_id: {account_id}")
        for key, value in changes.items():
            if value is not None:
                current[key] = value
        current["last_updated"] = _utcnow_iso()
        self._dues_table().put_item(Item=_to_dynamo(current))
        return current

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

    def list_calls(self) -> list[dict[str, Any]]:
        resp = self._calls_table().scan()
        items = resp.get("Items", [])
        return [_from_dynamo(i) for i in items]

    def get_call(self, call_id: str) -> dict[str, Any] | None:
        resp = self._calls_table().get_item(Key={"call_sid": call_id})
        item = resp.get("Item")
        return _from_dynamo(item) if item is not None else None


# Process-wide default store. FEAT-005 can swap this via set_default_store.
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
    """Override the process-wide default store (used by FEAT-005 / tests)."""
    global _default_store
    with _default_lock:
        _default_store = store
