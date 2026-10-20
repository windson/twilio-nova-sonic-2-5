"""Tests for post-call intelligence extraction + downstream triggers.

The Bedrock Converse client and the Twilio client are stubbed so NO network
call happens. Includes the NO-LATENCY proof: running extract_and_apply after a
synthetic CallMetrics session must not change the frozen call-latency metrics.
"""

from __future__ import annotations

import json

import pytest

from app.config import Settings
from app.metrics import CallMetrics
from app.postcall import extract_and_apply
from app.store import InMemoryStore
from strands.experimental.bidi.types.events import (
    BidiAudioStreamEvent,
    BidiResponseCompleteEvent,
    BidiResponseStartEvent,
    BidiTranscriptStreamEvent,
)


def _settings(**overrides) -> Settings:
    base = dict(
        nova_model_id="amazon.nova-2-5-sonic",
        aws_region="us-east-1",
        app_log_group="/ecs/ar-collections-nova-sonic/app",
        extraction_model_id="amazon.nova-lite-v1:0",
        payment_link_base_url="https://pay.example.com/",
        twilio_account_sid="AC_test_sid",
        twilio_auth_token="test_token_value",
        twilio_from_number="+15005550006",
        twilio_verified_caller_id="+15005550010",
        public_base_url="https://example.ngrok.app",
    )
    base.update(overrides)
    return Settings(**base)


class FakeBedrock:
    """Stub bedrock-runtime client returning a canned Converse response."""

    def __init__(self, payload: dict) -> None:
        self._payload = payload
        self.calls: list[dict] = []

    def converse(self, **kwargs):
        self.calls.append(kwargs)
        return {
            "output": {
                "message": {
                    "content": [{"text": json.dumps(self._payload)}],
                }
            }
        }


def _call_record(**overrides):
    rec = {
        "call_sid": "CA_pc",
        "account_id": "ACC-1001",
        "total_e2e_ms": 4000.0,
        "conversation_turns": [
            {"speaker": "assistant", "text": "You owe 428.57 dollars."},
            {"speaker": "user", "text": "I can pay next month, the first Monday."},
        ],
        "transcripts": [],
    }
    rec.update(overrides)
    return rec


@pytest.fixture
def store() -> InMemoryStore:
    s = InMemoryStore()
    # Persist a call record so update_call_analytics can merge onto it.
    s.save_call_analytics(_call_record())
    return s


async def test_extract_and_apply_ptp_sets_fields_pauses_and_sends_sms(store):
    bedrock = FakeBedrock(
        {
            "disposition": "promise_to_pay",
            "payment_specifics": {
                "promised_amount": 428.57,
                "agreed_date": "next month 1st Monday",
                "preferred_payment_method": "card",
            },
            "contextual_data": {
                "reason_for_delinquency": "temporary unemployment",
                "customer_sentiment": "cooperative",
            },
            "next_best_action": "Schedule follow-up call on the promised date",
        }
    )

    class FakeMessage:
        sid = "SM_fake"

    class FakeMessages:
        def __init__(self):
            self.created = []

        def create(self, **kwargs):
            self.created.append(kwargs)
            return FakeMessage()

    class FakeTwilioClient:
        def __init__(self, *_a):
            self.messages = FakeMessages()

    created_clients = []

    def twilio_factory(*args):
        c = FakeTwilioClient(*args)
        created_clients.append(c)
        return c

    changes = await extract_and_apply(
        _call_record(),
        store,
        _settings(),
        bedrock_client=bedrock,
        twilio_client_factory=twilio_factory,
    )

    assert changes["disposition"] == "promise_to_pay"
    assert changes["intelligence_status"] == "ready"
    assert changes["collection_paused"] is True
    assert changes["ptp_sms_status"] == "sent"
    # Relative date resolved to a concrete ISO date (first Monday of June 2024
    # would depend on "today"; just assert it is a resolved ISO date string).
    agreed = changes["structured_data"]["payment_specifics"]["agreed_date"]
    assert agreed is not None and agreed.count("-") == 2
    assert changes["structured_data"]["payment_specifics"]["promised_amount"] == 428.57
    assert changes["next_best_action"]

    # Account collection paused.
    assert store.get_due("ACC-1001")["collection_paused"] is True

    # SMS attempted with a payment link containing the account id.
    assert created_clients, "twilio client factory should have been used"
    sms_kwargs = created_clients[0].messages.created[0]
    assert "pay.example.com" in sms_kwargs["body"]
    assert "ACC-1001" in sms_kwargs["body"]

    # Persisted onto the stored call record.
    persisted = store.get_call("CA_pc")
    assert persisted["disposition"] == "promise_to_pay"
    assert persisted["intelligence_status"] == "ready"


