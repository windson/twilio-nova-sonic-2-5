"""Settings diagnostics must not reveal credentials, contact details, or URLs."""

import json

import pytest

from app.config import get_settings


def test_settings_repr_and_summary_omit_sensitive_values(monkeypatch):
    sensitive_values = {
        "TWILIO_ACCOUNT_SID": "synthetic-account-sid",
        "TWILIO_AUTH_TOKEN": "synthetic-auth-sentinel",
        "TWILIO_FROM_NUMBER": "+15005550006",
        "TWILIO_VERIFIED_CALLER_ID": "+15005550010",
        "PUBLIC_BASE_URL": "https://user:synthetic-web-sentinel@example.com",
        "PAYMENT_LINK_BASE_URL": "https://example.com/pay?code=synthetic-pay-sentinel",
        "OTEL_EXPORTER_OTLP_ENDPOINT": "https://user:synthetic-otel-sentinel@example.com",
    }
    for name, value in sensitive_values.items():
        monkeypatch.setenv(name, value)

    settings = get_settings()
    rendered = repr(settings)
    summary = json.dumps(settings.safe_summary())

    for value in sensitive_values.values():
        assert value not in rendered
        assert value not in summary
    assert "synthetic-" not in rendered
    assert settings.twilio_auth_token == sensitive_values["TWILIO_AUTH_TOKEN"]
    assert settings.twilio_configured is True
    assert settings.safe_summary()["twilio_configured"] is True
    assert "nova_model_id=" in rendered


@pytest.mark.parametrize("raw,expected", [("", None), (" high ", "HIGH"), ("MEDIUM", "MEDIUM"), ("LOW", "LOW")])
def test_endpointing_sensitivity_is_optional_and_validated(monkeypatch, raw, expected):
    monkeypatch.setenv("NOVA_ENDPOINTING_SENSITIVITY", raw)
    assert get_settings().endpointing_sensitivity == expected


def test_invalid_explicit_endpointing_sensitivity_fails_clearly(monkeypatch):
    monkeypatch.setenv("NOVA_ENDPOINTING_SENSITIVITY", "fastest")
    with pytest.raises(ValueError, match="NOVA_ENDPOINTING_SENSITIVITY must be"):
        get_settings()


def test_payment_timezone_and_build_revision_exposed_in_safe_config(monkeypatch):
    monkeypatch.setenv("PAYMENT_TIMEZONE", "Asia/Kolkata")
    monkeypatch.setenv("BUILD_REVISION", "synthetic-revision")
    summary = get_settings().safe_summary()
    assert summary["payment_timezone"] == "Asia/Kolkata"
    assert summary["build_revision"] == "synthetic-revision"
