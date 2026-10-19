"""Tests for the pluggable store: due records + call analytics round-trip."""

from __future__ import annotations

import pytest

from app.store import InMemoryStore


def test_seeded_dues_present():
    store = InMemoryStore()
    dues = store.list_dues()
    assert len(dues) >= 2
    ids = {d["account_id"] for d in dues}
    assert "ACC-1001" in ids


def test_update_due_changes_status_and_fields_and_persists():
    store = InMemoryStore()
    before = store.get_due("ACC-1001")
    assert before["status"] == "open"

    updated = store.update_due(
        "ACC-1001",
        status="promised_to_pay",
        promised_amount=200.0,
        promised_date="2024-07-01",
        notes="caller will pay next week",
    )
    assert updated["status"] == "promised_to_pay"
    assert updated["promised_amount"] == 200.0
    assert updated["promised_date"] == "2024-07-01"
    assert updated["notes"] == "caller will pay next week"
    assert updated["last_updated"] != before["last_updated"]

    # Change persists across a fresh read.
    again = store.get_due("ACC-1001")
    assert again["status"] == "promised_to_pay"
    assert again["promised_amount"] == 200.0


def test_update_due_ignores_none_values():
    store = InMemoryStore()
    store.update_due("ACC-1002", status="disputed", notes=None)
    rec = store.get_due("ACC-1002")
    assert rec["status"] == "disputed"
    assert rec["notes"] == ""  # untouched seeded empty string


def test_update_due_unknown_account_raises():
    store = InMemoryStore()
    with pytest.raises(KeyError):
        store.update_due("NOPE", status="paid")


def test_get_due_returns_copy_not_reference():
    store = InMemoryStore()
    rec = store.get_due("ACC-1001")
    rec["status"] = "mutated"
    assert store.get_due("ACC-1001")["status"] != "mutated"


def test_save_and_get_call_analytics_round_trip():
    store = InMemoryStore()
    record = {
        "call_sid": "CA123",
        "account_id": "ACC-1001",
        "total_e2e_ms": 4200.0,
        "turns": [{"index": 0, "duration_ms": 900.0}],
        "events_log": [{"event_type": "call_placed"}],
    }
    saved = store.save_call_analytics(record)
    assert saved["call_sid"] == "CA123"

    fetched = store.get_call("CA123")
    assert fetched["account_id"] == "ACC-1001"
    assert fetched["total_e2e_ms"] == 4200.0
    assert fetched["turns"][0]["duration_ms"] == 900.0

    calls = store.list_calls()
    assert any(c["call_sid"] == "CA123" for c in calls)


def test_save_call_analytics_without_call_sid_uses_fallback_key():
    store = InMemoryStore()
    saved = store.save_call_analytics({"account_id": "ACC-1002"})
    assert saved["call_sid"] == "ACC-1002"
    assert store.get_call("ACC-1002") is not None
