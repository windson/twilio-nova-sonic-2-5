"""Tests for outbound call TwiML generation (Twilio client mocked)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from app.config import Settings
from app.twilio_client import build_stream_twiml, place_outbound_call


def _settings(**overrides) -> Settings:
    base = dict(
        nova_model_id="amazon.nova-2-5-sonic",
        aws_region="us-east-1",
        app_log_group="/ecs/ar-collections-nova-sonic/app",
        twilio_account_sid="AC_test_sid",
        twilio_auth_token="test_token_value",
        twilio_from_number="+15005550006",
        twilio_verified_caller_id="+15005550010",
        public_base_url="https://example.ngrok.app",
    )
    base.update(overrides)
    return Settings(**base)


def test_build_stream_twiml_contains_connect_stream_and_wss_media():
    twiml = build_stream_twiml("https://example.ngrok.app", account_id="DUE-42")
    assert "<Connect>" in twiml
    assert "<Stream" in twiml
    assert 'wss://example.ngrok.app/media' in twiml
    assert 'name="accountId"' in twiml
    assert 'value="DUE-42"' in twiml


def test_build_stream_twiml_handles_bare_and_ws_hosts():
    assert "wss://host.example/media" in build_stream_twiml("host.example")
    assert "wss://host.example/media" in build_stream_twiml("ws://host.example/")
    assert "wss://host.example/media" in build_stream_twiml("https://host.example")


def test_place_outbound_call_uses_from_number_and_mocked_client():
    fake_call = MagicMock()
    fake_call.sid = "CA_test_call_sid"
    fake_client = MagicMock()
    fake_client.calls.create.return_value = fake_call
    factory = MagicMock(return_value=fake_client)

    result = place_outbound_call(
        "+15005550010",
        {"id": "DUE-99"},
        settings=_settings(),
        client_factory=factory,
    )

    # No real network: factory called with creds, calls.create called once.
    factory.assert_called_once_with("AC_test_sid", "test_token_value")
    fake_client.calls.create.assert_called_once()
    _, kwargs = fake_client.calls.create.call_args
    assert kwargs["from_"] == "+15005550006"
    assert kwargs["to"] == "+15005550010"
    twiml = kwargs["twiml"]
    assert "<Connect>" in twiml and "<Stream" in twiml
    assert "wss://example.ngrok.app/media" in twiml
    assert 'value="DUE-99"' in twiml

    assert result.call_sid == "CA_test_call_sid"
    assert result.account_id == "DUE-99"
    assert result.from_number == "+15005550006"


def test_place_outbound_call_requires_config():
    factory = MagicMock()
    with pytest.raises(ValueError):
        place_outbound_call(
            "+15005550010",
            {"id": "X"},
            settings=_settings(twilio_account_sid=None),
            client_factory=factory,
        )
    factory.assert_not_called()
