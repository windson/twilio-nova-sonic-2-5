"""The collections agent brain: system prompt + the due-record update tool.

Builds the Accounts Receivable collections persona (an AI-initiated outbound
call that greets the debtor by name, states the exact amount and due date,
converses compliantly, and records the outcome) and the Strands ``@tool`` the
model calls to write the outcome back to the store.

The factory functions :func:`get_system_prompt` and :func:`build_tools` are the
seams :mod:`app.sonic_bridge` injects per call.
"""

from __future__ import annotations

import logging
from typing import Any, Mapping

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
        "and end the call."
    )


def build_tools(store: Store | None = None) -> list[Any]:
    """Build the tool list for a call, bound to ``store``.

    Returns a list containing the ``update_due_record`` Strands tool that writes
    through :meth:`Store.update_due`. The returned tool is a plain
    ``@tool``-decorated function, so it can also be invoked directly in tests.
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

    return [update_due_record]
