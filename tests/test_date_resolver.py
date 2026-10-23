"""Unit tests for the HYBRID date resolver (app.date_resolver).

TIER 1 - the pure deterministic resolver (:func:`resolve_deterministic`) - is
pinned against a FIXED reference date (2026-10-19) for the common/enumerable
grammar, including ordinal weekdays in the following month:

    reference 2026-10-19, "second Tuesday of next month" -> 2026-11-10

The hybrid entrypoint (:func:`resolve_date_hybrid`) is proven to:

  * resolve common phrases via the DETERMINISTIC tier WITHOUT ever invoking the
    (stubbed) code interpreter - a stub factory asserts it is never called; and
  * FALL BACK to the stubbed code interpreter ONLY for an unparseable/open-ended
    phrase, labelling the result ``'code_interpreter'``.

No AWS call happens: the code-interpreter fallback is exercised through injected
``bedrock_client`` + ``code_interpreter_factory`` stubs.
"""

from __future__ import annotations

import pytest

from app import date_resolver
from app.date_resolver import (
    VIA_CODE_INTERPRETER,
    VIA_DETERMINISTIC,
    VIA_UNRESOLVED,
    resolve_date_hybrid,
    resolve_deterministic,
)

# Fixed reference date for all pins (a Monday, 2026-10-19).
REF = "2026-10-19"


class NeverCalledInterpreter:
    """Code-interpreter stub that FAILS the test if it is ever touched.

    Injected into the hybrid resolver for deterministic-hit cases so any call
    to start()/invoke() (i.e. a leaked fallback) raises immediately.
    """

    instantiated = 0

    def __init__(self, region):  # noqa: D401 - stub
        NeverCalledInterpreter.instantiated += 1
        raise AssertionError(
            "code interpreter must NOT be constructed on a deterministic hit"
        )

    def start(self):  # pragma: no cover - never reached
        raise AssertionError("code interpreter start() must not be called")

    def invoke(self, method, params=None):  # pragma: no cover
        raise AssertionError("code interpreter invoke() must not be called")

    def stop(self):  # pragma: no cover
        pass


class NeverCalledBedrock:
    """Bedrock stub that fails the test if ``converse`` is ever invoked."""

    def converse(self, **kwargs):  # pragma: no cover - must not run
        raise AssertionError("bedrock converse() must not run on a deterministic hit")


# -- TIER 1: deterministic grammar pins (fixed reference 2026-10-19) -------


@pytest.mark.parametrize(
    "phrase,expected",
    [
        # Ordinal weekday within the following month.
        ("second Tuesday of next month", "2026-11-10"),
        # Other common/enumerable phrases.
        ("next Friday", "2026-10-23"),
        ("in two weeks", "2026-11-02"),
        ("end of next month", "2026-11-30"),
        ("first Monday of next month", "2026-11-02"),
        ("1st Monday of next month", "2026-11-02"),
        ("last Friday of next month", "2026-11-27"),
        # Broader grammar coverage.
        ("tomorrow", "2026-10-20"),
        ("next week", "2026-10-26"),
        ("next month", "2026-11-19"),
        ("this Wednesday", "2026-10-21"),
        ("in 3 days", "2026-10-22"),
        ("in two months", "2026-12-19"),
        ("end of this month", "2026-10-31"),
        ("end of the month", "2026-10-31"),
        ("third Thursday of next month", "2026-11-19"),
        # Already-ISO short-circuits to deterministic.
        ("2026-12-25", "2026-12-25"),
    ],
)
def test_resolve_deterministic_pins(phrase, expected):
    assert resolve_deterministic(phrase, REF) == expected


def test_second_tuesday_of_next_month_is_2026_11_10():
    """Ordinal weekday resolution uses the fixed reference date."""
    assert resolve_deterministic("second Tuesday of next month", REF) == "2026-11-10"


@pytest.mark.parametrize(
    "phrase",
    [
        "the day after my next payday",
        "whenever my bonus clears",
        "sometime after the holidays",
        "when I get paid",
        "",
        "   ",
    ],
)
def test_resolve_deterministic_returns_none_for_unparseable(phrase):
    """Anything outside the grammar returns None (it never guesses)."""
    assert resolve_deterministic(phrase, REF) is None


def test_resolve_deterministic_accepts_date_object_reference():
    from datetime import date

    assert resolve_deterministic("tomorrow", date(2026, 10, 19)) == "2026-10-20"


# -- HYBRID: deterministic hits NEVER touch the interpreter ----------------


@pytest.mark.parametrize(
    "phrase,expected",
    [
        ("second Tuesday of next month", "2026-11-10"),
        ("next Friday", "2026-10-23"),
        ("in two weeks", "2026-11-02"),
        ("end of next month", "2026-11-30"),
        ("first Monday of next month", "2026-11-02"),
        ("1st Monday of next month", "2026-11-02"),
        ("last Friday of next month", "2026-11-27"),
    ],
)
def test_hybrid_deterministic_hit_never_calls_interpreter(phrase, expected):
    NeverCalledInterpreter.instantiated = 0
    iso, via = resolve_date_hybrid(
        phrase,
        REF,
        region="us-east-1",
        bedrock_client=NeverCalledBedrock(),
        code_interpreter_factory=NeverCalledInterpreter,
    )
    assert iso == expected
    assert via == VIA_DETERMINISTIC
    # The stub factory raises if constructed; also assert it was never built.
    assert NeverCalledInterpreter.instantiated == 0


