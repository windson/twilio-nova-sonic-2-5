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
import threading
from datetime import datetime, timezone
from typing import Any, Mapping, Protocol, runtime_checkable

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


# Process-wide default store. FEAT-005 can swap this via set_default_store.
_default_store: Store | None = None
_default_lock = threading.Lock()


def get_default_store() -> Store:
    """Return the process-wide default store, creating an in-memory one lazily."""
    global _default_store
    if _default_store is None:
        with _default_lock:
            if _default_store is None:
                _default_store = InMemoryStore()
    return _default_store


def set_default_store(store: Store) -> None:
    """Override the process-wide default store (used by FEAT-005 / tests)."""
    global _default_store
    with _default_lock:
        _default_store = store
