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

* :func:`resolve_date_hybrid` is the hybrid entrypoint. It first leaves
  ambiguous/tentative phrases for clarification, then tries the deterministic
  resolver and, ONLY on an unsupported grammar, defers to the existing
  model-authored AWS Bedrock AgentCore Code Interpreter
  (:func:`app.code_interpreter.resolve_date_phrase`) as the open-ended fallback.
  It returns ``(iso_or_none, resolved_via)`` where
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
import unicodedata
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
    if isinstance(reference_date, datetime):
        return reference_date.date()
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
    weeks = calendar.Calendar(firstweekday=calendar.MONDAY).monthdayscalendar(year, month)
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
    r"(?P<wd>\w+?)\s+(?:of\s+)?(?P<which>next|this|the)\s+month$"
)
_RE_WEEKDAY_SIMPLE = re.compile(r"^(?P<which>next|this)\s+(?P<wd>\w+)$")
_RE_IN_N = re.compile(
    r"^in\s+(?P<n>\d+|[a-z]+)\s+(?P<unit>day|days|week|weeks|month|months)$"
)
_RE_END_OF_MONTH = re.compile(
    r"^(?:the\s+)?end\s+of\s+(?P<which>next|this|the)\s+month$"
)
_ORDINAL_PATTERN = r"first|second|third|fourth|fifth|last|1st|2nd|3rd|4th|5th"
_MONTH_PATTERN = (
    r"(?:(?:(?:the\s+)?(?:next|this)|the)\s+month|january|february|march|april|may|june|july|"
    r"august|september|october|november|december)"
)
_RE_WEEK_OF_MONTH = re.compile(
    rf"\b(?:{_ORDINAL_PATTERN})\s+week\s+(?:(?:of|in)\s+)?{_MONTH_PATTERN}\b"
    rf"|\b{_MONTH_PATTERN}\s+(?:the\s+)?(?:{_ORDINAL_PATTERN})\s+week\b"
)
_RE_TENTATIVE = re.compile(
    r"\b(?:maybe|perhaps|possibly|probably|might|hopefully|tentatively)\b|\bnot sure\b"
    r"|\b(?:peut[- ]?etre|eventuellement|probablement|possiblement|pourrais|pourrait)\b"
    r"|\b(?:pas sure?|pas certaine?|je ne sais pas)\b"
    r"|\b(?:quizas?|posiblemente|probablemente|ojala|tal vez|a lo mejor)\b"
    r"|\bno estoy segur[oa]\b"
)
_RE_MULTILINGUAL_WEEK_OF_MONTH = re.compile(
    r"\b(?:premiere|deuxieme|seconde|troisieme|quatrieme|cinquieme|derniere|[1-5](?:re|ere|e|eme))"
    r"\s+semaine\s+(?:de |du |d['’])(?:le |la |l['’])?"
    r"(?:mois(?: prochain| suivant| en cours)?|janvier|fevrier|mars|avril|mai|juin|juillet|aout|septembre|octobre|novembre|decembre)\b"
    r"|\b(?:primera|segunda|tercera|cuarta|quinta|ultima|[1-5](?:a|ª))"
    r"\s+semana\s+(?:de |del )(?:el )?"
    r"(?:(?:proximo|siguiente|este) mes|mes(?: proximo| siguiente| que viene)?|enero|febrero|marzo|abril|mayo|junio|julio|agosto|septiembre|octubre|noviembre|diciembre)\b"
)


def _accent_fold(text: str) -> str:
    """Normalize accent variants for known grammar, without changing saved text."""
    return "".join(char for char in unicodedata.normalize("NFKD", text.casefold())
                   if not unicodedata.combining(char))


