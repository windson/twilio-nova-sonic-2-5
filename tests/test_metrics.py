"""Tests for the CallMetrics collector.

A synthetic ordered sequence of fake bi-di events plus synthetic Twilio media
timestamps is fed into :class:`CallMetrics` with an injected deterministic
clock, and the computed metrics are asserted against hand-computed expectations.
"""

from __future__ import annotations

import base64
import logging

import itertools

from app.metrics import CallMetrics
from strands.bidi.types.events import (
    BidiAudioDeltaEvent,
    BidiBargeInEvent,
    BidiResponseStartEvent,
    BidiResponseStopEvent,
    BidiToolUseBlocksEvent,
    BidiTranscriptBlockEvent,
    BidiTranscriptDeltaEvent,
    BidiUsageEvent,
)

# Monotonic content-id generator so each GA event carries a unique id without
# the tests having to thread one through by hand.
_content_ids = itertools.count()


def _cid() -> str:
    return f"c{next(_content_ids)}"


def _final_transcript(text: str, role: str) -> BidiTranscriptBlockEvent:
    """GA completed-utterance transcript (the old is_final=True transcript)."""
    return BidiTranscriptBlockEvent(transcript=text, role=role, content_id=_cid())


def _partial_transcript(
    delta: str, role: str, content_id: str | None = None
) -> BidiTranscriptDeltaEvent:
    """GA partial transcript delta (the old is_final=False transcript).

    Deltas of one utterance share a ``content_id``; pass an explicit one to
    group consecutive deltas into the same utterance (a new/omitted id means a
    new utterance).
    """
    return BidiTranscriptDeltaEvent(
        delta=delta, role=role, content_id=content_id or _cid()
    )


class FakeClock:
    """A controllable monotonic clock: returns the current value, advanced by set()."""

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


# Decoded byte length of the small "AAAA" payload used by _audio_event().
# base64 "AAAA" decodes to 3 bytes. For PCM16 mono 24 kHz the frame duration is
# decoded_bytes / (2 * 24000 * 1) * 1000 ms = decoded_bytes / 48 ms.
_SMALL_FRAME_BYTES = len(base64.b64decode("AAAA"))
_SMALL_FRAME_MS = _SMALL_FRAME_BYTES / 48.0


