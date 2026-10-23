"""Per-call metrics + analytics, driven by Strands bi-di events and Twilio timestamps.

:class:`CallMetrics` implements the :class:`app.sonic_bridge.MetricsSink`
protocol. The Nova Sonic bridge forwards every bi-di output event to
:meth:`CallMetrics.record_event`; the WebSocket handler reports inbound Twilio
media timestamps via :meth:`CallMetrics.record_inbound_media`. From those two
ordered streams this collector computes:

- total end-to-end time (``total_e2e_ms``),
- the split between Twilio transport latency and Nova Sonic active time
  (``twilio_latency_ms`` vs ``nova_total_ms``),
- time-to-first-token/first-audio per turn and conversation-level
  (``ttft_ms``),
- turn count, per-turn duration and per-turn latency,
- transcripts, token usage, and barge-in count,
- an in-depth, timestamped ``events_log`` for CloudWatch-friendly logging.

Measurement boundaries:

- A conversational "turn" is one side of the user<->assistant alternation,
  derived from GA ``BidiTranscriptBlockEvent`` roles (a block event IS a
  completed utterance; there is no ``is_final`` flag in GA). See
  :meth:`CallMetrics._build_conversation_turns`. This is the headline
  ``turn_count``. The response_id-based grouping below is kept as a secondary
  Nova-response timing signal and surfaced as ``nova_response_count``.
- A complete Nova response window is a matched ``BidiResponseStartEvent`` /
  ``BidiResponseStopEvent`` pair keyed by ``response_id``. Forced closures are
  retained as diagnostics with a distinct ``end_source``, not as completed
  response timelines. Per-response duration = stop_time - start_time.
- ``text_ttft_ms`` measures response start to first observed assistant text;
  ``ttfa_ms`` measures the same start to first audio. They overlap, so adding
  them double-counts time. ``ttft_ms`` remains a deprecated audio alias.
- Transcript timestamps are publication events, not acoustic speech boundaries.
  ``caller_reply_gap_ms`` is only the immediately preceding caller transcript
  publication to first observed reply audio, with its source recorded. A
  delayed caller transcript can arrive after response start or even first audio.
  The response-start-time snapshot cannot identify the current caller reliably.
- ``nova_total_ms`` = the wall-clock time Nova Sonic was actively producing
  speech, computed as the sum of outbound ``BidiAudioDeltaEvent`` PCM
  durations. Response-window wall time is a separate metric,
  ``nova_response_window_ms``, which can include pauses and tool activity.
- ``twilio_latency_ms`` / ``transport_delay_estimate_ms`` = a per-frame
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
  (RTT/jitter/packet-loss/MOS) requires a separate Voice Insights integration
  and is marked unavailable because this application does not fetch it.
- ``non_nova_ms`` = ``total_e2e_ms - nova_total_ms`` -- the share of the call's
  end-to-end wall-clock NOT spent with Sonic actively producing speech. It is a
  bookkeeping residual (network transport, user speech, silence, tool time),
  NOT a transport-latency measurement; the transport estimate above is a
  separate, independently-derived figure.

Each utterance exposes an explicit ``response_window`` when a recorded model
response contains its final transcript. Its first-text and first-audio markers
share one origin and its end is the observed response stop. It is not a
measurement of telephone playback. ``first_output_timing`` also retains
observed first-output markers for a response bounded by a next response start,
session end, or connection restart; such bounds never imply a completed response.
``transcript_interval_ms`` (also retained as
``turn_elapsed_ms`` / ``total_ms``) is the final-transcript-to-next-final interval;
``transcript_span_ms`` is a span between transcript publications. Neither is a
spoken duration or a sum of the first-output metrics.
"""

from __future__ import annotations

import base64
import json
import logging
import math
import time
from datetime import datetime, timezone
from typing import Any, Callable

from app.sonic_usage import UsageAccumulator

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
    BidiTextDeltaEvent,
    BidiTranscriptBlockEvent,
    BidiTranscriptDeltaEvent,
    BidiUsageEvent,
)

