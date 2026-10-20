"""The collections agent brain: system prompt + the due-record update tool.

Builds the Accounts Receivable collections persona (an AI-initiated outbound
call that greets the debtor by name, states the exact amount and due date,
converses compliantly, and records the outcome) and the Strands ``@tool`` the
model calls to write the outcome back to the store.

The factory functions :func:`get_system_prompt` and :func:`build_tools` are the
seams :mod:`app.sonic_bridge` injects per call.
"""

from __future__ import annotations

import calendar
import logging
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any, Mapping

from strands import tool

from app.store import DUE_STATUSES, Store, get_default_store

logger = logging.getLogger(__name__)

# Weekday names -> Python weekday() index (Monday=0 .. Sunday=6).
_WEEKDAYS = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}

# Small number words so phrases like "in two weeks" resolve without digits.
_NUMBER_WORDS = {
    "a": 1,
    "an": 1,
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
}

# Ordinal words -> 1-based index (for "first/second ... <weekday>").
_ORDINAL_WORDS = {
    "first": 1,
    "1st": 1,
    "second": 2,
    "2nd": 2,
    "third": 3,
    "3rd": 3,
    "fourth": 4,
    "4th": 4,
    "fifth": 5,
    "5th": 5,
    "last": -1,
}


def _add_months(d: date, months: int) -> date:
    """Add ``months`` calendar months to ``d`` (clamping the day of month)."""
    total = d.month - 1 + months
    year = d.year + total // 12
    month = total % 12 + 1
    last_day = calendar.monthrange(year, month)[1]
    return date(year, month, min(d.day, last_day))


def _nth_weekday_of_month(year: int, month: int, weekday: int, n: int) -> date:
    """Return the ``n``-th ``weekday`` of ``month``/``year`` (n=-1 => last)."""
    if n == -1:
        last_day = calendar.monthrange(year, month)[1]
        d = date(year, month, last_day)
        while d.weekday() != weekday:
            d -= timedelta(days=1)
        return d
    first = date(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    day = 1 + offset + (n - 1) * 7
    last_day = calendar.monthrange(year, month)[1]
    if day > last_day:
        # Fewer than n of that weekday this month: clamp to the last one.
        return _nth_weekday_of_month(year, month, weekday, -1)
    return date(year, month, day)


def _resolve_relative_date(phrase: str, reference_date: date) -> date | None:
    """Convert a natural-language date phrase to a concrete :class:`date`.

    Pure stdlib datetime math (no network, no sandbox) so it returns instantly
    on the turn path. Handles, at minimum:

    - ``tomorrow``
    - ``next <weekday>`` (e.g. "next Monday")
    - ``in N days/weeks/months`` (digits or small number words)
    - ``next month 1st <weekday>`` / ``first <weekday> of next month``
    - ``end of next month``

    Returns ``None`` when the phrase cannot be understood.
    """
    text = (phrase or "").strip().lower()
    if not text:
        return None

    # Already an ISO date -> pass through.
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        pass

    if "tomorrow" in text:
        return reference_date + timedelta(days=1)
    if "today" in text:
        return reference_date

    # "end of next month" / "end of the month"
    if "end of" in text and "month" in text:
        months = 1 if "next" in text else 0
        target = _add_months(reference_date, months)
        last_day = calendar.monthrange(target.year, target.month)[1]
        return date(target.year, target.month, last_day)

    # Ordinal weekday of next month, both orderings:
    #   "next month 1st monday", "first monday of next month"
    if "next month" in text or ("next" in text and "month" in text):
        weekday = next((_WEEKDAYS[w] for w in _WEEKDAYS if w in text), None)
        if weekday is not None:
            ordinal = next(
                (_ORDINAL_WORDS[o] for o in _ORDINAL_WORDS if re.search(rf"\b{o}\b", text)),
                1,
            )
            target_month = _add_months(reference_date.replace(day=1), 1)
            return _nth_weekday_of_month(
                target_month.year, target_month.month, weekday, ordinal
            )

    # "in N days/weeks/months"
    m = re.search(r"in\s+(\d+|[a-z]+)\s+(day|week|month)s?", text)
    if m:
        qty_token, unit = m.group(1), m.group(2)
        qty = int(qty_token) if qty_token.isdigit() else _NUMBER_WORDS.get(qty_token)
        if qty is not None:
            if unit == "day":
                return reference_date + timedelta(days=qty)
            if unit == "week":
                return reference_date + timedelta(weeks=qty)
            if unit == "month":
                return _add_months(reference_date, qty)

    # "next <weekday>" (strictly the following week's occurrence).
    if "next" in text:
        weekday = next((_WEEKDAYS[w] for w in _WEEKDAYS if w in text), None)
        if weekday is not None:
            days_ahead = (weekday - reference_date.weekday()) % 7
            days_ahead = days_ahead + 7 if days_ahead == 0 else days_ahead
            return reference_date + timedelta(days=days_ahead)

    # Bare "<weekday>" -> the next upcoming occurrence (today counts as +7).
    weekday = next((_WEEKDAYS[w] for w in _WEEKDAYS if w in text), None)
    if weekday is not None:
        days_ahead = (weekday - reference_date.weekday()) % 7
        days_ahead = days_ahead or 7
        return reference_date + timedelta(days=days_ahead)

    return None


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
        "Handling dates:\n"
        "- If the debtor gives a relative or natural-language date (for example "
        "'next month, the first Monday', 'in two weeks', 'next Friday', or "
        "'the end of next month'), call the `compute_future_date` tool with "
        "that phrase to resolve it to a concrete YYYY-MM-DD date. Confirm the "
        "resolved date with the debtor, then pass it as promised_date to "
        "`update_due_record`.\n\n"
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

    @tool
    def compute_future_date(phrase: str, reference_date: str | None = None) -> str:
        """Resolve a natural-language date phrase to a concrete calendar date.

        This is the in-call "code interpreter" tool: it performs deterministic,
        purely-local stdlib date math (no network, no sandbox) so it returns
        within the turn without adding meaningful latency. Call it whenever the
        debtor gives a relative or natural-language date so you can confirm and
        record a concrete date.

        Args:
            phrase: A natural-language date phrase, e.g. "next month 1st Monday",
                "first Monday of next month", "in two weeks", "in 10 days",
                "next Friday", "end of next month", or "tomorrow".
            reference_date: Optional ISO date (YYYY-MM-DD) to compute relative
                to. Defaults to today (UTC).

        Returns:
            A short sentence containing the resolved ISO date (YYYY-MM-DD) that
            the assistant can speak and pass to
            ``update_due_record.promised_date``.
        """
        if reference_date:
            try:
                ref = datetime.strptime(reference_date.strip(), "%Y-%m-%d").date()
            except ValueError:
                ref = datetime.now(timezone.utc).date()
        else:
            ref = datetime.now(timezone.utc).date()

        resolved = _resolve_relative_date(phrase, ref)
        if resolved is None:
            logger.info(
                "phrase=<%s> | compute_future_date: unparseable phrase",
                phrase,
                extra={"event_type": "tool_invocation", "tool": "compute_future_date"},
            )
            return (
                f"I couldn't work out a specific date from '{phrase}'. Could you "
                "give me a specific day?"
            )

        iso = resolved.isoformat()
        logger.info(
            "phrase=<%s>, resolved=<%s> | compute_future_date",
            phrase,
            iso,
            extra={"event_type": "tool_invocation", "tool": "compute_future_date"},
        )
        return f"That date is {iso}."

    return [update_due_record, compute_future_date]