def _audio_event_bytes(num_bytes: int) -> BidiAudioDeltaEvent:
    """Build a BidiAudioDeltaEvent whose decoded PCM payload is exactly
    ``num_bytes`` bytes, so the Nova speaking-time contribution is deterministic.

    PCM16 mono 24 kHz: ms = num_bytes / (2 * 24000) * 1000 = num_bytes / 48.
    """
    payload = base64.b64encode(b"\x00" * num_bytes).decode()
    return BidiAudioDeltaEvent(
        audio=payload, format="pcm", sample_rate=24000, channels=1, content_id=_cid()
    )


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
    m.record_event(_final_transcript("Hello, this is Ava.", role="assistant"))
    # response completes at t=2.2 -> duration = 1000ms
    clock.set(2.2)
    m.record_event(BidiResponseStopEvent(response_id="r1"))

    # Barge-in while user interrupts (GA event carries no reason).
    clock.set(2.5)
    m.record_event(BidiBargeInEvent())

    # User finishes speaking (final user transcript) at t=3.0
    clock.set(3.0)
    m.record_event(_final_transcript("I can pay next week.", role="user"))

    # Turn 2: response starts at t=3.2 -> latency = 3.2 - 3.0 = 200ms
    clock.set(3.2)
    m.record_event(BidiResponseStartEvent(response_id="r2"))
    # first audio at t=3.35 -> TTFT = 150ms
    clock.set(3.35)
    m.record_event(_audio_event())
    # completes at t=3.9 -> duration = 700ms
    clock.set(3.9)
    m.record_event(BidiResponseStopEvent(response_id="r2"))

    # Usage: GA shape - cache/modality live in input/output token details.
    clock.set(3.95)
    m.record_event(
        BidiUsageEvent(
            input_tokens=120,
            output_tokens=80,
            total_tokens=200,
            input_token_details={"cache_read": 0, "text": 100, "audio": 20},
            output_token_details={"text": 80},
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

    # Headline turn_count is now PER-UTTERANCE: each final user/assistant
    # transcript counts separately. Here: one assistant final + one user final
    # = 2 utterance turns.
    assert m.summary()["turn_count"] == 2

    # The GROUPED conversation_turns (chat view) still exist as a secondary
    # model: assistant opening segment + user segment = 2.
    assert len(m.conversation_turns) == 2
    assert [t["speaker"] for t in m.conversation_turns] == ["assistant", "user"]
    assert m.conversation_turns[0]["text"] == "Hello, this is Ava."
    assert m.conversation_turns[1]["text"] == "I can pay next week."
    # Assistant conversational turn carries ttft from the nearest response.
    assert m.conversation_turns[0]["ttft_ms"] == 300.0
    # FEAT-004: this is the OPENING assistant turn (AI-first) with no caller
    # turn preceding it, so the caller->reply gap is None (never a turn-to-turn
    # gap), and the backward-compat aliases mirror that None. The raw response
    # window's turn_latency_ms (asserted below on m.turns) is unaffected.
    assert m.conversation_turns[0]["caller_reply_gap_ms"] is None
    assert m.conversation_turns[0]["response_wait_ms"] is None
    assert m.conversation_turns[0]["latency_ms"] is None
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

    # CORRECTED Nova total = sum of outbound audio-frame durations. This stream
    # feeds three _audio_event() frames (t=1.5, t=1.7, t=3.35), each a 3-byte
    # PCM payload => 3 * (3/48) ms. The OLD response-window sum (1000 + 700 =
    # 1700ms) is now reported SEPARATELY as nova_response_window_ms.
    assert m.nova_total_ms == round(3 * _SMALL_FRAME_MS, 3)
    assert m.nova_response_window_ms == 1700.0
    assert m.nova_total_ms != m.nova_response_window_ms

    # Twilio vs Nova split: non-nova share = max(total - corrected nova, 0).
    assert m.non_nova_ms == round(4000.0 - m.nova_total_ms, 3)
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

    # Serializable analytics round-trip shape.
    analytics = m.to_call_analytics()
    assert analytics["call_sid"] == "CA-TEST"
    # 'turns' is now the PER-UTTERANCE list (one assistant + one user = 2).
    assert len(analytics["turns"]) == 2
    assert [t["speaker"] for t in analytics["turns"]] == ["assistant", "user"]
    for t in analytics["turns"]:
        assert set(("index", "speaker", "text", "duration_ms")) <= set(t)
    # response_id windows preserved separately.
    assert analytics["nova_response_count"] == 2
    assert len(analytics["nova_response_turns"]) == 2
    # Headline turn_count is the per-utterance count.
    assert analytics["turn_count"] == 2
    # Grouped chat model still present.
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
    m.record_event(_final_transcript("Hello, this is Ava from billing.", role="assistant"))
    # User replies.
    clock.set(1.5)
    m.record_event(_final_transcript("Hi, what is this about?", role="user"))
    # Assistant replies again (still same response_id).
    clock.set(2.0)
    m.record_event(_final_transcript("You have a balance of fifty dollars.", role="assistant"))
    # User replies again.
    clock.set(2.6)
    m.record_event(_final_transcript("Okay, I will pay tomorrow.", role="user"))

    clock.set(3.0)
    m.record_event(BidiResponseStopEvent(response_id="shared"))
    clock.set(3.5)
    m.on_session_end()

    # Old response_id-based count is 1 (the bug symptom), preserved distinctly.
    assert m.summary()["nova_response_count"] == 1
    assert len(m.turns) == 1

    # Headline turn_count is per-utterance. Here the four finals already
    # alternate role (assistant, user, assistant, user), so the per-utterance
    # count equals the grouped count: 4 (and > 1, which is the bug fix).
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
    m.record_event(_final_transcript("Hello there.", role="assistant"))
    clock.set(1.3)
    m.record_event(_final_transcript("How can I help?", role="assistant"))
    clock.set(2.0)
    m.record_event(_final_transcript("I need help.", role="user"))
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
    m.record_event(_final_transcript("Hello, this is Ava.", role="assistant"))
    clock.set(2.5)
    m.record_event(_final_transcript("I can pay next week.", role="user"))
    clock.set(4.0)
    m.record_event(_final_transcript("Great, thank you.", role="assistant"))
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
    m.record_event(_final_transcript("Hello.", role="assistant"))
    # User only emits partial (delta) transcripts for ONE utterance (same
    # content_id) -> accumulate to "I will pay".
    clock.set(1.5)
    m.record_event(_partial_transcript("I", role="user", content_id="u1"))
    clock.set(1.8)
    m.record_event(_partial_transcript(" will pay", role="user", content_id="u1"))
    clock.set(2.0)
    m.on_session_end()

    turns = m.conversation_turns
    speakers = [t["speaker"] for t in turns]
    assert "user" in speakers
    user_turn = next(t for t in turns if t["speaker"] == "user")
    assert user_turn["text"] == "I will pay"


def test_partial_only_utterances_do_not_concatenate_across_content_ids():
    """On a partial-only call with several user utterances (deltas with
    DIFFERENT content_ids and no block), the fallback is the LATEST utterance's
    text, not a run-on concatenation of every utterance.
    """
    clock = FakeClock()
    m = CallMetrics(clock=clock)
    clock.set(0.0)
    m.on_session_start()

    clock.set(1.0)
    m.record_event(_final_transcript("Hello.", role="assistant"))

    # Utterance 1: two deltas sharing content_id "u1".
    clock.set(1.5)
    m.record_event(_partial_transcript("I will", role="user", content_id="u1"))
    clock.set(1.7)
    m.record_event(_partial_transcript(" pay today", role="user", content_id="u1"))

    # Utterance 2: a NEW content_id "u2" -> must NOT append to utterance 1.
    clock.set(2.5)
    m.record_event(_partial_transcript("Actually", role="user", content_id="u2"))
    clock.set(2.7)
    m.record_event(_partial_transcript(" next week", role="user", content_id="u2"))

    clock.set(3.0)
    m.on_session_end()

    # The accumulator reflects only the latest utterance, not the concatenation.
    assert m._last_user_partial_text == "Actually next week"
    assert "I will pay today" not in (m._last_user_partial_text or "")

    user_turns = [t for t in m.conversation_turns if t["speaker"] == "user"]
    assert len(user_turns) == 1
    assert user_turns[0]["text"] == "Actually next week"


def test_user_block_final_clears_partial_fallback_accumulator():
    """A real final (BlockEvent) takes precedence over the partial fallback and
    resets the accumulator so a later utterance's deltas start fresh.
    """
    clock = FakeClock()
    m = CallMetrics(clock=clock)
    clock.set(0.0)
    m.on_session_start()

    clock.set(1.0)
    m.record_event(_partial_transcript("I will", role="user", content_id="u1"))
    clock.set(1.2)
    # Block closes utterance u1 -> clears the partial accumulator.
    m.record_event(_final_transcript("I will pay today.", role="user"))
    assert m._last_user_partial_text is None

    # A fresh partial-only utterance accumulates on its own, uncontaminated.
    clock.set(2.0)
    m.record_event(_partial_transcript("Thanks", role="user", content_id="u2"))
    assert m._last_user_partial_text == "Thanks"


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
    m.record_event(BidiResponseStopEvent(response_id="r2"))
    clock.set(2.1)
    m.record_event(BidiResponseStopEvent(response_id="r1"))
    clock.set(3.0)
    m.on_session_end()

    by_id = {t["response_id"]: t for t in m.turns}
    assert by_id["r2"]["ttft_ms"] == 300.0
    assert by_id["r1"]["ttft_ms"] is None


def test_nova_total_ms_sums_audio_not_response_window_regression():
    """Regression for the live 'Total Nova Sonic speaking time' bug.

    Nova Sonic reuses ONE response_id for the whole call: it fires many
    BidiResponseStartEvent (one per speech burst) but only a single late
    BidiResponseStopEvent at call end. The OLD metric summed the
    response_start..stop windows, so it measured only the last window and
    badly under-counted. The corrected nova_total_ms sums the actual outbound
    audio-frame durations.

    This test feeds several audio frames of KNOWN decoded byte sizes and asserts
    nova_total_ms equals their summed duration, which DIFFERS from the old
    response-window sum (still reported as nova_response_window_ms). Under the
    old calc nova_total_ms would equal the window sum and this test would FAIL.
    """
    clock = FakeClock()
    m = CallMetrics(call_sid="CA-NOVA", account_id="ACC-1001", clock=clock)

    clock.set(0.0)
    m.on_session_start()

    # Nova opens its (reused) response id repeatedly across the call.
    clock.set(0.5)
    m.record_event(BidiResponseStartEvent(response_id="shared"))

    # Three known-size audio frames: 48000, 24000, 12000 decoded bytes.
    # PCM16 mono 24 kHz: ms = bytes / 48 -> 1000, 500, 250 ms = 1750 ms total.
    frame_bytes = [48000, 24000, 12000]
    expected_nova_ms = round(sum(b / 48.0 for b in frame_bytes), 3)
    assert expected_nova_ms == 1750.0

    clock.set(1.0)
    m.record_event(_audio_event_bytes(frame_bytes[0]))
    # Nova "restarts" the same response id mid-call (repeated response_start).
    clock.set(2.0)
    m.record_event(BidiResponseStartEvent(response_id="shared"))
    clock.set(2.0)
    m.record_event(_audio_event_bytes(frame_bytes[1]))
    clock.set(3.0)
    m.record_event(BidiResponseStartEvent(response_id="shared"))
    clock.set(3.0)
    m.record_event(_audio_event_bytes(frame_bytes[2]))

    # ONE late response_stop at call end. The response-window sum therefore
    # captures only a small slice of the call.
    clock.set(10.0)
    m.record_event(BidiResponseStopEvent(response_id="shared"))
    clock.set(12.0)
    m.on_session_end()

    # CORRECTED headline: sum of audio-frame durations.
    assert m.nova_total_ms == expected_nova_ms
    assert m.summary()["nova_total_ms"] == expected_nova_ms
    assert m.to_call_analytics()["nova_total_ms"] == expected_nova_ms

    # The old erroneous response-window sum does NOT equal the corrected
    # audio-derived value: repeated response_start events reusing one id each
    # overwrote the open-turn start, so the single late response_stop
    # measures a bogus window (here 7000ms) unrelated to the 1750ms Nova
    # actually spoke. This inequality is exactly what would fail under the old
    # calc (where nova_total_ms WAS the window sum).
    assert m.nova_response_window_ms != m.nova_total_ms
    assert m.nova_response_window_ms == 7000.0

    # non_nova_ms stays coherent with the CORRECTED nova_total_ms.
    assert m.non_nova_ms == round(12000.0 - expected_nova_ms, 3)


def test_per_utterance_turn_model_counts_each_utterance():
    """Headline per-utterance turns: a user utterance is one turn and a Sonic
    utterance is another. Consecutive same-role finals are NOT merged (contrast
    with the grouped conversation_turns, which DOES merge them).
    """
    clock = FakeClock()
    m = CallMetrics(call_sid="CA-UTT", account_id="ACC-1001", clock=clock)

    clock.set(0.0)
    m.on_session_start()

    # Alternation that also includes TWO consecutive same-role (assistant)
    # finals, to prove they are counted as two separate utterance turns.
    clock.set(1.0)
    m.record_event(_final_transcript("Hello, this is Ava.", role="assistant"))
    clock.set(1.4)
    m.record_event(_final_transcript("How can I help you today?", role="assistant"))
    clock.set(2.0)
    m.record_event(_final_transcript("I can pay next week.", role="user"))
    clock.set(2.8)
    m.record_event(_final_transcript("Great, I will note that.", role="assistant"))
    clock.set(4.0)
    m.on_session_end()

    # Per-utterance (headline) turn count = 4 individual utterances, with NO
    # same-role merging of the two leading assistant finals.
    assert m.summary()["turn_count"] == 4
    analytics = m.to_call_analytics()
    assert analytics["turn_count"] == 4

    turns = analytics["turns"]
    assert len(turns) == 4
    assert [t["speaker"] for t in turns] == ["assistant", "assistant", "user", "assistant"]
    assert [t["text"] for t in turns] == [
        "Hello, this is Ava.",
        "How can I help you today?",
        "I can pay next week.",
        "Great, I will note that.",
    ]
    # Each per-utterance turn carries speaker / text / duration_ms.
    for t in turns:
        assert t["speaker"] in ("user", "assistant")
        assert t["text"]
        assert "duration_ms" in t
    # duration_ms = span to next utterance start; first turn 1.0 -> 1.4 = 400ms.
    assert turns[0]["duration_ms"] == 400.0
    # Last turn (assistant@2800) falls back to session end (4000) -> 1200ms.
    assert turns[3]["duration_ms"] == 1200.0

    # CONTRAST: the grouped conversation_turns DO merge the two leading
    # assistant finals into a single chat segment, giving a SMALLER count (3)
    # than the per-utterance headline count (4).
    assert len(m.conversation_turns) == 3
    assert [t["speaker"] for t in m.conversation_turns] == ["assistant", "user", "assistant"]
    assert m.conversation_turns[0]["text"] == "Hello, this is Ava. How can I help you today?"


def test_unmatched_response_stop_without_start_is_safe():
    clock = FakeClock()
    m = CallMetrics(clock=clock)
    clock.set(0.0)
    m.on_session_start()
    clock.set(0.5)
    # Stop with no prior start -> must not raise, no turn recorded.
    m.record_event(BidiResponseStopEvent(response_id="ghost"))
    assert len(m.turns) == 0


def test_tool_use_blocks_emit_one_invocation_per_tool():
    """GA groups tool calls in BidiToolUseBlocksEvent; one invocation each."""
    clock = FakeClock()
    m = CallMetrics(clock=clock)
    clock.set(0.0)
    m.on_session_start()
    clock.set(0.5)
    m.record_event(
        BidiToolUseBlocksEvent(
            tool_uses=[
                {"name": "update_due_record", "input": {}, "toolUseId": "t1"},
                {"name": "capture_payment_date_phrase", "input": {}, "toolUseId": "t2"},
            ]
        )
    )
    tool_events = [e for e in m.events_log if e["event_type"] == "tool_invocation"]
    assert [e["tool"] for e in tool_events] == [
        "update_due_record",
        "capture_payment_date_phrase",
    ]


def test_conversation_turns_expose_accurate_ask1_field_names():
    """ASK 1: the response-wait window must be exposed under an accurate name
    (``response_wait_ms``) and turn-elapsed under ``turn_elapsed_ms``, with the
    legacy ``latency_ms``/``total_ms`` kept as backward-compatible aliases of
    the SAME values. ttft_source stays honest. Fails if the relabel is reverted.
    """
    clock = FakeClock()
    m = CallMetrics(call_sid="CA-LBL", account_id="ACC-1001", clock=clock)
    clock.set(0.0)
    m.on_session_start()
    clock.set(0.5)
    m.record_event(BidiResponseStartEvent(response_id="shared"))
    clock.set(0.8)
    m.record_event(_audio_event())
    clock.set(1.0)
    m.record_event(_final_transcript("Hello, this is Ava.", role="assistant"))
    clock.set(1.5)
    m.record_event(_final_transcript("What is this about?", role="user"))
    clock.set(2.0)
    m.record_event(_final_transcript("You owe fifty dollars.", role="assistant"))
    clock.set(3.0)
    m.record_event(BidiResponseStopEvent(response_id="shared"))
    clock.set(3.5)
    m.on_session_end()

    turns = m.conversation_turns
    later_assistant = turns[2]
    assert later_assistant["speaker"] == "assistant"
    # FEAT-004: this assistant turn is directly preceded by a REAL caller turn
    # ("What is this about?") so the caller->reply gap IS computed, and the
    # backward-compat aliases carry the same value.
    assert "caller_reply_gap_ms" in later_assistant
    assert "response_wait_ms" in later_assistant
    assert later_assistant["caller_reply_gap_ms"] == 500.0
    assert later_assistant["response_wait_ms"] == later_assistant["caller_reply_gap_ms"]
    assert later_assistant["response_wait_ms"] == later_assistant["latency_ms"]
    assert later_assistant["response_wait_ms"] == 500.0
    # turn_elapsed_ms is present on every turn and aliases total_ms.
    for t in turns:
        assert "turn_elapsed_ms" in t
        assert t["turn_elapsed_ms"] == t["total_ms"]
    # ttft_source stays honest for the later (unmatched) assistant turn.
    assert later_assistant["ttft_source"] == "unavailable"
    assert turns[0]["ttft_source"] == "response_window"


def test_caller_reply_gap_none_for_ai_first_assistant_turns():
    """FEAT-004 (DEFECT D1): on an AI-first call the agent opens the
    conversation with assistant speech and no caller has spoken yet, so there
    is NO caller->reply gap to measure. The leading assistant turn (and any
    assistant turn not directly preceded by a genuine caller turn) must report
    ``caller_reply_gap_ms`` is None -- never a turn-to-turn gap -- and the
    backward-compat aliases must mirror that None. Fails if the old
    transcript-gap fallback is reintroduced.
    """
    clock = FakeClock()
    m = CallMetrics(call_sid="CA-AIFIRST", account_id="ACC-1001", clock=clock)
    clock.set(0.0)
    m.on_session_start()
    # AI-first: the agent speaks first (two separate response windows) with no
    # caller utterance in between -> these group into the leading assistant
    # turn, which has no preceding caller turn.
    clock.set(0.5)
    m.record_event(BidiResponseStartEvent(response_id="r1"))
    clock.set(0.8)
    m.record_event(_audio_event())
    clock.set(1.0)
    m.record_event(_final_transcript("Hi, this is Ava from billing.", role="assistant"))
    clock.set(2.0)
    m.record_event(_final_transcript("I'm calling about your account.", role="assistant"))
    clock.set(3.0)
    m.record_event(BidiResponseStopEvent(response_id="r1"))
    clock.set(3.5)
    m.on_session_end()

    turns = m.conversation_turns
    # Consecutive assistant finals with no caller between group into one leading
    # assistant turn with no preceding caller turn.
    first_assistant = turns[0]
    assert first_assistant["speaker"] == "assistant"
    assert first_assistant["caller_reply_gap_ms"] is None
    # Backward-compat aliases carry the same (None) value.
    assert first_assistant["response_wait_ms"] is None
    assert first_assistant["latency_ms"] is None
    # turn_elapsed_ms is unaffected by the gap semantics (still present).
    assert first_assistant["turn_elapsed_ms"] == first_assistant["total_ms"]


def test_caller_reply_gap_computed_only_after_a_real_caller_turn():
    """FEAT-004: the caller->reply gap is computed as
    ``start_ms - prev_user_end_ms`` ONLY for an assistant turn that is directly
    preceded by a genuine caller (user) turn. An assistant turn that opens the
    call (no caller yet) must be None, and the one immediately after the caller
    stops must carry the real gap. The alias equals the new field.
    """
    clock = FakeClock()
    m = CallMetrics(call_sid="CA-GAP", account_id="ACC-1001", clock=clock)
    clock.set(0.0)
    m.on_session_start()
    # Leading assistant turn (AI-first) -> gap must be None.
    clock.set(1.0)
    m.record_event(_final_transcript("Hello, this is Ava.", role="assistant"))
    # Caller replies, finishing at t=2.0s (t_ms=2000).
    clock.set(2.0)
    m.record_event(_final_transcript("Okay, go ahead.", role="user"))
    # Agent answers, first final at t=2.4s (t_ms=2400) -> gap 400ms.
    clock.set(2.4)
    m.record_event(_final_transcript("You owe fifty dollars.", role="assistant"))
    clock.set(3.0)
    m.on_session_end()

    turns = m.conversation_turns
    leading, caller, reply = turns[0], turns[1], turns[2]
    assert leading["speaker"] == "assistant"
    assert caller["speaker"] == "user"
    assert reply["speaker"] == "assistant"

    # AI-first leading assistant turn: no caller preceded -> None.
    assert leading["caller_reply_gap_ms"] is None
    assert leading["response_wait_ms"] is None

    # Assistant turn directly after a real caller turn: gap = 2400 - 2000.
    assert reply["caller_reply_gap_ms"] == 400.0
    assert reply["response_wait_ms"] == reply["caller_reply_gap_ms"]
    assert reply["latency_ms"] == reply["caller_reply_gap_ms"]

    # turn_elapsed_ms is unchanged by the gap fix (aliases total_ms everywhere).
    for t in turns:
        assert t["turn_elapsed_ms"] == t["total_ms"]


def test_sync_tool_calls_attached_to_nearest_assistant_turn():
    """ASK 5: in-call (sync) tool invocations with a call-timeline t_ms are
    attached to the nearest preceding assistant turn as tool_calls entries
    labelled kind='sync'. Fails if the association logic is reverted.
    """
    clock = FakeClock()
    m = CallMetrics(call_sid="CA-TOOL", account_id="ACC-1001", clock=clock)
    clock.set(0.0)
    m.on_session_start()
    clock.set(1.0)
    m.record_event(_final_transcript("Hello, I can update your record.", role="assistant"))
    # Sync tool fires during the assistant turn (t=1.2s -> t_ms=1200).
    clock.set(1.2)
    m.record_event(
        BidiToolUseBlocksEvent(
            tool_uses=[{"name": "update_due_record", "input": {}, "toolUseId": "t1"}]
        )
    )
    clock.set(2.0)
    m.record_event(_final_transcript("I will pay next week.", role="user"))
    clock.set(2.5)
    m.record_event(_final_transcript("Noted, thank you.", role="assistant"))
    # Second sync tool fires during the later assistant turn (t=2.7 -> 2700).
    clock.set(2.7)
    m.record_event(
        BidiToolUseBlocksEvent(
            tool_uses=[
                {"name": "capture_payment_date_phrase", "input": {}, "toolUseId": "t2"}
            ]
        )
    )
    clock.set(3.5)
    m.on_session_end()

    turns = m.conversation_turns
    # Every turn carries a tool_calls list.
    assert all("tool_calls" in t for t in turns)
    # First assistant turn (start 1000) owns the 1200ms update_due_record call.
    first_asst = turns[0]
    assert first_asst["speaker"] == "assistant"
    assert [c["tool"] for c in first_asst["tool_calls"]] == ["update_due_record"]
    assert first_asst["tool_calls"][0]["kind"] == "sync"
    assert first_asst["tool_calls"][0]["t_ms"] == 1200.0
    # Later assistant turn (start 2500) owns the 2700ms capture call.
    later_asst = turns[2]
    assert [c["tool"] for c in later_asst["tool_calls"]] == [
        "capture_payment_date_phrase"
    ]
    assert later_asst["tool_calls"][0]["kind"] == "sync"
    # The user turn has no tool calls attached.
    assert turns[1]["tool_calls"] == []


def test_interrupted_turn_marked_and_barge_in_inline_by_t_ms():
    """ASK 7: an assistant turn whose time window contains a barge-in is marked
    interrupted (the full intended transcript is preserved, NOT truncated), and
    the barge-in is surfaced inline as a timeline marker ordered by t_ms. Fails
    if interrupted-marking or the inline marker is reverted.
    """
    clock = FakeClock()
    m = CallMetrics(call_sid="CA-BARGE", account_id="ACC-1001", clock=clock)
    clock.set(0.0)
    m.on_session_start()
    # Assistant turn 0 starts at 1000ms.
    clock.set(1.0)
    m.record_event(_final_transcript("Hello, I am calling about your balance.", role="assistant"))
    # Barge-in at 1500ms falls inside assistant turn 0's window -> interrupted.
    clock.set(1.5)
    m.record_event(BidiBargeInEvent())
    # User turn 1 at 2000ms.
    clock.set(2.0)
    m.record_event(_final_transcript("Stop, who is this?", role="user"))
    # Assistant turn 2 at 2500ms (NOT interrupted).
    clock.set(2.5)
    m.record_event(_final_transcript("This is Ava from billing.", role="assistant"))
    clock.set(3.0)
    m.on_session_end()

    turns = m.conversation_turns
    # Turn 0 interrupted; the full intended transcript is preserved (not cut).
    assert turns[0]["interrupted"] is True
    assert turns[0]["barge_in_ms"] == [1500.0]
    assert turns[0]["text"] == "Hello, I am calling about your balance."
    # The non-interrupted turns are explicitly marked False.
    assert turns[1]["interrupted"] is False
    assert turns[2]["interrupted"] is False

    # The serialized record surfaces the barge-in inline as a timeline marker.
    analytics = m.to_call_analytics()
    markers = analytics["barge_in_events"]
    assert markers == [{"type": "barge_in", "t_ms": 1500.0}]


def test_transport_delay_estimate_named_honestly_and_async_tool_calls_empty():
    """ASK 6: the per-frame transport ESTIMATE is exposed as
    ``transport_delay_estimate_ms`` (alias of the legacy twilio_latency_ms) and
    ``non_nova_ms`` stays a documented residual. ASK 5: async_tool_calls is
    empty at call end (async tools run post-call, off-path).
    """
    clock = FakeClock()
    m = CallMetrics(call_sid="CA-TR", account_id="ACC-1001", clock=clock)
    clock.set(0.0)
    m.on_session_start()
    clock.set(0.5)
    m.record_inbound_media(480)
    clock.set(1.0)
    m.record_inbound_media(960)  # 500 local vs 480 twilio -> 20ms
    clock.set(1.04)
    m.record_inbound_media(980)  # 540 vs 500 -> 40ms
    clock.set(2.0)
    m.on_session_end()

    analytics = m.to_call_analytics()
    # Estimate present under the accurate name AND equal to the legacy alias.
    assert analytics["transport_delay_estimate_ms"] == analytics["twilio_latency_ms"]
    assert analytics["transport_delay_estimate_ms"] == 30.0
    assert m.transport_delay_estimate_ms == 30.0
    # non_nova_ms remains the documented residual (total - nova).
    assert analytics["non_nova_ms"] == round(2000.0 - m.nova_total_ms, 3)
    # Async tool calls empty at call end; twilio_insights is a pending marker.
    assert analytics["async_tool_calls"] == []
    assert analytics["twilio_insights"]["available"] is False
    assert analytics["twilio_insights"]["source"] == "pending"


def test_usage_adapts_ga_token_details():
    """GA BidiUsageEvent: cache_read comes from input_token_details."""
    clock = FakeClock()
    m = CallMetrics(clock=clock)
    clock.set(0.0)
    m.on_session_start()
    m.record_event(
        BidiUsageEvent(
            input_tokens=120,
            output_tokens=80,
            total_tokens=200,
            input_token_details={"cache_read": 15, "text": 90, "audio": 15},
            output_token_details={"text": 80},
        )
    )
    assert m.usage["input_tokens"] == 120
    assert m.usage["output_tokens"] == 80
    assert m.usage["total_tokens"] == 200
    assert m.usage["cache_read_input_tokens"] == 15
    assert m.usage["modality_details"]["input"]["audio"] == 15
    assert m.usage["modality_details"]["output"]["text"] == 80
