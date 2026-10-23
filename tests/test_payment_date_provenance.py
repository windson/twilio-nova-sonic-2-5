"""Offline regressions for call-anchored dates and nonblocking post-call work.

Fixtures are synthetic. All SDK clients are stubbed and cannot send messages.
"""

import asyncio
import json
import threading
from dataclasses import replace

import pytest

from app import postcall
from app.store import InMemoryStore
from tests.test_postcall import _settings


class Extractor:
    def __init__(self, agreed_date="2024-07-01"):
        self.agreed_date = agreed_date
        self.calls = []

    def converse(self, **kwargs):
        self.calls.append(kwargs)
        return {"output": {"message": {"content": [{"text": json.dumps({
            "disposition": "promise_to_pay",
            "payment_specifics": {"promised_amount": 50, "agreed_date": self.agreed_date,
                                  "preferred_payment_method": None},
            "contextual_data": {"reason_for_delinquency": None, "customer_sentiment": None},
            "next_best_action": "Review payment commitment",
        })}]}}}


def _record(**overrides):
    record = {
        "call_sid": "CA_synthetic_date",
        "account_id": "ACC-1001",
        "created_at": "2026-10-22T07:18:12+00:00",
        "conversation_turns": [
            {"speaker": "assistant", "text": "Your invoice was due June 1, 2024."},
            {"speaker": "user", "text": "first monday of next month"},
        ],
        "events_log": [{"event_type": "tool_invocation", "tool": "capture_payment_date_phrase"}],
    }
    record.update(overrides)
    return record


def _settings_without_network(timezone_name="Asia/Kolkata"):
    return replace(
        _settings(), twilio_account_sid=None, twilio_auth_token=None,
        payment_timezone=timezone_name,
    )


def _no_sandbox(_region):
    raise AssertionError("deterministic date must not start an AgentCore session")


@pytest.mark.asyncio
async def test_call_phrase_overrides_invented_iso_and_resolves_once():
    store = InMemoryStore()
    store.update_due("ACC-1001", promised_date_phrase="first monday of next month")
    record = _record()
    store.save_call_analytics(record)
    extractor = Extractor()

    changes = await postcall.extract_and_apply(
        record, store, _settings_without_network(),
        bedrock_client=extractor, code_interpreter_factory=_no_sandbox,
    )

    assert changes["structured_data"]["payment_specifics"]["agreed_date"] == "2026-11-02"
    assert store.get_due("ACC-1001")["promised_date"] == "2026-11-02"
    provenance = changes["payment_date_resolution"]
    assert provenance["reference_date"] == "2026-10-22"
    assert provenance["reference_timezone"] == "Asia/Kolkata"
    assert provenance["reference_source"] == "call.created_at"
    assert provenance["phrase_source"] == "legacy_capture_confirmed_in_call"
    assert provenance["model_date_overridden"] is True
    assert provenance["requires_review"] is False
    assert len(changes["async_tool_calls"]) == 1
    assert changes["async_tool_calls"][0]["duration_ms"] >= 0
    assert changes["async_tool_calls"][0]["execution_mode"] == "thread_offload"
    assert len(extractor.calls) == 1
    prompt = extractor.calls[0]["messages"][0]["content"][0]["text"]
    assert "Call-local reference date: 2026-10-22" in prompt
    assert "Calendar timezone: Asia/Kolkata" in prompt
    assert changes["ptp_sms_status"] is None


@pytest.mark.asyncio
async def test_call_capture_survives_delayed_replay_and_newer_account_capture():
    store = InMemoryStore()
    store.update_due(
        "ACC-1001", promised_date_phrase="tomorrow",
        promised_date_phrase_call_sid="CA_a_newer_call", promised_date="2028-03-04",
    )
    record = _record(payment_date_capture={
        "phrase": "first monday of next month", "call_sid": "CA_synthetic_date",
        "captured_at": "2026-10-22T07:18:50+00:00",
        "reference_date": "2026-10-22", "reference_timezone": "Asia/Kolkata",
    })
    store.save_call_analytics(record)
    changes = await postcall.extract_and_apply(
        record, store, _settings_without_network("America/New_York"),
        bedrock_client=Extractor(), code_interpreter_factory=_no_sandbox,
    )
    assert changes["structured_data"]["payment_specifics"]["agreed_date"] == "2026-11-02"
    assert changes["payment_date_resolution"]["reference_timezone"] == "Asia/Kolkata"
    assert store.get_due("ACC-1001")["promised_date"] == "2028-03-04"


@pytest.mark.asyncio
async def test_timezone_controls_calendar_day_at_year_boundary():
    store = InMemoryStore()
    record = _record(
        created_at="2026-12-31T20:00:00+00:00",
        conversation_turns=[{"speaker": "user", "text": "tomorrow"}],
    )
    store.save_call_analytics(record)
    changes = await postcall.extract_and_apply(
        record, store, _settings_without_network(),
        bedrock_client=Extractor("tomorrow"), code_interpreter_factory=_no_sandbox,
    )
    assert changes["payment_date_resolution"]["reference_date"] == "2027-01-01"
    assert changes["structured_data"]["payment_specifics"]["agreed_date"] == "2027-01-02"


@pytest.mark.asyncio
@pytest.mark.parametrize("model_date", ["2024-07-01", "2030-07-01"])
async def test_unconfirmed_model_iso_requires_review_instead_of_guessing(model_date):
    store = InMemoryStore()
    record = _record(events_log=[])
    store.save_call_analytics(record)
    changes = await postcall.extract_and_apply(
        record, store, _settings_without_network(), bedrock_client=Extractor(model_date),
        code_interpreter_factory=_no_sandbox,
    )
    assert changes["structured_data"]["payment_specifics"]["agreed_date"] is None
    assert changes["payment_date_resolution"]["requires_review"] is True
    assert changes["payment_date_resolution"]["reason"] == "unconfirmed_model_iso_date"


@pytest.mark.asyncio
async def test_stale_due_phrase_is_not_used_without_call_evidence():
    store = InMemoryStore()
    store.update_due("ACC-1001", promised_date_phrase="second Tuesday of next month")
    record = _record(events_log=[])
    store.save_call_analytics(record)
    changes = await postcall.extract_and_apply(
        record, store, _settings_without_network(), bedrock_client=Extractor(None),
        code_interpreter_factory=_no_sandbox,
    )
    assert changes["structured_data"]["payment_specifics"]["agreed_date"] is None
    assert changes["payment_date_resolution"]["requires_review"] is True


@pytest.mark.asyncio
async def test_blocked_sdk_call_does_not_block_event_loop():
    entered = threading.Event()
    release = threading.Event()

    class BlockingExtractor(Extractor):
        def converse(self, **kwargs):
            entered.set()
            if not release.wait(timeout=2):
                raise RuntimeError("test SDK release timed out")
            return super().converse(**kwargs)

    record = _record(events_log=[])
    store = InMemoryStore()
    store.save_call_analytics(record)
    task = asyncio.create_task(postcall.extract_and_apply(
        record, store, _settings_without_network(),
        bedrock_client=BlockingExtractor(None), code_interpreter_factory=_no_sandbox,
    ))
    try:
        assert await asyncio.wait_for(asyncio.to_thread(entered.wait, 1), timeout=1.5)
        # This assertion fails if the coroutine executes the blocking SDK on
        # the event loop: by the time control returns, the 2s timeout completed.
        assert not task.done()
        await asyncio.wait_for(asyncio.sleep(0), timeout=0.2)
    finally:
        release.set()
        await task
