"""Tests for the off-path Twilio Call-resource enrichment (ASK 2/6).

:func:`app.twilio_client.fetch_call_insights` fetches the REAL Twilio Call
resource for a completed call POST-CALL, off the real-time audio path. The
``twilio.rest.Client`` is injected/mocked so NO network call happens. Both the
add-on-available-style (authentic Call fields present) and the add-on-gated /
trial branch (advanced QoS unavailable, graceful failure) are exercised. These
fail if the enrichment logic is reverted or if advanced QoS is ever fabricated.
"""

from __future__ import annotations

from datetime import datetime, timezone

from app.config import Settings
from app.twilio_client import (
    ADVANCED_QOS_UNAVAILABLE_REASON,
    fetch_call_insights,
    hangup_call,
)


def _settings(**overrides) -> Settings:
    base = dict(
        nova_model_id="amazon.nova-2-5-sonic",
        aws_region="us-east-1",
        app_log_group="/ecs/ar-collections-nova-sonic/app",
        extraction_model_id="amazon.nova-lite-v1:0",
        code_interpreter_id="aws.codeinterpreter.v1",
        payment_link_base_url="https://pay.example.com/",
        twilio_trial_mode=True,
        twilio_account_sid="AC_test_sid",
        twilio_auth_token="test_token_value",
        twilio_from_number="+15005550006",
        twilio_verified_caller_id="+15005550010",
        public_base_url="https://example.ngrok.app",
        otel_enabled=True,
        otel_exporter="console",
        otel_endpoint=None,
    )
    base.update(overrides)
    return Settings(**base)


