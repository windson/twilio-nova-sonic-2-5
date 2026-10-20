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
  derived from ``BidiTranscriptStreamEvent`` roles (see
  :meth:`CallMetrics._build_conversation_turns`). This is the headline
  ``turn_count``. The response_id-based grouping below is kept as a secondary
  Nova-response timing signal and surfaced as ``nova_response_count``.
- A Nova response window is a matched ``BidiResponseStartEvent`` /
  ``BidiResponseCompleteEvent`` pair keyed by ``response_id``. Per-response
  duration = complete_time - start_time.
- TTFT for a turn = time from that turn's ``response_start`` to the first
  ``BidiAudioStreamEvent`` of the same response. The conversation-level TTFT is
  the first turn's TTFT.
- Per-turn latency = time from when the user finished speaking to the turn's
  ``response_start``. "User finished speaking" is approximated as the timestamp
  of the last inbound Twilio media frame seen before ``response_start`` (or, if
  present, the last final user transcript before it).
- ``nova_total_ms`` = sum of active response windows (response_start..complete),
  i.e. the wall-clock time Nova Sonic was actively producing a response.
- ``twilio_latency_ms`` = transport delay derived from Twilio media ``timestamp``
  (stream-relative ms) vs local receive wall-clock. Twilio's ``timestamp`` is
  milliseconds since the stream started; comparing its growth to locally
  measured elapsed time yields the extra transport delay. We report the mean
  observed transport delay. As a cross-check, the share of total e2e time not
  attributable to Nova responses is reported as ``non_nova_ms``.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Callable

