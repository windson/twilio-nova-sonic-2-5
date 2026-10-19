"""Tests for the CallMetrics collector.

A synthetic ordered sequence of fake bi-di events plus synthetic Twilio media
timestamps is fed into :class:`CallMetrics` with an injected deterministic
clock, and the computed metrics are asserted against hand-computed expectations.
"""

from __future__ import annotations

import logging

from app.metrics import CallMetrics
from strands.experimental.bidi.types.events import (
    BidiAudioStreamEvent,
    BidiInterruptionEvent,
    BidiResponseCompleteEvent,
    BidiResponseStartEvent,
    BidiTranscriptStreamEvent,
    BidiUsageEvent,
)


class FakeClock:
    """A controllable monotonic clock: returns the current value, advanced by set()."""

    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def set(self, seconds: float) -> None:
        self.t = seconds


def _audio_event() -> BidiAudioStreamEvent:
    return BidiAudioStreamEvent(audio="AAAA", format="pcm", sample_rate=24000, channels=1)


def test_metrics_full_synthetic_stream():
    clock = FakeClock()
    m = CallMetrics(call_sid="CA-TEST", account_id="ACC-1001", clock=clock)

    # t=0.0 session start
    clock.set(0.0)
    m.on_session_start()

    # Inbound Twilio media: user speaking. Twilio 'timestamp' is stream-relative
    # ms. The first frame anchors the baseline (no delay). Subsequent frames
    # measure how far local receive time drifts ahead of the Twilio clock.
    # Anchor at t=0.5 local / twilio=480ms.
    clock.set(0.5)
    m.record_inbound_media(480)
    # t=1.0 local -> local_elapsed=500ms vs twilio_elapsed=480ms -> delay 20ms.
    clock.set(1.0)
    m.record_inbound_media(960)
    # t=1.04 local -> local_elapsed=540ms vs twilio_elapsed=500ms -> delay 40ms.
    # (last inbound before the first response_start, used for turn latency)
    clock.set(1.04)
    m.record_inbound_media(980)

    # Turn 1: response starts at t=1.2 (latency = 1.2 - 1.04 = 160ms)
    clock.set(1.2)
    m.record_event(BidiResponseStartEvent(response_id="r1"))
    # First audio at t=1.5 -> TTFT = 300ms
    clock.set(1.5)
    m.record_event(_audio_event())
    # more audio (should not change TTFT)
    clock.set(1.7)
    m.record_event(_audio_event())
    # assistant transcript
    clock.set(1.8)
    m.record_event(
        BidiTranscriptStreamEvent(
            delta=None, text="Hello, this is Ava.", role="assistant", is_final=True,
            current_transcript="Hello, this is Ava.",
        )
    )
    # response completes at t=2.2 -> duration = 1000ms
    clock.set(2.2)
    m.record_event(BidiResponseCompleteEvent(response_id="r1", stop_reason="complete"))

    # Barge-in while user interrupts
    clock.set(2.5)
    m.record_event(BidiInterruptionEvent(reason="user_speech"))

    # User finishes speaking (final user transcript) at t=3.0
    clock.set(3.0)
    m.record_event(
        BidiTranscriptStreamEvent(
            delta=None, text="I can pay next week.", role="user", is_final=True,
            current_transcript="I can pay next week.",
        )
    )

    # Turn 2: response starts at t=3.2 -> latency = 3.2 - 3.0 = 200ms
    clock.set(3.2)
    m.record_event(BidiResponseStartEvent(response_id="r2"))
    # first audio at t=3.35 -> TTFT = 150ms
    clock.set(3.35)
    m.record_event(_audio_event())
    # completes at t=3.9 -> duration = 700ms
    clock.set(3.9)
    m.record_event(BidiResponseCompleteEvent(response_id="r2", stop_reason="complete"))

    # Usage
    clock.set(3.95)
    m.record_event(
        BidiUsageEvent(
            input_tokens=120,
            output_tokens=80,
            total_tokens=200,
            modality_details=None,
            cache_read_input_tokens=0,
            cache_write_input_tokens=0,
        )
    )

    # Session ends at t=4.0 -> total e2e = 4000ms
    clock.set(4.0)
    m.on_session_end()

    # -- Assertions ----------------------------------------------------------
    assert m.call_sid == "CA-TEST"
    assert m.account_id == "ACC-1001"

    # Nova response windows (response_id-based, preserved as secondary signal)
    assert len(m.turns) == 2
    assert m.summary()["nova_response_count"] == 2

    # Conversational turns: assistant opening segment + user segment = 2.
    assert m.summary()["turn_count"] == 2
    assert len(m.conversation_turns) == 2
    assert [t["speaker"] for t in m.conversation_turns] == ["assistant", "user"]
    assert m.conversation_turns[0]["text"] == "Hello, this is Ava."
    assert m.conversation_turns[1]["text"] == "I can pay next week."
    # Assistant conversational turn carries ttft/latency from nearest response.
    assert m.conversation_turns[0]["ttft_ms"] == 300.0
    assert m.conversation_turns[0]["latency_ms"] == 160.0
    # Every conversational turn exposes total_ms.
    for t in m.conversation_turns:
        assert "total_ms" in t

    # Per-turn duration
    assert m.turns[0]["duration_ms"] == 1000.0
    assert m.turns[1]["duration_ms"] == 700.0

    # Per-turn latency (user-stop -> response_start)
    assert m.turns[0]["turn_latency_ms"] == 160.0
    assert m.turns[1]["turn_latency_ms"] == 200.0

    # TTFT per-turn and conversation-level (first turn TTFT)
    assert m.turns[0]["ttft_ms"] == 300.0
    assert m.turns[1]["ttft_ms"] == 150.0
    assert m.first_ttft_ms == 300.0

    # Total end-to-end
    assert m.total_e2e_ms == 4000.0

    # Nova total = sum of active response windows = 1000 + 700 = 1700
    assert m.nova_total_ms == 1700.0

    # Twilio vs Nova split: non-nova share = total - nova
    assert m.non_nova_ms == 2300.0
    # Twilio transport delay mean of [20, 40] = 30ms
    assert m.twilio_latency_ms == 30.0

    # Barge-in count
    assert m.barge_in_count == 1
    assert m.summary()["barge_in_count"] == 1

    # Token usage captured
    assert m.usage["total_tokens"] == 200

    # Transcripts captured (one assistant final, one user final)
    roles = {t["role"] for t in m.transcripts if t["is_final"]}
    assert {"assistant", "user"} <= roles

    # Serializable analytics round-trip shape
    analytics = m.to_call_analytics()
    assert analytics["call_sid"] == "CA-TEST"
    assert len(analytics["turns"]) == 2
    assert analytics["nova_response_count"] == 2
    assert analytics["turn_count"] == 2
    assert len(analytics["conversation_turns"]) == 2
    assert analytics["events_log"]  # in-depth log present


