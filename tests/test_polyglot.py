"""Polyglot prompt/tool contracts and real controller handshakes with fake I/O."""

import asyncio

import pytest
from strands.bidi.types.events import BidiBargeInEvent

from app import date_resolver, postcall
from app.call_lifecycle import CallState, TerminationBounds
from app.collections_agent import CallToolRuntime, build_tools, get_system_prompt
from app.config import get_settings
from app.sonic_bridge import DEFAULT_KICKOFF_TEXT, DEFAULT_VOICE
from app.store import InMemoryStore
from tests.test_call_lifecycle import Harness
from tests.test_payment_date_provenance import Extractor, _no_sandbox, _record, _settings_without_network


@pytest.mark.asyncio
@pytest.mark.parametrize("language,text", [
    ("en", "Thank you for your time. Goodbye."),
    ("fr", "Merci pour votre temps. AU REVOIR !"),
    ("es", "Gracias por su tiempo. ADIOS!"),
])
async def test_localized_farewell_requires_complete_audio_text_and_playback_ack(language, text):
    h = Harness()
    await h.ready(language=language)
    h.farewell(text=text)
    mark = await h.mark()
    assert h.controller.summary["closing_language"] == language
    assert not h.hangups and not h.saved
    assert not h.controller.acknowledge_playback(mark, "wrong-stream")
    assert h.controller.acknowledge_playback(mark, "MZ_fake")
    await h.controller.closed.wait()
    assert len(h.hangups) == len(h.saved) == 1
    assert h.saved[0][1]["termination"]["closing_language"] == language
    assert h.saved[0][1]["termination"]["outcome"] == "farewell_played"


@pytest.mark.asyncio
@pytest.mark.parametrize("language,wrong_text", [
    ("fr", "Thank you for your time. Goodbye."),
    ("es", "Merci pour votre temps. Au revoir."),
    ("en", "Gracias por su tiempo. Adiós."),
    ("es", "Podemos hablar de estadios."),
    ("fr", "Un tableau revoir la facture."),
])
async def test_another_languages_phrase_does_not_authorize_closing(language, wrong_text):
    h = Harness(bounds=TerminationBounds(.1, .02, .02, .03, .03))
    await h.ready(language=language)
    h.farewell(text=wrong_text)
    await asyncio.sleep(.04)
    assert h.controller.state == CallState.ACTIVE
    assert not h.session.marks and not h.hangups
    assert len(h.session.prompts) == 1
    await h.finish()


@pytest.mark.asyncio
async def test_duplicate_end_request_cannot_change_an_attempts_language():
    h = Harness()
    attempt_id = await h.ready(language="fr")
    duplicate = await h.controller.request_end(language="es", tool_use_id="duplicate")
    assert duplicate["attempt_id"] == attempt_id
    assert duplicate["language"] == "fr" and duplicate["status"] == "pending"
    assert h.controller.attempt.closing_phrase == "Au revoir"
    invalid = await h.controller.request_end(language="de", tool_use_id="duplicate")
    assert invalid["reason"] == "unsupported_conversation_language"
    assert h.controller.attempt.id == attempt_id and h.controller.attempt.language == "fr"
    assert h.controller.attempt_count == 1
    h.farewell(text="Merci pour votre temps. Au revoir.")
    await h.mark()
    await h.finish()


@pytest.mark.asyncio
async def test_interruption_can_select_new_language_but_cannot_replay_old_nonce():
    h = Harness()
    old = await h.ready(language="fr")
    h.emit(BidiBargeInEvent())
    new = await h.ready(language="es", end_id="end-2", begin_id="begin-2")
    assert old != new
    replay = await h.controller.begin_farewell(old, tool_use_id="begin-2")
    assert replay["status"] == "error"
    assert h.controller.attempt.language == "es"
    h.farewell(text="Gracias por su tiempo. Adiós.")
    mark = await h.mark()
    assert h.controller.acknowledge_playback(mark, "MZ_fake")
    await h.controller.closed.wait()
    assert h.saved[0][1]["termination"]["closing_language"] == "es"


@pytest.mark.asyncio
@pytest.mark.parametrize("language", ["de", "French", "FR", "", None])
async def test_unsupported_language_never_creates_an_ending_attempt(language):
    h = Harness()
    h.register(("end", "end_conversation"))
    result = await h.controller.request_end(language=language, tool_use_id="end")
    assert result["status"] == "error" and result["reason"] == "unsupported_conversation_language"
    assert h.controller.attempt is None and h.controller.attempt_count == 0
    assert h.controller.state == CallState.ACTIVE
    assert not h.session.marks and not h.hangups
    await h.finish()