def _supported_relative_alias(phrase: str) -> str:
    """Translate only anchored, unambiguous FR/ES relative-date grammar.

    Arbitrary language interpretation remains with the existing Luna fallback.
    Original phrases are preserved in capture and resolution provenance.
    """
    text = re.sub(r"\s+", " ", _accent_fold(phrase)).strip()
    fixed = {
        "aujourd'hui": "today", "aujourd’hui": "today", "demain": "tomorrow",
        "apres-demain": "in 2 days", "apres demain": "in 2 days",
        "hoy": "today", "manana": "tomorrow", "pasado manana": "in 2 days",
    }
    if text in fixed:
        return fixed[text]
    numbers = {"un": 1, "une": 1, "deux": 2, "trois": 3, "quatre": 4,
               "cinq": 5, "six": 6, "sept": 7, "huit": 8, "neuf": 9, "dix": 10,
               "onze": 11, "douze": 12, "treize": 13, "quatorze": 14, "quinze": 15,
               "seize": 16, "uno": 1, "una": 1, "dos": 2, "tres": 3,
               "cuatro": 4, "cinco": 5, "seis": 6, "siete": 7, "ocho": 8,
               "nueve": 9, "diez": 10, "once": 11, "doce": 12, "trece": 13,
               "catorce": 14, "quince": 15}
    match = re.fullmatch(r"(?:dans|dentro de|en) (\d+|[a-z]+) (jours?|semaines?|mois|dias?|semanas?|mes(?:es)?)", text)
    if match:
        raw_number, unit = match.groups()
        number = int(raw_number) if raw_number.isdigit() else numbers.get(raw_number)
        if number is not None:
            english_unit = "days" if unit.startswith(("jour", "dia")) else "weeks" if unit.startswith(("semaine", "semana")) else "months"
            return f"in {number} {english_unit}"
    return phrase


def phrase_clarification_reasons(phrase: str) -> list[str]:
    """Recognize explicit uncertainty without choosing a calendar convention.

    An ordinal *week of a month* has no configured first-week rule here. It is
    different from an ordinal weekday, whose date is uniquely calculable.
    Tentative wording must not be discarded to fabricate a firm agreement.
    """
    text = _accent_fold(normalize_date_phrase(phrase))
    reasons = []
    if _RE_WEEK_OF_MONTH.search(text) or _RE_MULTILINGUAL_WEEK_OF_MONTH.search(text):
        reasons.append("ambiguous_week_of_month")
    if _RE_TENTATIVE.search(text):
        reasons.append("tentative_date")
    return reasons


def record_diagnostic(diagnostics: dict[str, Any] | None, reason: str | None, stage: str, **fields: Any) -> None:
    """Populate an optional side channel without changing the legacy return API."""
    if diagnostics is not None:
        diagnostics.update(reason=reason, stage=stage, **fields)


def payment_date_rejection(value: str, reference_date: date | str) -> str | None:
    """Explain an exact-date validation result, without inventing another date."""
    try:
        resolved = date.fromisoformat(value)
    except (TypeError, ValueError):
        return "invalid_calendar_date"
    return "past_date" if resolved < _coerce_reference(reference_date) else None


def normalize_date_phrase(phrase: str) -> str:
    """Normalize equivalent spoken month/ordinal ordering without guessing."""
    text = re.sub(r"[,\.]", " ", (phrase or "").strip().lower())
    text = re.sub(r"\s+", " ", text).strip()
    text = _supported_relative_alias(text)
    text = re.sub(r"^(?:on\s+)?(?:the\s+)?", "", text)
    month_first = re.fullmatch(
        r"(next|this) month (?:the )?"
        r"(first|second|third|fourth|fifth|last|1st|2nd|3rd|4th|5th) (\w+)",
        text,
    )
    if month_first:
        which, ordinal, weekday = month_first.groups()
        text = f"{ordinal} {weekday} of {which} month"
    return text


