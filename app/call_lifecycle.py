"""Telephone lifecycle, separate from the lifetime of a recoverable Nova stream.

All observer methods are synchronous: they record evidence and wake the owner.
Only the controller's tasks wait for barriers, playback, and bounded cleanup.
There is no application reconnect loop and no alternate speech generator.
"""
from __future__ import annotations

import asyncio
import base64
import logging
import re
import unicodedata
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Awaitable, Callable

from strands.types._events import ToolResultMessageEvent
from strands.types.events import ToolResultEvent

from app.languages import LANGUAGE_NAMES, SUPPORTED_CONVERSATION_LANGUAGES, closing_for_language

logger = logging.getLogger(__name__)
TERMINAL_TOOLS = frozenset({"end_conversation", "begin_farewell"})
_RETAINED: set[asyncio.Task[Any]] = set()


def retain(task: asyncio.Task[Any]) -> asyncio.Task[Any]:
    """Observe work that cannot truthfully be cancelled with its awaiter."""
    _RETAINED.add(task)

    def completed(done: asyncio.Task[Any]) -> None:
        _RETAINED.discard(done)
        if not done.cancelled():
            error = done.exception()
            if error is not None:
                logger.warning("retained lifecycle work failed (%s)", type(error).__name__)

    task.add_done_callback(completed)
    return task


class CallState(str, Enum):
    ACTIVE = "ACTIVE"
    SETTLING_TOOLS = "SETTLING_TOOLS"
    PREPARING_FAREWELL = "PREPARING_FAREWELL"
    WAITING_FOR_PLAYBACK = "WAITING_FOR_PLAYBACK"
    COMMITTING = "COMMITTING"
    CLOSED = "CLOSED"


@dataclass(frozen=True)
class TerminationBounds:
    tools: float = 20.0
    generation: float = 15.0
    playback: float = 30.0
    sdk_cleanup: float = 5.0
    twilio_request: float = 10.0


@dataclass
class _Tool:
    name: str
    group: int
    received_sequence: int = 0
    started: bool = False
    completed: bool = False
    status: str | None = None
    recorded: bool = False
    submitted: bool = False


@dataclass
class _Attempt:
    id: str
    reason: str
    generation: int
    activity: int
    end_id: str
    unsettled_ids: set[str]
    failed_writes_before: int
    requested_at: float
    language: str
    closing_greeting: str
    closing_phrase: str
    begin_id: str | None = None
    begin_ready: bool = False
    grant_sequence: int | None = None
    response: str | None = None
    audio: set[str] = field(default_factory=set)
    stopped_audio: set[str] = field(default_factory=set)
    open_audio: set[str] = field(default_factory=set)
    open_transcripts: set[str] = field(default_factory=set)
    transcript_confirmed: bool = False
    response_complete: bool = False
    invalid_reason: str | None = None
    recovery_sent: bool = False


@dataclass
class _PlaybackMark:
    name: str
    attempt_id: str
    generation: int
    revision: int
    sent: bool = False
    acknowledged: bool = False


def _normalized(text: str) -> str:
    # Transcript punctuation, whitespace and accent rendering can vary. This
    # accepts Adiós/Adios without broadening one attempt to other languages.
    text = "".join(char for char in unicodedata.normalize("NFKD", text.casefold())
                   if not unicodedata.combining(char))
    # Preserve word boundaries: 'Adios' must not match inside 'estadios'.
    return re.sub(r"[\W_]+", " ", text).strip()


def _contains_closing_phrase(text: str, phrase: str) -> bool:
    normalized_phrase = _normalized(phrase).replace(" ", "")
    if not normalized_phrase:
        return False
    # Retain the existing GOOD - BYE normalization, but require the whole
    # phrase to begin and end at word boundaries (Adios != estadios).
    pattern = r"(?<!\w)" + r"\s*".join(re.escape(char) for char in normalized_phrase) + r"(?!\w)"
    return bool(re.search(pattern, _normalized(text)))