def test_conversation_turns_from_alternation_with_shared_response_id():
    """The core bug fix: Nova reuses ONE response_id across several user<->
    assistant exchanges, yet the conversational turn_count must be > 1.

    Also asserts conversation_turns carry speaker/text/total_ms and assistant
    turns carry ttft_ms/latency_ms, while nova_response_count stays at the old
    response_id-based value (here: 1, because of the single shared id).
    """
    clock = FakeClock()
    m = CallMetrics(call_sid="CA-SHARED", account_id="ACC-1001", clock=clock)

    clock.set(0.0)
    m.on_session_start()

    # Nova opens ONE response and keeps the same response_id for everything.
    clock.set(0.5)
    m.record_event(BidiResponseStartEvent(response_id="shared"))
    # First audio -> TTFT 300ms for this (only) Nova response.
    clock.set(0.8)
    m.record_event(_audio_event())

    # Assistant opening turn (AI speaks first).
    clock.set(1.0)
    m.record_event(
        BidiTranscriptStreamEvent(
            delta=None, text="Hello, this is Ava from billing.", role="assistant",
            is_final=True, current_transcript="Hello, this is Ava from billing.",
        )
    )
    # User replies.
    clock.set(1.5)
    m.record_event(
        BidiTranscriptStreamEvent(
            delta=None, text="Hi, what is this about?", role="user",
            is_final=True, current_transcript="Hi, what is this about?",
        )
    )
    # Assistant replies again (still same response_id).
    clock.set(2.0)
    m.record_event(
        BidiTranscriptStreamEvent(
            delta=None, text="You have a balance of fifty dollars.", role="assistant",
            is_final=True, current_transcript="You have a balance of fifty dollars.",
        )
    )
    # User replies again.
    clock.set(2.6)
    m.record_event(
        BidiTranscriptStreamEvent(
            delta=None, text="Okay, I will pay tomorrow.", role="user",
            is_final=True, current_transcript="Okay, I will pay tomorrow.",
        )
    )

    clock.set(3.0)
    m.record_event(BidiResponseCompleteEvent(response_id="shared", stop_reason="complete"))
    clock.set(3.5)
    m.on_session_end()

    # Old response_id-based count is 1 (the bug symptom), preserved distinctly.
    assert m.summary()["nova_response_count"] == 1
    assert len(m.turns) == 1

    # Conversational turn_count reflects the real back-and-forth: 4 segments
    # (assistant, user, assistant, user) -> > 1.
    assert m.summary()["turn_count"] == 4
    assert m.summary()["turn_count"] > 1

    turns = m.conversation_turns
    assert [t["speaker"] for t in turns] == ["assistant", "user", "assistant", "user"]
    for t in turns:
        assert t["text"]
        assert "total_ms" in t

    # The FIRST assistant turn maps to the one Nova response window available,
    # so it carries a real TTFT labelled as coming from the response window.
    assert turns[0]["ttft_ms"] == 300.0
    assert turns[0]["ttft_source"] == "response_window"

    # FINDING #1 fix: the LATER assistant turn (index 2) still gets a
    # meaningful, NON-None per-utterance latency derived from the preceding
    # user turn's last final transcript (1500ms) to this assistant turn's first
    # final transcript (2000ms) = 500ms, even though Nova reused a single
    # response_id and no response window is left for it.
    assert turns[2]["speaker"] == "assistant"
    assert turns[2]["latency_ms"] == 500.0
    assert turns[2]["latency_ms"] is not None
    # TTFT for the later turn cannot be derived per-utterance (Nova emits no
    # per-utterance audio marker under a shared response_id), so it is honestly
    # reported as unavailable rather than silently blank.
    assert turns[2]["ttft_ms"] is None
    assert turns[2]["ttft_source"] == "unavailable"

    # total_ms (elapsed conversation time) is non-zero for turns that are a
    # single final transcript, because it measures to the next turn's start.
    # Turn 0 (assistant@1000ms) -> next turn (user@1500ms) = 500ms.
    assert turns[0]["total_ms"] == 500.0
    # Turn 1 (user@1500ms) -> next turn (assistant@2000ms) = 500ms.
    assert turns[1]["total_ms"] == 500.0