class _FakeCall:
    """A stand-in for a Twilio Call instance with authentic Call-resource attrs."""

    sid = "CA_done"
    parent_call_sid = None
    to = "+15005550010"
    to_formatted = "(500) 555-0010"
    from_ = "+15005550006"
    from_formatted = "(500) 555-0006"
    status = "completed"
    start_time = datetime(2024, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
    end_time = datetime(2024, 6, 1, 12, 1, 30, tzinfo=timezone.utc)
    duration = "90"
    price = "-0.017"
    price_unit = "USD"
    direction = "outbound-api"
    answered_by = None
    queue_time = "0"
    date_created = datetime(2024, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
    date_updated = datetime(2024, 6, 1, 12, 1, 30, tzinfo=timezone.utc)


class _FakeCallContext:
    def __init__(self, call_sid, call):
        self.call_sid = call_sid
        self._call = call

    def fetch(self):
        return self._call


class _FakeTwilioClient:
    """Mock ``twilio.rest.Client`` exposing ``.calls(sid).fetch()``."""

    def __init__(self, account_sid, auth_token, call=None):
        self.account_sid = account_sid
        self.auth_token = auth_token
        self._call = call if call is not None else _FakeCall()
        self.fetched: list[str] = []

    def calls(self, call_sid):
        self.fetched.append(call_sid)
        return _FakeCallContext(call_sid, self._call)


def test_fetch_call_insights_returns_authentic_call_fields():
    """Add-on-available-style branch: the authentic Call-resource fields are
    persisted with available=True; datetimes are ISO strings; from_ is exposed
    as a friendly 'from' key. Advanced QoS is STILL marked unavailable (never
    fabricated), since basic Call fields do not include RTT/jitter/MOS.
    """
    created = []

    def factory(account_sid, auth_token):
        c = _FakeTwilioClient(account_sid, auth_token)
        created.append(c)
        return c

    insights = fetch_call_insights(
        "CA_done", settings=_settings(), client_factory=factory
    )

    assert insights["available"] is True
    assert insights["source"] == "twilio_call_resource"
    fields = insights["fields"]
    assert fields["status"] == "completed"
    assert fields["duration"] == "90"
    assert fields["from"] == "+15005550006"  # from_ mapped to friendly key
    assert fields["to"] == "+15005550010"
    assert fields["price"] == "-0.017"
    assert fields["direction"] == "outbound-api"
    # Datetimes serialized to ISO-8601 strings (JSON/Dynamo safe).
    assert fields["start_time"].startswith("2024-06-01T12:00:00")
    assert fields["end_time"].startswith("2024-06-01T12:01:30")

    # Advanced QoS (paid add-on) is NOT fabricated even when basic fields load.
    assert insights["advanced_qos_available"] is False
    assert insights["advanced_qos_reason"] == ADVANCED_QOS_UNAVAILABLE_REASON

    # The injected client was used (fetched the right call sid), no real call.
    assert created and created[0].fetched == ["CA_done"]


def test_fetch_call_insights_add_on_gated_marks_qos_unavailable_not_faked():
    """Trial/add-on-gated branch: when the Voice Insights advanced add-on would
    be queried, the result still carries NO fabricated QoS numbers -- only the
    explicit unavailable marker + reason. We assert the shape never contains
    RTT/jitter/packet-loss/MOS keys.
    """
    insights = fetch_call_insights(
        "CA_done",
        settings=_settings(),
        client_factory=lambda sid, tok: _FakeTwilioClient(sid, tok),
    )
    assert insights["advanced_qos_available"] is False
    assert insights["advanced_qos_reason"]
    forbidden = {"rtt", "jitter", "packet_loss", "mos", "round_trip"}
    assert not (forbidden & set(k.lower() for k in insights["fields"])), (
        "advanced QoS metrics must never be fabricated"
    )


def test_fetch_call_insights_graceful_when_fetch_raises():
    """A Twilio API error during the off-path fetch must NOT raise; it returns
    an explicit unavailable marker so the post-call pipeline never crashes and
    no data is fabricated.
    """

    class _Raiser:
        def __init__(self, *_a):
            pass

        def calls(self, _sid):
            raise RuntimeError("Unable to fetch record: HTTP 404")

    insights = fetch_call_insights(
        "CA_missing",
        settings=_settings(),
        client_factory=lambda *a: _Raiser(),
    )
    assert insights["available"] is False
    assert insights["source"] == "unavailable"
    assert insights["fields"] == {}
    assert insights["error"] == "RuntimeError"


def test_fetch_call_insights_unavailable_without_call_sid_or_creds():
    """No call_sid, or Twilio not configured -> explicit unavailable marker,
    never a crash, never fabricated data, and the client is never built.
    """
    built = []

    def factory(*a):
        built.append(a)
        return _FakeTwilioClient(*a)

    # No call sid.
    no_sid = fetch_call_insights(None, settings=_settings(), client_factory=factory)
    assert no_sid["available"] is False
    assert no_sid["error"] == "no_call_sid"

    # Twilio not configured (no creds).
    not_configured = _settings(twilio_account_sid=None, twilio_auth_token=None)
    no_creds = fetch_call_insights(
        "CA_done", settings=not_configured, client_factory=factory
    )
    assert no_creds["available"] is False
    assert no_creds["error"] == "twilio_not_configured"

    # The client factory was never invoked in either short-circuit path.
    assert built == []


# --- FEAT-002: end-of-call Twilio hangup (Status=completed) ------------------


class _FakeCallUpdateContext:
    """Stand-in for ``client.calls(sid)`` exposing ``.update(status=...)``."""

    def __init__(self, call_sid, recorder):
        self.call_sid = call_sid
        self._recorder = recorder

    def update(self, status=None, **kwargs):
        self._recorder.append((self.call_sid, status))
        return object()


class _FakeHangupClient:
    """Mock ``twilio.rest.Client`` exposing ``.calls(sid).update(...)``."""

    def __init__(self, account_sid, auth_token):
        self.account_sid = account_sid
        self.auth_token = auth_token
        self.updates: list[tuple[str, str]] = []

    def calls(self, call_sid):
        return _FakeCallUpdateContext(call_sid, self.updates)


def test_hangup_call_updates_status_completed():
    """hangup_call issues client.calls(sid).update(status='completed') with the
    Twilio client injected/stubbed (no live call placed)."""
    created = []

    def factory(account_sid, auth_token):
        c = _FakeHangupClient(account_sid, auth_token)
        created.append(c)
        return c

    marker = hangup_call("CA_live", settings=_settings(), client_factory=factory)

    assert marker["hung_up"] is True
    assert marker["status"] == "completed"
    assert marker["call_sid"] == "CA_live"
    assert created and created[0].updates == [("CA_live", "completed")]


def test_hangup_call_missing_sid_is_benign_noop():
    """No call_sid -> benign marker, never raises, client never built."""
    built = []

    def factory(*a):
        built.append(a)
        return _FakeHangupClient(*a)

    marker = hangup_call(None, settings=_settings(), client_factory=factory)
    assert marker["hung_up"] is False
    assert marker["status"] == "no_call_sid"
    assert built == []


def test_hangup_call_failure_safe_when_update_raises():
    """A Twilio error (e.g. call already completed/gone) must NOT raise; it
    returns an explicit marker instead."""

    class _Raiser:
        def __init__(self, *_a):
            pass

        def calls(self, _sid):
            raise RuntimeError("Unable to update record: Call is not in-progress")

    marker = hangup_call(
        "CA_gone",
        settings=_settings(),
        client_factory=lambda *a: _Raiser(),
    )
    assert marker["hung_up"] is False
    assert marker["status"] == "error"
    assert marker["error"] == "RuntimeError"


def test_hangup_call_unconfigured_is_benign_noop():
    """Twilio not configured -> benign marker, client never built."""
    built = []

    def factory(*a):
        built.append(a)
        return _FakeHangupClient(*a)

    not_configured = _settings(twilio_account_sid=None, twilio_auth_token=None)
    marker = hangup_call("CA_live", settings=not_configured, client_factory=factory)
    assert marker["hung_up"] is False
    assert marker["status"] == "twilio_not_configured"
    assert built == []


def test_hangup_call_never_logs_auth_token(caplog):
    """The auth token must never appear in any emitted log record."""
    import logging

    with caplog.at_level(logging.INFO):
        hangup_call(
            "CA_live",
            settings=_settings(),
            client_factory=lambda sid, tok: _FakeHangupClient(sid, tok),
        )
    for rec in caplog.records:
        assert "test_token_value" not in rec.getMessage()


def test_fetch_call_insights_never_logs_auth_token(caplog):
    """The auth token must never appear in any emitted log record."""
    import logging

    with caplog.at_level(logging.INFO):
        fetch_call_insights(
            "CA_done",
            settings=_settings(),
            client_factory=lambda sid, tok: _FakeTwilioClient(sid, tok),
        )
    for rec in caplog.records:
        assert "test_token_value" not in rec.getMessage()