from strands.experimental.bidi.types.events import (
    BidiAudioStreamEvent,
    BidiConnectionCloseEvent,
    BidiConnectionRestartEvent,
    BidiConnectionStartEvent,
    BidiErrorEvent,
    BidiInterruptionEvent,
    BidiResponseCompleteEvent,
    BidiResponseStartEvent,
    BidiTranscriptStreamEvent,
    BidiUsageEvent,
    ToolUseStreamEvent,
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
        self._last_user_partial_text: str | None = None
        self._last_user_partial_t_ms: float | None = None

        # Conversation-level aggregates.
        self.first_ttft_ms: float | None = None
        self.nova_total_ms: float = 0.0
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
        elif isinstance(event, BidiAudioStreamEvent):
            self._on_audio(event, now)
        elif isinstance(event, BidiResponseCompleteEvent):
            self._on_response_complete(event, now)
        elif isinstance(event, BidiTranscriptStreamEvent):
            self._on_transcript(event, now)
        elif isinstance(event, BidiUsageEvent):
            self._on_usage(event)
        elif isinstance(event, BidiInterruptionEvent):
            self.barge_in_count += 1
            self._emit("barge_in", reason=getattr(event, "reason", None))
        elif isinstance(event, ToolUseStreamEvent):
            self._emit(
                "tool_invocation",
                tool=_tool_name(event),
            )
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
        elif isinstance(event, BidiConnectionCloseEvent):
            self._emit(
                "connection_close",
                connection_id=getattr(event, "connection_id", None),
                reason=getattr(event, "reason", None),
            )
        elif isinstance(event, BidiErrorEvent):
            self._emit(
                "error",
                error=getattr(event, "error", None),
                code=getattr(event, "code", None),
                error_message=getattr(event, "message", None),
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

    def _on_audio(self, event: BidiAudioStreamEvent, now: float) -> None:
        # Attribute first audio to the most-recently-started still-open turn.
        #
        # The installed BidiAudioStreamEvent carries no response_id (only
        # ``audio``/``format``/``sample_rate``/``channels``), so audio cannot be
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

    def _on_response_complete(self, event: BidiResponseCompleteEvent, now: float) -> None:
        response_id = getattr(event, "response_id", None)
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
        self.nova_total_ms += duration_ms
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

    def _on_transcript(self, event: BidiTranscriptStreamEvent, now: float) -> None:
        role = getattr(event, "role", None)
        text = getattr(event, "text", None)
        is_final = bool(getattr(event, "is_final", False))
        if role == "user":
            if is_final:
                self._last_user_final_wall = now
            elif text:
                # Retain the latest partial user transcript as a fallback so the
                # chat view is not empty when Nova only emits non-final user
                # transcripts (observed in some live calls).
                self._last_user_partial_text = text
                self._last_user_partial_t_ms = self._rel_ms(now)
        self.transcripts.append(
            {
                "role": role,
                "text": text,
                "is_final": is_final,
                "t_ms": self._rel_ms(now),
            }
        )
        if is_final:
            self._emit("transcript", role=role, text=text)

    def _on_usage(self, event: BidiUsageEvent) -> None:
        self.usage = {
            "input_tokens": getattr(event, "input_tokens", None),
            "output_tokens": getattr(event, "output_tokens", None),
            "total_tokens": getattr(event, "total_tokens", None),
            "cache_read_input_tokens": getattr(event, "cache_read_input_tokens", None),
            "cache_write_input_tokens": getattr(event, "cache_write_input_tokens", None),
            "modality_details": getattr(event, "modality_details", None),
        }
        self._emit("usage", **self.usage)

    # -- conversational turns -------------------------------------------------

    # ------------------------------------------------------------------------
    # What is a "turn"?
    #
    # The user's observation: a turn is one back-and-forth in the conversation
    # (the user says something, Nova Sonic answers), not an internal grouping of
    # Nova's output. The ORIGINAL implementation counted a turn as a matched
    # BidiResponseStartEvent/BidiResponseCompleteEvent pair keyed by
    # ``response_id``. In live calls Nova Sonic REUSED a single ``response_id``
    # for an entire multi-exchange conversation, so that count collapsed to 1
    # even though the user and the agent alternated several times. That is the
    # bug this method fixes.
    #
    # Reliable signal: BidiTranscriptStreamEvent carries ``role`` in
    # {"user", "assistant"} with ``is_final`` and a timestamp. We derive
    # conversational turns from that alternation: group consecutive FINAL
    # transcripts of the same role into one segment; each segment is one
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
                # Per-utterance latency: from when the caller last finished
                # speaking (preceding user turn's last final transcript) to this
                # assistant turn's first final transcript. This is derivable for
                # EVERY assistant turn, unlike the shared response window.
                latency_ms: float | None = None
                if prev_user_end_ms is not None and start_ms is not None:
                    latency_ms = round(max(start_ms - prev_user_end_ms, 0.0), 3)

                timing = self._closest_timing(start_ms, assistant_timings, used_timing)
                if timing is not None:
                    seg["ttft_ms"] = timing.get("ttft_ms")
                    seg["ttft_source"] = "response_window"
                    # Prefer the response-window latency when we have it (it is
                    # the user-stop -> response_start measure); else keep the
                    # per-utterance transcript-derived latency.
                    if timing.get("turn_latency_ms") is not None:
                        latency_ms = timing.get("turn_latency_ms")
                else:
                    # No response window left for this turn (Nova reused one
                    # response_id). We cannot derive a true per-utterance first-
                    # token time, so TTFT is honestly unavailable; latency still
                    # comes from the transcript gap above.
                    seg["ttft_ms"] = None
                    seg["ttft_source"] = "unavailable"
                seg["latency_ms"] = latency_ms
            elif seg["speaker"] == "user" and end_ms is not None:
                prev_user_end_ms = end_ms

        self.conversation_turns = turns
        return turns

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
    def non_nova_ms(self) -> float | None:
        """Share of total e2e time not spent in active Nova responses."""
        total = self.total_e2e_ms
        if total is None:
            return None
        return round(max(total - self.nova_total_ms, 0.0), 3)

    def summary(self) -> dict[str, Any]:
        """Compact metrics summary (the headline numbers).

        ``turn_count`` reflects CONVERSATIONAL turns (user<->assistant
        alternation). The old response_id-based count is preserved as
        ``nova_response_count``.
        """
        conversation_turns = self._build_conversation_turns()
        return {
            "call_sid": self.call_sid,
            "account_id": self.account_id,
            "total_e2e_ms": self.total_e2e_ms,
            "nova_total_ms": round(self.nova_total_ms, 3),
            "twilio_latency_ms": self.twilio_latency_ms,
            "non_nova_ms": self.non_nova_ms,
            "ttft_ms": self.first_ttft_ms,
            "turn_count": len(conversation_turns),
            "nova_response_count": len(self.turns),
            "barge_in_count": self.barge_in_count,
            "usage": self.usage,
        }

    def to_call_analytics(self) -> dict[str, Any]:
        """Serializable CallAnalytics record for the store/UI (FEAT-003/004).

        ``turn_count`` is the conversational-turn count; ``nova_response_count``
        preserves the response_id-based count. ``conversation_turns`` carries
        the chat-style user<->assistant model for the UI.
        """
        conversation_turns = self._build_conversation_turns()
        # Post-call intelligence fields (FEAT-002) start BLANK here and are
        # filled asynchronously, OFF the audio path, by app.postcall after the
        # call ends. total_e2e_ms / nova_total_ms / ttft_ms above are computed
        # from session_start..session_end on the audio path only, so the
        # post-call extraction can never inflate those latency numbers.
        return {
            "call_sid": self.call_sid,
            "account_id": self.account_id,
            "total_e2e_ms": self.total_e2e_ms,
            "nova_total_ms": round(self.nova_total_ms, 3),
            "twilio_latency_ms": self.twilio_latency_ms,
            "non_nova_ms": self.non_nova_ms,
            "ttft_ms": self.first_ttft_ms,
            "turn_count": len(conversation_turns),
            "nova_response_count": len(self.turns),
            "barge_in_count": self.barge_in_count,
            "usage": self.usage,
            "conversation_turns": list(conversation_turns),
            "turns": list(self.turns),
            "transcripts": list(self.transcripts),
            "events_log": list(self.events_log),
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


def _tool_name(event: Any) -> Any:
    current = getattr(event, "current_tool_use", None)
    if isinstance(current, dict):
        return current.get("name")
    return getattr(current, "name", None)