def test_conversation_turns_join_consecutive_same_role_finals():
    """Consecutive final transcripts of the same role collapse into one turn."""
    clock = FakeClock()
    m = CallMetrics(clock=clock)
    clock.set(0.0)
    m.on_session_start()

    clock.set(1.0)
    m.record_event(
        BidiTranscriptStreamEvent(
            delta=None, text="Hello there.", role="assistant", is_final=True,
            current_transcript="Hello there.",
        )
    )
    clock.set(1.3)
    m.record_event(
        BidiTranscriptStreamEvent(
            delta=None, text="How can I help?", role="assistant", is_final=True,
            current_transcript="How can I help?",
        )
    )
    clock.set(2.0)
    m.record_event(
        BidiTranscriptStreamEvent(
            delta=None, text="I need help.", role="user", is_final=True,
            current_transcript="I need help.",
        )
    )
    clock.set(2.5)
    m.on_session_end()

    turns = m.conversation_turns
    assert len(turns) == 2
    assert turns[0]["speaker"] == "assistant"
    assert turns[0]["text"] == "Hello there. How can I help?"
    # Raw intra-segment span of the joined assistant finals (1300ms - 1000ms).
    assert turns[0]["transcript_span_ms"] == 300.0
    # total_ms is the elapsed time the turn occupied the conversation = span to
    # the NEXT turn's start (user turn at 2000ms) - this turn's start (1000ms).
    assert turns[0]["total_ms"] == 1000.0
    assert turns[1]["speaker"] == "user"


def test_single_final_turn_total_ms_is_nonzero_elapsed():
    """FINDING #2 fix: a turn made of a single final transcript must report a
    sensible non-zero elapsed time (span to the next turn's start), not 0.

    Also asserts the LAST turn falls back to its transcript span / session end,
    and that the raw intra-segment span is still exposed as transcript_span_ms.
    """
    clock = FakeClock()
    m = CallMetrics(clock=clock)
    clock.set(0.0)
    m.on_session_start()

    # Each turn is a SINGLE final transcript -> intra-segment span is 0 for all.
    clock.set(1.0)
    m.record_event(
        BidiTranscriptStreamEvent(
            delta=None, text="Hello, this is Ava.", role="assistant", is_final=True,
            current_transcript="Hello, this is Ava.",
        )
    )
    clock.set(2.5)
    m.record_event(
        BidiTranscriptStreamEvent(
            delta=None, text="I can pay next week.", role="user", is_final=True,
            current_transcript="I can pay next week.",
        )
    )
    clock.set(4.0)
    m.record_event(
        BidiTranscriptStreamEvent(
            delta=None, text="Great, thank you.", role="assistant", is_final=True,
            current_transcript="Great, thank you.",
        )
    )
    clock.set(6.0)
    m.on_session_end()  # total_e2e = 6000ms

    turns = m.conversation_turns
    # Every segment is a single final -> raw span is 0 for all of them.
    assert all(t["transcript_span_ms"] == 0.0 for t in turns)

    # But total_ms (elapsed conversation time) is NON-zero and sensible:
    # turn 0 (assistant@1000) -> next (user@2500) = 1500ms.
    assert turns[0]["total_ms"] == 1500.0
    # turn 1 (user@2500) -> next (assistant@4000) = 1500ms.
    assert turns[1]["total_ms"] == 1500.0
    # LAST turn (assistant@4000) has no following turn; its span is 0, so it
    # falls back to the session end (6000ms) -> 2000ms.
    assert turns[2]["total_ms"] == 2000.0
    assert all(t["total_ms"] not in (None, 0.0) for t in turns)


