"""Per-call metrics + analytics, driven by Strands bi-di events and Twilio timestamps.

:class:`CallMetrics` implements the :class:`app.sonic_bridge.MetricsSink`
protocol. The Nova Sonic bridge forwards every bi-di output event to
:meth:`CallMetrics.record_event`; the WebSocket handler reports inbound Twilio
media timestamps via :meth:`CallMetrics.record_inbound_media`. From those two
ordered streams this collector computes the metrics the user asked for:

- total end-to-end time (``total_e2e_ms``),
- the split between Twilio transport latency and Nova Sonic active time
  (``twilio_latency_ms`` vs ``nova_total_ms``),
- time-to-first-token/first-audio per turn and conversation-level
  (``ttft_ms``),
- turn count, per-turn duration and per-turn latency,
- transcripts, token usage, and barge-in count,
- an in-depth, timestamped ``events_log`` for CloudWatch-friendly logging.

Approximations (documented here and in FEAT-003 findings):

- A conversational "turn" is one side of the user<->assistant alternation,
  derived from GA ``BidiTranscriptBlockEvent`` roles (a block event IS a
  completed utterance; there is no ``is_final`` flag in GA). See
  :meth:`CallMetrics._build_conversation_turns`. This is the headline
  ``turn_count``. The response_id-based grouping below is kept as a secondary
  Nova-response timing signal and surfaced as ``nova_response_count``.
- A Nova response window is a matched ``BidiResponseStartEvent`` /
  ``BidiResponseStopEvent`` pair keyed by ``response_id``. Per-response
  duration = stop_time - start_time.
- TTFT for a turn = time from that turn's ``response_start`` to the first
  ``BidiAudioDeltaEvent`` of the same response. The conversation-level TTFT is
  the first turn's TTFT.
- Per-turn latency = time from when the user finished speaking to the turn's
  ``response_start``. "User finished speaking" is approximated as the timestamp
  of the last inbound Twilio media frame seen before ``response_start`` (or, if
  present, the last final user transcript before it).
- ``nova_total_ms`` = the wall-clock time Nova Sonic was actively producing
  speech, computed as the sum of outbound ``BidiAudioDeltaEvent`` PCM
  durations (see the ``nova_total_ms`` property for why the old
  response_start..complete window sum under-counted). The legacy window sum is
  retained as ``nova_response_window_ms`` for diagnostics.
- ``twilio_latency_ms`` / ``transport_delay_estimate_ms`` (ASK 2/6) = a per-frame
  transport-delay ESTIMATE derived from Twilio media ``timestamp``
  (stream-relative ms) vs local receive wall-clock. Twilio's ``timestamp`` is
  milliseconds since the stream started; comparing its growth to locally
  measured elapsed time yields the extra transport delay. We report the mean
  observed transport delay. This is an ESTIMATE, not an authoritative network
  measurement -- it is surfaced under the accurate name
  ``transport_delay_estimate_ms`` (with ``twilio_latency_ms`` kept as a
  deprecated backward-compatible alias). The REAL, authentic Twilio Call
  resource facts (duration, start/end, price, from/to, status) are fetched
  OFF-PATH post-call by :func:`app.twilio_client.fetch_call_insights` and
  attached to the stored record as ``twilio_insights``; advanced QoS
  (RTT/jitter/packet-loss/MOS) is a paid Voice Insights add-on that is NOT
  enabled on the trial account and is marked unavailable rather than fabricated.
- ``non_nova_ms`` = ``total_e2e_ms - nova_total_ms`` -- the share of the call's
  end-to-end wall-clock NOT spent with Sonic actively producing speech. It is a
  bookkeeping residual (network transport, user speech, silence, tool time),
  NOT a transport-latency measurement; the transport estimate above is a
  separate, independently-derived figure.

Terminology (ASK 1, from FEAT-001): per conversational turn the app exposes
``response_wait_ms`` (time for Sonic to begin answering; the value previously
MISLABELED ``latency_ms``, kept as a deprecated alias), ``ttft_ms`` (official
time-to-first-audio, ``gen_ai.server.time_to_first_audio``) with an honest
``ttft_source``, ``turn_elapsed_ms`` (turn-elapsed; alias of ``total_ms``) and
``transcript_span_ms`` (spoken duration).
"""

from __future__ import annotations

import base64
import json
import logging
import time
from datetime import datetime, timezone
from typing import Any, Callable

from strands.bidi.types.events import (
    BidiAudioDeltaEvent,
    BidiBargeInEvent,
    BidiConnectionRestartEvent,
    BidiConnectionStartEvent,
    BidiConnectionStopEvent,
    BidiConnectionWarningEvent,
    BidiResponseStartEvent,
    BidiResponseStopEvent,
    BidiToolUseBlocksEvent,
    BidiTranscriptBlockEvent,
    BidiTranscriptDeltaEvent,
    BidiUsageEvent,
)

# Dedicated logger that emits one JSON object per line to stdout. FEAT-005 ships
# these to CloudWatch; the format is intentionally a bare JSON document per
# record so a log agent can parse each line without extra configuration.
metrics_logger = logging.getLogger("ar_collections.metrics")


