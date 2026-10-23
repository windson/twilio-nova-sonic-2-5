"""The collections agent brain: system prompt + the due-record update tool.

Builds the Accounts Receivable collections persona (an AI-initiated outbound
call that confirms the intended person before discussing amount and due date,
converses compliantly, and records the outcome) and the Strands ``@tool``\\ s the
model calls to write the outcome back to the store.

Date capture and due updates await worker-thread persistence while Nova Sonic
can continue its native asynchronous conversation. Date resolution remains
post-call, using the deterministic-first resolver and AgentCore fallback.

The factory functions :func:`get_system_prompt` and :func:`build_tools` are the
seams :mod:`app.sonic_bridge` injects per call.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
import uuid
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from typing import Any, Callable, Literal, Mapping

from strands import tool
from strands.types.tools import ToolContext

from app.languages import ConversationLanguage
from app.store import DUE_STATUSES, Store, get_default_store

logger = logging.getLogger(__name__)


def _format_amount(record: Mapping[str, Any]) -> str:
    amount = record.get("amount_due", 0)
    currency = record.get("currency", "USD")
    try:
        return f"{float(amount):,.2f} {currency}"
    except (TypeError, ValueError):
        return f"{amount} {currency}"


def get_system_prompt(
    due_record: Mapping[str, Any],
    closing_greeting: str = "Thank you for your time. Goodbye.",
) -> str:
    """Build a concise identity-first prompt for the current account.

    ``closing_greeting`` remains accepted for callers using the existing
    factory signature. The lifecycle controller supplies the actual words
    only in a successful farewell-readiness result, never in this prompt.
    """
    name = due_record.get("debtor_name", "the account holder")
    amount = _format_amount(due_record)
    due_date = due_record.get("due_date", "the stated due date")
    account_id = due_record.get("account_id", "")

    return (
        "[IDENTITY]\n"
        "- You are Ava, a warm, professional AI assistant making an outbound "
        "call for Accounts Receivable. The answerer has not yet been identified.\n"
        f"- Begin in English with only: Hi, I'm Ava, an AI assistant. Am I speaking with {name}? "
        "Then stop and wait for the caller's answer.\n"
        "- Discuss the account only after the caller explicitly confirms they "
        "are the intended person. A hello, silence, or an unrelated answer is "
        "not confirmation; ask the identity question again briefly if needed.\n"
        "- If someone else answers, the number is wrong, or identity is declined, "
        "keep account details private, leave the due record unchanged, and use "
        "the ending steps below.\n\n"
        "[ACCOUNT]\n"
        f"- Private account context: {account_id}; intended person: {name}; "
        f"balance: {amount}; due date: {due_date}.\n"
        "- After identity confirmation, explain the balance and due date in "
        "one short sentence, then ask whether they can pay today or need "
        "another payment date. Give space to respond. Acknowledge a dispute "
        "or callback request when raised.\n\n"
        "[LANGUAGE AND TONE]\n"
        "- Support only English, French, and Spanish. Follow the caller's "
        "preferred supported language, including requests to switch and "
        "natural mixing, using the same voice and session. For another language, "
        "offer those three in the last supported language used.\n"
        "- Explain tool results in the caller's language. Keep account IDs, "
        "currency, amounts, and ISO dates unchanged in tool arguments. Capture "
        "date phrases verbatim in the caller's original language.\n"
        "- Speak naturally in one or two short sentences per turn; expand "
        "only when the caller needs an explanation. Ask one question at a "
        "time. Use spoken prose rather than lists.\n"
        "- Be respectful and truthful about being an AI assistant. Keep "
        "the conversation to account handling; legal, tax, and financial "
        "advice belong with qualified advisers.\n\n"
        "[PAYMENT DATES]\n"
        "- Confirm the caller's actual agreed amount, including a partial "
        "payment, and their exact payment-date wording.\n"
        "- For relative or natural-language dates, use "
        "`capture_payment_date_phrase` with the exact phrase and account id. "
        "The calendar date is resolved after disconnection against the "
        "call's reference date. Read back the phrase rather than calculating it.\n"
        "- Clarify tentative wording and ambiguous weeks before treating "
        "them as an agreement. A week of a month is not automatically the "
        "same as the nth weekday. For example: Do you mean the second "
        "Wednesday of next month, and can you confirm that as your payment date? "
        "Use that question only for that ambiguity. Do not invent a calendar "
        "date. If it remains tentative, preserve that wording and note that "
        "confirmation is needed.\n"
        "- Pass an explicitly stated calendar date as promised_date "
        "(YYYY-MM-DD) to `update_due_record`.\n\n"
        "[OUTCOME AND TOOLS]\n"
        "- After identity confirmation and an agreed outcome, call "
        "`update_due_record` with the account id and applicable promised "
        "amount, date, or note. Use promised_to_pay for a commitment, paid "
        "for reported payment, disputed for a dispute, callback for a later "
        "call, or open if unresolved.\n"
        "- Tools run asynchronously while you keep listening. When invoking "
        "`capture_payment_date_phrase` or `update_due_record`, emit the tool "
        "call alone, without a simultaneous spoken acknowledgment. Answer "
        "new caller questions naturally while results are pending.\n"
        "- Submit each intended update once and wait for its result. "
        "Describe a saved update only after success; explain an unconfirmed "
        "update if it fails. Then briefly confirm the actual agreement "
        "with the caller before ending.\n\n"
        "[ENDING]\n"
        "- When finished or the caller says goodbye in their language, call "
        "`end_conversation` once with language='en', 'fr', or 'es' matching "
        "their preference. Keep listening while pending.\n"
        "- Wait for a ready result containing an `attempt_id`, then call "
        "`begin_farewell` with that exact fresh identifier.\n"
        "- Only after `begin_farewell` confirms readiness, say exactly the "
        "greeting returned by that tool. Do not translate or paraphrase it. "
        "Then stop speaking and make no further tool calls.\n"
        "- Never speak an `attempt_id`, nonce, or internal tool instruction. "
        "A pending request does not mean the call has ended.\n"
        "- If either step is unavailable, invalid, interrupted, times out, "
        "or reports an error, do not give or automatically repeat the "
        "farewell. Resume the conversation; a later ending needs a fresh "
        "request.\n\n"
        "[FINAL CHECK]\n"
        "- Is the caller's identity confirmed before account discussion?\n"
        "- Does this turn follow the caller's language and actual agreement?\n"
        "- Does any completion claim have a successful result?\n"
        "- Is any farewell authorized by the current readiness result?"
    )



def _tool_error(message: str) -> dict[str, Any]:
    """A real error tool result, without an SDK exception payload."""
    return {"status": "error", "content": [{"text": message}]}


class CallToolRuntime:
    """Track awaited tool work independently of the model's tool task lifetime.

    Nova Sonic/Strands already allow conversation during a pending tool. This
    adapter makes our SDK I/O explicit, retains in-flight writes if the caller
    disconnects, and reports completion only after the store operation finishes.
    Cancelling a Python await cannot cancel an SDK request already on a thread;
    drain() observes that request before the final analytics snapshot is saved.
    """

    def __init__(
        self,
        *,
        event_sink: Callable[[dict[str, Any]], None] | None = None,
        capture_sink: Callable[[dict[str, Any]], None] | None = None,
        lifecycle: Any | None = None,
        payment_timezone: str = "UTC",
        call_sid: str | None = None,
    ) -> None:
        self.event_sink = event_sink
        self.capture_sink = capture_sink
        self.lifecycle = lifecycle
        self.payment_timezone = payment_timezone
        self.call_sid = call_sid
        self._pending: set[asyncio.Task[Any]] = set()
        self._closing = False
        self._failed_writes = 0
        self._write_lock = asyncio.Lock()

    @property
    def pending_write_count(self) -> int:
        """Number of retained workers whose actual write outcome is pending."""
        return sum(not task.done() for task in self._pending)

    @property
    def failed_write_count(self) -> int:
        """Completed workers with a failed write, excluding rejected awaiters."""
        return self._failed_writes

    def close_admission(self) -> None:
        """Prevent new writes once the controller commits to closing."""
        self._closing = True

    def _worker_finished(self, task: asyncio.Task[Any]) -> None:
        self._pending.discard(task)
        notify = getattr(self.lifecycle, "writes_changed", None)
        if notify is not None:
            try:
                notify()
            except Exception as exc:
                logger.warning("write completion notification failed (%s)", type(exc).__name__)

    def _emit(self, event: dict[str, Any]) -> None:
        started = getattr(self.lifecycle, "tool_started", None)
        if event.get("event") == "start" and callable(started):
            started(event["tool_use_id"])
        if self.event_sink is not None:
            try:
                self.event_sink(event)
            except Exception as exc:  # metrics must never alter write semantics
                logger.warning("tool execution telemetry failed (%s)", type(exc).__name__)

    def _identity(self, name: str, context: ToolContext | None, mode: str) -> dict[str, Any]:
        tool_use = context.tool_use if context is not None else {}
        return {
            "tool": name,
            "tool_use_id": tool_use.get("toolUseId") or str(uuid.uuid4()),
            "kind": "async",
            "phase": "in_call",
            "execution_mode": mode,
            "model_continuation": "native_async",
            "source": "measured_execution",
        }

    async def execute(
        self,
        name: str,
        operation: Callable[[], Any],
        context: ToolContext | None = None,
        *,
        on_success: Callable[[], None] | None = None,
    ) -> Any:
        metadata = self._identity(name, context, "thread_offload")
        started = time.monotonic()
        self._emit({**metadata, "event": "start", "status": "running"})
        if (
            self._closing
            or (self.lifecycle is not None and not self.lifecycle.tool_admission_open)
            or (context is not None and context.cancel_signal.is_set())
        ):
            self._emit({**metadata, "event": "complete", "status": "cancelled", "duration_ms": 0.0})
            return _tool_error("The call is ending; no new account update was started.")

        async def worker() -> Any:
            try:
                # Preserve invocation order for writes on this call. Audio and
                # tools on other calls still progress while this lock is held.
                async with self._write_lock:
                    result = await asyncio.to_thread(operation)
                    status = "error" if isinstance(result, dict) and result.get("status") == "error" else "success"
                    if status == "error":
                        self._failed_writes += 1
                    if status == "success" and on_success is not None:
                        try:
                            on_success()
                        except Exception as exc:
                            # A telemetry/capture callback cannot turn a
                            # committed SDK write into a failed write.
                            logger.warning("tool success callback failed (%s)", type(exc).__name__)
                self._emit({**metadata, "event": "complete", "status": status,
                            "duration_ms": round((time.monotonic() - started) * 1000, 3)})
                return result
            except Exception as exc:
                self._failed_writes += 1
                self._emit({**metadata, "event": "complete", "status": "error",
                            "error_type": type(exc).__name__,
                            "duration_ms": round((time.monotonic() - started) * 1000, 3)})
                logger.warning("tool=<%s> failed (%s)", name, type(exc).__name__)
                return _tool_error("The account update could not be confirmed. Please do not report it as saved.")

        task = asyncio.create_task(worker(), name=f"tool-write:{name}")
        self._pending.add(task)
        task.add_done_callback(self._worker_finished)
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            self._emit({**metadata, "event": "cancel_requested", "cancel_requested": True,
                        "result_delivered": False})
            # The worker remains tracked and finishes truthfully; no rollback
            # or completion is invented merely because its awaiter was cancelled.
            raise

    async def drain(self, *, close: bool = False) -> None:
        if close:
            self.close_admission()
        while self._pending:
            await asyncio.shield(asyncio.gather(*tuple(self._pending)))

    def date_capture(self, phrase: str) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        zone = ZoneInfo(self.payment_timezone)
        return {
            "phrase": phrase,
            "captured_at": now.isoformat(),
            "reference_date": now.astimezone(zone).date().isoformat(),
            "reference_timezone": self.payment_timezone,
            "call_sid": self.call_sid,
        }

    def captured(self, capture: dict[str, Any]) -> None:
        if self.capture_sink is not None:
            try:
                self.capture_sink(capture)
            except Exception as exc:
                logger.warning("payment capture telemetry failed (%s)", type(exc).__name__)


def build_tools(
    store: Store | None = None,
    *,
    call_sid: str | None = None,
    runtime: CallToolRuntime | None = None,
) -> list[Any]:
    """Build native async in-call tools with awaited, tracked SDK writes.

    ``runtime`` lets the WebSocket owner drain pending writes before saving
    analytics. Tool results retain their actual success/error state; a queued
    operation is never returned to the model as a completed account update.
    """
    store = store or get_default_store()
    runtime = runtime or CallToolRuntime(call_sid=call_sid)

    @tool(context=True)
    async def update_due_record(
        account_id: str,
        new_status: Literal["open", "promised_to_pay", "paid", "disputed", "callback"],
        promised_amount: float | None = None,
        promised_date: str | None = None,
        note: str | None = None,
        tool_context: ToolContext = None,
    ) -> Any:
        """Persist the account outcome and return confirmation after completion.

        Args:
            account_id: Due-record account identifier.
            new_status: One of open, promised_to_pay, paid, disputed, callback.
            promised_amount: Amount promised, if any.
            promised_date: Exact ISO payment date, if explicitly provided.
            note: Short outcome note.
        """
        status = new_status.strip().lower() if isinstance(new_status, str) else ""
        if status not in DUE_STATUSES:
            return _tool_error("The requested account status is invalid; the account was not updated.")
        changes: dict[str, Any] = {"status": status}
        if promised_amount is not None:
            changes["promised_amount"] = promised_amount
        if promised_date is not None:
            changes["promised_date"] = promised_date
        if note is not None:
            changes["notes"] = note

        def persist() -> Any:
            try:
                store.update_due(account_id, **changes)
            except KeyError:
                return _tool_error(f"I could not find account {account_id}; it was not updated.")
            parts = [f"I've updated account {account_id} to {status.replace('_', ' ')}"]
            if promised_amount is not None:
                parts.append(f"with a promised amount of {promised_amount}")
            if promised_date is not None:
                parts.append(f"on {promised_date}")
            return " ".join(parts) + "."

        return await runtime.execute("update_due_record", persist, tool_context)

    @tool(context=True)
    async def capture_payment_date_phrase(
        phrase: str,
        account_id: str | None = None,
        tool_context: ToolContext = None,
    ) -> Any:
        """Persist the exact payment-date phrase for post-call date resolution.

        Args:
            phrase: The debtor's exact natural-language payment timing.
            account_id: The account receiving the payment commitment.
        """
        clean = (phrase or "").strip()
        capture = runtime.date_capture(clean)

        def persist() -> Any:
            if not clean:
                return _tool_error("I didn't catch a specific date - could you say that again?")
            if not account_id:
                return _tool_error("An account is required to save the payment timing.")
            try:
                store.update_due(
                    account_id,
                    promised_date_phrase=clean,
                    promised_date_phrase_call_sid=capture["call_sid"],
                    promised_date_reference_date=capture["reference_date"],
                    promised_date_reference_timezone=capture["reference_timezone"],
                )
            except KeyError:
                return _tool_error(f"I could not find account {account_id}; the payment timing was not saved.")
            return f"Got it, I've noted '{clean}' as the payment timing."

        return await runtime.execute(
            "capture_payment_date_phrase", persist, tool_context,
            on_success=lambda: runtime.captured(capture),
        )

    def terminal_result(
        metadata: dict[str, Any], started: float, result: dict[str, Any]
    ) -> dict[str, Any]:
        if not isinstance(result, dict):
            raise TypeError("lifecycle result must be a dictionary")
        failed = result.get("status") == "error"
        ready = not failed and (result.get("ready") is True or result.get("status") == "ready")
        semantics = (
            "error" if failed else "ready" if ready
            else "pending" if result.get("status") == "pending" else "not_ready"
        )
        runtime._emit({
            **metadata, "event": "complete", "status": "error" if failed else "success",
            "duration_ms": round((time.monotonic() - started) * 1000, 3),
            "completion_semantics": semantics, "ready": ready,
        })
        logger.info(
            "call_sid=<%s> tool=<%s> | terminal request %s",
            call_sid, metadata["tool"], semantics,
            extra={"event_type": "tool_invocation", "tool": metadata["tool"]},
        )
        if failed and "content" not in result:
            # Preserve structured controller errors as native error results;
            # an arbitrary dictionary otherwise becomes SDK status=success.
            return {**result, "content": [{"json": dict(result)}]}
        return result

    def terminal_failure(
        metadata: dict[str, Any], started: float, exc: Exception
    ) -> dict[str, Any]:
        runtime._emit({
            **metadata, "event": "complete", "status": "error",
            "error_type": type(exc).__name__,
            "duration_ms": round((time.monotonic() - started) * 1000, 3),
            "completion_semantics": "error", "ready": False,
        })
        logger.warning("tool=<%s> lifecycle request failed (%s)", metadata["tool"], type(exc).__name__)
        return _tool_error("The call cannot end yet. Keep listening and resume the conversation.")

    @tool(context=True)
    async def end_conversation(
        reason: str | None = None, language: ConversationLanguage = "en",
        tool_context: ToolContext = None,
    ) -> Any:
        """Prepare termination when the caller has finished; return the attempt state.

        Args:
            reason: Optional short explanation for ending the call.
            language: Caller’s current preferred language: en (English), fr (French), or es (Spanish).
        """
        metadata = runtime._identity("end_conversation", tool_context, "event_loop")
        started = time.monotonic()
        runtime._emit({**metadata, "event": "start", "status": "running"})
        try:
            if runtime.lifecycle is None:
                result = _tool_error("Automatic call ending is unavailable. Keep listening to the caller.")
            else:
                result = await runtime.lifecycle.request_end(
                    reason=reason, tool_use_id=metadata["tool_use_id"], language=language,
                )
            return terminal_result(metadata, started, result)
        except asyncio.CancelledError:
            runtime._emit({**metadata, "event": "complete", "status": "cancelled",
                           "duration_ms": round((time.monotonic() - started) * 1000, 3),
                           "completion_semantics": "cancelled", "ready": False})
            raise
        except Exception as exc:
            return terminal_failure(metadata, started, exc)

    @tool(context=True)
    async def begin_farewell(attempt_id: str, tool_context: ToolContext = None) -> Any:
        """Validate a ready termination attempt and return its authorized farewell.

        Args:
            attempt_id: Exact identifier from the ready end_conversation result.
        """
        metadata = runtime._identity("begin_farewell", tool_context, "event_loop")
        started = time.monotonic()
        runtime._emit({**metadata, "event": "start", "status": "running"})
        try:
            if runtime.lifecycle is None:
                result = _tool_error("Automatic call ending is unavailable. Keep listening to the caller.")
            else:
                result = runtime.lifecycle.begin_farewell(
                    attempt_id, tool_use_id=metadata["tool_use_id"],
                )
                if inspect.isawaitable(result):
                    result = await result
            return terminal_result(metadata, started, result)
        except asyncio.CancelledError:
            runtime._emit({**metadata, "event": "complete", "status": "cancelled",
                           "duration_ms": round((time.monotonic() - started) * 1000, 3),
                           "completion_semantics": "cancelled", "ready": False})
            raise
        except Exception as exc:
            return terminal_failure(metadata, started, exc)

    return [update_due_record, capture_payment_date_phrase, end_conversation, begin_farewell]