def test_hybrid_already_iso_short_circuits_to_deterministic():
    iso, via = resolve_date_hybrid(
        "2026-12-25",
        REF,
        region="us-east-1",
        bedrock_client=NeverCalledBedrock(),
        code_interpreter_factory=NeverCalledInterpreter,
    )
    assert iso == "2026-12-25"
    assert via == VIA_DETERMINISTIC


# -- HYBRID: unparseable phrases FALL BACK to the stubbed interpreter ------


class StubInterpreter:
    """AgentCore stub that returns a canned ISO date, recording that it ran."""

    def __init__(self, region, canned_iso="2027-02-14"):
        self.region = region
        self.canned_iso = canned_iso
        self.started = False
        self.invoked = False
        self.stopped = False

    def start(self):
        self.started = True

    def invoke(self, method, params=None):
        self.invoked = True
        return {
            "stream": [
                {
                    "result": {
                        "isError": False,
                        "content": [{"type": "text", "text": self.canned_iso + "\n"}],
                    }
                }
            ]
        }

    def stop(self):
        self.stopped = True


class StubBedrock:
    """Bedrock stub returning a canned model-authored program; records calls."""

    def __init__(self, program="print('2027-02-14')"):
        self.program = program
        self.calls: list[dict] = []

    def converse(self, **kwargs):
        self.calls.append(kwargs)
        return {"output": {"message": {"content": [{"text": self.program}]}}}


def test_hybrid_unparseable_falls_back_to_code_interpreter():
    """A deterministic miss defers to the (stubbed) interpreter, labelled so."""
    built: list[StubInterpreter] = []

    def factory(region):
        c = StubInterpreter(region, canned_iso="2027-02-14")
        built.append(c)
        return c

    bedrock = StubBedrock(program="print('2027-02-14')")
    iso, via = resolve_date_hybrid(
        "the day after my next payday",
        REF,
        region="us-east-1",
        bedrock_client=bedrock,
        code_interpreter_factory=factory,
    )

    assert iso == "2027-02-14"
    assert via == VIA_CODE_INTERPRETER
    # The deterministic tier missed, so the model + sandbox actually ran.
    assert bedrock.calls, "the model should have been asked to author a program"
    assert built and built[0].started and built[0].invoked and built[0].stopped
    # The phrase + reference date were supplied to the model as arguments.
    user_text = bedrock.calls[0]["messages"][0]["content"][0]["text"]
    assert "the day after my next payday" in user_text
    assert REF in user_text


def test_hybrid_unresolved_when_interpreter_also_fails():
    """If the interpreter cannot resolve either, resolved_via is 'unresolved'."""

    def factory(region):
        c = StubInterpreter(region)
        c.canned_iso = "UNRESOLVED"  # yields no ISO; returns None
        return c

    bedrock = StubBedrock(program="print('UNRESOLVED')")
    iso, via = resolve_date_hybrid(
        "whenever my bonus clears",
        REF,
        region="us-east-1",
        bedrock_client=bedrock,
        code_interpreter_factory=factory,
    )
    assert iso is None
    assert via == VIA_UNRESOLVED


def test_hybrid_empty_phrase_is_unresolved_without_any_client():
    iso, via = resolve_date_hybrid(
        "   ",
        REF,
        region="us-east-1",
        bedrock_client=NeverCalledBedrock(),
        code_interpreter_factory=NeverCalledInterpreter,
    )
    assert iso is None
    assert via == VIA_UNRESOLVED


@pytest.mark.parametrize("phrase,reference,expected", [
    ("first monday of next month", "2026-10-22", "2026-11-02"),
    ("next month, the first Monday", "2026-10-22", "2026-11-02"),
    ("next month 1st Monday", "2026-10-22", "2026-11-02"),
    ("the first Monday next month", "2026-12-22", "2027-01-04"),
    ("last Monday of next month", "2028-01-15", "2028-02-28"),
    ("this Monday", "2026-10-19", "2026-10-19"),
    ("next Monday", "2026-10-19", "2026-10-26"),
])
def test_spoken_order_calendar_rollover_and_same_day(phrase, reference, expected):
    assert resolve_deterministic(phrase, reference) == expected


@pytest.mark.parametrize("phrase", [
    "2024-07-01", "2026-02-30", "first Monday of this month",
    "fifth Tuesday of next month", "in 999999999999999999999 days",
])
def test_invalid_or_past_known_dates_do_not_fall_back_to_model(phrase):
    # November 2026 has only four Tuesdays; a fifth must not become another date.
    resolved, via = resolve_date_hybrid(
        phrase, "2026-10-22", region="us-east-1",
        bedrock_client=NeverCalledBedrock(), code_interpreter_factory=NeverCalledInterpreter,
    )
    assert resolved is None
    assert via == VIA_UNRESOLVED


def test_weekday_math_is_independent_of_global_calendar_firstweekday():
    import calendar

    original = calendar.firstweekday()
    try:
        calendar.setfirstweekday(calendar.SUNDAY)
        assert resolve_deterministic("first Monday of next month", "2026-10-22") == "2026-11-02"
    finally:
        calendar.setfirstweekday(original)
