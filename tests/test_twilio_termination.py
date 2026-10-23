"""Termination confirmation and HTTP deadlines, using only synthetic clients."""

from __future__ import annotations

import asyncio
import threading
import time
from types import SimpleNamespace

import pytest
from requests.exceptions import ReadTimeout
from twilio.base.exceptions import TwilioRestException

from app import twilio_client
from app.config import Settings
from app.twilio_client import hangup_call, hangup_call_async


def _settings() -> Settings:
    return Settings(
        nova_model_id="amazon.nova-2-5-sonic",
        aws_region="us-east-1",
        app_log_group="/test/app",
        extraction_model_id="amazon.nova-lite-v1:0",
        code_interpreter_id="aws.codeinterpreter.v1",
        payment_link_base_url="https://pay.example.com/",
        twilio_trial_mode=True,
        twilio_account_sid="AC_test_sid",
        twilio_auth_token="synthetic-test-token",
        twilio_from_number="+15005550006",
        twilio_verified_caller_id="+15005550010",
        public_base_url="https://example.com",
        otel_enabled=False,
        otel_exporter="console",
        otel_endpoint=None,
    )


class _ScriptedClient:
    """Assert request order as well as count; extra calls fail the test."""

    def __init__(self, script):
        self.script = list(script)
        self.calls_made = []
        self.http_clients = []

    def factory(self, sid, token, *, http_client):
        assert sid == "AC_test_sid"
        assert token == "synthetic-test-token"
        self.http_clients.append(http_client)
        return self

    def calls(self, sid):
        assert sid == "CA_test"
        return self

    def _result(self, operation):
        self.calls_made.append(operation)
        expected, result = self.script.pop(0)
        assert expected == operation
        if isinstance(result, Exception):
            raise result
        return SimpleNamespace(status=result) if result is not None else object()

    def update(self, *, status):
        assert status == "completed"
        return self._result("update")

    def fetch(self):
        return self._result("fetch")


def _run(script, **kwargs):
    client = _ScriptedClient(script)
    marker = hangup_call("CA_test", settings=_settings(), client_factory=client.factory, **kwargs)
    assert client.script == []
    return marker, client


@pytest.mark.parametrize("status", ["completed", "canceled", "failed", "busy", "no-answer"])
def test_only_observed_terminal_status_confirms_hangup(status):
    marker, client = _run([("update", status)])
    assert marker == {
        "hung_up": True,
        "confirmed_terminal": True,
        "outcome": "confirmed_terminal",
        "status": status,
        "attempts": 1,
        "pending_request": False,
        "call_sid": "CA_test",
    }
    assert client.calls_made == ["update"]


def test_ambiguous_update_timeout_then_terminal_readback():
    marker, client = _run([("update", ReadTimeout("sensitive-response")), ("fetch", "completed")])
    assert marker["confirmed_terminal"] is True
    assert marker["attempts"] == 1
    assert marker["status"] == "completed"
    assert "error_type" not in marker
    assert client.calls_made == ["update", "fetch"]


def test_nonterminal_readback_allows_only_one_update_retry():
    marker, client = _run([
        ("update", ReadTimeout()),
        ("fetch", "in-progress"),
        ("update", "completed"),
    ])
    assert marker["confirmed_terminal"] is True
    assert marker["attempts"] == 2
    assert client.calls_made == ["update", "fetch", "update"]


def test_unknown_fetch_and_retry_failure_remains_unknown():
    marker, client = _run([
        ("update", ReadTimeout("sensitive-response")),
        ("fetch", ReadTimeout("sensitive-response")),
        ("update", ReadTimeout("sensitive-response")),
    ])
    assert marker["outcome"] == "unknown"
    assert marker["confirmed_terminal"] is marker["hung_up"] is False
    assert marker["status"] is None
    assert marker["attempts"] == 2
    assert marker["error_type"] == "ReadTimeout"
    assert client.calls_made == ["update", "fetch", "update"]
    assert "sensitive-response" not in str(marker)


