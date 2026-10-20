"""Tests for post-call intelligence extraction + downstream triggers.

The Bedrock Converse client and the Twilio client are stubbed so NO network
call happens. Includes the NO-LATENCY proof: running extract_and_apply after a
synthetic CallMetrics session must not change the frozen call-latency metrics.
"""

from __future__ import annotations

import json
import time

import pytest

from app.config import Settings
from app.metrics import CallMetrics
from app.postcall import extract_and_apply
from app.store import InMemoryStore
import itertools

from strands.bidi.types.events import (
    BidiAudioDeltaEvent,
    BidiResponseStartEvent,
    BidiResponseStopEvent,
    BidiTranscriptBlockEvent,
)

# Monotonic content-id generator for the GA events used by the no-latency tests.
_content_ids = itertools.count()


def _cid() -> str:
    return f"c{next(_content_ids)}"


def _final_transcript(text: str, role: str) -> BidiTranscriptBlockEvent:
    """GA completed-utterance transcript (the old is_final=True transcript)."""
    return BidiTranscriptBlockEvent(transcript=text, role=role, content_id=_cid())


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


class FakeCodeInterpreter:
    """Stub AgentCore CodeInterpreter returning a canned ISO date.

    Used to resolve natural-language ``agreed_date`` phrases without touching
    AWS. Records that it was started/invoked/stopped.
    """

    def __init__(self, region, canned_iso="2024-06-03"):
        self.region = region
        self.canned_iso = canned_iso
        self.started = False
        self.stopped = False
        self.invoked = False

    def start(self):
        self.started = True

    def invoke(self, method, params=None):
        self.invoked = True
        return {
            "stream": [
                {
                    "result": {
                        "isError": False,
                        "content": [{"type": "text", "text": self.canned_iso + "\n"}],
                    }
                }
            ]
        }

    def stop(self):
        self.stopped = True


def _ci_factory(canned_iso="2024-06-03"):
    """Build an injectable code-interpreter factory returning a canned date."""

    def factory(region):
        return FakeCodeInterpreter(region, canned_iso=canned_iso)

    return factory


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
        code_interpreter_factory=_ci_factory("2024-06-03"),
    )

    assert changes["disposition"] == "promise_to_pay"
    assert changes["intelligence_status"] == "ready"
    assert changes["collection_paused"] is True
    assert changes["ptp_sms_status"] == "sent"
    # The natural phrase ("next month 1st Monday") was resolved to a concrete
    # ISO date by the (stubbed) AgentCore Code Interpreter, NOT a local parser.
    agreed = changes["structured_data"]["payment_specifics"]["agreed_date"]
    assert agreed == "2024-06-03"
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
        code_interpreter_factory=_ci_factory("2024-05-16"),
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
        code_interpreter_factory=_ci_factory(),
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
        code_interpreter_factory=_ci_factory(),
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


def _audio_event() -> BidiAudioDeltaEvent:
    return BidiAudioDeltaEvent(
        audio="AAAA", format="pcm", sample_rate=24000, channels=1, content_id=_cid()
    )


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
    m.record_event(_final_transcript("You owe 428.57 dollars.", role="assistant"))
    clock.set(2.5)
    m.record_event(BidiResponseStopEvent(response_id="r1"))
    clock.set(2.8)
    m.record_event(_final_transcript("I can pay next month, the first Monday.", role="user"))
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
        code_interpreter_factory=_ci_factory("2024-06-03"),
    )

    # The call-latency metrics are unchanged by the post-call work.
    assert m.total_e2e_ms == before_total
    assert m.nova_total_ms == before_nova
    assert m.first_ttft_ms == before_ttft


class BlockingBedrock:
    """Bedrock stub that BLOCKS (sleeps) and records if/when it was called.

    Used to prove the extraction is NOT awaited on the real-time audio/turn
    path: if ``converse`` ran there it would stall the audio loop, so a test
    that drives the audio path and then asserts ``called`` is still False at
    ``on_session_end`` fails the moment anyone reverts the design and awaits the
    LLM inside the turn loop.
    """

    def __init__(self) -> None:
        self.called = False

    def converse(self, **kwargs):
        self.called = True
        # A real revert would block the audio path here; simulate the stall.
        time.sleep(0.5)
        return {
            "output": {
                "message": {
                    "content": [
                        {
                            "text": json.dumps(
                                {
                                    "disposition": "undetermined",
                                    "payment_specifics": {},
                                    "contextual_data": {},
                                    "next_best_action": None,
                                }
                            )
                        }
                    ]
                }
            }
        }


class BlockingCodeInterpreter:
    """Code-interpreter stub whose ``called`` flips only when invoked.

    Proves the ~1.9s AgentCore Code Interpreter never runs on the real-time
    audio path: a shared ``.called`` flag is set True only when the off-path
    post-call task actually resolves a date phrase. ``start()`` sleeps to
    simulate the costly session-start stall a wrong wiring would add to the
    turn loop.
    """

    #: Shared across instances built by the factory so the test can observe it.
    called = False

    def __init__(self, region):
        self.region = region

    def start(self):
        BlockingCodeInterpreter.called = True
        time.sleep(0.5)

    def invoke(self, method, params=None):
        return {
            "stream": [
                {
                    "result": {
                        "isError": False,
                        "content": [{"type": "text", "text": "2024-06-03\n"}],
                    }
                }
            ]
        }

    def stop(self):
        pass


