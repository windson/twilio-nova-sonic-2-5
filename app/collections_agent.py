"""The collections agent brain: system prompt + the due-record update tool.

Builds the Accounts Receivable collections persona (an AI-initiated outbound
call that greets the debtor by name, states the exact amount and due date,
converses compliantly, and records the outcome) and the Strands ``@tool``\\ s the
model calls to write the outcome back to the store.

Date handling is split for latency: the in-call tool only CAPTURES the raw
natural-language date phrase (fast, no network) so the audio/turn path stays
responsive. The concrete ``YYYY-MM-DD`` is resolved later OFF the real-time path
by the AWS Bedrock AgentCore Code Interpreter in :mod:`app.code_interpreter`,
invoked from the post-call task in :mod:`app.postcall`. (The former hand-written
deterministic date parser has been removed in favour of that real code
interpreter.)

The factory functions :func:`get_system_prompt` and :func:`build_tools` are the
seams :mod:`app.sonic_bridge` injects per call.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Mapping

from strands import tool

from app.store import DUE_STATUSES, Store, get_default_store

logger = logging.getLogger(__name__)


def _format_amount(record: Mapping[str, Any]) -> str:
    amount = record.get("amount_due", 0)
    currency = record.get("currency", "USD")
    try:
        return f"{float(amount):,.2f} {currency}"
    except (TypeError, ValueError):
        return f"{amount} {currency}"


def get_system_prompt(due_record: Mapping[str, Any]) -> str:
    """Build the collections system prompt for a specific due record.

    The prompt names the debtor, states the exact amount due and due date, and
    constrains the agent to be professional and compliant (identify as an AI
    assistant, no threats, offer to arrange payment or note a dispute/callback,
    keep turns concise).
    """
    name = due_record.get("debtor_name", "there")
    amount = _format_amount(due_record)
    due_date = due_record.get("due_date", "the stated due date")
    account_id = due_record.get("account_id", "")
    status = due_record.get("status", "open")

    return (
        "You are Ava, a professional, courteous AI voice assistant calling on "
        "behalf of the Accounts Receivable department. You placed this outbound "
        "call; the person who answers is the debtor.\n\n"
        f"Account on file: account id {account_id}, debtor name {name}, "
        f"current status '{status}'. The outstanding balance is {amount}, "
        f"which was due on {due_date}.\n\n"
        "Call objectives, in order:\n"
        f"1. Politely greet {name} by name and clearly identify yourself as an "
        "AI assistant calling about an outstanding account balance.\n"
        f"2. State the exact amount due ({amount}) and the due date ({due_date}).\n"
        "3. Ask whether they are able to pay today, arrange a payment date, or "
        "whether they dispute the balance.\n"
        "4. If they agree to pay or promise a date/amount, confirm the details. "
        "If they dispute it or ask for a callback, acknowledge and capture that.\n"
        "5. Thank them and close the call politely.\n\n"
        "Handling dates:\n"
        "- If the debtor gives a relative or natural-language payment date (for "
        "example 'next month, the first Monday', 'in two weeks', 'next Friday', "
        "or 'the end of next month'), call the `capture_payment_date_phrase` "
        "tool with that exact phrase and the account id. This simply RECORDS "
        "the phrase for you; the concrete calendar date is computed securely "
        "right after the call, so you do not need to work out the exact date "
        "yourself. Read back the phrase in plain words to confirm (for example "
        "'the first Monday of next month'), then still call `update_due_record` "
        "with the outcome.\n"
        "- If the debtor gives an exact calendar date, pass it directly as "
        "promised_date (YYYY-MM-DD) to `update_due_record`.\n\n"
        "Compliance and tone rules (strict):\n"
        "- Always be respectful and professional. Never threaten, harass, or "
        "use deceptive or aggressive language.\n"
        "- You are an AI assistant; if asked, say so plainly.\n"
        "- Do not provide legal, tax, or financial advice.\n"
        "- Keep each spoken turn short and natural for a phone conversation.\n\n"
        "Recording the outcome:\n"
        "- When the conversation reaches an outcome, call the "
        "`update_due_record` tool exactly once with the right new_status "
        f"(one of: {', '.join(DUE_STATUSES)}), and any promised_amount, "
        "promised_date, or note. For account id use "
        f"'{account_id}'.\n"
        "- Use 'promised_to_pay' when they commit to pay, 'paid' if paid now, "
        "'disputed' if they dispute, 'callback' if they want a later call, and "
        "keep 'open' if unresolved.\n"
        "- After the tool confirms, briefly restate the outcome to the caller "
        "and deliver your closing/parting greeting (for example 'Thank you, "
        "goodbye.').\n\n"
        "Ending the call (final action):\n"
        "- Once `update_due_record` has confirmed the outcome and you have "
        "delivered your closing greeting, call the `end_conversation` tool "
        "EXACTLY ONCE to end the call. This is always your final action.\n"
        "- Also call `end_conversation` immediately if the caller says a parting "
        "phrase such as 'goodbye', 'bye', or 'that's all'.\n"
        "- Do not call `end_conversation` more than once, and do not call it "
        "before you have finished the conversation and said goodbye."
    )


def build_tools(
    store: Store | None = None,
    *,
    call_sid: str | None = None,
    on_end: Callable[[], Any] | None = None,
) -> list[Any]:
    """Build the tool list for a call, bound to ``store``.

    Returns the ``update_due_record`` tool (which writes through
    :meth:`Store.update_due`), the lightweight ``capture_payment_date_phrase``
    tool, and the ``end_conversation`` terminal tool (FEAT-002). The date tool
    only records the raw natural-language date phrase on the due record and
    returns immediately so the real-time turn path stays fast; the concrete
    ``YYYY-MM-DD`` is resolved OFF-path post-call by
    :func:`app.code_interpreter.resolve_date_phrase`. The returned tools are
    plain ``@tool``-decorated functions, so they can also be invoked directly in
    tests.

    Args:
        store: Store the tools write through (defaults to the process store).
        call_sid: The live Twilio Call sid for this session, used by
            ``end_conversation`` for logging/diagnostics. Defaults to ``None``
            so existing callers/tests keep working.
        on_end: Injectable callback invoked by ``end_conversation`` to perform
            the actual Twilio hangup + session teardown. It is called with no
            arguments and is GUARDED so a failure never raises into the model.
            Defaults to ``None`` (no-op), keeping the tool safe when unwired.
    """
    store = store or get_default_store()

    @tool
    def update_due_record(
        account_id: str,
        new_status: str,
        promised_amount: float | None = None,
        promised_date: str | None = None,
        note: str | None = None,
    ) -> str:
        """Update a collections due record with the outcome of the call.

        Args:
            account_id: The account id of the due record to update.
            new_status: New status, one of open, promised_to_pay, paid,
                disputed, callback.
            promised_amount: Amount the debtor promised to pay, if any.
            promised_date: Date the debtor promised to pay (ISO date), if any.
            note: Free-text note summarizing the interaction.

        Returns:
            A short confirmation sentence the assistant can speak aloud.
        """
        status = (new_status or "").strip().lower()
        if status not in DUE_STATUSES:
            status = "open"

        changes: dict[str, Any] = {"status": status}
        if promised_amount is not None:
            changes["promised_amount"] = promised_amount
        if promised_date is not None:
            changes["promised_date"] = promised_date
        if note is not None:
            changes["notes"] = note

        try:
            record = store.update_due(account_id, **changes)
        except KeyError:
            logger.warning(
                "account_id=<%s> | update_due_record: unknown account", account_id
            )
            return (
                f"I could not find account {account_id} on file, so I was unable "
                "to update it."
            )

        logger.info(
            "account_id=<%s>, new_status=<%s> | due record updated via tool",
            account_id,
            status,
            extra={"event_type": "tool_invocation", "tool": "update_due_record"},
        )

        parts = [f"I've updated account {account_id} to {status.replace('_', ' ')}"]
        if promised_amount is not None:
            parts.append(f"with a promised amount of {promised_amount}")
        if promised_date is not None:
            parts.append(f"on {promised_date}")
        confirmation = " ".join(parts) + "."
        _ = record  # record kept for potential future use/logging
        return confirmation

    @tool
    def capture_payment_date_phrase(phrase: str, account_id: str | None = None) -> str:
        """Capture a natural-language payment-date phrase for later resolution.

        This is a LIGHTWEIGHT in-call tool: it merely records the raw phrase the
        debtor gave (e.g. "next month, the first Monday", "in two weeks") and
        returns immediately. It deliberately does NOT compute the concrete date
        here - the real computation is delegated to the AWS Bedrock AgentCore
        Code Interpreter, which runs OFF the real-time audio/turn path in the
        post-call task so it adds no call latency. Resolving inline would cost
        roughly ~1.9s on the turn, which is why it is captured now and resolved
        later.

        Args:
            phrase: The debtor's natural-language payment-date phrase.
            account_id: The account id to attach the phrase to (optional).

        Returns:
            A short confirmation sentence the assistant can speak aloud.
        """
        clean = (phrase or "").strip()
        if account_id and clean:
            try:
                store.update_due(account_id, promised_date_phrase=clean)
            except KeyError:
                logger.warning(
                    "account_id=<%s> | capture_payment_date_phrase: unknown account",
                    account_id,
                )

        logger.info(
            "account_id=<%s>, phrase=<%s> | payment-date phrase captured",
            account_id,
            clean,
            extra={
                "event_type": "tool_invocation",
                "tool": "capture_payment_date_phrase",
            },
        )
        if not clean:
            return "I didn't catch a specific date - could you say that again?"
        return f"Got it, I've noted '{clean}' as the payment timing."

    # Latch so the hangup/teardown runs at most once even if the model emits
    # ``end_conversation`` more than once (idempotency, FEAT-002).
    ended = {"done": False}

    @tool
    def end_conversation(reason: str | None = None) -> str:
        """End the call after the conversation has reached its conclusion.

        This is the agent's FINAL action: call it exactly once after you have
        confirmed the outcome and delivered your closing greeting, or when the
        caller says a parting phrase (e.g. "goodbye", "bye", "that's all"). It
        terminates the live phone call.

        The handler is idempotent (a second invocation is a no-op) and is
        GUARDED so the underlying Twilio hangup / session teardown can never
        raise back into the model. The actual hangup is performed by the
        injected ``on_end`` callback; it is the intended terminal REST action
        and is kept off any heavy work.

        Args:
            reason: Optional short reason the call is ending (diagnostics only).

        Returns:
            A short spoken confirmation the assistant can say before the line
            drops.
        """
        logger.info(
            "call_sid=<%s>, reason=<%s> | end_conversation tool invoked",
            call_sid,
            (reason or "").strip() or None,
            extra={"event_type": "tool_invocation", "tool": "end_conversation"},
        )

        if ended["done"]:
            # Already ended: do not trigger the hangup/teardown twice.
            return "Thank you, goodbye."
        ended["done"] = True

        if on_end is not None:
            try:
                on_end()
            except Exception:  # noqa: BLE001 - a hangup failure must never surface
                logger.warning(
                    "call_sid=<%s> | end_conversation on_end callback failed",
                    call_sid,
                )

        return "Thank you, goodbye."

    return [update_due_record, capture_payment_date_phrase, end_conversation]