@pytest.mark.parametrize("status", [400, 401, 403, 404, 409, 429])
def test_explicit_request_rejection_never_claims_termination_or_retries(status):
    marker, client = _run([("update", TwilioRestException(status, "/private-uri", "sensitive-response"))])
    assert marker["outcome"] == "request_failed"
    assert marker["confirmed_terminal"] is False
    assert marker["status"] is None
    assert marker["attempts"] == 1
    assert marker["error_type"] == "TwilioRestException"
    assert client.calls_made == ["update"]
    assert "sensitive-response" not in str(marker)
    assert "private-uri" not in str(marker)


@pytest.mark.parametrize("status", [408, 500, 502, 503])
def test_http_timeout_and_server_errors_require_readback(status):
    marker, _ = _run([
        ("update", TwilioRestException(status, "/private-uri", "sensitive-response")),
        ("fetch", "completed"),
    ])
    assert marker["confirmed_terminal"] is True


def test_retry_rejection_does_not_resolve_an_unknown_initial_write():
    marker, _ = _run([
        ("update", ReadTimeout()),
        ("fetch", ReadTimeout()),
        ("update", TwilioRestException(400, "/private-uri")),
    ])
    assert marker["outcome"] == "unknown"
    assert marker["status"] is None


def test_retry_rejection_after_nonterminal_readback_is_request_failed():
    marker, _ = _run([
        ("update", ReadTimeout()),
        ("fetch", "in-progress"),
        ("update", TwilioRestException(400, "/private-uri")),
    ])
    assert marker["outcome"] == "request_failed"
    assert marker["status"] == "in-progress"


@pytest.mark.parametrize("status", [None, "in-progress", "ringing", "unrecognized"])
def test_successful_update_without_terminal_status_never_fabricates_completed(status):
    marker, client = _run([("update", status), ("fetch", status), ("update", status)])
    assert marker["outcome"] == "unknown"
    assert marker["hung_up"] is False
    assert marker["status"] == status
    assert marker["attempts"] == 2
    assert len(client.calls_made) == 3


def test_http_timeout_is_injected_and_sdk_retries_disabled():
    marker, client = _run([
        ("update", "in-progress"),
        ("fetch", "in-progress"),
        ("update", "completed"),
    ], request_timeout_s=2.5)
    assert marker["confirmed_terminal"] is True
    assert len({id(http) for http in client.http_clients}) == 3
    for http in client.http_clients:
        assert http.timeout == 2.5
        assert http.session.get_adapter("https://api.twilio.com").max_retries.total == 0


@pytest.mark.parametrize("timeout", [0, -1, float("nan"), float("inf"), None])
def test_invalid_timeout_does_not_construct_client(timeout):
    marker = hangup_call(
        "CA_test", settings=_settings(), request_timeout_s=timeout,
        client_factory=lambda *a, **kw: pytest.fail("invalid timeout must fail before client creation"),
    )
    assert marker["outcome"] == "request_failed"
    assert marker["error_type"] == "invalid_request_timeout"
    assert marker["attempts"] == 0


def test_no_exception_payload_or_credentials_logged(caplog):
    marker, _ = _run([
        ("update", ReadTimeout("synthetic-test-token /private-uri sensitive-response")),
        ("fetch", ReadTimeout("synthetic-test-token /private-uri sensitive-response")),
        ("update", ReadTimeout("synthetic-test-token /private-uri sensitive-response")),
    ])
    for text in (str(marker), caplog.text):
        assert "synthetic-test-token" not in text
        assert "private-uri" not in text
        assert "sensitive-response" not in text


@pytest.mark.asyncio
async def test_async_helper_uses_same_confirmation_policy():
    client = _ScriptedClient([("update", ReadTimeout()), ("fetch", "in-progress"), ("update", "completed")])
    marker = await hangup_call_async("CA_test", settings=_settings(), client_factory=client.factory)
    assert marker["confirmed_terminal"] is True
    assert marker["pending_request"] is False
    assert marker["attempts"] == 2
    assert client.script == []