# One JSON object per stdout line lets the configured log collector parse
# each record without additional formatting rules.
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
        nova_model_id: str | None = None,
        aws_region: str | None = None,
    ) -> None:
        self.call_sid = call_sid
        self.account_id = account_id
        self.nova_model_id = nova_model_id
        self.aws_region = aws_region
        self._clock = clock
        self._log = logger_ or metrics_logger

        self._session_start: float | None = None
        self._session_end: float | None = None
        # UTC timestamp for displaying when a call started. The monotonic
        # clock measures durations but cannot supply a calendar timestamp.
        # Set once at session start, outside per-frame handlers.
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
        self.first_ttft_ms: float | None = None  # legacy alias for first audio
        self.first_text_ttft_ms: float | None = None
        self.tool_executions: dict[str, dict[str, Any]] = {}
        self.payment_date_capture: dict[str, Any] | None = None
        # HEADLINE Nova speaking time: sum of outbound audio-frame durations.
        # See :meth:`_on_audio` and the :attr:`nova_total_ms` property for why
        # The old response-window sum was wrong and this replaces it.
        self.nova_speaking_ms: float = 0.0
        # SECONDARY (legacy) measure: sum of matched response_start..complete
        # windows keyed by response_id. Accumulated in :meth:`_finalize_turn`.
        # Kept for diagnostics only; NOT the headline nova_total_ms.
        self.nova_response_window_ms: float = 0.0
        self.barge_in_count: int = 0
        self.usage: dict[str, Any] = {}
        self._usage = UsageAccumulator()
        self.transcripts: list[dict[str, Any]] = []
        self.events_log: list[dict[str, Any]] = []
        self.termination: dict[str, Any] | None = None

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

    def record_lifecycle_event(self, event: str, **fields: Any) -> None:
        """Record application lifecycle evidence without SDK exception payloads."""
        self._emit(event, **fields)

    # -- lifecycle ------------------------------------------------------------

    def on_session_start(self) -> None:
        self._session_start = self._clock()
        # Capture the absolute wall-clock start time (UTC ISO-8601) so
        # The calls-under-dues view can show WHEN the call happened. Independent
        # of the monotonic clock used for durations.
        self.created_at = datetime.now(timezone.utc).isoformat()
        self._emit("call_placed")
        self._emit("stream_start")

    def on_session_end(self) -> None:
        self._session_end = self._clock()
        # Close any turns still open (e.g. call dropped mid-response).
        for response_id in list(self._open_turns):
            self._finalize_turn(response_id, stop_reason="incomplete", end_source="session_end")
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

    def record_tool_execution(self, event: dict[str, Any]) -> None:
        """Record execution, rather than guessing async mode from call phase."""
        now = self._rel_ms(self._clock())
        data = dict(event)
        lifecycle = data.pop("event")
        identity = str(data["tool_use_id"])
        current = self.tool_executions.setdefault(identity, {**data, "t_ms": now})
        current.update(data)
        if lifecycle == "complete":
            current["completed_t_ms"] = now
        self._emit(f"tool_execution_{lifecycle}", **data)

    def capture_payment_date_phrase(self, capture: dict[str, Any]) -> None:
        """Snapshot the successful phrase capture for this call's date resolver."""
        self.payment_date_capture = dict(capture)

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
        elif isinstance(event, BidiTextDeltaEvent):
            if getattr(event, "delta", None):
                self._on_first_text(now)
        elif isinstance(event, BidiTranscriptDeltaEvent):
            if getattr(event, "role", None) == "assistant" and getattr(event, "delta", None):
                self._on_first_text(now)
            # Partial transcript deltas provide a fallback before a final block arrives.
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
                self._emit("tool_invocation", tool=_tool_name(tool_use),
                           tool_use_id=tool_use.get("toolUseId") if isinstance(tool_use, dict) else None)
        elif isinstance(event, BidiConnectionStartEvent):
            self._emit(
                "connection_start",
                connection_id=getattr(event, "connection_id", None),
                model=getattr(event, "model", None),
            )
        elif isinstance(event, BidiConnectionRestartEvent):
            for response_id in list(self._open_turns):
                self._finalize_turn(response_id, stop_reason="connection_restart", end_source="connection_restart", now=now)
            self._emit(
                "connection_restart",
                reason=event.reason,
                turn_interrupted=event.turn_interrupted,
                error_type=type(event.timeout_error).__name__ if event.timeout_error is not None else None,
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
        # A repeated provider response id begins a new timing window; retain
        # The prior one rather than discarding its first-audio/text evidence.
        if response_id in self._open_turns:
            self._finalize_turn(response_id, stop_reason="next_response_start", end_source="next_response_start", now=now)
        # Retain the legacy diagnostic snapshot. It can refer to a PREVIOUS
        # caller utterance because this response can precede the current final
        # transcript. Never use it as the caller's reply gap.
        user_stop = self._last_user_final_wall or self._last_inbound_wall
        turn_latency_ms = (now - user_stop) * 1000.0 if user_stop is not None else None
        self._open_turns[response_id] = {
            "response_id": response_id,
            "start": now,
            "first_audio": None,
            "first_text": None,
            "turn_latency_ms": round(turn_latency_ms, 3) if turn_latency_ms is not None else None,
            "turn_latency_source": "latest_publication_or_media_to_response_start",
        }
        self._emit(
            "turn_start",
            response_id=response_id,
            turn_latency_ms=self._open_turns[response_id]["turn_latency_ms"],
        )

    def _on_first_text(self, now: float) -> None:
        # A transcript/text delta is observed text, not an inferred token from
        # audio. Absence of such a delta leaves text TTFT unavailable.
        for turn in list(self._open_turns.values())[-1:]:
            if turn.get("first_text") is None:
                turn["first_text"] = now
                duration = round((now - turn["start"]) * 1000.0, 3)
                turn["text_ttft_ms"] = duration
                if self.first_text_ttft_ms is None:
                    self.first_text_ttft_ms = duration
                self._emit("text_ttft_measured", response_id=turn["response_id"], text_ttft_ms=duration)
                break

    def _on_audio(self, event: BidiAudioDeltaEvent, now: float) -> None:
        # HEADLINE Nova speaking time: accumulate the real duration of each
        # outbound audio frame. Nova Sonic emits PCM16 mono at 24 kHz, so the
        # frame duration (ms) = decoded_bytes / (2 * sample_rate * channels) *
        # 1000 (2 bytes per PCM16 sample). This is O(1) per frame and must never
        # raise -- record_event is wrapped in try/except by the bridge, but we
        # stay defensive here too so one malformed frame cannot drop metrics.
        decoded_bytes = 0
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
        if decoded_bytes <= 0:
            return

        # Attribute first audio to the most-recently-started still-open turn.
        #
        # The GA BidiAudioDeltaEvent carries no response_id (only
        # ``audio``/``format``/``sample_rate``/``channels``/``content_id``), so
        # audio cannot be
        # matched to its turn by id. Under normal sequencing only one turn is
        # open at a time. Under a barge-in that opens turn N+1 while turn N is
        # still finishing, the freshest audio belongs to the newest turn, so we
        # inspect only the newest window. Later chunks must never fill an older
        # window that missed its own first-audio observation.
        for turn in list(self._open_turns.values())[-1:]:
            if turn["first_audio"] is None:
                turn["first_audio"] = now
                ttft_ms = round((now - turn["start"]) * 1000.0, 3)
                turn["ttft_ms"] = ttft_ms
                turn["ttfa_ms"] = ttft_ms
                if self.first_ttft_ms is None:
                    self.first_ttft_ms = ttft_ms
                self._emit("ttft_measured", response_id=turn["response_id"], ttft_ms=ttft_ms)
                break

    def _on_response_complete(self, event: BidiResponseStopEvent, now: float) -> None:
        response_id = getattr(event, "response_id", None)
        # GA BidiResponseStopEvent only carries response_id; stop_reason is gone.
        stop_reason = getattr(event, "stop_reason", None)
        if response_id is None or response_id not in self._open_turns:
            # An unknown/stale stop cannot establish another response's end.
            return
        self._finalize_turn(response_id, stop_reason=stop_reason, now=now)

    def _finalize_turn(
        self, response_id: str, *, stop_reason: Any, end_source: str = "response_stop", now: float | None = None
    ) -> None:
        turn = self._open_turns.pop(response_id, None)
        if turn is None:
            return
        end = now if now is not None else self._clock()
        duration_ms = round((end - turn["start"]) * 1000.0, 3)
        # SECONDARY measure only. A reused response_id is closed at its next
        # response_start, preserving every observed window. These windows can
        # include silence; headline speaking time comes from audio durations.
        self.nova_response_window_ms += duration_ms
        record = {
            "response_id": response_id,
            "index": len(self.turns),
            "start_ms": self._rel_ms(turn["start"]),
            "end_ms": self._rel_ms(end),
            "end_source": end_source,
            "first_text_ms": self._rel_ms(turn["first_text"]) if turn.get("first_text") is not None else None,
            "first_audio_ms": self._rel_ms(turn["first_audio"]) if turn.get("first_audio") is not None else None,
            "duration_ms": duration_ms,
            "ttft_ms": turn.get("ttft_ms"),
            "ttfa_ms": turn.get("ttfa_ms"),
            "text_ttft_ms": turn.get("text_ttft_ms"),
            "turn_latency_ms": turn.get("turn_latency_ms"),
            "turn_latency_source": turn.get("turn_latency_source"),
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
        # The cache + modality breakdowns under input_token_details /
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
        delta = {
            "input_tokens": getattr(event, "input_tokens", None),
            "output_tokens": getattr(event, "output_tokens", None),
            "total_tokens": getattr(event, "total_tokens", None),
            "cache_read_input_tokens": input_details.get("cache_read"),
            "cache_write_input_tokens": None,
            "modality_details": modality_details,
        }
        # Strands 1.62 converts usageEvent.details.delta, not details.total.
        # Accumulate once across the whole telephone call, including renewals.
        self._usage.add(delta)
        self.usage = self._usage.snapshot()
        self._emit("usage", **delta, usage_accounting="delta", usage_schema_version=2,
                   nova_model_id=self.nova_model_id, aws_region=self.aws_region)

    # -- conversational turns -------------------------------------------------

    # Completed transcript blocks identify conversational utterances. Consecutive
    # final transcripts from one speaker form one segment, including an initial
    # assistant greeting. Response IDs can be shared across several exchanges,
    # so response-window counts remain a separate timing signal.

    def _build_conversation_turns(self) -> list[dict[str, Any]]:
        """Group consecutive final transcripts without treating them as speech clocks.

        ``start_ms`` / ``end_ms`` are first/last transcript publications.
        ``transcript_interval_ms`` spans from that first publication to the next
        segment's first publication, or to call end for the last segment. The
        legacy elapsed aliases retain that interval. First-output metrics come
        only from a recorded response containing this segment's first final.
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
                "_is_final": item.get("is_final", False),
                "start_ms": t_ms,
                "end_ms": t_ms,
            }
            turns.append(seg)
            idx += 1

        session_end_ms = self.total_e2e_ms
        for i, seg in enumerate(turns):
            seg["text"] = " ".join(seg.pop("_texts")).strip()
            start_ms = seg.get("start_ms")
            end_ms = seg.get("end_ms")

            # Span between transcript publications, not spoken duration.
            if start_ms is not None and end_ms is not None:
                seg["transcript_span_ms"] = round(end_ms - start_ms, 3)
            else:
                seg["transcript_span_ms"] = None

            total_ms: float | None = None
            next_start = turns[i + 1].get("start_ms") if i + 1 < len(turns) else session_end_ms
            if start_ms is not None and next_start is not None:
                total_ms = round(max(next_start - start_ms, 0.0), 3)
            seg["total_ms"] = total_ms
            seg["turn_elapsed_ms"] = seg.get("total_ms")
            is_final = seg.pop("_is_final")
            seg["transcript_final_ms"] = start_ms if is_final else None
            seg["transcript_interval_ms"] = total_ms
            seg["transcript_interval_source"] = (
                "final_transcript_to_next_final_or_call_end" if is_final
                else "partial_transcript_to_next_final_or_call_end"
            )

        self._attach_response_timings(turns)

        # Annotate interrupted turns (barge-in inside the turn's
        # time window) and attach the in-call/sync tool invocations to the
        # nearest assistant turn. Both are derived purely from the already-
        # recorded events_log (off the real-time audio path -- this runs only
        # when the conversation model is (re)built for a summary/serialization).
        self._annotate_barge_ins(turns)
        self._attach_sync_tool_calls(turns)

        self.conversation_turns = turns
        return turns

    # -- barge-in / interrupted-turn annotation ------------------------

    def _barge_in_markers(self) -> list[dict[str, Any]]:
        """Return the barge-in events as inline timeline markers (by t_ms).

        GA's ``BidiBargeInEvent`` carries no payload and GA exposes NO
        delivered-vs-intended transcript, so the ONLY
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

    # -- In-call tool execution and transcript association -------------------

    def _sync_tool_calls(self) -> list[dict[str, Any]]:
        """Legacy method name: in-call tools with measured execution metadata.

        Historical invocation-only events identify a phase, not the Python or
        model execution mode. Preserve those as unknown instead of inventing
        a sync/async classification. New toolUseIds correlate invocation and
        actual execution without duplicate entries.
        """
        calls = [dict(entry) for entry in self.tool_executions.values()]
        measured_ids = set(self.tool_executions)
        for event in self.events_log:
            if event.get("event_type") != "tool_invocation":
                continue
            if event.get("tool_use_id") in measured_ids:
                continue
            calls.append({"tool": event.get("tool"), "tool_use_id": event.get("tool_use_id"),
                          "kind": "unknown", "phase": "in_call", "t_ms": event.get("t_ms"),
                          "source": "invocation_only"})
        return sorted(calls, key=lambda entry: entry.get("t_ms") or 0)

    def _attach_sync_tool_calls(self, turns: list[dict[str, Any]]) -> None:
        """Associate in-call tools using transcript event timestamps.

        An in-call tool fires during an assistant's turn (Sonic invokes the
        tool mid-response), so each ``tool_invocation`` t_ms is associated to
        the assistant turn whose start_ms is the greatest start <= the tool
        t_ms. If a user turn intervenes, prefer its following assistant reply
        because tool dispatch can precede transcript publication. This remains
        an explicitly labeled estimate. Every turn gets a ``tool_calls`` list.
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
            # Tool dispatch commonly precedes publication of the assistant's
            # transcript. If the latest published turn is the user, associate
            # with its following reply rather than the previous assistant.
            preceding = [seg for seg in turns if t_ms is not None and seg.get("start_ms") is not None
                         and seg["start_ms"] <= t_ms]
            if preceding and preceding[-1]["speaker"] == "user":
                after_user = False
                for seg in turns:
                    if seg is preceding[-1]:
                        after_user = True
                    elif after_user and seg["speaker"] == "assistant":
                        target = seg
                        break
            if target is not None:
                target["tool_calls"].append({**call, "association": "transcript_event_time_estimate"})

    def _attach_response_timings(self, turns: list[dict[str, Any]]) -> None:
        """Attach only observed response boundaries that contain a final transcript.

        A transcript may be published after a response starts, so a snapshot of
        the latest caller at response start can refer to the previous exchange.
        Resolve the immediately preceding caller after all transcript events
        are available. Publication-to-audio is an observed proxy, not acoustic
        silence or telephone playback latency.

        A response can contain multiple transcripts or share its ID with later
        responses. Its first-output measurements belong to its first matched
        assistant transcript only. Never borrow the nearest unrelated response
        or reuse an older overlapping window when the newest is already claimed.
        """
        def finite(value: Any) -> float | None:
            if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
                return float(value)
            return None

        windows: list[dict[str, Any]] = []
        for position, timing in enumerate(self.turns):
            boundary_source = timing.get("end_source")
            if boundary_source is None:
                boundary_source = {
                    "incomplete": "session_end", "next_response_start": "next_response_start",
                    "connection_restart": "connection_restart",
                }.get(timing.get("stop_reason"), "response_stop")
            if boundary_source not in ("response_stop", "next_response_start", "session_end", "connection_restart"):
                continue
            start = finite(timing.get("start_ms"))
            end = finite(timing.get("end_ms"))
            duration = finite(timing.get("duration_ms"))
            if start is None:
                continue
            if end is None and duration is not None and duration >= 0:
                end = start + duration
            if end is None or end < start:
                continue

            def marker(timestamp: str, latency: str, legacy: str | None = None) -> float | None:
                value = finite(timing.get(timestamp))
                if value is None:
                    offset = finite(timing.get(latency))
                    if offset is None and legacy:
                        offset = finite(timing.get(legacy))
                    if offset is not None and offset >= 0:
                        value = start + offset
                return round(value, 3) if value is not None and start <= value <= end else None

            first_text = marker("first_text_ms", "text_ttft_ms")
            first_audio = marker("first_audio_ms", "ttfa_ms", "ttft_ms")
            # A tool-only response must not claim a transcript from another
            # response. Missing output evidence is represented as unavailable.
            if first_text is None and first_audio is None:
                continue
            windows.append({
                "response_id": timing.get("response_id"),
                "response_index": timing.get("index", position),
                "start_ms": start,
                "end_ms": round(end, 3),
                "duration_ms": round(end - start, 3),
                "first_text_ms": first_text,
                "first_audio_ms": first_audio,
                "text_ttft_ms": round(first_text - start, 3) if first_text is not None else None,
                "ttfa_ms": round(first_audio - start, 3) if first_audio is not None else None,
                "basis": "application_event_receipt",
                "association": "contains_transcript_final",
                "boundary_source": boundary_source,
            })

        used: set[int] = set()
        for i, turn in enumerate(turns):
            window = None
            first_output = None
            final = finite(turn.get("transcript_final_ms"))
            if turn["speaker"] == "assistant" and final is not None:
                containing = [
                    (j, item) for j, item in enumerate(windows)
                    if item["start_ms"] <= final <= item["end_ms"]
                    and any(marker is not None and marker <= final
                            for marker in (item["first_text_ms"], item["first_audio_ms"]))
                ]
                if containing:
                    candidate, item = max(containing, key=lambda pair: (pair[1]["start_ms"], pair[0]))
                    if candidate not in used:
                        used.add(candidate)
                        first_output = {
                            key: value for key, value in item.items()
                            if key not in ("end_ms", "duration_ms", "association")
                        }
                        first_output["boundary_ms"] = item["end_ms"]
                        first_output["association"] = "transcript_final_within_observed_boundary"
                        if item["boundary_source"] == "response_stop":
                            window = {key: value for key, value in item.items() if key != "boundary_source"}

            turn["response_window"] = window
            turn["first_output_timing"] = first_output
            turn["text_ttft_ms"] = first_output["text_ttft_ms"] if first_output else None
            turn["ttfa_ms"] = first_output["ttfa_ms"] if first_output else None
            turn["ttft_ms"] = turn["ttfa_ms"]  # Deprecated AUDIO alias, never text TTFT.
            source = "response_window" if window else "bounded_response"
            turn["text_ttft_source"] = source if turn["text_ttft_ms"] is not None else "unavailable"
            turn["ttft_source"] = source if turn["ttfa_ms"] is not None else "unavailable"
            turn["ttfa_source"] = turn["ttft_source"]

            gap = None
            gap_source = "unavailable"
            previous = turns[i - 1] if i else None
            if first_output and previous and previous["speaker"] == "user" and previous.get("transcript_final_ms") is not None:
                caller_final = finite(previous.get("end_ms", previous.get("transcript_final_ms")))
                first_audio = first_output["first_audio_ms"]
                if caller_final is not None and first_audio is not None:
                    if first_audio >= caller_final:
                        gap = round(first_audio - caller_final, 3)
                        gap_source = "caller_final_to_first_audio"
                    else:
                        gap_source = "audio_before_caller_final"
            turn["caller_reply_gap_ms"] = gap
            turn["caller_reply_gap_source"] = gap_source
            turn["response_wait_ms"] = gap
            turn["latency_ms"] = gap

    # ------------------------------------------------------------------------
    # Three distinct "turn" notions live in this class; do not confuse them:
    #
    # 1. PER-UTTERANCE turns (HEADLINE) -- :meth:`_build_utterance_turns`.
    #    The user's authoritative definition: "a user speaking is one turn and
    #    Sonic speaking is another." Each individual FINAL user transcript is
    #    one turn and each individual FINAL assistant transcript is a separate
    #    turn, counted in transcript publication order. Same-role
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
        each Sonic/assistant final transcript is one turn; counted in transcript
        publication order. The legacy duration is the interval to the next
        final transcript publication, or call end for the final row. It does
        not measure spoken duration.

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
            # Compatibility interval between transcript publications, never a
            # synthetic estimate of speech start/end.
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
                    "transcript_final_ms": start_ms if item.get("is_final") else None,
                    "transcript_interval_ms": duration_ms,
                    "transcript_interval_source": (
                        "final_transcript_to_next_final_or_call_end" if item.get("is_final")
                        else "partial_transcript_to_next_final_or_call_end"
                    ),
                }
            )
        self._attach_response_timings(turns)
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

        Response windows can reuse identifiers and include silence or tool
        activity. Summing decoded outbound audio-frame durations measures the
        generated speech independently of those windows.
        :attr:`nova_response_window_ms` records response-window wall time.
        """
        return round(self.nova_speaking_ms, 3)

    @property
    def transport_delay_estimate_ms(self) -> float | None:
        """ESTIMATE of mean Twilio transport delay (ms).

        Accurate-named alias of :attr:`twilio_latency_ms`: a per-frame estimate
        derived from Twilio stream-relative timestamps, NOT an authoritative
        network measurement. The authentic Twilio Call-resource facts are
        fetched off-path post-call into ``twilio_insights``. Advanced QoS is
        outside this application's Twilio integration and is not inferred.
        """
        return self.twilio_latency_ms

    @property
    def non_nova_ms(self) -> float | None:
        """Residual of total e2e time not spent with Sonic actively speaking.

        ``non_nova_ms = total_e2e_ms - nova_total_ms``. This is a bookkeeping
        RESIDUAL (network transport, user speech, silence, tool time), NOT a
        transport-latency figure. The transport estimate is reported separately
        as :attr:`transport_delay_estimate_ms` and the real transport facts live
        in ``twilio_insights``.
        """
        total = self.total_e2e_ms
        if total is None:
            return None
        return round(max(total - self.nova_total_ms, 0.0), 3)

    def async_tool_calls(self) -> list[dict[str, Any]]:
        """Return call-level ASYNC (post-call, off-path) tool-call entries.

        The AgentCore Code Interpreter date resolution runs POST-CALL,
        off the real-time audio path, and is logged with
        ``event_type='code_interpreter'`` WITHOUT a call-timeline ``t_ms`` (it
        happens after the call ends). Such tools are NOT
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

    def _usage_snapshot(self) -> dict[str, Any]:
        if not self.usage:
            return {}
        snapshot = self._usage.snapshot()
        termination = self.termination or {}
        if termination.get("sdk_cleanup") in {"timeout_work_retained", "failed"}:
            snapshot.update(complete=False, coverage_reason="sdk_cleanup_incomplete")
        return snapshot

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
            # Accurate name for the per-frame transport ESTIMATE, with
            # The legacy key kept as a deprecated alias (same value).
            "transport_delay_estimate_ms": self.transport_delay_estimate_ms,
            "twilio_latency_ms": self.twilio_latency_ms,
            "non_nova_ms": self.non_nova_ms,
            "ttft_ms": self.first_ttft_ms,
            "ttfa_ms": self.first_ttft_ms,
            "text_ttft_ms": self.first_text_ttft_ms,
            "latency_measurement": "response_start_to_first_observed_output",
            "turn_count": len(utterance_turns),
            "nova_response_count": len(self.turns),
            "barge_in_count": self.barge_in_count,
            "usage": self._usage_snapshot(),
        }

    def to_call_analytics(self) -> dict[str, Any]:
        """Serializable CallAnalytics record for the store/UI.

        ``turn_count`` is the PER-UTTERANCE turn count and ``turns`` is the
        per-utterance list the per-turn UI table renders (each user utterance
        and each Sonic utterance is its own row). ``nova_response_count``
        preserves the response_id window count and ``conversation_turns``
        carries the grouped chat-style user<->assistant model for the chat view.
        """
        conversation_turns = self._build_conversation_turns()
        utterance_turns = self._build_utterance_turns()
        # Post-call intelligence fields start BLANK here and are
        # filled asynchronously, OFF the audio path, by app.postcall after the
        # call ends. total_e2e_ms / nova_total_ms / ttft_ms above are computed
        # from session_start..session_end on the audio path only, so the
        # post-call extraction can never inflate those latency numbers.
        return {
            "call_sid": self.call_sid,
            "account_id": self.account_id,
            **({"termination": dict(self.termination)} if self.termination is not None else {}),
            "nova_model_id": self.nova_model_id,
            "aws_region": self.aws_region,
            # Absolute wall-clock start time (UTC ISO-8601) for the
            # calls-under-dues "WHEN" column (absolute + relative).
            "created_at": self.created_at,
            "total_e2e_ms": self.total_e2e_ms,
            "nova_total_ms": round(self.nova_total_ms, 3),
            # accurate-named transport ESTIMATE + legacy alias (same
            # value). The REAL Twilio facts arrive post-call in twilio_insights.
            "transport_delay_estimate_ms": self.transport_delay_estimate_ms,
            "twilio_latency_ms": self.twilio_latency_ms,
            "non_nova_ms": self.non_nova_ms,
            "ttft_ms": self.first_ttft_ms,
            "ttfa_ms": self.first_ttft_ms,
            "text_ttft_ms": self.first_text_ttft_ms,
            "latency_measurement": "response_start_to_first_observed_output",
            "turn_count": len(utterance_turns),
            "nova_response_count": len(self.turns),
            "barge_in_count": self.barge_in_count,
            "usage": self._usage_snapshot(),
            "conversation_turns": list(conversation_turns),
            # HEADLINE per-turn data: one row per utterance (user or Sonic).
            "turns": list(utterance_turns),
            # SECONDARY: response_id window timings (Nova-response diagnostics).
            "nova_response_turns": list(self.turns),
            "transcripts": list(self.transcripts),
            "events_log": list(self.events_log),
            "tool_executions": [dict(entry) for entry in self.tool_executions.values()],
            "payment_date_capture": dict(self.payment_date_capture) if self.payment_date_capture else None,
            # Barge-in markers, interleaved into the UI timeline by t_ms.
            "barge_in_events": self._barge_in_markers(),
            # Call-level ASYNC tool calls (post-call, off-path, NOT
            # contributing to call latency). Empty at call end; the post-call
            # pipeline appends the code-interpreter resolution here.
            "async_tool_calls": self.async_tool_calls(),
            # Honest Twilio transport split. twilio_insights holds the
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
    """Placeholder ``twilio_insights`` block set at call end.

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
