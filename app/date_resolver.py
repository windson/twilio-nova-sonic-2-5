"""Hybrid date resolution: deterministic fast path + AgentCore fallback.

This module implements TIER 1 of a two-tier date resolver used by the off-path
post-call pipeline (:mod:`app.postcall`):

* :func:`resolve_deterministic` is a PURE, dependency-free resolver (only
  ``datetime`` + ``calendar``; no network, no AWS, sub-millisecond) that parses
  the common/enumerable date grammar a collections debtor actually uses
  ("second Tuesday of next month", "next Friday", "in two weeks", "end of next
  month", "1st Monday of next month", ...). It returns an ISO ``YYYY-MM-DD``
  string on a hit and ``None`` when it cannot confidently parse the phrase, so
  the caller knows to fall back. It never guesses.

* :func:`resolve_date_hybrid` is the hybrid entrypoint. It tries the
  deterministic resolver first and, ONLY on a miss, defers to the existing
  model-authored AWS Bedrock AgentCore Code Interpreter
  (:func:`app.code_interpreter.resolve_date_phrase`) which is kept UNCHANGED as
  the open-ended fallback. It returns ``(iso_or_none, resolved_via)`` where
  ``resolved_via`` is one of ``'deterministic'``, ``'code_interpreter'`` or
  ``'unresolved'`` so the caller can label/log which tier resolved each phrase.

Both tiers run exclusively off the real-time audio path (post-call
fire-and-forget task in :mod:`app.postcall`); nothing here touches
``app/sonic_bridge.py``.

The weekday math mirrors the pattern documented in the code-interpreter prompt
(:mod:`app.code_interpreter`): :func:`calendar.monthcalendar` returns weeks as
lists where index ``0=Monday .. 6=Sunday`` and ``0`` marks days outside the
month; collect the non-zero entries for the target weekday column and take the
Nth (``'last'`` == the final entry). The year rolls over in December.
"""

from __future__ import annotations

import calendar
import logging
import re
from datetime import date, datetime, timedelta
from typing import Any, Callable

logger = logging.getLogger(__name__)

# resolved_via labels.
VIA_DETERMINISTIC = "deterministic"
VIA_CODE_INTERPRETER = "code_interpreter"
VIA_UNRESOLVED = "unresolved"

# Weekday name -> Python weekday index (Monday=0 .. Sunday=6), matching
# calendar.MONDAY..calendar.SUNDAY and the monthcalendar column index.
_WEEKDAYS: dict[str, int] = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
    # Common shorthands.
    "mon": 0,
    "tue": 1,
    "tues": 1,
    "wed": 2,
    "weds": 2,
    "thu": 3,
    "thur": 3,
    "thurs": 3,
    "fri": 4,
    "sat": 5,
    "sun": 6,
}

# Ordinal words / digits -> 1-based index. 'last' is handled specially.
_ORDINALS: dict[str, int] = {
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
}

