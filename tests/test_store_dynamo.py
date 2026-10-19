"""Tests for env-var-driven DynamoDB store selection (boto3 fully mocked).

No real AWS calls are made: the DynamoDB resource/tables are replaced by small
in-process fakes so we can assert both the backend-selection logic and that the
:class:`DynamoDbStore` satisfies the same contract as the in-memory store.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

import app.store as store_mod
from app.store import DynamoDbStore, InMemoryStore, Store


class ConditionalCheckFailedException(Exception):
    """Mimics botocore's ConditionalCheckFailedException by class name."""


class _FakeTable:
    """Minimal in-memory stand-in for a boto3 DynamoDB Table resource."""

    def __init__(self, key_name: str) -> None:
        self._key_name = key_name
        self.items: dict[str, dict] = {}

    def put_item(self, Item):  # noqa: N803 - match boto3 kwarg name
        key = Item[self._key_name]
        self.items[key] = dict(Item)
        return {}

    def get_item(self, Key):  # noqa: N803 - match boto3 kwarg name
        key = Key[self._key_name]
        item = self.items.get(key)
        return {"Item": dict(item)} if item is not None else {}

    def update_item(
        self,
        Key,  # noqa: N803 - match boto3 kwarg name
        UpdateExpression,  # noqa: N803
        ConditionExpression=None,  # noqa: N803
        ExpressionAttributeNames=None,  # noqa: N803
        ExpressionAttributeValues=None,  # noqa: N803
        ReturnValues=None,  # noqa: N803
    ):
        """Apply a SET UpdateExpression with an attribute_exists condition.

        Supports exactly the shape :meth:`DynamoDbStore.update_due` emits:
        ``SET #n0 = :v0, #n1 = :v1`` with an ``attribute_exists(#pk)``
        condition. Raises a ConditionalCheckFailedException-named error when the
        item is absent, matching real DynamoDB behavior.
        """
        key = Key[self._key_name]
        names = ExpressionAttributeNames or {}
        values = ExpressionAttributeValues or {}

        if ConditionExpression and "attribute_exists" in ConditionExpression:
            if key not in self.items:
                raise ConditionalCheckFailedException("item does not exist")

        item = self.items.get(key, {self._key_name: key})
        assignments = UpdateExpression[len("SET ") :].split(",")
        for assignment in assignments:
            name_ph, value_ph = (part.strip() for part in assignment.split("="))
            attr = names[name_ph]
            item[attr] = values[value_ph]
        self.items[key] = item
        if ReturnValues == "ALL_NEW":
            return {"Attributes": dict(item)}
        return {}

    def scan(self):
        return {"Items": [dict(v) for v in self.items.values()]}


class _FakeResource:
    """Stand-in for boto3.resource('dynamodb')."""

    def __init__(self) -> None:
        self.tables: dict[str, _FakeTable] = {
            "dues": _FakeTable("account_id"),
            "calls": _FakeTable("call_sid"),
        }

    def Table(self, name):  # noqa: N802 - match boto3 resource API
        # Map the configured table names onto our two fakes by key shape.
        if name.endswith("calls") or "call" in name.lower():
            return self.tables["calls"]
        return self.tables["dues"]


@pytest.fixture(autouse=True)
def _reset_default_store():
    """Ensure the module-level default store cache does not leak across tests."""
    store_mod.set_default_store(None)  # type: ignore[arg-type]
    store_mod._default_store = None
    yield
    store_mod._default_store = None


def _make_dynamo_store() -> DynamoDbStore:
    resource = _FakeResource()
    return DynamoDbStore(
        "ar-dues", "ar-calls", region_name="us-east-1", resource_factory=lambda: resource
    )


def test_selects_in_memory_when_table_env_absent(monkeypatch):
    monkeypatch.delenv("DUES_TABLE", raising=False)
    monkeypatch.delenv("CALLS_TABLE", raising=False)
    store = store_mod.get_default_store()
    assert isinstance(store, InMemoryStore)


def test_selects_dynamo_when_both_table_envs_present(monkeypatch):
    monkeypatch.setenv("DUES_TABLE", "ar-dues")
    monkeypatch.setenv("CALLS_TABLE", "ar-calls")
    # Avoid touching real boto3: patch the resource factory used lazily.
    monkeypatch.setattr(
        store_mod.DynamoDbStore,
        "_resource_handle",
        lambda self: _FakeResource(),
    )
    store = store_mod.get_default_store()
    assert isinstance(store, DynamoDbStore)
    assert store._dues_table_name == "ar-dues"
    assert store._calls_table_name == "ar-calls"


def test_in_memory_used_when_only_one_table_env_present(monkeypatch):
    monkeypatch.setenv("DUES_TABLE", "ar-dues")
    monkeypatch.delenv("CALLS_TABLE", raising=False)
    store = store_mod.get_default_store()
    assert isinstance(store, InMemoryStore)


def test_dynamo_store_satisfies_store_protocol():
    assert isinstance(_make_dynamo_store(), Store)


def test_dynamo_due_round_trip_and_update():
    store = _make_dynamo_store()
    store.add_due(
        {
            "account_id": "ACC-9001",
            "debtor_name": "Dana Doe",
            "amount_due": 428.57,
            "due_date": "2024-05-15",
        }
    )
    rec = store.get_due("ACC-9001")
    assert rec is not None
    assert rec["debtor_name"] == "Dana Doe"
    # float preserved exactly (stored as Decimal, read back as float)
    assert rec["amount_due"] == 428.57
    assert rec["status"] == "open"

    updated = store.update_due("ACC-9001", status="promised_to_pay", promised_amount=100.0)
    assert updated["status"] == "promised_to_pay"
    assert store.get_due("ACC-9001")["promised_amount"] == 100.0

    dues = store.list_dues()
    assert any(d["account_id"] == "ACC-9001" for d in dues)


def test_dynamo_update_unknown_account_raises():
    store = _make_dynamo_store()
    with pytest.raises(KeyError):
        store.update_due("NOPE", status="paid")


def test_dynamo_call_analytics_round_trip():
    store = _make_dynamo_store()
    record = {
        "call_sid": "CA999",
        "account_id": "ACC-9001",
        "total_e2e_ms": 4200.0,
        "turns": [{"index": 0, "duration_ms": 900.0}],
        "events_log": [{"event_type": "call_placed"}],
    }
    store.save_call_analytics(record)
    fetched = store.get_call("CA999")
    assert fetched is not None
    assert fetched["account_id"] == "ACC-9001"
    assert fetched["total_e2e_ms"] == 4200.0
    assert fetched["turns"][0]["duration_ms"] == 900.0
    assert any(c["call_sid"] == "CA999" for c in store.list_calls())


def test_dynamo_save_without_call_sid_uses_fallback_key():
    store = _make_dynamo_store()
    saved = store.save_call_analytics({"account_id": "ACC-9001"})
    assert saved["call_sid"] == "ACC-9001"
    assert store.get_call("ACC-9001") is not None


def test_decimal_helpers_round_trip():
    original = {"amount": 12.5, "count": 3, "nested": [{"x": 1.25}], "flag": True}
    converted = store_mod._to_dynamo(original)
    assert isinstance(converted["amount"], Decimal)
    assert isinstance(converted["nested"][0]["x"], Decimal)
    # bools must stay bools (not become Decimal)
    assert converted["flag"] is True
    back = store_mod._from_dynamo(converted)
    assert back["amount"] == 12.5
    assert back["count"] == 3
    assert back["nested"][0]["x"] == 1.25
    assert back["flag"] is True
