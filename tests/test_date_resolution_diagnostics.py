"""Future-facing clarification and factual resolution diagnostics, fully offline."""

from dataclasses import replace
import json
import logging

import pytest

from app import code_interpreter, date_resolver, postcall
from app.collections_agent import get_system_prompt
from app.config import get_settings
from app.store import InMemoryStore


REFERENCE = "2026-10-22"
AMBIGUOUS = "second week of next month maybe wednesday"
UNSUPPORTED = "the day after the regional holiday"
SECRET = "SYNTHETIC_DIAGNOSTIC_PAYLOAD"


class Model:
    def __init__(self, *, program="print('2026-11-11')", fail=False, extraction=None):
        self.program, self.fail, self.extraction = program, fail, extraction
        self.calls = 0

    def converse(self, **kwargs):
        self.calls += 1
        if self.extraction is not None and self.calls == 1:
            text = json.dumps(self.extraction)
        elif self.fail:
            raise RuntimeError(SECRET)
        else:
            text = self.program
        return {"output": {"message": {"content": [{"text": text}]}}}


class Sandbox:
    def __init__(self, output="2026-11-11\n", *, is_error=False, fail=False):
        self.output, self.is_error, self.fail = output, is_error, fail
        self.started = self.stopped = False

    def start(self):
        self.started = True

    def invoke(self, method, parameters):
        if self.fail:
            raise RuntimeError(SECRET)
        return {"stream": [{"result": {"isError": self.is_error,
                                        "content": [{"type": "text", "text": self.output}]}}]}

    def stop(self):
        self.stopped = True


def _forbidden(_):
    pytest.fail("this phrase must not start a code interpreter")


def test_tentative_week_phrase_requires_clarification_without_inventing_a_date():
    diagnostics = {}
    model = Model()
    assert date_resolver.resolve_date_hybrid(
        AMBIGUOUS, REFERENCE, region="us-east-1", bedrock_client=model,
        code_interpreter_factory=_forbidden, diagnostics=diagnostics,
    ) == (None, "unresolved")
    assert model.calls == 0
    assert diagnostics == {
        "reason": "ambiguous_week_of_month", "stage": "clarification", "fallback_attempted": False,
        "phrase_issues": ["ambiguous_week_of_month", "tentative_date"],
    }
    # Direct use of the legacy fallback cannot bypass the clarification guard.
    assert code_interpreter.resolve_date_phrase(
        AMBIGUOUS, reference_date=REFERENCE, region="us-east-1",
        bedrock_client=model, client_factory=_forbidden,
    ) is None
    assert model.calls == 0


@pytest.mark.parametrize("phrase,reason", [
    ("second week of next month Wednesday", "ambiguous_week_of_month"),
    ("second week of the next month Wednesday", "ambiguous_week_of_month"),
    ("Wednesday of the second week in the next month", "ambiguous_week_of_month"),
    ("Wednesday in the second week of November", "ambiguous_week_of_month"),
    ("next month the second week Wednesday", "ambiguous_week_of_month"),
    ("maybe second Wednesday next month", "tentative_date"),
    ("perhaps tomorrow", "tentative_date"),
])
def test_uncertainty_is_not_silently_removed(phrase, reason):
    diagnostics = {}
    assert date_resolver.resolve_date_hybrid(
        phrase, REFERENCE, region="us-east-1", bedrock_client=Model(),
        code_interpreter_factory=_forbidden, diagnostics=diagnostics,
    ) == (None, "unresolved")
    assert diagnostics["reason"] == reason and diagnostics["fallback_attempted"] is False


def test_confirmed_ordinal_weekday_remains_calculable_without_network():
    diagnostics = {}
    assert date_resolver.resolve_date_hybrid(
        "second Wednesday next month", REFERENCE, region="us-east-1",
        bedrock_client=Model(), code_interpreter_factory=_forbidden, diagnostics=diagnostics,
    ) == ("2026-11-11", "deterministic")
    assert diagnostics == {"reason": None, "stage": "deterministic", "fallback_attempted": False}


@pytest.mark.parametrize("output,expected", [
    ("UNRESOLVED\n", "model_unresolved"),
    (SECRET, "invalid_model_output"),
    ("2026-11-11\n2026-11-12\n", "invalid_model_output"),
    ("2026-02-30\n", "invalid_calendar_date"),
    ("2024-07-01\n", "past_date"),
])
def test_fallback_outcome_is_distinct_from_unsupported_local_grammar(output, expected, caplog):
    diagnostics = {}
    sandbox = Sandbox(output)
    with caplog.at_level(logging.INFO):
        result = date_resolver.resolve_date_hybrid(
            UNSUPPORTED, REFERENCE, region="us-east-1", bedrock_client=Model(program=SECRET),
            code_interpreter_factory=lambda _: sandbox, diagnostics=diagnostics,
        )
    assert result == (None, "unresolved")
    assert diagnostics["reason"] == expected
    assert diagnostics["stage"] == "output_validation"
    assert diagnostics["deterministic_miss"] == "unsupported_phrase"
    assert diagnostics["fallback_attempted"] and diagnostics["code_generation_attempted"]
    assert diagnostics["sandbox_attempted"] and sandbox.stopped
    assert SECRET not in json.dumps(diagnostics) + caplog.text