def test_user_partial_transcript_fallback_populates_chat():
    """If only non-final user transcripts arrive, the latest partial is used so
    the chat view is not empty.
    """
    clock = FakeClock()
    m = CallMetrics(clock=clock)
    clock.set(0.0)
    m.on_session_start()

    clock.set(1.0)
    m.record_event(
        BidiTranscriptStreamEvent(
            delta=None, text="Hello.", role="assistant", is_final=True,
            current_transcript="Hello.",
        )
    )
    # User only emits partial (non-final) transcripts.
    clock.set(1.5)
    m.record_event(
        BidiTranscriptStreamEvent(
            delta="I", text="I", role="user", is_final=False, current_transcript="I",
        )
    )
    clock.set(1.8)
    m.record_event(
        BidiTranscriptStreamEvent(
            delta=" will pay", text="I will pay", role="user", is_final=False,
            current_transcript="I will pay",
        )
    )
    clock.set(2.0)
    m.on_session_end()

    turns = m.conversation_turns
    speakers = [t["speaker"] for t in turns]
    assert "user" in speakers
    user_turn = next(t for t in turns if t["speaker"] == "user")
    assert user_turn["text"] == "I will pay"


def test_metrics_emits_json_log_lines_for_lifecycle(caplog):
    """call_placed and call_end_summary are emitted as structured log records."""
    clock = FakeClock()
    logger = logging.getLogger("ar_collections.metrics")
    m = CallMetrics(call_sid="CA-LOG", account_id="ACC-1001", clock=clock, logger_=logger)

    with caplog.at_level(logging.INFO, logger="ar_collections.metrics"):
        clock.set(0.0)
        m.on_session_start()
        clock.set(1.0)
        m.on_session_end()

    messages = [r.getMessage() for r in caplog.records]
    assert "call_placed" in messages
    assert "call_end_summary" in messages

    # The events_log holds structured entries for both lifecycle events.
    logged_types = {e["event_type"] for e in m.events_log}
    assert "call_placed" in logged_types
    assert "stream_start" in logged_types
    assert "call_end_summary" in logged_types


def test_ttft_attributed_to_newest_open_turn_under_overlap():
    """When a barge-in opens turn 2 while turn 1 is still open, the next audio
    first-token is attributed to the newest (turn 2) turn, not the stale one.
    """
    clock = FakeClock()
    m = CallMetrics(clock=clock)
    clock.set(0.0)
    m.on_session_start()

    # Turn 1 starts and (in this overlap scenario) never gets its own audio
    # before turn 2 opens.
    clock.set(1.0)
    m.record_event(BidiResponseStartEvent(response_id="r1"))
    # Barge-in opens turn 2 at t=1.2 while r1 is still open.
    clock.set(1.2)
    m.record_event(BidiResponseStartEvent(response_id="r2"))
    # First audio at t=1.5 -> should be TTFT for r2 (newest open turn) = 300ms,
    # NOT r1 (which would wrongly read 500ms).
    clock.set(1.5)
    m.record_event(_audio_event())

    clock.set(2.0)
    m.record_event(BidiResponseCompleteEvent(response_id="r2", stop_reason="complete"))
    clock.set(2.1)
    m.record_event(BidiResponseCompleteEvent(response_id="r1", stop_reason="interrupted"))
    clock.set(3.0)
    m.on_session_end()

    by_id = {t["response_id"]: t for t in m.turns}
    assert by_id["r2"]["ttft_ms"] == 300.0
    assert by_id["r1"]["ttft_ms"] is None


def test_unmatched_response_complete_without_start_is_safe():
    clock = FakeClock()
    m = CallMetrics(clock=clock)
    clock.set(0.0)
    m.on_session_start()
    clock.set(0.5)
    # Complete with no prior start -> must not raise, no turn recorded.
    m.record_event(BidiResponseCompleteEvent(response_id="ghost", stop_reason="complete"))
    assert len(m.turns) == 0
