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

    # Turn count
    assert len(m.turns) == 2
    assert m.summary()["turn_count"] == 2

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
    assert analytics["events_log"]  # in-depth log present


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