@pytest.mark.parametrize("failure,expected,stage", [
    ("model", "code_generation_failed", "code_generation"),
    ("empty", "empty_model_program", "code_generation"),
    ("sandbox", "code_interpreter_failed", "code_interpreter"),
    ("execution", "code_interpreter_execution_error", "code_interpreter"),
])
def test_actual_provider_and_execution_failures_are_not_labelled_ambiguity(failure, expected, stage, caplog):
    diagnostics = {}
    model = Model(program="" if failure == "empty" else SECRET, fail=failure == "model")
    sandbox = Sandbox(SECRET, fail=failure == "sandbox", is_error=failure == "execution")
    with caplog.at_level(logging.INFO):
        assert date_resolver.resolve_date_hybrid(
            UNSUPPORTED, REFERENCE, region="us-east-1", bedrock_client=model,
            code_interpreter_factory=lambda _: sandbox, diagnostics=diagnostics,
        ) == (None, "unresolved")
    assert diagnostics["reason"] == expected and diagnostics["stage"] == stage
    assert "phrase_issues" not in diagnostics
    assert sandbox.started is (failure in {"sandbox", "execution"})
    assert SECRET not in json.dumps(diagnostics) + caplog.text


@pytest.mark.parametrize("phrase,reason", [
    ("2024-07-01", "past_date"), ("2026-02-30", "invalid_calendar_date"),
    ("first Monday of this month", "past_date"),
    ("fifth Tuesday of next month", "invalid_date_expression"),
])
def test_known_invalid_dates_report_their_actual_rejection(phrase, reason):
    diagnostics = {}
    assert date_resolver.resolve_date_hybrid(
        phrase, REFERENCE, region="us-east-1", bedrock_client=Model(),
        code_interpreter_factory=_forbidden, diagnostics=diagnostics,
    ) == (None, "unresolved")
    assert diagnostics["reason"] == reason and not diagnostics["fallback_attempted"]


@pytest.mark.asyncio
@pytest.mark.parametrize("phrase,output,fail_model,reason,resolved", [
    (AMBIGUOUS, "2026-11-11\n", False, "ambiguous_week_of_month", None),
    ("second Wednesday next month", "unused", False, None, "2026-11-11"),
    (UNSUPPORTED, "UNRESOLVED\n", False, "model_unresolved", None),
    (UNSUPPORTED, "2024-07-01\n", False, "past_date", None),
    (UNSUPPORTED, SECRET, False, "invalid_model_output", None),
    (UNSUPPORTED, "unused", True, "code_generation_failed", None),
])
async def test_postcall_persists_specific_reason_and_emits_safe_structured_evidence(
    phrase, output, fail_model, reason, resolved, monkeypatch,
):
    store = InMemoryStore()
    record = {"call_sid": "CA_synthetic_diagnostics", "account_id": "ACC-1001",
              "created_at": "2026-10-22T08:00:00+00:00",
              "payment_date_capture": {"phrase": phrase, "call_sid": "CA_synthetic_diagnostics",
                                       "reference_date": REFERENCE, "reference_timezone": "Asia/Kolkata"}}
    store.save_call_analytics(record)
    store.update_due("ACC-1001", promised_date_phrase=phrase, promised_date_phrase_call_sid=record["call_sid"])
    settings = replace(get_settings(), twilio_account_sid=None, twilio_auth_token=None,
                       payment_timezone="Asia/Kolkata")
    model = Model(program=SECRET, fail=fail_model, extraction={
        "disposition": "promise_to_pay",
        "payment_specifics": {"promised_amount": None, "agreed_date": "2026-11-11",
                              "preferred_payment_method": None},
        "contextual_data": {"reason_for_delinquency": None, "customer_sentiment": None},
        "next_best_action": "Review the payment date before following up.",
    })
    sandbox = Sandbox(output)
    logged = []
    monkeypatch.setattr(logging.getLogger("ar_collections.metrics"), "info",
                        lambda message, **kwargs: logged.append(kwargs["extra"]["data"]))
    changes = await postcall.extract_and_apply(
        record, store, settings, bedrock_client=model, code_interpreter_factory=lambda _: sandbox,
    )
    resolution = changes["payment_date_resolution"]
    assert changes["intelligence_status"] == "ready"
    assert changes["structured_data"]["payment_specifics"]["agreed_date"] == resolved
    assert resolution["reason"] == reason and resolution["requires_review"] is (resolved is None)
    assert store.get_call(record["call_sid"])["payment_date_resolution"] == resolution
    assert store.get_due("ACC-1001").get("promised_date") == resolved
    assert changes["ptp_sms_status"] is None
    date_logs = [event for event in logged if event["event_type"] == "payment_date_resolution"]
    assert len(date_logs) == 1
    assert date_logs[0]["reason"] == reason
    assert "phrase" not in date_logs[0] and "phrase" not in resolution["diagnostics"]
    assert any(event["event_type"] == "postcall_intelligence_ready" for event in logged)
    assert SECRET not in json.dumps(logged) + json.dumps(resolution["diagnostics"])
    if phrase == AMBIGUOUS:
        assert model.calls == 1 and not sandbox.started


def test_voice_prompt_asks_for_confirmation_without_inventing_a_calendar_date():
    prompt = get_system_prompt(InMemoryStore().get_due("ACC-1001"))
    assert "Do you mean the second Wednesday of next month, and can you confirm that as your payment date?" in prompt
    assert "Do not invent a calendar date" in prompt
    assert "If it remains tentative, preserve that wording" in prompt
