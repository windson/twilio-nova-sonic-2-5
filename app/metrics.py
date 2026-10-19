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

- A "turn" is a matched ``BidiResponseStartEvent`` / ``BidiResponseCompleteEvent``
  pair keyed by ``response_id``. Per-turn duration = complete_time - start_time.
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

        # Turn state keyed by response_id.
        self._open_turns: dict[str, dict[str, Any]] = {}
        self.turns: list[dict[str, Any]] = []

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
        # First audio for the most-recently-started still-open turn => TTFT.
        for turn in self._open_turns.values():
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
        if role == "user" and is_final:
            self._last_user_final_wall = now
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
        """Compact metrics summary (the headline numbers)."""
        return {
            "call_sid": self.call_sid,
            "account_id": self.account_id,
            "total_e2e_ms": self.total_e2e_ms,
            "nova_total_ms": round(self.nova_total_ms, 3),
            "twilio_latency_ms": self.twilio_latency_ms,
            "non_nova_ms": self.non_nova_ms,
            "ttft_ms": self.first_ttft_ms,
            "turn_count": len(self.turns),
            "barge_in_count": self.barge_in_count,
            "usage": self.usage,
        }

    def to_call_analytics(self) -> dict[str, Any]:
        """Serializable CallAnalytics record for the store/UI (FEAT-003/004)."""
        return {
            "call_sid": self.call_sid,
            "account_id": self.account_id,
            "total_e2e_ms": self.total_e2e_ms,
            "nova_total_ms": round(self.nova_total_ms, 3),
            "twilio_latency_ms": self.twilio_latency_ms,
            "non_nova_ms": self.non_nova_ms,
            "ttft_ms": self.first_ttft_ms,
            "turn_count": len(self.turns),
            "barge_in_count": self.barge_in_count,
            "usage": self.usage,
            "turns": list(self.turns),
            "transcripts": list(self.transcripts),
            "events_log": list(self.events_log),
        }


def _tool_name(event: Any) -> Any:
    current = getattr(event, "current_tool_use", None)
    if isinstance(current, dict):
        return current.get("name")
    return getattr(current, "name", None)