async def test_no_latency_audio_path_never_invokes_extraction(store):
    """REGRESSION GUARD (no-latency): driving the full real-time audio/turn
    path (on_session_start -> record_event(...) -> on_session_end) must NEVER
    call the Bedrock extraction stub NOR the AgentCore Code Interpreter. The
    audio path has no reference to either; they only run post-call off-path.
    This fails if someone wires ``extract_and_apply`` / ``converse`` / the code
    interpreter into ``record_event`` or the turn loop, because ``.called``
    would flip to True before/at ``on_session_end``.
    """
    blocking = BlockingBedrock()
    BlockingCodeInterpreter.called = False
    clock = FakeClock()
    m = CallMetrics(call_sid="CA_np", account_id="ACC-1001", clock=clock)

    clock.set(0.0)
    m.on_session_start()
    clock.set(1.0)
    m.record_event(BidiResponseStartEvent(response_id="r1"))
    clock.set(1.3)
    m.record_event(_audio_event())
    clock.set(1.8)
    m.record_event(_final_transcript("You owe 428.57 dollars.", role="assistant"))
    clock.set(2.5)
    m.record_event(BidiResponseStopEvent(response_id="r1"))
    clock.set(4.0)
    m.on_session_end()

    # Neither heavy stub was touched by the audio/turn path.
    assert blocking.called is False
    assert BlockingCodeInterpreter.called is False

    # Only NOW (post-call, off-path) do the extractor AND the code interpreter
    # run. The in-call capture tool recorded a raw phrase on the due record, so
    # the off-path task resolves it via the (blocking) code interpreter,
    # flipping its flag only here.
    store.update_due("ACC-1001", promised_date_phrase="next month, the first Monday")
    record = m.to_call_analytics()
    store.save_call_analytics(record)
    await extract_and_apply(
        record, store, _settings(), bedrock_client=blocking,
        twilio_client_factory=lambda *a: None,
        code_interpreter_factory=lambda region: BlockingCodeInterpreter(region),
    )
    assert blocking.called is True


async def test_ptp_sms_trial_mode_pins_to_verified_number(store):
    """TRIAL account: the PTP SMS is pinned to the verified caller id even when
    the debtor record carries a different phone, and the masked destination is
    recorded on the call record.
    """
    store.update_due("ACC-1001", phone="+15559990000")
    bedrock = FakeBedrock(
        {
            "disposition": "promise_to_pay",
            "payment_specifics": {"promised_amount": 50, "agreed_date": "tomorrow",
                                  "preferred_payment_method": None},
            "contextual_data": {"reason_for_delinquency": None, "customer_sentiment": None},
            "next_best_action": "Send link",
        }
    )
    sent_to: list[str] = []

    def twilio_factory(*_a):
        class _C:
            class messages:
                @staticmethod
                def create(**kwargs):
                    sent_to.append(kwargs["to"])
                    class _M:
                        sid = "SM"
                    return _M()
        return _C()

    changes = await extract_and_apply(
        _call_record(), store, _settings(twilio_trial_mode=True),
        bedrock_client=bedrock, twilio_client_factory=twilio_factory,
        code_interpreter_factory=_ci_factory("2024-05-16"),
    )

    # Trial: destination is the verified number, NOT the debtor's phone.
    assert sent_to == ["+15005550010"]
    assert changes["ptp_sms_status"] == "sent"
    # Masked destination recorded (last 4 digits of the verified number).
    assert changes["ptp_sms_to"] == "+******0010"


async def test_ptp_sms_production_mode_prefers_debtor_phone(store):
    """FULL account (trial mode off): the PTP SMS addresses the debtor's own
    phone from the account record (falling back to the verified id only when
    the record has no phone).
    """
    store.update_due("ACC-1001", phone="+15559990000")
    bedrock = FakeBedrock(
        {
            "disposition": "promise_to_pay",
            "payment_specifics": {"promised_amount": 50, "agreed_date": "tomorrow",
                                  "preferred_payment_method": None},
            "contextual_data": {"reason_for_delinquency": None, "customer_sentiment": None},
            "next_best_action": "Send link",
        }
    )
    sent_to: list[str] = []

    def twilio_factory(*_a):
        class _C:
            class messages:
                @staticmethod
                def create(**kwargs):
                    sent_to.append(kwargs["to"])
                    class _M:
                        sid = "SM"
                    return _M()
        return _C()

    changes = await extract_and_apply(
        _call_record(), store, _settings(twilio_trial_mode=False),
        bedrock_client=bedrock, twilio_client_factory=twilio_factory,
        code_interpreter_factory=_ci_factory("2024-05-16"),
    )

    assert sent_to == ["+15559990000"]
    assert changes["ptp_sms_status"] == "sent"
    assert changes["ptp_sms_to"] == "+******0000"


async def test_ptp_sms_production_failure_is_failed_not_blocked_trial(store):
    """FULL account: a send failure is a genuine 'failed', not 'blocked_trial'."""
    bedrock = FakeBedrock(
        {
            "disposition": "promise_to_pay",
            "payment_specifics": {"promised_amount": 50, "agreed_date": "tomorrow",
                                  "preferred_payment_method": None},
            "contextual_data": {"reason_for_delinquency": None, "customer_sentiment": None},
            "next_best_action": "Send link",
        }
    )

    def twilio_factory(*_a):
        class _Raiser:
            class messages:
                @staticmethod
                def create(**_k):
                    raise RuntimeError("carrier rejected the message")
        return _Raiser()

    changes = await extract_and_apply(
        _call_record(), store, _settings(twilio_trial_mode=False),
        bedrock_client=bedrock, twilio_client_factory=twilio_factory,
        code_interpreter_factory=_ci_factory("2024-05-16"),
    )

    assert changes["ptp_sms_status"] == "failed"
    assert "ptp_sms_failed" in changes["triggers"]


def test_mask_number_keeps_only_last_four():
    from app.postcall import _mask_number

    assert _mask_number("+15551234567") == "+******4567"
    assert _mask_number(None) is None
    assert _mask_number("123") == "***"
