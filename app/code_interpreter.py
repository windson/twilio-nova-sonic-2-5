"""AWS Bedrock AgentCore Code Interpreter wrapper (off-path date resolution).

This module is the REAL code interpreter that replaces the former hand-written
``_resolve_relative_date`` date parser. Instead of re-implementing date math in
Python here and calling it a "code interpreter", it builds a Python PROGRAM as
a string that reasons from a supplied *today's* date and the debtor's
natural-language phrase, then ships that program to the AgentCore Code
Interpreter sandbox (``bedrock_agentcore.tools.code_interpreter_client``) which
*executes* it and returns stdout. The executed program does the computation;
this module only supplies the arguments (the phrase + the reference date) and
parses the resolved ``YYYY-MM-DD`` the sandbox prints.

Latency note: a Code Interpreter session costs roughly ~1.75s to start plus
~0.13s to execute (~1.9s total), so this MUST only ever be called OFF the
real-time audio/turn path. The single caller is
:func:`app.postcall.extract_and_apply`, which already runs post-call as a
fire-and-forget asyncio task. The in-call tool merely captures the raw phrase.

The client is injectable (``client_factory``) so tests never touch AWS, and the
wrapper is fully defensive: any exception or an ``isError`` result returns
``None`` (it never raises into the caller), and the session is always stopped
in a ``finally`` block.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any, Callable

logger = logging.getLogger(__name__)

# Managed Code Interpreter identifier (no CreateCodeInterpreter needed).
DEFAULT_CODE_INTERPRETER_ID = "aws.codeinterpreter.v1"

# Sentinel the executed program prints when it cannot resolve the phrase.
_UNRESOLVED = "UNRESOLVED"

_ISO_DATE_RE = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")


def _build_program(phrase: str, reference_date: str) -> str:
    """Build the Python PROGRAM the sandbox executes to resolve the date.

    The program imports the stdlib and computes the concrete calendar date from
    the given ``reference_date`` (today) and the natural-language ``phrase``,
    then prints ONLY the resolved ``YYYY-MM-DD`` (or the ``UNRESOLVED``
    sentinel). The computation lives inside the executed program - not in this
    module - which is the whole point of delegating to the code interpreter.

    The phrase and reference date are injected as ``repr()`` literals so the
    program is self-contained and the values are safely quoted.
    """
    return f'''
import calendar, re
from datetime import date, datetime, timedelta

PHRASE = {phrase!r}
REFERENCE = {reference_date!r}

WEEKDAYS = {{
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
}}
NUMBER_WORDS = {{
    "a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11,
    "twelve": 12,
}}
ORDINAL_WORDS = {{
    "first": 1, "1st": 1, "second": 2, "2nd": 2, "third": 3, "3rd": 3,
    "fourth": 4, "4th": 4, "fifth": 5, "5th": 5, "last": -1,
}}


def add_months(d, months):
    total = d.month - 1 + months
    year = d.year + total // 12
    month = total % 12 + 1
    last_day = calendar.monthrange(year, month)[1]
    return date(year, month, min(d.day, last_day))


def nth_weekday_of_month(year, month, weekday, n):
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
        return nth_weekday_of_month(year, month, weekday, -1)
    return date(year, month, day)


def resolve(phrase, ref):
    text = (phrase or "").strip().lower()
    if not text:
        return None
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        pass
    if "tomorrow" in text:
        return ref + timedelta(days=1)
    if "today" in text:
        return ref
    if "end of" in text and "month" in text:
        months = 1 if "next" in text else 0
        target = add_months(ref, months)
        last_day = calendar.monthrange(target.year, target.month)[1]
        return date(target.year, target.month, last_day)
    if "next month" in text or ("next" in text and "month" in text):
        weekday = next((WEEKDAYS[w] for w in WEEKDAYS if w in text), None)
        if weekday is not None:
            ordinal = next(
                (ORDINAL_WORDS[o] for o in ORDINAL_WORDS
                 if re.search(r"\\b" + re.escape(o) + r"\\b", text)),
                1,
            )
            target_month = add_months(ref.replace(day=1), 1)
            return nth_weekday_of_month(
                target_month.year, target_month.month, weekday, ordinal
            )
    m = re.search(r"in\\s+(\\d+|[a-z]+)\\s+(day|week|month)s?", text)
    if m:
        qty_token, unit = m.group(1), m.group(2)
        qty = int(qty_token) if qty_token.isdigit() else NUMBER_WORDS.get(qty_token)
        if qty is not None:
            if unit == "day":
                return ref + timedelta(days=qty)
            if unit == "week":
                return ref + timedelta(weeks=qty)
            if unit == "month":
                return add_months(ref, qty)
    if "next" in text:
        weekday = next((WEEKDAYS[w] for w in WEEKDAYS if w in text), None)
        if weekday is not None:
            days_ahead = (weekday - ref.weekday()) % 7
            days_ahead = days_ahead + 7 if days_ahead == 0 else days_ahead
            return ref + timedelta(days=days_ahead)
    weekday = next((WEEKDAYS[w] for w in WEEKDAYS if w in text), None)
    if weekday is not None:
        days_ahead = (weekday - ref.weekday()) % 7
        days_ahead = days_ahead or 7
        return ref + timedelta(days=days_ahead)
    return None


try:
    ref = datetime.strptime(REFERENCE, "%Y-%m-%d").date()
except (ValueError, TypeError):
    ref = date.today()

result = resolve(PHRASE, ref)
print(result.isoformat() if result is not None else "{_UNRESOLVED}")
'''


def _default_client_factory(region: str) -> Any:
    """Build a real AgentCore Code Interpreter client (lazy import)."""
    from bedrock_agentcore.tools.code_interpreter_client import CodeInterpreter

    return CodeInterpreter(region)


def _collect_text(response: Any) -> tuple[str, bool]:
    """Collect stdout text + isError flag from an executeCode stream response."""
    chunks: list[str] = []
    is_error = False
    for event in response.get("stream", []):
        result = event.get("result", {}) or {}
        if result.get("isError"):
            is_error = True
        for item in result.get("content", []) or []:
            if isinstance(item, dict) and item.get("type") == "text":
                text = item.get("text")
                if isinstance(text, str):
                    chunks.append(text)
    return "".join(chunks), is_error


def resolve_date_phrase(
    phrase: str,
    reference_date: str | None = None,
    *,
    region: str,
    client_factory: Callable[[str], Any] | None = None,
) -> str | None:
    """Resolve a natural-language date phrase to a concrete ISO date.

    Ships a self-contained Python program (built from ``phrase`` +
    ``reference_date``) to the AgentCore Code Interpreter sandbox, which
    *executes* it and prints the resolved ``YYYY-MM-DD``. This module only
    supplies the arguments; the sandbox does the date math.

    Args:
        phrase: The raw natural-language date phrase (e.g. "next month, the
            first Monday", "in two weeks", "next Friday", "end of next month").
        reference_date: Optional ISO date (YYYY-MM-DD) treated as *today* by the
            executed program. Defaults to today (UTC).
        region: AWS region for the Code Interpreter session.
        client_factory: Optional factory ``(region) -> client`` returning an
            object with ``start()``, ``invoke(method, params)`` and ``stop()``.
            Injected in tests so no AWS call happens. Defaults to the real
            AgentCore client.

    Returns:
        The resolved ISO date string, or ``None`` when the phrase cannot be
        resolved or the sandbox errors. Never raises into the caller.
    """
    text = (phrase or "").strip()
    if not text:
        return None

    if reference_date and reference_date.strip():
        ref = reference_date.strip()
    else:
        ref = datetime.now(timezone.utc).date().isoformat()

    factory = client_factory or _default_client_factory
    program = _build_program(text, ref)

    client = None
    try:
        client = factory(region)
        client.start()
        response = client.invoke(
            "executeCode", {"language": "python", "code": program}
        )
        stdout, is_error = _collect_text(response)
        if is_error:
            logger.warning(
                "phrase=<%s> | code interpreter returned isError", phrase
            )
            return None
        match = _ISO_DATE_RE.search(stdout or "")
        if not match:
            logger.info(
                "phrase=<%s> | code interpreter produced no ISO date (stdout=<%s>)",
                phrase,
                (stdout or "").strip()[:120],
            )
            return None
        iso = match.group(1)
        # Validate it is a real calendar date before returning.
        try:
            datetime.strptime(iso, "%Y-%m-%d")
        except ValueError:
            return None
        logger.info(
            "phrase=<%s>, resolved=<%s> | code interpreter resolved date",
            phrase,
            iso,
            extra={"event_type": "code_interpreter", "tool": "resolve_date_phrase"},
        )
        return iso
    except Exception as exc:  # noqa: BLE001 - never raise into the caller
        logger.warning(
            "phrase=<%s> | code interpreter failed: %s", phrase, exc
        )
        return None
    finally:
        if client is not None:
            try:
                client.stop()
            except Exception:  # noqa: BLE001 - best-effort cleanup
                logger.debug("code interpreter stop() failed", exc_info=True)
