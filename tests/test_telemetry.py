"""Tests for GA Strands OTEL telemetry wiring and the OTEL config knobs.

These tests never require a live OTEL collector: ``StrandsTelemetry`` is
monkeypatched to a stub that records calls, so we assert only on how the app
drives it (configured exactly once, guarded against failure, exporter selected
from config) and on the new :class:`app.config.Settings` fields.
"""

from __future__ import annotations

import importlib

import pytest

import app.telemetry as telemetry_mod
from app.config import Settings, get_settings


def _settings(**overrides) -> Settings:
    base = dict(
        nova_model_id="amazon.nova-2-5-sonic",
        aws_region="us-east-1",
        app_log_group="/ecs/ar-collections-nova-sonic/app",
        extraction_model_id="amazon.nova-lite-v1:0",
        code_interpreter_id="aws.codeinterpreter.v1",
        payment_link_base_url="https://pay.example.com/",
        twilio_trial_mode=True,
        twilio_account_sid=None,
        twilio_auth_token=None,
        twilio_from_number=None,
        twilio_verified_caller_id=None,
        public_base_url=None,
        otel_enabled=True,
        otel_exporter="console",
        otel_endpoint=None,
    )
    base.update(overrides)
    return Settings(**base)


class _StubTelemetry:
    """Records which exporter setup methods were called."""

    instances: list["_StubTelemetry"] = []

    def __init__(self, *args, **kwargs) -> None:
        self.console_calls = 0
        self.otlp_calls = 0
        _StubTelemetry.instances.append(self)

    def setup_console_exporter(self, **kwargs):
        self.console_calls += 1
        return self

    def setup_otlp_exporter(self, **kwargs):
        self.otlp_calls += 1
        return self


@pytest.fixture(autouse=True)
def _reset_guard(monkeypatch):
    """Reset the one-time guard and the stub registry around each test."""
    telemetry_mod._reset_for_tests()
    _StubTelemetry.instances = []
    yield
    telemetry_mod._reset_for_tests()


def _patch_strands(monkeypatch, cls=_StubTelemetry):
    """Patch ``strands.telemetry.StrandsTelemetry`` with ``cls``."""
    import strands.telemetry as strands_telemetry

    monkeypatch.setattr(strands_telemetry, "StrandsTelemetry", cls)


# -- exporter selection -------------------------------------------------------


def test_console_exporter_configured_by_default(monkeypatch):
    _patch_strands(monkeypatch)
    configured = telemetry_mod.setup_telemetry(_settings())
    assert configured is True
    assert len(_StubTelemetry.instances) == 1
    assert _StubTelemetry.instances[0].console_calls == 1
    assert _StubTelemetry.instances[0].otlp_calls == 0


def test_otlp_exporter_configured_when_selected(monkeypatch):
    _patch_strands(monkeypatch)
    configured = telemetry_mod.setup_telemetry(
        _settings(otel_exporter="otlp", otel_endpoint="http://collector:4318")
    )
    assert configured is True
    assert _StubTelemetry.instances[0].otlp_calls == 1
    assert _StubTelemetry.instances[0].console_calls == 0


def test_disabled_is_a_noop(monkeypatch):
    _patch_strands(monkeypatch)
    assert telemetry_mod.setup_telemetry(_settings(otel_enabled=False)) is False
    assert _StubTelemetry.instances == []


def test_exporter_none_is_a_noop(monkeypatch):
    _patch_strands(monkeypatch)
    assert telemetry_mod.setup_telemetry(_settings(otel_exporter="none")) is False
    assert _StubTelemetry.instances == []


# -- configured exactly once --------------------------------------------------


def test_configured_exactly_once(monkeypatch):
    _patch_strands(monkeypatch)
    first = telemetry_mod.setup_telemetry(_settings())
    second = telemetry_mod.setup_telemetry(_settings())
    third = telemetry_mod.setup_telemetry(_settings())
    assert first is True
    assert second is False
    assert third is False
    # StrandsTelemetry is instantiated only on the first call.
    assert len(_StubTelemetry.instances) == 1
    assert _StubTelemetry.instances[0].console_calls == 1


# -- failure is swallowed -----------------------------------------------------


def test_setup_exception_is_swallowed(monkeypatch):
    class _Boom:
        def __init__(self, *args, **kwargs):
            raise RuntimeError("telemetry boom")

    _patch_strands(monkeypatch, cls=_Boom)
    # Must NOT raise; returns False and the guard still flips so it is not
    # retried on the audio path.
    assert telemetry_mod.setup_telemetry(_settings()) is False
    # A subsequent call is still a no-op (idempotent even after failure).
    _patch_strands(monkeypatch)
    assert telemetry_mod.setup_telemetry(_settings()) is False


def test_console_setup_method_exception_is_swallowed(monkeypatch):
    class _BoomExporter:
        def __init__(self, *args, **kwargs):
            pass

        def setup_console_exporter(self, **kwargs):
            raise RuntimeError("exporter boom")

    _patch_strands(monkeypatch, cls=_BoomExporter)
    assert telemetry_mod.setup_telemetry(_settings()) is False


# -- config knobs -------------------------------------------------------------


def test_settings_otel_defaults(monkeypatch):
    for var in ("OTEL_ENABLED", "OTEL_EXPORTER", "OTEL_EXPORTER_OTLP_ENDPOINT"):
        monkeypatch.delenv(var, raising=False)
    settings = get_settings()
    assert settings.otel_enabled is True
    assert settings.otel_exporter == "console"
    assert settings.otel_endpoint is None


def test_settings_otel_from_env(monkeypatch):
    monkeypatch.setenv("OTEL_ENABLED", "false")
    monkeypatch.setenv("OTEL_EXPORTER", "OTLP")  # case-insensitive
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector:4318")
    settings = get_settings()
    assert settings.otel_enabled is False
    assert settings.otel_exporter == "otlp"
    assert settings.otel_endpoint == "http://collector:4318"


def test_settings_otel_invalid_exporter_falls_back(monkeypatch):
    monkeypatch.setenv("OTEL_EXPORTER", "bogus")
    assert get_settings().otel_exporter == "console"


def test_safe_summary_surfaces_otel(monkeypatch):
    summary = _settings(
        otel_enabled=True, otel_exporter="otlp", otel_endpoint="http://c:4318"
    ).safe_summary()
    assert summary["otel_enabled"] is True
    assert summary["otel_exporter"] == "otlp"
    assert summary["otel_endpoint_set"] is True
    # No raw endpoint value is leaked into the safe summary.
    assert "http://c:4318" not in summary.values()


def test_main_module_configures_telemetry_on_import(monkeypatch):
    """Importing app.main triggers a single telemetry setup before any run."""
    _patch_strands(monkeypatch)
    telemetry_mod._reset_for_tests()
    import app.main as main_mod

    importlib.reload(main_mod)
    # Setup ran exactly once during import/reload.
    assert len(_StubTelemetry.instances) == 1