def validate_payment_date(value: str | None, reference_date: date | str) -> str | None:
    """Require one valid calendar date on or after the call-local date.

    Explicit past promises are left unresolved for review; silently rolling a
    year/month forward would invent a new agreement. No arbitrary upper bound
    is imposed on a genuinely explicit future agreement.
    """
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return None
    try:
        resolved = date.fromisoformat(value)
    except ValueError:
        return None
    return resolved.isoformat() if resolved >= _coerce_reference(reference_date) else None


def resolve_deterministic(phrase: str, reference_date: date | str) -> str | None:
    """Resolve supported phrases and reject impossible or past payment dates."""
    ref = _coerce_reference(reference_date)
    try:
        resolved = _resolve_deterministic(phrase, ref)
    except (ValueError, OverflowError):
        return None
    return validate_payment_date(resolved, ref)


def _resolve_deterministic(phrase: str, reference_date: date | str) -> str | None:
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
    text = normalize_date_phrase(text)

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
        # 'this <weekday>' includes today; an already-passed weekday means
        # its upcoming occurrence, consistent with a future payment promise.
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
    diagnostics: dict[str, Any] | None = None,
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
    touch AWS. Optional ``diagnostics`` receives actual stage/reason codes while
    preserving the legacy tuple return value. This runs OFF the audio path.
    """
    ref = _coerce_reference(reference_date)
    if diagnostics is not None:
        diagnostics.clear()
        diagnostics["fallback_attempted"] = False
    text = (phrase or "").strip()
    if not text:
        record_diagnostic(diagnostics, "empty_phrase", "input")
        return None, VIA_UNRESOLVED

    issues = phrase_clarification_reasons(text)
    if issues:
        record_diagnostic(diagnostics, issues[0], "clarification", phrase_issues=issues)
        return None, VIA_UNRESOLVED

    # TIER 1: deterministic fast path (sub-millisecond, no network/AWS).
    try:
        candidate = _resolve_deterministic(text, ref)
    except (ValueError, OverflowError):
        candidate = None
    deterministic = validate_payment_date(candidate, ref)
    if deterministic:
        record_diagnostic(diagnostics, None, "deterministic")
        logger.info(
            "payment date resolved by deterministic grammar",
            extra={
                "event_type": "tool_invocation",
                "tool": "resolve_date_phrase",
                "resolved_via": VIA_DETERMINISTIC,
            },
        )
        return deterministic, VIA_DETERMINISTIC

    # A known grammar with no valid result (past date, impossible ordinal or
    # overflowing offset) must not be reinterpreted by a model into a new date.
    normalized = normalize_date_phrase(text)
    if candidate:
        record_diagnostic(diagnostics, payment_date_rejection(candidate, ref), "deterministic")
        return None, VIA_UNRESOLVED
    if (
        re.fullmatch(r"\d{4}-\d{2}-\d{2}", text)
        or _RE_NTH_WEEKDAY.fullmatch(normalized)
        or _RE_IN_N.fullmatch(normalized)
    ):
        reason = (payment_date_rejection(text, ref) if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text)
                  else "invalid_date_expression")
        record_diagnostic(diagnostics, reason, "deterministic")
        return None, VIA_UNRESOLVED

    # TIER 2: model-authored AgentCore Code Interpreter fallback.
    from app import code_interpreter

    record_diagnostic(diagnostics, None, "code_generation", fallback_attempted=True,
                      deterministic_miss="unsupported_phrase")
    resolved = code_interpreter.resolve_date_phrase(
        text,
        reference_date=ref.isoformat(),
        region=region,
        model_id=model_id,
        bedrock_client=bedrock_client,
        client_factory=code_interpreter_factory,
        diagnostics=diagnostics,
    )
    resolved = validate_payment_date(resolved, ref)
    via = VIA_CODE_INTERPRETER if resolved else VIA_UNRESOLVED
    logger.info(
        "payment date fallback completed (resolved_via=%s)",
        via,
        extra={
            "event_type": "tool_invocation",
            "tool": "resolve_date_phrase",
            "resolved_via": via,
        },
    )
    return resolved, via