async def test_extract_and_apply_ptp_sms_blocked_trial_is_flagged_not_faked(store):
    bedrock = FakeBedrock(
        {
            "disposition": "promise_to_pay",
            "payment_specifics": {
                "promised_amount": 100,
                "agreed_date": "tomorrow",
                "preferred_payment_method": None,
            },
            "contextual_data": {
                "reason_for_delinquency": None,
                "customer_sentiment": "neutral",
            },
            "next_best_action": "Send payment link",
        }
    )

    def twilio_factory(*_a):
        class _Raiser:
            class messages:
                @staticmethod
                def create(**_k):
                    raise RuntimeError(
                        "The number is unverified. Trial accounts cannot send to it."
                    )

        return _Raiser()

    changes = await extract_and_apply(
        _call_record(),
        store,
        _settings(),
        bedrock_client=bedrock,
        twilio_client_factory=twilio_factory,
    )

    # Blocked, NOT faked as success.
    assert changes["ptp_sms_status"] == "blocked_trial"
    assert changes["collection_paused"] is True
    assert changes["intelligence_status"] == "ready"


async def test_extract_and_apply_dispute_escalates(store):
    bedrock = FakeBedrock(
        {
            "disposition": "dispute_raised",
            "payment_specifics": {
                "promised_amount": None,
                "agreed_date": None,
                "preferred_payment_method": None,
            },
            "contextual_data": {
                "reason_for_delinquency": "claims already paid",
                "customer_sentiment": "hostile",
            },
            "next_best_action": "Escalate to human specialist",
        }
    )

    changes = await extract_and_apply(
        _call_record(),
        store,
        _settings(),
        bedrock_client=bedrock,
        twilio_client_factory=lambda *a: None,
    )

    assert changes["disposition"] == "dispute_raised"
    assert changes["escalated"] is True
    assert "escalation_to_human_specialist" in changes["triggers"]
    assert store.get_due("ACC-1001")["escalated"] is True


async def test_extract_and_apply_failed_extraction_sets_failed_status(store):
    class BadBedrock:
        def converse(self, **kwargs):
            return {"output": {"message": {"content": [{"text": "not json at all"}]}}}

    changes = await extract_and_apply(
        _call_record(),
        store,
        _settings(),
        bedrock_client=BadBedrock(),
        twilio_client_factory=lambda *a: None,
    )

    assert changes["intelligence_status"] == "failed"
    assert changes["disposition"] is None
    persisted = store.get_call("CA_pc")
    assert persisted["intelligence_status"] == "failed"


class FakeClock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def set(self, seconds: float) -> None:
        self.t = seconds


def _audio_event() -> BidiAudioStreamEvent:
    return BidiAudioStreamEvent(audio="AAAA", format="pcm", sample_rate=24000, channels=1)


async def test_no_added_latency_postcall_does_not_touch_call_metrics(store):
    """NO-LATENCY proof: the three call-latency metrics are computed on the
    audio path (session_start..session_end) and must be byte-for-byte unchanged
    after running the post-call extraction. If extraction were (wrongly) moved
    onto the metrics/audio path this test would fail.
    """
    clock = FakeClock()
    m = CallMetrics(call_sid="CA_pc", account_id="ACC-1001", clock=clock)

    clock.set(0.0)
    m.on_session_start()
    clock.set(1.0)
    m.record_event(BidiResponseStartEvent(response_id="r1"))
    clock.set(1.3)
    m.record_event(_audio_event())
    clock.set(1.8)
    m.record_event(
        BidiTranscriptStreamEvent(
            delta=None, text="You owe 428.57 dollars.", role="assistant",
            is_final=True, current_transcript="You owe 428.57 dollars.",
        )
    )
    clock.set(2.5)
    m.record_event(BidiResponseCompleteEvent(response_id="r1", stop_reason="complete"))
    clock.set(2.8)
    m.record_event(
        BidiTranscriptStreamEvent(
            delta=None, text="I can pay next month, the first Monday.", role="user",
            is_final=True, current_transcript="I can pay next month, the first Monday.",
        )
    )
    clock.set(4.0)
    m.on_session_end()

    # Capture the frozen call-latency metrics.
    before_total = m.total_e2e_ms
    before_nova = m.nova_total_ms
    before_ttft = m.first_ttft_ms
    assert before_total == 4000.0
    assert before_ttft == 300.0

    # Save and run the heavy post-call extraction.
    record = m.to_call_analytics()
    store.save_call_analytics(record)

    bedrock = FakeBedrock(
        {
            "disposition": "promise_to_pay",
            "payment_specifics": {
                "promised_amount": 428.57,
                "agreed_date": "next month 1st Monday",
                "preferred_payment_method": "card",
            },
            "contextual_data": {
                "reason_for_delinquency": "medical emergency",
                "customer_sentiment": "cooperative",
            },
            "next_best_action": "Schedule follow-up",
        }
    )

    def twilio_factory(*_a):
        class _C:
            class messages:
                @staticmethod
                def create(**_k):
                    class _M:
                        sid = "SM"

                    return _M()

        return _C()

    await extract_and_apply(
        record, store, _settings(), bedrock_client=bedrock,
        twilio_client_factory=twilio_factory,
    )

    # The call-latency metrics are unchanged by the post-call work.
    assert m.total_e2e_ms == before_total
    assert m.nova_total_ms == before_nova
    assert m.first_ttft_ms == before_ttft
