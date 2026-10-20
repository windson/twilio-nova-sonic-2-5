"""Tests for the pluggable store: due records + call analytics round-trip."""

from __future__ import annotations

import pytest

from app.store import CALL_DISPOSITIONS, InMemoryStore


def test_seeded_dues_present():
    store = InMemoryStore()
    dues = store.list_dues()
    assert len(dues) >= 2
    ids = {d["account_id"] for d in dues}
    assert "ACC-1001" in ids
    assert "ACC-1002" in ids


def test_seeded_acc_1002_debtor_name():
    store = InMemoryStore()
    rec = store.get_due("ACC-1002")
    assert rec is not None
    assert rec["debtor_name"] == "Pavan Navule"


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


def test_call_dispositions_taxonomy_contains_required_tags():
    for tag in (
        "promise_to_pay",
        "refusal_to_pay",
        "dispute_raised",
        "inbound_callback_requested",
        "wrong_number",
    ):
        assert tag in CALL_DISPOSITIONS
    # Plus the added sensible others.
    for tag in ("no_answer", "left_voicemail", "already_paid", "undetermined"):
        assert tag in CALL_DISPOSITIONS


def test_seeded_dues_carry_new_state_fields():
    store = InMemoryStore()
    rec = store.get_due("ACC-1001")
    assert rec["collection_paused"] is False
    assert rec["escalated"] is False


def test_update_call_analytics_merges_intelligence():
    store = InMemoryStore()
    store.save_call_analytics(
        {"call_sid": "CA-INT", "account_id": "ACC-1001", "intelligence_status": "pending"}
    )
    updated = store.update_call_analytics(
        "CA-INT",
        disposition="promise_to_pay",
        intelligence_status="ready",
        next_best_action="Send payment link",
    )
    assert updated["disposition"] == "promise_to_pay"
    assert updated["intelligence_status"] == "ready"
    # Original fields preserved.
    assert updated["account_id"] == "ACC-1001"

    again = store.get_call("CA-INT")
    assert again["next_best_action"] == "Send payment link"


def test_update_call_analytics_unknown_id_raises():
    store = InMemoryStore()
    with pytest.raises(KeyError):
        store.update_call_analytics("NOPE", disposition="paid")