@pytest.mark.asyncio
async def test_async_request_deadlines_bound_wait_and_retain_workers(monkeypatch):
    release = threading.Event()
    calls_made = []
    prior_workers = set(twilio_client._HANGUP_WORKERS)

    def blocked_request(operation, *args):
        calls_made.append(operation)
        assert release.wait(timeout=3)
        return twilio_client._HangupRequestResult(status="completed")

    monkeypatch.setattr(twilio_client, "_perform_hangup_request", blocked_request)
    started = time.monotonic()
    try:
        marker = await hangup_call_async("CA_test", settings=_settings(), request_timeout_s=0.03)
        elapsed = time.monotonic() - started
        assert 0.08 <= elapsed < 0.5
        assert calls_made == ["update", "fetch", "update"]
        assert marker["confirmed_terminal"] is False
        assert marker["outcome"] == "unknown"
        assert marker["status"] is None
        assert marker["pending_request"] is True
        assert marker["attempts"] == 2
        workers = set(twilio_client._HANGUP_WORKERS) - prior_workers
        assert len(workers) == 3
        assert all(not worker.done() for worker in workers)
    finally:
        release.set()
        workers = set(twilio_client._HANGUP_WORKERS) - prior_workers
        if workers:
            await asyncio.gather(*workers)
        await asyncio.sleep(0)
    assert set(twilio_client._HANGUP_WORKERS) == prior_workers
    # Later worker completion does not silently rewrite the returned evidence.
    assert marker["outcome"] == "unknown"


@pytest.mark.asyncio
async def test_async_terminal_readback_while_initial_update_remains_pending(monkeypatch):
    release = threading.Event()
    prior_workers = set(twilio_client._HANGUP_WORKERS)
    calls_made = []

    def request(operation, *args):
        calls_made.append(operation)
        if operation == "update":
            assert release.wait(timeout=3)
        return twilio_client._HangupRequestResult(status="completed")

    monkeypatch.setattr(twilio_client, "_perform_hangup_request", request)
    try:
        marker = await hangup_call_async("CA_test", settings=_settings(), request_timeout_s=0.03)
        assert marker["confirmed_terminal"] is True
        assert marker["pending_request"] is True
        assert marker["attempts"] == 1
        assert calls_made == ["update", "fetch"]
    finally:
        release.set()
        workers = set(twilio_client._HANGUP_WORKERS) - prior_workers
        if workers:
            await asyncio.gather(*workers)
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_caller_cancellation_does_not_cancel_or_lose_http_worker(monkeypatch):
    release = threading.Event()
    started = threading.Event()
    prior_workers = set(twilio_client._HANGUP_WORKERS)

    def request(*args):
        started.set()
        assert release.wait(timeout=3)
        return twilio_client._HangupRequestResult(status="completed")

    monkeypatch.setattr(twilio_client, "_perform_hangup_request", request)
    helper = asyncio.create_task(hangup_call_async("CA_test", settings=_settings()))
    try:
        assert await asyncio.to_thread(started.wait, 1)
        helper.cancel()
        with pytest.raises(asyncio.CancelledError):
            await helper
        workers = set(twilio_client._HANGUP_WORKERS) - prior_workers
        assert len(workers) == 1
        assert not next(iter(workers)).done()
    finally:
        release.set()
        workers = set(twilio_client._HANGUP_WORKERS) - prior_workers
        if workers:
            await asyncio.gather(*workers)
        await asyncio.sleep(0)
    assert set(twilio_client._HANGUP_WORKERS) == prior_workers


def test_real_twilio_sdk_forwards_timeout_and_returns_observed_status(monkeypatch):
    from requests import Response
    from twilio.http.http_client import Session

    requests = []

    def send(_session, request, **kwargs):
        requests.append((request, kwargs))
        response = Response()
        response.status_code = 200
        response._content = b'{"sid":"CA_test","status":"canceled"}'
        response.headers = {}
        return response

    monkeypatch.setattr(Session, "send", send)
    marker = hangup_call("CA_test", settings=_settings(), request_timeout_s=2.5)
    assert marker["confirmed_terminal"] is True
    assert marker["status"] == "canceled"
    assert len(requests) == 1
    request, kwargs = requests[0]
    assert request.method == "POST"
    assert request.body == "Status=completed"
    assert kwargs["timeout"] == 2.5
    assert kwargs["allow_redirects"] is False