# Small number words for "in N days/weeks/months".
_NUMBER_WORDS: dict[str, int] = {
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


def _coerce_reference(reference_date: date | str | None) -> date:
    """Coerce the reference date to a :class:`datetime.date`."""
    if isinstance(reference_date, date):
        return reference_date
    if isinstance(reference_date, str) and reference_date.strip():
        return datetime.strptime(reference_date.strip(), "%Y-%m-%d").date()
    raise ValueError("reference_date must be a date or an ISO YYYY-MM-DD string")


def _next_month(ref: date) -> tuple[int, int]:
    """Return (year, month) for the month AFTER ``ref`` (roll over December)."""
    if ref.month == 12:
        return ref.year + 1, 1
    return ref.year, ref.month + 1


def _add_months(ref: date, months: int) -> date:
    """Add ``months`` calendar months to ``ref``, clamping the day-of-month."""
    total = (ref.year * 12 + (ref.month - 1)) + months
    year, month = divmod(total, 12)
    month += 1
    last_day = calendar.monthrange(year, month)[1]
    return date(year, month, min(ref.day, last_day))


def _nth_weekday(year: int, month: int, weekday: int, nth: int | str) -> date | None:
    """Return the Nth ``weekday`` of ``year``/``month`` using monthcalendar.

    ``nth`` is a 1-based index or the string ``'last'``. Returns ``None`` when
    the requested occurrence does not exist (e.g. a 5th Tuesday that month).
    """
    weeks = calendar.monthcalendar(year, month)
    # Collect the non-zero entries for the target weekday column.
    days = [week[weekday] for week in weeks if week[weekday] != 0]
    if not days:
        return None
    if nth == "last":
        return date(year, month, days[-1])
    index = int(nth) - 1
    if index < 0 or index >= len(days):
        return None
    return date(year, month, days[index])


# -- regexes for the enumerable grammar -----------------------------------
_RE_NTH_WEEKDAY = re.compile(
    r"^(?P<ord>first|second|third|fourth|fifth|last|1st|2nd|3rd|4th|5th)\s+"
    r"(?P<wd>\w+?)\s+of\s+(?P<which>next|this|the)\s+month$"
)
_RE_WEEKDAY_SIMPLE = re.compile(r"^(?P<which>next|this)\s+(?P<wd>\w+)$")
_RE_IN_N = re.compile(
    r"^in\s+(?P<n>\d+|[a-z]+)\s+(?P<unit>day|days|week|weeks|month|months)$"
)
_RE_END_OF_MONTH = re.compile(
    r"^(?:the\s+)?end\s+of\s+(?P<which>next|this|the)\s+month$"
)


def resolve_deterministic(phrase: str, reference_date: date | str) -> str | None:
    """Resolve the common/enumerable date grammar WITHOUT any network/AWS.

    Returns an ISO ``YYYY-MM-DD`` string on a confident parse, or ``None`` for
    anything outside the supported grammar (so the hybrid caller falls back to
    the code interpreter). Never raises for a normal phrase; an invalid
    ``reference_date`` raises :class:`ValueError`.
    """
    ref = _coerce_reference(reference_date)
    text = (phrase or "").strip().lower()
    if not text:
        return None

    # Already-ISO fast path: a phrase that is already YYYY-MM-DD.
    try:
        return datetime.strptime(text, "%Y-%m-%d").date().isoformat()
    except ValueError:
        pass

    # Normalise punctuation/whitespace; strip a leading article.
    text = re.sub(r"[,\.]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"^on\s+", "", text)

    # -- simple fixed phrases --------------------------------------------
    if text == "today":
        return ref.isoformat()
    if text == "tomorrow":
        return (ref + timedelta(days=1)).isoformat()
    if text in ("next week", "in a week"):
        return (ref + timedelta(weeks=1)).isoformat()
    if text == "next month":
        # Same day-of-month next month, clamped.
        return _add_months(ref, 1).isoformat()
    if text in ("this month", "end of month"):
        # Bare 'end of month' == end of this month.
        if text == "end of month":
            last = calendar.monthrange(ref.year, ref.month)[1]
            return date(ref.year, ref.month, last).isoformat()
        return None

    # -- end of {this,next,the} month ------------------------------------
    m = _RE_END_OF_MONTH.match(text)
    if m:
        which = m.group("which")
        if which == "next":
            year, month = _next_month(ref)
        else:  # 'this' or 'the' -> the current month
            year, month = ref.year, ref.month
        last = calendar.monthrange(year, month)[1]
        return date(year, month, last).isoformat()

    # -- Nth <weekday> of {next,this,the} month --------------------------
    m = _RE_NTH_WEEKDAY.match(text)
    if m:
        wd = _WEEKDAYS.get(m.group("wd"))
        if wd is None:
            return None
        ord_token = m.group("ord")
        nth: int | str = "last" if ord_token == "last" else _ORDINALS.get(ord_token, 0)
        if nth == 0:
            return None
        which = m.group("which")
        if which == "next":
            year, month = _next_month(ref)
        else:  # 'this' / 'the' -> current month
            year, month = ref.year, ref.month
        resolved = _nth_weekday(year, month, wd, nth)
        return resolved.isoformat() if resolved else None

    # -- next/this <weekday> ---------------------------------------------
    m = _RE_WEEKDAY_SIMPLE.match(text)
    if m:
        wd = _WEEKDAYS.get(m.group("wd"))
        if wd is None:
            return None
        which = m.group("which")
        delta = (wd - ref.weekday()) % 7
        if which == "next":
            # 'next <weekday>' = the upcoming occurrence (always in the future);
            # if today IS that weekday, jump a full week.
            delta = delta or 7
        else:  # 'this <weekday>' = the occurrence in the current week forward
            # If it already passed this week, treat it as the next one.
            delta = delta or 7
        return (ref + timedelta(days=delta)).isoformat()

    # -- in N days/weeks/months ------------------------------------------
    m = _RE_IN_N.match(text)
    if m:
        raw = m.group("n")
        if raw.isdigit():
            n = int(raw)
        else:
            n = _NUMBER_WORDS.get(raw, 0)
        if n <= 0:
            return None
        unit = m.group("unit")
        if unit.startswith("day"):
            return (ref + timedelta(days=n)).isoformat()
        if unit.startswith("week"):
            return (ref + timedelta(weeks=n)).isoformat()
        if unit.startswith("month"):
            return _add_months(ref, n).isoformat()

    # Outside the supported grammar: signal a fallback.
    return None


def resolve_date_hybrid(
    phrase: str,
    reference_date: date | str,
    *,
    region: str,
    model_id: str | None = None,
    bedrock_client: Any | None = None,
    code_interpreter_factory: Callable[[str], Any] | None = None,
) -> tuple[str | None, str]:
    """Resolve ``phrase`` deterministically first, else via the code interpreter.

    Returns ``(iso_or_none, resolved_via)`` where ``resolved_via`` is:

    * ``'deterministic'`` - the pure fast path parsed the phrase (the code
      interpreter was NOT called);
    * ``'code_interpreter'`` - the deterministic resolver missed and the
      model-authored AgentCore fallback resolved it;
    * ``'unresolved'`` - neither tier could resolve the phrase.

    The injectable ``bedrock_client`` / ``code_interpreter_factory`` seams are
    forwarded to :func:`app.code_interpreter.resolve_date_phrase` so tests never
    touch AWS. This runs OFF the real-time audio path.
    """
    ref = _coerce_reference(reference_date)
    text = (phrase or "").strip()
    if not text:
        return None, VIA_UNRESOLVED

    # TIER 1: deterministic fast path (sub-millisecond, no network/AWS).
    try:
        deterministic = resolve_deterministic(text, ref)
    except ValueError:
        deterministic = None
    if deterministic:
        logger.info(
            "phrase=<%s>, resolved=<%s> | deterministic fast path",
            text,
            deterministic,
            extra={
                "event_type": "tool_invocation",
                "tool": "resolve_date_phrase",
                "resolved_via": VIA_DETERMINISTIC,
            },
        )
        return deterministic, VIA_DETERMINISTIC

    # TIER 2: model-authored AgentCore Code Interpreter fallback (UNCHANGED).
    from app import code_interpreter

    resolved = code_interpreter.resolve_date_phrase(
        text,
        reference_date=ref.isoformat(),
        region=region,
        model_id=model_id,
        bedrock_client=bedrock_client,
        client_factory=code_interpreter_factory,
    )
    via = VIA_CODE_INTERPRETER if resolved else VIA_UNRESOLVED
    logger.info(
        "phrase=<%s>, resolved=<%s> | code interpreter fallback",
        text,
        resolved,
        extra={
            "event_type": "tool_invocation",
            "tool": "resolve_date_phrase",
            "resolved_via": via,
        },
    )
    return resolved, via