@pytest.mark.asyncio
async def test_configured_english_farewell_is_bound_before_settings_can_change():
    h = Harness()
    h.controller.closing_greeting = "Thank you. Take care."
    h.controller.closing_phrase = "Take care"
    await h.ready(language="en")
    h.controller.closing_greeting = "Changed later."
    h.controller.closing_phrase = "Changed later"
    h.farewell(text="Thank you. Take care.")
    await h.mark()
    await h.finish()


def test_tool_schema_limits_closing_language_and_forwards_it_without_translation():
    seen = []
    class Lifecycle:
        async def request_end(self, **kwargs):
            seen.append(kwargs)
            return {"status": "ready", "ready": True, "attempt_id": "synthetic", "language": kwargs["language"]}
    tool = next(t for t in build_tools(InMemoryStore(), runtime=CallToolRuntime(lifecycle=Lifecycle()))
                if t.tool_name == "end_conversation")
    schema = tool.tool_spec["inputSchema"]["json"]["properties"]["language"]
    assert schema["enum"] == ["en", "fr", "es"]
    result = asyncio.run(tool.__wrapped__(reason="caller finished", language="fr"))
    assert result["language"] == "fr" and seen[0]["language"] == "fr"


def test_prompt_and_safe_config_describe_native_three_language_policy():
    prompt = get_system_prompt(InMemoryStore().get_due("ACC-1001"))
    assert "only English, French, and Spanish" in prompt
    assert "original language" in prompt and "Do not translate or paraphrase" in prompt
    assert "English" in DEFAULT_KICKOFF_TEXT and DEFAULT_VOICE == "tiffany"
    summary = get_settings().safe_summary()
    assert summary["conversation_languages"] == ["en", "fr", "es"]
    assert summary["polyglot_voice"] == DEFAULT_VOICE


@pytest.mark.parametrize("phrase,expected", [
    ("demain", "2026-10-23"), ("après-demain", "2026-10-24"),
    ("mañana", "2026-10-23"), ("pasado mañana", "2026-10-24"),
    ("dans dix jours", "2026-11-01"), ("dentro de diez días", "2026-11-01"),
    ("dans deux semaines", "2026-11-05"), ("en dos semanas", "2026-11-05"),
    ("dans un mois", "2026-11-22"), ("dentro de un mes", "2026-11-22"),
    ("Dans dix jours.", "2026-11-01"), ("Dentro de diez días.", "2026-11-01"),
])
def test_unambiguous_multilingual_relative_dates_use_existing_deterministic_math(phrase, expected):
    assert date_resolver.resolve_date_hybrid(phrase, "2026-10-22", region="us-east-1",
                                            code_interpreter_factory=_no_sandbox) == (expected, "deterministic")


@pytest.mark.parametrize("phrase,reason", [
    ("peut-être mercredi", "tentative_date"), ("peut etre demain", "tentative_date"),
    ("quizás el miércoles", "tentative_date"), ("tal vez mañana", "tentative_date"),
    ("no estoy segura, mañana", "tentative_date"),
    ("la deuxième semaine du mois prochain", "ambiguous_week_of_month"),
    ("la segunda semana del próximo mes", "ambiguous_week_of_month"),
    ("la tercera semana de noviembre", "ambiguous_week_of_month"),
])
def test_multilingual_ambiguity_never_reaches_model_or_sandbox(phrase, reason):
    diagnostics = {}
    assert date_resolver.resolve_date_hybrid(phrase, "2026-10-22", region="us-east-1", code_interpreter_factory=_no_sandbox,
                                            diagnostics=diagnostics) == (None, "unresolved")
    assert diagnostics["reason"] == reason and diagnostics["fallback_attempted"] is False


@pytest.mark.parametrize("phrase", ["le deuxième mercredi du mois prochain", "el segundo miércoles del próximo mes"])
def test_ordinal_weekday_is_not_confused_with_ordinal_week(phrase):
    assert date_resolver.phrase_clarification_reasons(phrase) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("phrase", ["dans dix jours", "dentro de diez días"])
async def test_original_multilingual_capture_survives_english_model_translation(phrase):
    store = InMemoryStore()
    capture = {"phrase": phrase, "call_sid": "CA_synthetic_date", "reference_date": "2026-10-22",
               "reference_timezone": "Asia/Kolkata"}
    record = _record(conversation_turns=[{"speaker": "user", "text": phrase}], payment_date_capture=capture)
    store.save_call_analytics(record)
    changes = await postcall.extract_and_apply(record, store, _settings_without_network(),
        bedrock_client=Extractor("in ten days"), code_interpreter_factory=_no_sandbox)
    assert changes["payment_date_resolution"]["phrase"] == phrase
    assert changes["payment_date_resolution"]["resolved_date"] == "2026-11-01"
    assert changes["payment_date_resolution"]["model_date_overridden"] is True
    assert changes["ptp_sms_status"] is None
