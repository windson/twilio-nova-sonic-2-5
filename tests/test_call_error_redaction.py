"""SDK error payloads must not escape through HTTP responses or application logs.

All external clients are mocked; these tests never place calls or send SMS.
"""

import logging
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from app import main, postcall
from app.config import get_settings
from app.store import InMemoryStore


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "synthetic-account-sid")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "synthetic-auth-sentinel")
    monkeypatch.setenv("TWILIO_FROM_NUMBER", "+15005550006")
    monkeypatch.setenv("TWILIO_VERIFIED_CALLER_ID", "+15005550010")
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://example.com")
    monkeypatch.setenv("PAYMENT_LINK_BASE_URL", "https://pay.example.com")
    monkeypatch.setenv("TWILIO_TRIAL_MODE", "true")
    return get_settings()


@pytest.mark.parametrize("error_type,status", [(RuntimeError, 502), (ValueError, 503)])
def test_call_sdk_failure_does_not_return_or_log_payload(
    monkeypatch, caplog, configured, error_type, status
):
    store = InMemoryStore()
    monkeypatch.setattr(main, "get_default_store", lambda: store)
    monkeypatch.setattr(main, "get_settings", lambda: configured)
    client_mock = MagicMock(side_effect=error_type(configured.twilio_auth_token))
    monkeypatch.setattr(main, "place_outbound_call", client_mock)

    with caplog.at_level(logging.WARNING):
        response = TestClient(main.app).post("/api/calls", json={"account_id": "ACC-1001"})

    assert response.status_code == status
    assert configured.twilio_auth_token not in response.text
    assert configured.twilio_auth_token not in caplog.text
    assert error_type.__name__ in caplog.text
    assert all(record.exc_info is None for record in caplog.records if record.name == "app.main")
    client_mock.assert_called_once()


def test_sms_sdk_failure_does_not_log_payload(caplog, configured):
    client = MagicMock()
    client.messages.create.side_effect = RuntimeError(configured.twilio_auth_token)

    with caplog.at_level(logging.INFO):
        result = postcall._apply_ptp_trigger(
            "ACC-1001", {}, InMemoryStore(), configured, lambda *_: client
        )

    assert result["status"] == "blocked_trial"
    assert configured.twilio_auth_token not in caplog.text
    assert any(getattr(record, "data", {}).get("error_type") == "RuntimeError"
               for record in caplog.records)
    client.messages.create.assert_called_once()


@pytest.mark.asyncio
async def test_extraction_sdk_failure_does_not_log_payload(caplog, configured):
    bedrock = MagicMock()
    bedrock.converse.side_effect = RuntimeError(configured.twilio_auth_token)
    twilio = MagicMock()
    store = InMemoryStore()
    record = store.save_call_analytics(
        {
            "call_sid": "CA_synthetic",
            "account_id": "ACC-1001",
            "transcripts": [{"role": "user", "text": "I will pay tomorrow", "is_final": True}],
        }
    )

    with caplog.at_level(logging.WARNING):
        changes = await postcall.extract_and_apply(
            record,
            store,
            configured,
            bedrock_client=bedrock,
            twilio_client_factory=lambda *_: twilio,
        )

    assert changes["intelligence_status"] == "failed"
    assert configured.twilio_auth_token not in caplog.text
    assert "RuntimeError" in caplog.text
    assert all(record.exc_info is None for record in caplog.records if record.name == "app.postcall")
    bedrock.converse.assert_called_once()