def _error(reason: str) -> dict[str, Any]:
    return {"status": "error", "ready": False, "reason": reason,
            "content": [{"text": "Closing was not confirmed. Continue listening and respond to the caller. "
                         "Do not claim the call ended or repeat a farewell automatically."}]}


class CallLifecycleController:
    """One owner for tools, SDK/reader supervision, telephone end and persistence."""

    def __init__(
        self, *, call_sid: str, stream_sid: str, runtime: Any, metrics: Any,
        hangup: Callable[[], Awaitable[dict[str, Any]]],
        persist: Callable[[bool], Awaitable[None]],
        close_websocket: Callable[[], Awaitable[None]],
        bounds: TerminationBounds | None = None,
        closing_greeting: str = "Thank you for your time. Goodbye.",
        closing_phrase: str = "Goodbye",
    ) -> None:
        self.call_sid, self.stream_sid = call_sid, stream_sid
        self.runtime, self.metrics = runtime, metrics
        self._hangup, self._persist, self._close_websocket = hangup, persist, close_websocket
        self.bounds = bounds or TerminationBounds()
        self.closing_greeting, self.closing_phrase = closing_greeting, closing_phrase
        self.session: Any = None
        self.state = CallState.ACTIVE
        self.generation = 0
        self.activity = 0
        self.connection_ready = False
        self.transport_open = True
        self.tool_admission_open = True
        self.attempt_count = 0
        self.attempt: _Attempt | None = None
        self.tools: dict[str, _Tool] = {}
        self._retired_tools: dict[str, _Tool] = {}
        self._groups = 0
        self._sequence = 0
        self._received: dict[int, tuple[int, Any]] = {}
        self._stale_received: dict[int, Any] = {}
        self._pending_output: set[int] = set()
        self._early_activity: set[int] = set()
        self._response: str | None = None
        self._transcripts: dict[str, dict[str, Any]] = {}
        self._revision = 0
        self._mark: _PlaybackMark | None = None
        self._changed = asyncio.Event()
        self.closed = asyncio.Event()
        self.sdk_task: asyncio.Task[Any] | None = None
        self.reader_task: asyncio.Task[Any] | None = None
        self._attempt_task: asyncio.Task[Any] | None = None
        self._recovery_task: asyncio.Task[Any] | None = None
        self._close_task: asyncio.Task[Any] | None = None
        self._finalization_task: asyncio.Task[Any] | None = None
        self._started = asyncio.get_running_loop().time()
        self.summary: dict[str, Any] = {"outcome": "active", "reason": None,
            "attempt_count": 0, "telephone_completion_confirmed": False,
            "timestamps": {"active": self._utc()}, "transitions": []}
        runtime.lifecycle = self
        metrics.on_session_start()

    @staticmethod
    def _utc() -> str:
        return datetime.now(timezone.utc).isoformat()

    def bind_session(self, session: Any) -> None:
        self.session = session

    def _record(self, event: str, **fields: Any) -> None:
        self.metrics.record_lifecycle_event(event, **fields)

    def _transition(self, state: CallState) -> None:
        self.state = state
        now = self._utc()
        self.summary["timestamps"][state.value.lower()] = now
        self.summary["transitions"].append({"state": state.value, "at": now,
                                             "attempt": self.attempt_count})
        self._record("call_lifecycle", state=state.value, attempt=self.attempt_count)
        self._changed.set()

    def writes_changed(self) -> None:
        self._changed.set()

    def accept_output(self, event: Any) -> bool:
        """Fence superseded provider audio before it reaches the telephone."""
        return (self.tool_admission_open and self.state != CallState.CLOSED
                and self._stale_received.get(id(event)) is not event)

    def output_discarded(self, event: Any) -> None:
        """Consume a provider stamp without claiming unsent audio was played."""
        self._received.pop(id(event), None)
        self._stale_received.pop(id(event), None)
        self._pending_output.discard(id(event))
        self._early_activity.discard(id(event))
        self._changed.set()

    def observe_provider_event(self, event: Any) -> None:
        """Called before yield: queued old audio must not satisfy a later grant."""
        self._sequence += 1
        self._received[id(event)] = (self._sequence, event)
        kind = event.get("type")
        if kind == "bidi_tool_use_blocks":
            self.register_tool_group(event.tool_uses)
        elif kind == "bidi_barge_in" or (kind == "bidi_transcript_start" and event.get("role") == "user"):
            self._early_activity.add(id(event))
            self._caller_activity(kind)
        elif kind in {"bidi_connection_start", "bidi_connection_restart"}:
            self._abort("connection_restart")
            if self._recovery_task:
                self._recovery_task.cancel()
        if kind in {"bidi_response_start", "bidi_audio_start", "bidi_audio_delta"} or (
            kind in {"bidi_transcript_start", "bidi_transcript_delta"} and event.get("role") == "assistant"
        ):
            self._pending_output.add(id(event))
            self._new_output()
            if kind == "bidi_response_start":
                self._check_response(event.get("response_id"))

    def register_tool_group(self, tool_uses: list[dict[str, Any]]) -> None:
        self._groups += 1
        new_business = False
        for use in tool_uses:
            tool_id, name = use["toolUseId"], use["name"]
            if tool_id in self.tools or tool_id in self._retired_tools:
                self._abort("reused_tool_identity")
                continue
            self.tools[tool_id] = _Tool(name=name, group=self._groups, received_sequence=self._sequence)
            new_business |= name not in TERMINAL_TOOLS
        if self.attempt is not None and new_business:
            self._abort("new_tool_operation")
        self._changed.set()

    def tool_started(self, tool_id: str) -> None:
        tool = self.tools.get(tool_id)
        if tool is None and tool_id in self._retired_tools:
            tool = self.tools[tool_id] = self._retired_tools.pop(tool_id)
            if tool.name not in TERMINAL_TOOLS:
                self._abort("resumed_tool_operation")
        if tool:
            tool.started = True
        self._changed.set()

    def _retire_previous_connection(self, sequence: int) -> None:
        # The SDK drops superseded reader events before the output callback.
        # Keep object identity for any late deliveries without leaving them in
        # the new generation's playback barrier (or risking Python id reuse).
        for identity, (received_sequence, event) in tuple(self._received.items()):
            if received_sequence < sequence:
                self._stale_received[identity] = event
                self._received.pop(identity)
                self._pending_output.discard(identity)
                self._early_activity.discard(identity)
        for identity, tool in tuple(self.tools.items()):
            if tool.received_sequence < sequence and not tool.started and not tool.completed and not tool.submitted:
                self._retired_tools[identity] = self.tools.pop(identity)

    def results_submitted(self, tool_use_ids: list[str]) -> None:
        """A successful public model.send, distinct from SDK history recording."""
        self._sequence += 1
        for tool_id in tool_use_ids:
            self.tool_started(tool_id)
            if tool_id in self.tools:
                self.tools[tool_id].submitted = True
        attempt = self.attempt
        if attempt and attempt.begin_ready and attempt.begin_id in tool_use_ids and attempt.grant_sequence is None:
            attempt.grant_sequence = self._sequence
            self._record("farewell_readiness_submitted", attempt=self.attempt_count)
        self._changed.set()

    def observe_event(self, event: Any) -> None:
        """SDK output callback. Never await teardown, tool settlement, or playback."""
        if self._stale_received.pop(id(event), None) is event:
            return
        receipt = self._received.pop(id(event), None)
        sequence = receipt[0] if receipt is not None else None
        self._pending_output.discard(id(event))
        if sequence is None:
            self._sequence += 1
            sequence = self._sequence
        kind = event.get("type")
        if isinstance(event, ToolResultEvent):
            result = event.tool_result
            self.tool_started(result.get("toolUseId"))
            tracked = self.tools.get(result.get("toolUseId"))
            if tracked:
                tracked.completed, tracked.status = True, result.get("status")
        elif isinstance(event, ToolResultMessageEvent):
            for block in event.get("message", {}).get("content", []):
                tool_id = block.get("toolResult", {}).get("toolUseId")
                self.tool_started(tool_id)
                tracked = self.tools.get(tool_id)
                if tracked:
                    tracked.recorded = True
        elif kind in {"bidi_connection_restart", "bidi_connection_start"}:
            self._abort("connection_restart")
            self.generation += 1
            self.connection_ready = kind == "bidi_connection_start"
            if self.connection_ready:
                self._retire_previous_connection(sequence)
            self._response = None
            self._transcripts.clear()
            self._mark = None
        elif kind in {"bidi_barge_in", "bidi_transcript_start"} and (
            kind == "bidi_barge_in" or event.get("role") == "user"
        ):
            if id(event) in self._early_activity:
                self._early_activity.discard(id(event))
            else:
                self._caller_activity(kind)
            if kind == "bidi_barge_in":
                self._response = None
                self._transcripts.clear()
        elif kind == "bidi_response_start":
            self._check_response(event.get("response_id"))
            self._response = event.get("response_id")
            if self.attempt:
                self.attempt.response_complete = False
            self._new_output()
        elif kind == "bidi_transcript_start" and event.get("role") == "assistant":
            if self.attempt:
                self.attempt.open_transcripts.add(event["content_id"])
                self.attempt.transcript_confirmed = False
                self.attempt.response_complete = False
            self._transcripts[event["content_id"]] = {
                "response": self._response, "after_grant": "", "sequence": sequence}
            self._new_output()
        elif kind == "bidi_transcript_delta" and event.get("role") == "assistant":
            transcript = self._transcripts.get(event["content_id"])
            attempt = self.attempt
            if transcript and attempt and attempt.grant_sequence is not None and sequence > attempt.grant_sequence:
                transcript["after_grant"] += event.get("delta", "")
                if event.get("delta") and attempt.response is None:
                    attempt.response = transcript["response"]
            self._new_output()
        elif kind == "bidi_transcript_block" and event.get("role") == "assistant":
            transcript = self._transcripts.pop(event["content_id"], None)
            attempt = self.attempt
            if attempt:
                attempt.open_transcripts.discard(event["content_id"])
            if attempt and transcript and transcript["response"] == attempt.response:
                attempt.transcript_confirmed = (
                    _contains_closing_phrase(transcript["after_grant"], attempt.closing_phrase)
                    and _contains_closing_phrase(event.get("transcript", ""), attempt.closing_phrase))
        elif kind == "bidi_audio_start":
            self._new_output()
            if self.attempt:
                self.attempt.open_audio.add(event.get("content_id"))
                self.attempt.response_complete = False
                self.attempt.stopped_audio.discard(event.get("content_id"))
        elif kind == "bidi_audio_delta":
            self._new_output()
            attempt = self.attempt
            if attempt and attempt.grant_sequence is not None and sequence > attempt.grant_sequence:
                try:
                    nonempty = bool(base64.b64decode(event.get("audio", ""), validate=True))
                except (ValueError, TypeError):
                    nonempty = False
                if nonempty:
                    if not self._response or not event.get("content_id"):
                        self._abort("unidentified_closing_audio")
                    elif attempt.response is not None and attempt.response != self._response:
                        self._abort("ambiguous_closing_response")
                    else:
                        attempt.response = self._response
                        attempt.audio.add(event["content_id"])
                        attempt.open_audio.add(event["content_id"])
                        attempt.stopped_audio.discard(event["content_id"])
                        attempt.response_complete = False
        elif kind == "bidi_audio_stop":
            if self.attempt:
                self.attempt.open_audio.discard(event.get("content_id"))
                if event.get("content_id") in self.attempt.audio:
                    self.attempt.stopped_audio.add(event["content_id"])
        elif kind == "bidi_response_stop":
            if self.attempt and event.get("response_id") == self.attempt.response:
                self.attempt.response_complete = True
            if event.get("response_id") == self._response:
                self._response = None
        self._changed.set()

    def _caller_activity(self, kind: str) -> None:
        if self._recovery_task:
            self._recovery_task.cancel()
        self.activity += 1
        self._record("caller_activity", signal=kind, state=self.state.value,
                     playback_pending=self._mark is not None,
                     since_start_ms=round((asyncio.get_running_loop().time() - self._started) * 1000, 3))
        self._abort("caller_interruption")
        self._mark = None

    def _check_response(self, response_id: str | None) -> None:
        if self.attempt and self.attempt.response and self.attempt.response != response_id:
            self._abort("new_response_during_farewell")

    def _new_output(self) -> None:
        self._revision += 1
        self._mark = None
        self._changed.set()

    def _valid(self, attempt: _Attempt) -> bool:
        return (self.attempt is attempt and attempt.invalid_reason is None and self.transport_open
                and self.tool_admission_open and self.connection_ready
                and attempt.generation == self.generation and attempt.activity == self.activity)

    def _abort(self, reason: str, expected: _Attempt | None = None) -> bool:
        if expected is not None and self.attempt is not expected:
            return False
        if self.state in {CallState.COMMITTING, CallState.CLOSED}:
            return False
        attempt = self.attempt
        if attempt is not None:
            attempt.invalid_reason = reason
            self.attempt = None
            self.summary.update(outcome="resumed", reason=reason)
            self._record("farewell_aborted", reason=reason, attempt=self.attempt_count)
            self._transition(CallState.ACTIVE)
        self._mark = None
        self._changed.set()
        return attempt is not None

    async def request_end(
        self, reason: str | None = None, tool_use_id: str | None = None, language: str = "en",
    ) -> dict[str, Any]:
        if not self.tool_admission_open or not self.transport_open:
            return _error("call_closing")
        if language not in SUPPORTED_CONVERSATION_LANGUAGES:
            return _error("unsupported_conversation_language")
        if self.attempt is not None:
            return {"status": "pending", "ready": False, "state": self.state.value,
                    "attempt_id": self.attempt.id,
                    "language": self.attempt.language,
                    "instruction": "An ending attempt already exists; await its result and do not repeat tools or greetings."}
        if not tool_use_id or tool_use_id not in self.tools or not self.connection_ready:
            return _error("unregistered_end_request")
        greeting, phrase = closing_for_language(language,
            english_greeting=self.closing_greeting, english_phrase=self.closing_phrase)
        self.attempt_count += 1
        attempt = _Attempt(id=uuid.uuid4().hex, reason=(reason or "conversation_complete")[:200],
            generation=self.generation, activity=self.activity, end_id=tool_use_id,
            unsettled_ids={key for key, value in self.tools.items()
                           if value.name not in TERMINAL_TOOLS and not value.submitted},
            failed_writes_before=self.runtime.failed_write_count,
            requested_at=asyncio.get_running_loop().time(), language=language,
            closing_greeting=greeting, closing_phrase=phrase)
        self.attempt = attempt
        self.summary["attempt_count"] = self.attempt_count
        self.summary["closing_language"] = language
        self._record("closing_language_selected", language=language, attempt=self.attempt_count)
        self._transition(CallState.SETTLING_TOOLS)
        deadline = asyncio.get_running_loop().time() + self.bounds.tools
        try:
            while self._valid(attempt):
                self._changed.clear()
                settled = all(tool.completed for tool in self.tools.values() if tool.name not in TERMINAL_TOOLS)
                if settled and self.runtime.pending_write_count == 0:
                    if (any(self.tools[key].status == "error" for key in attempt.unsettled_ids)
                            or self.runtime.failed_write_count > attempt.failed_writes_before):
                        self._abort("tool_write_failed", attempt)
                        return _error("tool_write_failed")
                    self._transition(CallState.PREPARING_FAREWELL)
                    self._attempt_task = retain(asyncio.create_task(self._watch_farewell(attempt), name="farewell-handshake"))
                    return {"status": "ready", "ready": True, "attempt_id": attempt.id, "language": attempt.language,
                            "instruction": "Call begin_farewell with this attempt_id. Its ready result supplies "
                                           "the authorized farewell. Keep the attempt ID internal."}
                await self._wait_change(deadline)
        except TimeoutError:
            if self._abort("tool_settlement_timeout", attempt):
                self._recover(attempt)
        except asyncio.CancelledError:
            self._abort("end_request_cancelled", attempt)
            raise
        return _error(attempt.invalid_reason or "closing_invalidated")

    async def begin_farewell(self, attempt_id: str, tool_use_id: str | None = None) -> dict[str, Any]:
        attempt = self.attempt
        if not attempt or attempt.id != attempt_id or not self._valid(attempt):
            return _error("invalid_attempt")
        current_tool = self.tools.get(tool_use_id or "")
        if not current_tool or current_tool.name != "begin_farewell" or attempt.begin_id is not None:
            return _error("farewell_nonce_already_used_or_unregistered")
        if self.state != CallState.PREPARING_FAREWELL:
            return _error("farewell_not_prepared")
        # Reserve before the first await so concurrent begin requests cannot
        # both consume the nonce. Reservation is NOT permission to speak.
        attempt.begin_id = tool_use_id
        prior = [tool for tool in self.tools.values() if tool.group != current_tool.group]
        deadline = asyncio.get_running_loop().time() + self.bounds.generation
        try:
            while self._valid(attempt):
                self._changed.clear()
                # SDK output can lag a successful send. Wait for PRIOR results
                # to be recorded, never for this native group's own completion.
                if (self.runtime.pending_write_count == 0
                        and all(tool.completed and tool.recorded and tool.submitted for tool in prior)):
                    attempt.begin_ready = True
                    return {"status": "ready", "ready": True, "language": attempt.language,
                            "instruction": f"Now say exactly: {attempt.closing_greeting} Then listen while playback finishes."}
                await self._wait_change(deadline)
        except TimeoutError:
            if self._abort("farewell_barrier_timeout", attempt):
                self._recover(attempt)
        except asyncio.CancelledError:
            self._abort("farewell_request_cancelled", attempt)
            raise
        return _error(attempt.invalid_reason or "closing_invalidated")

    def _farewell_complete(self, attempt: _Attempt) -> bool:
        return (self._valid(attempt) and attempt.grant_sequence is not None and bool(attempt.audio)
                and not attempt.open_audio and not attempt.open_transcripts
                and attempt.audio <= attempt.stopped_audio and attempt.response_complete
                and attempt.transcript_confirmed and not self._pending_output and self.runtime.pending_write_count == 0
                and all(tool.completed and tool.recorded and tool.submitted for tool in self.tools.values()))

    async def _wait_change(self, deadline: float) -> None:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise TimeoutError
        await asyncio.wait_for(self._changed.wait(), remaining)

    async def _watch_farewell(self, attempt: _Attempt) -> None:
        generation_deadline = asyncio.get_running_loop().time() + self.bounds.generation
        playback_deadline: float | None = None
        try:
            while self._valid(attempt):
                self._changed.clear()
                if self._farewell_complete(attempt):
                    if playback_deadline is None:
                        playback_deadline = asyncio.get_running_loop().time() + self.bounds.playback
                        self._transition(CallState.WAITING_FOR_PLAYBACK)
                        self._changed.clear()
                    if self._mark is None:
                        mark = _PlaybackMark(name=f"farewell-{self.generation}-{attempt.id}-{uuid.uuid4().hex}",
                                             attempt_id=attempt.id, generation=self.generation, revision=self._revision)
                        self._mark = mark
                        await self._bounded(self.session.send_playback_mark(mark.name),
                                            max(0.001, playback_deadline - asyncio.get_running_loop().time()),
                                            "twilio-playback-mark")
                        mark.sent = True
                        self._maybe_commit(mark)
                    if not self._valid(attempt):
                        return
                await self._wait_change(playback_deadline or generation_deadline)
        except TimeoutError:
            if self._abort("playback_timeout" if playback_deadline else "closing_generation_timeout", attempt):
                self._recover(attempt)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._record("farewell_transport_failure", error_type=type(exc).__name__)
            self.start_close(reason="playback_transport_failure", outcome="technical_failure")

    def acknowledge_playback(self, name: str, stream_sid: str | None) -> bool:
        mark = self._mark
        if not mark or name != mark.name or stream_sid != self.stream_sid:
            return False
        mark.acknowledged = True
        self._maybe_commit(mark)
        self._changed.set()
        return True

    def _maybe_commit(self, mark: _PlaybackMark) -> None:
        attempt = self.attempt
        if (mark is self._mark and mark.sent and mark.acknowledged and attempt
                and mark.attempt_id == attempt.id and mark.generation == self.generation
                and mark.revision == self._revision and self._farewell_complete(attempt)):
            self.summary["closing_handshake_ms"] = round(
                (asyncio.get_running_loop().time() - attempt.requested_at) * 1000, 3)
            self._record("farewell_playback_confirmed", attempt=self.attempt_count)
            # No await between the last evidence check and closing admission.
            self.start_close(reason=attempt.reason, outcome="farewell_played")

    def _recover(self, attempt: _Attempt) -> None:
        if attempt.recovery_sent or not self.transport_open or not self.tool_admission_open:
            return
        attempt.recovery_sent = True
        async def prompt() -> None:
            # Recheck after scheduling; never speak over a subsequent caller.
            if (self.activity != attempt.activity or self.state != CallState.ACTIVE
                    or self.generation != attempt.generation):
                return
            await asyncio.wait_for(self.session.send_recovery_prompt(
                "The closing attempt timed out and the call remains connected. "
                f"Respond in {LANGUAGE_NAMES[attempt.language]}. "
                "Briefly tell the caller you are still here, then listen. Do not say goodbye "
                "or call either closing tool until a new caller request."), self.bounds.generation)
        self._recovery_task = retain(asyncio.create_task(prompt(), name="nova-closing-recovery"))

    def transport_closed(self) -> None:
        self.transport_open = False
        self._mark = None
        if self.session is not None:
            self.session.notify_transport_closed()
        self._changed.set()

    def start_close(self, *, reason: str, outcome: str) -> asyncio.Task[Any]:
        """Converge every terminal path onto one operation, without awaiting it."""
        if self._close_task is None:
            self.tool_admission_open = False
            self.runtime.close_admission()
            if self.session is not None:
                self.session.close_output()
            self._mark = None
            self.summary.update(reason=reason, outcome=outcome)
            self._transition(CallState.COMMITTING)
            self._close_task = retain(asyncio.create_task(self._finish(), name="telephone-cleanup"))
        return self._close_task

    async def _bounded(self, awaitable: Awaitable[Any], timeout: float, name: str) -> Any:
        task = retain(asyncio.create_task(awaitable, name=name))
        return await asyncio.wait_for(asyncio.shield(task), timeout)

    async def _finish(self) -> None:
        confirmed = False
        try:
            cleanup_tasks: set[asyncio.Task[Any]] = set()
            for task in (self._attempt_task, self._recovery_task):
                if task and task is not asyncio.current_task():
                    task.cancel()
                    cleanup_tasks.add(task)
            if self.session is not None:
                cleanup_tasks.add(retain(asyncio.create_task(self.session.close(), name="sdk-cleanup")))
            if cleanup_tasks:
                done, pending = await asyncio.wait(cleanup_tasks, timeout=self.bounds.sdk_cleanup)
                if pending:
                    self.summary["sdk_cleanup"] = "timeout_work_retained"
                    self._record("sdk_cleanup_timeout")
                for task in done:
                    if not task.cancelled() and task.exception() is not None:
                        self.summary["sdk_cleanup"] = "failed"
                        self._record("sdk_cleanup_failed", error_type=type(task.exception()).__name__)
            try:
                result = await self._bounded(self._hangup(), 3 * self.bounds.twilio_request + 1, "telephone-hangup")
                confirmed = result.get("confirmed_terminal") is True
                self.summary["telephone_status"] = result.get("status")
                self.summary["hangup_outcome"] = result.get("outcome", "unknown")
                self.summary["hangup_attempts"] = result.get("attempts", 0)
                self.summary["hangup_request_pending"] = result.get("pending_request", False)
            except Exception as exc:
                self.summary["hangup_outcome"] = "unknown"
                self._record("telephone_hangup_unconfirmed", error_type=type(exc).__name__)
            self.summary["telephone_completion_confirmed"] = confirmed
            if not confirmed:
                self.summary["requested_outcome"] = self.summary["outcome"]
                self.summary["outcome"] = "telephone_completion_unresolved"
            else:
                self.summary["timestamps"]["telephone_completed"] = self._utc()
            # Freeze telephone duration now, before outstanding worker/persistence
            # time. Later workers still update their true execution outcomes.
            self.metrics.on_session_end()
        finally:
            self.transport_closed()
            try:
                await self._bounded(self._close_websocket(), self.bounds.sdk_cleanup, "websocket-close")
            except Exception as exc:
                self._record("websocket_cleanup_unconfirmed", error_type=type(exc).__name__)
            self._transition(CallState.CLOSED)
            self.metrics.termination = self.summary
            self._finalization_task = retain(asyncio.create_task(self._finish_storage(confirmed), name="call-finalization"))
            # Bounded handler cleanup, retained storage work. A timeout never
            # asserts that a running SDK write was cancelled or rolled back.
            try:
                await asyncio.wait_for(asyncio.shield(self._finalization_task), self.bounds.tools)
            except TimeoutError:
                self._record("call_finalization_pending", pending_writes=self.runtime.pending_write_count)
            finally:
                self.closed.set()

    async def _finish_storage(self, confirmed: bool) -> None:
        await self.runtime.drain(close=True)
        self.summary["timestamps"]["writes_settled"] = self._utc()
        await self._persist(confirmed)

    async def run(self, reader: Awaitable[None]) -> None:
        """Supervise peers: an unexpected SDK exit cannot leave a silent phone."""
        self.sdk_task = self.session.start()
        self.reader_task = asyncio.create_task(reader, name="twilio-reader")
        closed_waiter = asyncio.create_task(self.closed.wait(), name="telephone-closed-wait")
        try:
            done, _ = await asyncio.wait({self.sdk_task, self.reader_task, closed_waiter},
                                         return_when=asyncio.FIRST_COMPLETED)
            if self._close_task is None:
                if self.sdk_task in done:
                    if not self.sdk_task.cancelled() and self.sdk_task.exception() is not None:
                        self._record("sdk_run_failed", error_type=type(self.sdk_task.exception()).__name__)
                    self.start_close(reason="sdk_unexpected_exit", outcome="technical_failure")
                else:
                    self.transport_closed()
                    failed = not self.reader_task.cancelled() and self.reader_task.exception() is not None
                    self.start_close(reason="websocket_failure" if failed else "caller_disconnect",
                                     outcome="technical_failure" if failed else "caller_disconnected")
            await asyncio.shield(self._close_task)
        finally:
            if self._close_task is None:
                self.start_close(reason="handler_cancelled", outcome="technical_failure")
            await asyncio.shield(self._close_task)
            closed_waiter.cancel()
            self.reader_task.cancel()
            retain(self.reader_task)
            await asyncio.wait({closed_waiter, self.reader_task}, timeout=self.bounds.sdk_cleanup)