class _JsonLineFormatter(logging.Formatter):
    """Format a log record as a single-line JSON object (CloudWatch-friendly)."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created))
            + f".{int(record.msecs):03d}Z",
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        data = getattr(record, "data", None)
        if isinstance(data, dict):
            payload.update(data)
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_json_logging(level: int = logging.INFO) -> None:
    """Attach a JSON-line handler to the metrics logger (idempotent).

    Emits to stdout so container log drivers / CloudWatch agents capture it.
    """
    import sys

    for handler in metrics_logger.handlers:
        if getattr(handler, "_ar_json", False):
            metrics_logger.setLevel(level)
            return
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(_JsonLineFormatter())
    handler._ar_json = True  # type: ignore[attr-defined]
    metrics_logger.addHandler(handler)
    metrics_logger.setLevel(level)
    metrics_logger.propagate = False


def _now() -> float:
    return time.monotonic()


class CallMetrics:
    """Collects metrics for a single outbound call.

    Satisfies the :class:`app.sonic_bridge.MetricsSink` protocol
    (``on_session_start`` / ``record_event`` / ``on_session_end``).

    Args:
        call_sid: Twilio call sid the analytics are tagged to.
        account_id: Due/account id the call is about.
        clock: Monotonic clock function (injected in tests for determinism).
        logger_: Logger for structured JSON lines (defaults to the module one).
    """

    def __init__(
        self,
        *,
        call_sid: str | None = None,
        account_id: str | None = None,
        clock: Callable[[], float] = _now,
        logger_: logging.Logger | None = None,
    ) -> None:
        self.call_sid = call_sid
        self.account_id = account_id
        self._clock = clock
        self._log = logger_ or metrics_logger

        self._session_start: float | None = None
        self._session_end: float | None = None
        # Wall-clock (UTC, ISO-8601) timestamp of when the call session
        # started. ASK 3 (calls-under-dues) needs a real "WHEN did this call
        # happen" value; the monotonic clock above only measures elapsed
        # durations and cannot be rendered as an absolute/relative time, so we
        # capture an independent wall-clock stamp at session start. This is set
        # once, off the audio hot path (on_session_start), and never mutated by
        # per-frame handlers.
        self.created_at: str | None = None

        # Nova response timing keyed by response_id (secondary signal; see the
        # conversational-turn comment in :meth:`_build_conversation_turns`).
        self._open_turns: dict[str, dict[str, Any]] = {}
        self.turns: list[dict[str, Any]] = []

        # Conversational turns (user<->assistant alternation). Rebuilt on
        # demand from the transcript stream. See :meth:`_build_conversation_turns`.
        self.conversation_turns: list[dict[str, Any]] = []

        # Fallback: latest non-final user transcript text seen. If a call only
        # yields partial (non-final) user transcripts, we still want the chat
        # view to show what the user said rather than an empty bubble.
        #
        # GA keys each transcript stream by ``content_id``: all deltas of one
        # user utterance share a content_id, a new utterance opens a new one,
        # and a ``BidiTranscriptBlockEvent`` closes that content_id's stream. We
        # therefore accumulate deltas only within the CURRENT user content_id
        # and start fresh when the content_id changes (or when the matching
        # block finalizes the utterance), so the fallback reflects a SINGLE
        # utterance rather than a run-on concatenation of every utterance in the
        # call.
        self._last_user_partial_text: str | None = None
        self._last_user_partial_t_ms: float | None = None
        self._user_partial_content_id: str | None = None

        # Conversation-level aggregates.
        self.first_ttft_ms: float | None = None
        # HEADLINE Nova speaking time: sum of outbound audio-frame durations.
        # See :meth:`_on_audio` and the :attr:`nova_total_ms` property for why
        # the old response-window sum was wrong and this replaces it.
        self.nova_speaking_ms: float = 0.0
        # SECONDARY (legacy) measure: sum of matched response_start..complete
        # windows keyed by response_id. Accumulated in :meth:`_finalize_turn`.
        # Kept for diagnostics only; NOT the headline nova_total_ms.
        self.nova_response_window_ms: float = 0.0
        self.barge_in_count: int = 0
        self.usage: dict[str, Any] = {}
        self.transcripts: list[dict[str, Any]] = []
        self.events_log: list[dict[str, Any]] = []

        # Twilio transport-delay tracking. We anchor on the first inbound frame
        # (local receive time + Twilio stream-relative timestamp) and measure
        # how much the local inter-arrival time drifts ahead of the Twilio
        # stream clock for later frames; that drift is transport delay.
        self._inbound_anchor: tuple[float, float] | None = None
        self._last_inbound_wall: float | None = None
        self._last_user_final_wall: float | None = None
        self._transport_delays_ms: list[float] = []

    # -- structured logging ---------------------------------------------------

    def _emit(self, event_type: str, **data: Any) -> dict[str, Any]:
        entry = {
            "event_type": event_type,
            "call_sid": self.call_sid,
            "account_id": self.account_id,
            "t_ms": self._rel_ms(self._clock()),
            **data,
        }
        self.events_log.append(entry)
        self._log.info(event_type, extra={"data": entry})
        return entry

    def _rel_ms(self, when: float) -> float | None:
        if self._session_start is None:
            return None
        return round((when - self._session_start) * 1000.0, 3)

    # -- lifecycle ------------------------------------------------------------

    def on_session_start(self) -> None:
        self._session_start = self._clock()
        # ASK 3: capture the absolute wall-clock start time (UTC ISO-8601) so
        # the calls-under-dues view can show WHEN the call happened. Independent
        # of the monotonic clock used for durations.
        self.created_at = datetime.now(timezone.utc).isoformat()
        self._emit("call_placed")
        self._emit("stream_start")

    def on_session_end(self) -> None:
        self._session_end = self._clock()
        # Close any turns still open (e.g. call dropped mid-response).
        for response_id in list(self._open_turns):
            self._finalize_turn(response_id, stop_reason="incomplete")
        summary = self.summary()
        self._emit("call_end_summary", summary=summary)

    # -- Twilio inbound media -------------------------------------------------

    def record_inbound_media(self, twilio_timestamp_ms: float | int | str | None) -> None:
        """Record arrival of one inbound Twilio media frame.

        ``twilio_timestamp_ms`` is Twilio's stream-relative timestamp (ms since
        the stream started). We compare its growth against locally measured
        elapsed time to estimate transport delay, and remember the local arrival
        time so per-turn latency can be approximated.
        """
        now = self._clock()
        self._last_inbound_wall = now

        if twilio_timestamp_ms is None:
            return
        try:
            ts_ms = float(twilio_timestamp_ms)
        except (TypeError, ValueError):
            return

        if self._inbound_anchor is None:
            # First frame establishes the baseline alignment; no delay yet.
            self._inbound_anchor = (now, ts_ms)
            return

        local_base, ts_base = self._inbound_anchor
        local_elapsed_ms = (now - local_base) * 1000.0
        twilio_elapsed_ms = ts_ms - ts_base
        # Transport delay: how far our local receive time drifts ahead of the
        # Twilio stream clock (only positive drift counts as added delay).
        delay = local_elapsed_ms - twilio_elapsed_ms
        if delay > 0:
            self._transport_delays_ms.append(delay)

    # -- Strands bi-di events -------------------------------------------------

    def record_event(self, event: Any) -> None:
        """Dispatch one Nova Sonic bi-di output event into metric aggregation."""
        now = self._clock()

        if isinstance(event, BidiResponseStartEvent):
            self._on_response_start(event, now)
        elif isinstance(event, BidiAudioDeltaEvent):
            self._on_audio(event, now)
        elif isinstance(event, BidiResponseStopEvent):
            self._on_response_complete(event, now)
        elif isinstance(event, BidiTranscriptBlockEvent):
            # A GA block event IS a completed (final) utterance.
            self._on_transcript_final(event, now)
        elif isinstance(event, BidiTranscriptDeltaEvent):
            # GA partial transcript delta -> fallback (previously non-final).
            self._on_transcript_delta(event, now)
        elif isinstance(event, BidiUsageEvent):
            self._on_usage(event)
        elif isinstance(event, BidiBargeInEvent):
            # GA BidiBargeInEvent carries no .reason attribute.
            self.barge_in_count += 1
            self._emit("barge_in")
        elif isinstance(event, BidiToolUseBlocksEvent):
            # GA groups tool calls: emit one tool_invocation per ToolUse.
            for tool_use in getattr(event, "tool_uses", None) or []:
                self._emit("tool_invocation", tool=_tool_name(tool_use))
        elif isinstance(event, BidiConnectionStartEvent):
            self._emit(
                "connection_start",
                connection_id=getattr(event, "connection_id", None),
                model=getattr(event, "model", None),
            )
        elif isinstance(event, BidiConnectionRestartEvent):
            self._emit(
                "connection_restart",
                timeout_error=getattr(event, "timeout_error", None),
            )
        elif isinstance(event, BidiConnectionStopEvent):
            self._emit(
                "connection_stop",
                connection_id=getattr(event, "connection_id", None),
                reason=getattr(event, "reason", None),
            )
        elif isinstance(event, BidiConnectionWarningEvent):
            # GA replaced the former error branch with a proactive restart
            # warning; record it for diagnostics (informational only).
            self._emit(
                "connection_warning",
                time_left_s=getattr(event, "time_left_s", None),
            )

    # -- per-event handlers ---------------------------------------------------

    def _on_response_start(self, event: BidiResponseStartEvent, now: float) -> None:
        response_id = getattr(event, "response_id", None) or f"turn-{len(self.turns) + 1}"
        # Approximate the moment the user finished speaking for turn latency.
        user_stop = self._last_user_final_wall or self._last_inbound_wall
        turn_latency_ms = (now - user_stop) * 1000.0 if user_stop is not None else None
        self._open_turns[response_id] = {
            "response_id": response_id,
            "start": now,
            "first_audio": None,
            "turn_latency_ms": round(turn_latency_ms, 3) if turn_latency_ms is not None else None,
        }
        self._emit(
            "turn_start",
            response_id=response_id,
            turn_latency_ms=self._open_turns[response_id]["turn_latency_ms"],
        )

    def _on_audio(self, event: BidiAudioDeltaEvent, now: float) -> None:
        # HEADLINE Nova speaking time: accumulate the real duration of each
        # outbound audio frame. Nova Sonic emits PCM16 mono at 24 kHz, so the
        # frame duration (ms) = decoded_bytes / (2 * sample_rate * channels) *
        # 1000 (2 bytes per PCM16 sample). This is O(1) per frame and must never
        # raise -- record_event is wrapped in try/except by the bridge, but we
        # stay defensive here too so one malformed frame cannot drop metrics.
        try:
            audio_b64 = getattr(event, "audio", None)
            if audio_b64:
                decoded_bytes = len(base64.b64decode(audio_b64))
                sample_rate = getattr(event, "sample_rate", None) or 24000
                channels = getattr(event, "channels", None) or 1
                denom = 2 * sample_rate * channels
                if denom > 0:
                    self.nova_speaking_ms += decoded_bytes / denom * 1000.0
        except Exception:  # noqa: BLE001 - never break the audio path
            pass

        # Attribute first audio to the most-recently-started still-open turn.
        #
        # The GA BidiAudioDeltaEvent carries no response_id (only
        # ``audio``/``format``/``sample_rate``/``channels``/``content_id``), so
        # audio cannot be
        # matched to its turn by id. Under normal sequencing only one turn is
        # open at a time. Under a barge-in that opens turn N+1 while turn N is
        # still finishing, the freshest audio belongs to the newest turn, so we
        # scan open turns newest-first (``reversed`` over the insertion-ordered
        # dict) and assign TTFT to the newest turn that has not yet seen audio.
        for turn in reversed(list(self._open_turns.values())):
            if turn["first_audio"] is None:
                turn["first_audio"] = now
                ttft_ms = round((now - turn["start"]) * 1000.0, 3)
                turn["ttft_ms"] = ttft_ms
                if self.first_ttft_ms is None:
                    self.first_ttft_ms = ttft_ms
                self._emit("ttft_measured", response_id=turn["response_id"], ttft_ms=ttft_ms)
                break

    def _on_response_complete(self, event: BidiResponseStopEvent, now: float) -> None:
        response_id = getattr(event, "response_id", None)
        # GA BidiResponseStopEvent only carries response_id; stop_reason is gone.
        stop_reason = getattr(event, "stop_reason", None)
        if response_id is None or response_id not in self._open_turns:
            # Unmatched complete; pick the oldest open turn if any.
            if self._open_turns:
                response_id = next(iter(self._open_turns))
            else:
                return
        self._finalize_turn(response_id, stop_reason=stop_reason, now=now)

    def _finalize_turn(
        self, response_id: str, *, stop_reason: Any, now: float | None = None
    ) -> None:
        turn = self._open_turns.pop(response_id, None)
        if turn is None:
            return
        end = now if now is not None else self._clock()
        duration_ms = round((end - turn["start"]) * 1000.0, 3)
        # SECONDARY measure only. Nova Sonic reuses ONE response_id for the whole
        # call and fires many response_start events but a single response_complete
        # at call end, so this window sum under-counts Nova's real speaking time;
        # the headline nova_total_ms is derived from audio durations instead.
        self.nova_response_window_ms += duration_ms
        record = {
            "response_id": response_id,
            "index": len(self.turns),
            "start_ms": self._rel_ms(turn["start"]),
            "duration_ms": duration_ms,
            "ttft_ms": turn.get("ttft_ms"),
            "turn_latency_ms": turn.get("turn_latency_ms"),
            "stop_reason": stop_reason,
        }
        self.turns.append(record)
        self._emit("turn_complete", **record)

    def _on_transcript_final(self, event: BidiTranscriptBlockEvent, now: float) -> None:
        """Record a GA completed-utterance transcript (``BidiTranscriptBlockEvent``).

        GA has no ``is_final`` flag: a block event IS the completed utterance, so
        it maps to what the per-utterance/conversation turn model treats as a
        final transcript (``is_final=True``). The transcript text lives on
        ``.transcript`` (not ``.text``).
        """
        role = getattr(event, "role", None)
        text = getattr(event, "transcript", None)
        if role == "user":
            self._last_user_final_wall = now
            # The block closes this user utterance's transcript stream. A real
            # final takes precedence over the partial fallback, so clear the
            # per-utterance delta accumulator; the next utterance's deltas start
            # fresh under their own content_id.
            self._last_user_partial_text = None
            self._last_user_partial_t_ms = None
            self._user_partial_content_id = None
        self.transcripts.append(
            {
                "role": role,
                "text": text,
                "is_final": True,
                "t_ms": self._rel_ms(now),
            }
        )
        self._emit("transcript", role=role, text=text)

    def _on_transcript_delta(self, event: BidiTranscriptDeltaEvent, now: float) -> None:
        """Record a GA partial transcript delta (``BidiTranscriptDeltaEvent``).

        Deltas are the GA equivalent of the old non-final transcripts. We keep
        the latest partial user text as a fallback so the chat view is not empty
        when a call only yields partial user transcripts, and we record the
        partial in the transcript log with ``is_final=False`` so the turn model
        continues to key off final (block) transcripts.
        """
        role = getattr(event, "role", None)
        delta = getattr(event, "delta", None)
        content_id = getattr(event, "content_id", None)
        if role == "user" and delta:
            # Accumulate consecutive deltas of the SAME user utterance (keyed by
            # ``content_id``) so the fallback reflects the full partial
            # utterance, not just the last chunk. When the content_id changes a
            # new utterance has started, so reset the accumulator instead of
            # concatenating across utterances (which would produce a single
            # run-on turn on a partial-only call).
            if content_id != self._user_partial_content_id:
                self._user_partial_content_id = content_id
                self._last_user_partial_text = delta
            else:
                self._last_user_partial_text = (self._last_user_partial_text or "") + delta
            self._last_user_partial_t_ms = self._rel_ms(now)
            text: str | None = self._last_user_partial_text
        else:
            text = delta
        self.transcripts.append(
            {
                "role": role,
                "text": text,
                "is_final": False,
                "t_ms": self._rel_ms(now),
            }
        )

    def _on_usage(self, event: BidiUsageEvent) -> None:
        # GA BidiUsageEvent keeps input/output/total tokens top-level but moved
        # the cache + modality breakdowns under input_token_details /
        # output_token_details (TokenDetails with text/audio/cache_read/
        # reasoning). Rebuild the dashboard usage dict from the new fields:
        # cache_read_input_tokens comes from the input detail map, and a
        # modality_details-equivalent is assembled from both detail maps so the
        # usage display keeps working.
        input_details = dict(getattr(event, "input_token_details", None) or {})
        output_details = dict(getattr(event, "output_token_details", None) or {})
        modality_details = (
            {"input": input_details, "output": output_details}
            if (input_details or output_details)
            else None
        )
        self.usage = {
            "input_tokens": getattr(event, "input_tokens", None),
            "output_tokens": getattr(event, "output_tokens", None),
            "total_tokens": getattr(event, "total_tokens", None),
            "cache_read_input_tokens": input_details.get("cache_read"),
            "cache_write_input_tokens": None,
            "modality_details": modality_details,
        }
        self._emit("usage", **self.usage)

    # -- conversational turns -------------------------------------------------

    # ------------------------------------------------------------------------
    # What is a "turn"?
    #
    # The user's observation: a turn is one back-and-forth in the conversation
    # (the user says something, Nova Sonic answers), not an internal grouping of
    # Nova's output. The ORIGINAL implementation counted a turn as a matched
    # response-start / response-stop pair keyed by
    # ``response_id``. In live calls Nova Sonic REUSED a single ``response_id``
    # for an entire multi-exchange conversation, so that count collapsed to 1
    # even though the user and the agent alternated several times. That is the
    # bug this method fixes.
    #
    # Reliable signal: the GA BidiTranscriptBlockEvent is a completed utterance
    # carrying ``role`` in {"user", "assistant"} and a timestamp (there is no
    # is_final flag in GA; a block event IS a final transcript, recorded with
    # is_final=True). We derive conversational turns from that alternation:
    # group consecutive FINAL transcripts of the same role into one segment;
    # each segment is one
    # conversational turn (speaker = that role). This agent is AI-initiated and
    # speaks first (the kickoff), so a leading assistant-only segment is counted
    # as the opening turn. The old response_id-based count is preserved intact
    # as ``self.turns`` and surfaced as ``nova_response_count`` so no signal is
    # lost; it is only demoted from being the headline "turn_count".
    #
    # Model-invocation-logging decision (do NOT enable it):
    # Amazon Bedrock model invocation logging (the account-level
    # PutModelInvocationLoggingConfiguration API) ONLY logs Converse,
    # ConverseStream, InvokeModel and InvokeModelWithResponseStream. It does NOT
    # capture bedrock:InvokeModelWithBidirectionalStream, which is the operation
    # Nova Sonic bi-di streaming (this app) uses, so it would NOT provide
    # turn/latency ground truth for Nova Sonic. There is also no CloudFormation
    # resource type for it (account-level API only), so it does not fit this
    # project's IaC-first model. Verified against the AWS docs
    # (bedrock/latest/userguide/model-invocation-logging.html). We therefore
    # rely on the transcript alternation above plus the response_start/first-
    # audio timing in ``self.turns`` for turn and latency semantics.
    # ------------------------------------------------------------------------

    def _build_conversation_turns(self) -> list[dict[str, Any]]:
        """Build the user<->assistant conversational-turn model.

        Groups consecutive final transcripts by role into segments; each segment
        is one conversational turn.

        Turn duration semantics (``total_ms``): the value the UI shows as a
        turn's "total time" is the ELAPSED time that turn occupied the
        conversation, i.e. the span from this turn's ``start_ms`` to the NEXT
        turn's ``start_ms``. A turn made of a single final transcript would have
        an intra-segment span of 0 (``start_ms == end_ms``), which reads as a
        broken measurement; measuring to the next turn's start gives a sensible
        non-zero elapsed time whenever ordering allows it. For the LAST turn
        (no following turn) we fall back to its own transcript span, and if that
        is 0 we fall back to the session end. The raw intra-segment transcript
        span is still exposed as ``transcript_span_ms`` for callers that want
        the pure utterance duration.

        Assistant TTFT/latency semantics: Nova Sonic may reuse a single
        ``response_id`` for the whole call, so the response-window timing in
        :attr:`turns` only yields one TTFT/latency window even when several
        assistant turns occur. To give EVERY assistant turn a meaningful
        latency, we always derive a per-utterance ``latency_ms`` from the
        preceding user turn's last final transcript to this assistant turn's
        first final transcript (how long the caller waited for Sonic to answer).
        TTFT is taken from the matched response window when one is still
        available for that turn (``ttft_source == "response_window"``);
        otherwise, because Nova emits no per-utterance audio marker we cannot
        derive a true first-token time for later turns, so ``ttft_ms`` is left
        ``None`` with ``ttft_source == "unavailable"`` so the UI can label it
        honestly rather than silently showing nothing.

        Returns:
            A list of conversational-turn dicts (also stored on
            :attr:`conversation_turns`).
        """
        finals = [
            t
            for t in self.transcripts
            if t.get("is_final") and t.get("role") in ("user", "assistant") and t.get("text")
        ]

        # Fallback: if no final user transcript was captured but we retained a
        # partial, inject it so the chat view reflects user speech.
        if (
            self._last_user_partial_text
            and not any(t.get("role") == "user" for t in finals)
        ):
            finals.append(
                {
                    "role": "user",
                    "text": self._last_user_partial_text,
                    "is_final": False,
                    "t_ms": self._last_user_partial_t_ms,
                }
            )
            finals.sort(key=lambda t: (t.get("t_ms") if t.get("t_ms") is not None else 0.0))

        # Assistant Nova response timings sorted by start, each used at most once
        # so repeated assistant segments map to successive responses.
        assistant_timings = sorted(
            (t for t in self.turns if t.get("start_ms") is not None),
            key=lambda t: t["start_ms"],
        )
        used_timing: set[int] = set()

        turns: list[dict[str, Any]] = []
        idx = 0
        for item in finals:
            role = item["role"]
            t_ms = item.get("t_ms")
            if turns and turns[-1]["speaker"] == role:
                # Extend the current same-role segment.
                seg = turns[-1]
                if item.get("text"):
                    seg["_texts"].append(item["text"])
                if t_ms is not None:
                    seg["end_ms"] = t_ms
                    if seg["start_ms"] is None:
                        seg["start_ms"] = t_ms
                continue
            seg = {
                "index": idx,
                "speaker": role,
                "_texts": [item["text"]] if item.get("text") else [],
                "start_ms": t_ms,
                "end_ms": t_ms,
            }
            turns.append(seg)
            idx += 1

        session_end_ms = self.total_e2e_ms
        prev_user_end_ms: float | None = None
        for i, seg in enumerate(turns):
            seg["text"] = " ".join(seg.pop("_texts")).strip()
            start_ms = seg.get("start_ms")
            end_ms = seg.get("end_ms")

            # Raw intra-segment span (0 for a single-final turn) kept for callers
            # that want the pure utterance duration.
            if start_ms is not None and end_ms is not None:
                seg["transcript_span_ms"] = round(end_ms - start_ms, 3)
            else:
                seg["transcript_span_ms"] = None

            # Turn "total time" = elapsed time this turn occupied the
            # conversation = span to the NEXT turn's start. See the docstring for
            # why end_ms - start_ms (which is 0 for single-final turns) is a poor
            # measure here.
            total_ms: float | None = None
            next_start = None
            if i + 1 < len(turns):
                next_start = turns[i + 1].get("start_ms")
            if start_ms is not None and next_start is not None:
                total_ms = round(next_start - start_ms, 3)
            elif start_ms is not None:
                # Last turn (or next start missing): own transcript span, then
                # fall back to the session end so the value is not a bare 0.
                span = seg["transcript_span_ms"]
                if span:
                    total_ms = span
                elif session_end_ms is not None and session_end_ms >= start_ms:
                    total_ms = round(session_end_ms - start_ms, 3)
            seg["total_ms"] = total_ms

            if seg["speaker"] == "assistant":
                # Per-utterance "response wait": from when the caller last
                # finished speaking (preceding user turn's last final transcript)
                # to this assistant turn's first final transcript, i.e. how long
                # the caller waited for Sonic to BEGIN answering. This is a
                # response-WAIT window, NOT model/network latency, and is
                # derivable for EVERY assistant turn unlike the shared response
                # window. ASK 1: it is exposed under the accurate name
                # ``response_wait_ms`` (``latency_ms`` is kept as a deprecated
                # backward-compatible alias with the SAME value).
                response_wait_ms: float | None = None
                if prev_user_end_ms is not None and start_ms is not None:
                    response_wait_ms = round(max(start_ms - prev_user_end_ms, 0.0), 3)

                timing = self._closest_timing(start_ms, assistant_timings, used_timing)
                if timing is not None:
                    seg["ttft_ms"] = timing.get("ttft_ms")
                    seg["ttft_source"] = "response_window"
                    # Prefer the response-window wait when we have it (it is the
                    # user-stop -> response_start measure); else keep the
                    # per-utterance transcript-derived wait.
                    if timing.get("turn_latency_ms") is not None:
                        response_wait_ms = timing.get("turn_latency_ms")
                else:
                    # No response window left for this turn (Nova reused one
                    # response_id). We cannot derive a true per-utterance first-
                    # token time, so TTFT is honestly unavailable; the response
                    # wait still comes from the transcript gap above.
                    seg["ttft_ms"] = None
                    seg["ttft_source"] = "unavailable"
                # Accurate name (ASK 1) + deprecated backward-compatible alias.
                seg["response_wait_ms"] = response_wait_ms
                seg["latency_ms"] = response_wait_ms
            elif seg["speaker"] == "user" and end_ms is not None:
                prev_user_end_ms = end_ms

        # ASK 1: expose the turn-elapsed time under an accurate name alongside
        # the legacy ``total_ms`` key the frontend currently reads.
        for seg in turns:
            seg["turn_elapsed_ms"] = seg.get("total_ms")

        # ASK 7 + ASK 5: annotate interrupted turns (barge-in inside the turn's
        # time window) and attach the in-call/sync tool invocations to the
        # nearest assistant turn. Both are derived purely from the already-
        # recorded events_log (off the real-time audio path -- this runs only
        # when the conversation model is (re)built for a summary/serialization).
        self._annotate_barge_ins(turns)
        self._attach_sync_tool_calls(turns)

        self.conversation_turns = turns
        return turns

    # -- ASK 7: barge-in / interrupted-turn annotation ------------------------

    def _barge_in_markers(self) -> list[dict[str, Any]]:
        """Return the barge-in events as inline timeline markers (by t_ms).

        GA's ``BidiBargeInEvent`` carries no payload and GA exposes NO
        delivered-vs-intended transcript (see FEAT-001 finding c), so the ONLY
        honest signal is the ``barge_in`` entries recorded into
        :attr:`events_log` with a real call-relative ``t_ms``. We surface each
        as a lightweight marker the UI can interleave into the timeline; we do
        NOT fabricate a truncated "delivered" transcript.
        """
        return [
            {"type": "barge_in", "t_ms": e.get("t_ms")}
            for e in self.events_log
            if e.get("event_type") == "barge_in"
        ]

    def _annotate_barge_ins(self, turns: list[dict[str, Any]]) -> None:
        """Mark assistant turns whose time window contains a barge-in.

        An assistant turn is ``interrupted`` when a ``barge_in`` t_ms falls in
        its ``[start_ms, next-turn-start)`` window (last turn extends to session
        end). The full INTENDED transcript is preserved; the turn is only
        annotated ``interrupted: True`` with the offending barge-in t_ms(s), and
        each barge-in is also surfaced inline via ``barge_in_marker``. This is
        the honest signal available on GA; we do not rewrite the transcript.
        """
        barge_ins = [
            e.get("t_ms")
            for e in self.events_log
            if e.get("event_type") == "barge_in" and e.get("t_ms") is not None
        ]
        session_end_ms = self.total_e2e_ms
        for i, seg in enumerate(turns):
            seg.setdefault("interrupted", False)
            seg.setdefault("barge_in_ms", [])
            if seg["speaker"] != "assistant":
                continue
            start_ms = seg.get("start_ms")
            if start_ms is None:
                continue
            next_start = turns[i + 1].get("start_ms") if i + 1 < len(turns) else None
            window_end = next_start if next_start is not None else session_end_ms
            hits = [
                t
                for t in barge_ins
                if t >= start_ms and (window_end is None or t < window_end)
            ]
            if hits:
                seg["interrupted"] = True
                seg["barge_in_ms"] = hits

    # -- ASK 5: sync in-call tool-call association ----------------------------

    def _sync_tool_calls(self) -> list[dict[str, Any]]:
        """Return the in-call (sync) tool invocations from events_log.

        SYNC tools (``update_due_record``, ``capture_payment_date_phrase``) are
        emitted into :attr:`events_log` as ``tool_invocation`` entries carrying
        a call-relative ``t_ms`` (FEAT-001 finding d). Each is classified
        ``kind='sync'`` because it has a call-timeline t_ms.
        """
        return [
            {"tool": e.get("tool"), "kind": "sync", "t_ms": e.get("t_ms")}
            for e in self.events_log
            if e.get("event_type") == "tool_invocation"
        ]

    def _attach_sync_tool_calls(self, turns: list[dict[str, Any]]) -> None:
        """Attach each sync tool call to the nearest preceding assistant turn.

        An in-call tool fires during an assistant's turn (Sonic invokes the
        tool mid-response), so each ``tool_invocation`` t_ms is associated to
        the assistant turn whose start_ms is the greatest start <= the tool
        t_ms; if no assistant turn has started yet it attaches to the first
        assistant turn. Every turn gets a ``tool_calls`` list (possibly empty).
        """
        for seg in turns:
            seg.setdefault("tool_calls", [])
        assistant_turns = [t for t in turns if t["speaker"] == "assistant"]
        for call in self._sync_tool_calls():
            t_ms = call.get("t_ms")
            target: dict[str, Any] | None = None
            for seg in assistant_turns:
                start_ms = seg.get("start_ms")
                if start_ms is None:
                    continue
                if t_ms is None or start_ms <= t_ms:
                    target = seg
                elif target is None:
                    # First assistant turn starts after the tool t_ms: attach
                    # to it rather than dropping the call.
                    target = seg
                    break
            if target is None and assistant_turns:
                target = assistant_turns[0]
            if target is not None:
                target["tool_calls"].append(call)

    @staticmethod
    def _closest_timing(
        start_ms: float | None,
        timings: list[dict[str, Any]],
        used: set[int],
    ) -> dict[str, Any] | None:
        """Return the unused Nova response timing closest to ``start_ms``.

        Matching by closest start time (rather than response_id) because Nova
        may reuse one response_id across the whole exchange.
        """
        best_i: int | None = None
        best_delta: float | None = None
        for i, timing in enumerate(timings):
            if i in used:
                continue
            if start_ms is None:
                best_i = i
                break
            delta = abs(timing["start_ms"] - start_ms)
            if best_delta is None or delta < best_delta:
                best_delta = delta
                best_i = i
        if best_i is None:
            return None
        used.add(best_i)
        return timings[best_i]

    # ------------------------------------------------------------------------
    # Three distinct "turn" notions live in this class; do not confuse them:
    #
    # 1. PER-UTTERANCE turns (HEADLINE) -- :meth:`_build_utterance_turns`.
    #    The user's authoritative definition: "a user speaking is one turn and
    #    Sonic speaking is another." Each individual FINAL user transcript is
    #    one turn and each individual FINAL assistant transcript is a separate
    #    turn, counted in spoken order, each with its own duration. Same-role
    #    utterances are NOT merged. This drives ``turn_count`` and the ``turns``
    #    list the UI renders.
    # 2. GROUPED conversation_turns -- :meth:`_build_conversation_turns`.
    #    Consecutive same-role finals merged into one segment; used only for the
    #    chat view, not the headline count.
    # 3. response_id WINDOWS -- :attr:`turns` (built in :meth:`_finalize_turn`).
    #    Nova-response timing (response_start..complete), surfaced as
    #    ``nova_response_count``; a diagnostics-only signal.
    # ------------------------------------------------------------------------

    def _build_utterance_turns(self) -> list[dict[str, Any]]:
        """Build the PER-UTTERANCE (headline) turn model.

        Rule: A turn = one utterance. Each user final transcript is one turn;
        each Sonic/assistant final transcript is one turn; counted in spoken
        order; duration = span to the next utterance start (last utterance falls
        back to its own span then session end).

        Unlike :meth:`_build_conversation_turns`, consecutive same-role
        utterances are NOT merged -- every final transcript becomes its own
        turn. Each turn dict carries: ``index``, ``speaker`` ('user' |
        'assistant'), ``text``, ``start_ms`` and ``duration_ms``.
        """
        finals = [
            t
            for t in self.transcripts
            if t.get("is_final") and t.get("role") in ("user", "assistant") and t.get("text")
        ]

        # Reuse the partial-user-transcript fallback so a user turn is not
        # dropped when only non-final user transcripts arrived.
        if self._last_user_partial_text and not any(
            t.get("role") == "user" for t in finals
        ):
            finals.append(
                {
                    "role": "user",
                    "text": self._last_user_partial_text,
                    "is_final": False,
                    "t_ms": self._last_user_partial_t_ms,
                }
            )

        finals.sort(key=lambda t: (t.get("t_ms") if t.get("t_ms") is not None else 0.0))

        session_end_ms = self.total_e2e_ms
        turns: list[dict[str, Any]] = []
        for i, item in enumerate(finals):
            start_ms = item.get("t_ms")
            # duration_ms = span to the NEXT utterance's start. For the last
            # utterance (no next) fall back to its own transcript span, then to
            # the session end, mirroring _build_conversation_turns so a single
            # final utterance is not shown as 0.
            duration_ms: float | None = None
            next_start = finals[i + 1].get("t_ms") if i + 1 < len(finals) else None
            if start_ms is not None and next_start is not None:
                duration_ms = round(max(next_start - start_ms, 0.0), 3)
            elif start_ms is not None and session_end_ms is not None and session_end_ms >= start_ms:
                duration_ms = round(session_end_ms - start_ms, 3)
            turns.append(
                {
                    "index": i,
                    "speaker": item["role"],
                    "text": (item.get("text") or "").strip(),
                    "start_ms": start_ms,
                    "duration_ms": duration_ms,
                }
            )
        return turns

    @property
    def conversation_turn_count(self) -> int:
        """Number of conversational (user<->assistant) turns."""
        return len(self._build_conversation_turns())

    # -- summary / serialization ----------------------------------------------

    @property
    def total_e2e_ms(self) -> float | None:
        if self._session_start is None or self._session_end is None:
            return None
        return round((self._session_end - self._session_start) * 1000.0, 3)

    @property
    def twilio_latency_ms(self) -> float | None:
        """Mean observed Twilio transport delay (ms), or None if unmeasured."""
        if not self._transport_delays_ms:
            return None
        return round(sum(self._transport_delays_ms) / len(self._transport_delays_ms), 3)

    @property
    def nova_total_ms(self) -> float:
        """HEADLINE "Total Nova Sonic speaking time" (ms).

        This is the SUM OF OUTBOUND AUDIO durations
        (:attr:`nova_speaking_ms`), i.e. the wall-clock time Nova was actually
        producing speech.

        Why this replaces the old response-window sum: Nova Sonic reuses ONE
        ``response_id`` for the entire call and emits MANY
        ``BidiResponseStartEvent`` (one per speech burst), each overwriting the
        previous open-turn start time, while only ONE
        ``BidiResponseStopEvent`` fires at call end. ``_finalize_turn``
        therefore measured only
        the final "Goodbye" window (~6 s) instead of the ~30 s Nova actually
        spoke, badly under-counting. Summing the duration of every audio frame
        Nova emitted is the trustworthy measure. The old window sum is kept as
        :attr:`nova_response_window_ms` for diagnostics.
        """
        return round(self.nova_speaking_ms, 3)

    @property
    def transport_delay_estimate_ms(self) -> float | None:
        """ESTIMATE of mean Twilio transport delay (ms) (ASK 2/6).

        Accurate-named alias of :attr:`twilio_latency_ms`: a per-frame estimate
        derived from Twilio stream-relative timestamps, NOT an authoritative
        network measurement. The authentic Twilio Call-resource facts are
        fetched off-path post-call into ``twilio_insights``; advanced QoS is a
        paid add-on unavailable on the trial account (never fabricated).
        """
        return self.twilio_latency_ms

    @property
    def non_nova_ms(self) -> float | None:
        """Residual of total e2e time not spent with Sonic actively speaking.

        ``non_nova_ms = total_e2e_ms - nova_total_ms``. This is a bookkeeping
        RESIDUAL (network transport, user speech, silence, tool time), NOT a
        transport-latency figure. The transport estimate is reported separately
        as :attr:`transport_delay_estimate_ms` and the real transport facts live
        in ``twilio_insights`` (ASK 6).
        """
        total = self.total_e2e_ms
        if total is None:
            return None
        return round(max(total - self.nova_total_ms, 0.0), 3)

    def async_tool_calls(self) -> list[dict[str, Any]]:
        """Return call-level ASYNC (post-call, off-path) tool-call entries.

        ASK 5: the AgentCore Code Interpreter date resolution runs POST-CALL,
        off the real-time audio path, and is logged with
        ``event_type='code_interpreter'`` WITHOUT a call-timeline ``t_ms`` (it
        happens after the call ends; FEAT-001 finding d). Such tools are NOT
        part of the live call and must NOT be counted toward call latency, so
        they are surfaced here at the call level (not on any turn) with
        ``kind='async'`` and ``t_ms=None``.

        At call end this is empty -- async tools run later and the post-call
        pipeline records them on the stored record (see
        :func:`app.postcall.extract_and_apply`). Any ``code_interpreter`` entry
        that did land in :attr:`events_log` is still reflected here defensively.
        """
        return [
            {
                "tool": e.get("tool"),
                "kind": "async",
                "t_ms": None,
                "off_path": True,
            }
            for e in self.events_log
            if e.get("event_type") == "code_interpreter"
        ]

    def summary(self) -> dict[str, Any]:
        """Compact metrics summary (the headline numbers).

        ``turn_count`` reflects PER-UTTERANCE turns (each user utterance and
        each Sonic utterance counted separately, per the user's definition). The
        response_id-based window count is preserved as ``nova_response_count``.
        """
        # Build the grouped conversation turns too so the chat view
        # (``self.conversation_turns``) stays populated after on_session_end().
        self._build_conversation_turns()
        utterance_turns = self._build_utterance_turns()
        return {
            "call_sid": self.call_sid,
            "account_id": self.account_id,
            "total_e2e_ms": self.total_e2e_ms,
            "nova_total_ms": round(self.nova_total_ms, 3),
            # ASK 6: accurate name for the per-frame transport ESTIMATE, with
            # the legacy key kept as a deprecated alias (same value).
            "transport_delay_estimate_ms": self.transport_delay_estimate_ms,
            "twilio_latency_ms": self.twilio_latency_ms,
            "non_nova_ms": self.non_nova_ms,
            "ttft_ms": self.first_ttft_ms,
            "turn_count": len(utterance_turns),
            "nova_response_count": len(self.turns),
            "barge_in_count": self.barge_in_count,
            "usage": self.usage,
        }

    def to_call_analytics(self) -> dict[str, Any]:
        """Serializable CallAnalytics record for the store/UI (FEAT-003/004).

        ``turn_count`` is the PER-UTTERANCE turn count and ``turns`` is the
        per-utterance list the per-turn UI table renders (each user utterance
        and each Sonic utterance is its own row). ``nova_response_count``
        preserves the response_id window count and ``conversation_turns``
        carries the grouped chat-style user<->assistant model for the chat view.
        """
        conversation_turns = self._build_conversation_turns()
        utterance_turns = self._build_utterance_turns()
        # Post-call intelligence fields (FEAT-002) start BLANK here and are
        # filled asynchronously, OFF the audio path, by app.postcall after the
        # call ends. total_e2e_ms / nova_total_ms / ttft_ms above are computed
        # from session_start..session_end on the audio path only, so the
        # post-call extraction can never inflate those latency numbers.
        return {
            "call_sid": self.call_sid,
            "account_id": self.account_id,
            # ASK 3: absolute wall-clock start time (UTC ISO-8601) for the
            # calls-under-dues "WHEN" column (absolute + relative).
            "created_at": self.created_at,
            "total_e2e_ms": self.total_e2e_ms,
            "nova_total_ms": round(self.nova_total_ms, 3),
            # ASK 6: accurate-named transport ESTIMATE + legacy alias (same
            # value). The REAL Twilio facts arrive post-call in twilio_insights.
            "transport_delay_estimate_ms": self.transport_delay_estimate_ms,
            "twilio_latency_ms": self.twilio_latency_ms,
            "non_nova_ms": self.non_nova_ms,
            "ttft_ms": self.first_ttft_ms,
            "turn_count": len(utterance_turns),
            "nova_response_count": len(self.turns),
            "barge_in_count": self.barge_in_count,
            "usage": self.usage,
            "conversation_turns": list(conversation_turns),
            # HEADLINE per-turn data: one row per utterance (user or Sonic).
            "turns": list(utterance_turns),
            # SECONDARY: response_id window timings (Nova-response diagnostics).
            "nova_response_turns": list(self.turns),
            "transcripts": list(self.transcripts),
            "events_log": list(self.events_log),
            # ASK 7: barge-in markers, interleaved into the UI timeline by t_ms.
            "barge_in_events": self._barge_in_markers(),
            # ASK 5: call-level ASYNC tool calls (post-call, off-path, NOT
            # contributing to call latency). Empty at call end; the post-call
            # pipeline appends the code-interpreter resolution here.
            "async_tool_calls": self.async_tool_calls(),
            # ASK 6: honest Twilio transport split. twilio_insights holds the
            # REAL Call-resource facts once post-call enrichment runs; it starts
            # as an explicit "pending" marker so the UI never implies the real
            # data is present before enrichment (never fabricated).
            "twilio_insights": _pending_twilio_insights(),
            # -- post-call intelligence (blank at call end) ------------------
            "disposition": None,
            "structured_data": _blank_structured_data(),
            "next_best_action": None,
            "intelligence_status": "pending",
            "collection_paused": False,
            "escalated": False,
            "ptp_sms_status": None,
            "ptp_sms_to": None,
            "triggers": [],
        }


def _blank_structured_data() -> dict[str, Any]:
    """Empty structured-conversation-data skeleton (see app.store)."""
    from app.store import blank_structured_data

    return blank_structured_data()


def _pending_twilio_insights() -> dict[str, Any]:
    """Placeholder ``twilio_insights`` block set at call end (ASK 2/6).

    The REAL Twilio Call-resource facts are fetched OFF the audio path,
    post-call, by :func:`app.twilio_client.fetch_call_insights`. Until then the
    record carries this explicit ``available=False`` / ``source='pending'``
    marker so the UI never implies authentic Twilio data is present before the
    enrichment runs. We NEVER fabricate Twilio metrics.
    """
    return {
        "available": False,
        "source": "pending",
        "advanced_qos_available": False,
        "advanced_qos_reason": None,
        "fields": {},
    }


def _tool_name(tool_use: Any) -> Any:
    """Derive the tool name from a GA ``ToolUse``.

    GA ``BidiToolUseBlocksEvent`` carries a list of ``ToolUse`` TypedDicts
    (``{"name", "input", "toolUseId"}``). Fall back to attribute access for any
    object-style tool use.
    """
    if isinstance(tool_use, dict):
        return tool_use.get("name")
    return getattr(tool_use, "name", None)
