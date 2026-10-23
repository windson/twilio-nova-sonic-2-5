"""Tests for the collections agent: tool mutation + system prompt content.

The in-call date tool now only CAPTURES the raw natural-language phrase; the
concrete calendar date is resolved off-path by the AgentCore Code Interpreter
(see tests/test_code_interpreter.py and tests/test_postcall.py). So there is no
longer any in-process date math to assert here.
"""

from __future__ import annotations

import asyncio

from app.collections_agent import CallToolRuntime, build_tools, get_system_prompt
from app.store import InMemoryStore


def _call_tool(decorated_tool, **kwargs):
    """Invoke a Strands @tool directly via its wrapped original function."""
    func = getattr(decorated_tool, "__wrapped__", decorated_tool)
    return asyncio.run(func(**kwargs))


def test_update_due_record_mutates_store_and_returns_confirmation():
    store = InMemoryStore()
    tools = build_tools(store)
    assert len(tools) == 4
    tool_names = {t.tool_name for t in tools}
    assert "update_due_record" in tool_names
    assert "capture_payment_date_phrase" in tool_names
    assert "end_conversation" in tool_names
    assert "begin_farewell" in tool_names
    # The removed hand-written parser tool must be gone.
    assert "compute_future_date" not in tool_names
    update_tool = next(t for t in tools if t.tool_name == "update_due_record")

    confirmation = _call_tool(
        update_tool,
        account_id="ACC-1001",
        new_status="promised_to_pay",
        promised_amount=150.0,
        promised_date="2024-08-01",
        note="agreed to pay by August",
    )

    assert isinstance(confirmation, str) and confirmation
    assert "ACC-1001" in confirmation
    assert "promised to pay" in confirmation.lower()

    rec = store.get_due("ACC-1001")
    assert rec["status"] == "promised_to_pay"
    assert rec["promised_amount"] == 150.0
    assert rec["promised_date"] == "2024-08-01"
    assert rec["notes"] == "agreed to pay by August"


def test_update_due_record_invalid_status_returns_error_without_mutation():
    store = InMemoryStore()
    store.update_due("ACC-1002", status="callback", notes="Existing agreement")
    before = dict(store.get_due("ACC-1002"))
    update_tool = build_tools(store)[0]
    result = _call_tool(update_tool, account_id="ACC-1002", new_status="not-a-status")
    assert result["status"] == "error"
    assert store.get_due("ACC-1002") == before


def test_update_due_record_schema_constrains_status_to_supported_values():
    from app.store import DUE_STATUSES
    update_tool = build_tools(InMemoryStore())[0]
    schema = update_tool.tool_spec["inputSchema"]["json"]["properties"]["new_status"]
    assert schema["enum"] == list(DUE_STATUSES)


def test_update_due_record_unknown_account_returns_message_not_raises():
    store = InMemoryStore()
    update_tool = build_tools(store)[0]
    msg = _call_tool(update_tool, account_id="GHOST", new_status="paid")
    assert msg["status"] == "error"
    assert "GHOST" in msg["content"][0]["text"]


def _capture_phrase_tool(store):
    tools = build_tools(store)
    return next(t for t in tools if t.tool_name == "capture_payment_date_phrase")


def test_capture_payment_date_phrase_records_phrase_and_returns_promptly():
    store = InMemoryStore()
    tool = _capture_phrase_tool(store)

    msg = _call_tool(
        tool, phrase="next month, the first Monday", account_id="ACC-1001"
    )

    # Returns a short confirmation echoing the captured phrase (no date math).
    assert isinstance(msg, str) and msg
    assert "next month, the first Monday" in msg
    # The raw phrase is recorded on the due record for off-path resolution; no
    # concrete promised_date is computed in-call.
    rec = store.get_due("ACC-1001")
    assert rec["promised_date_phrase"] == "next month, the first Monday"
    assert rec.get("promised_date") is None


def test_capture_payment_date_phrase_unknown_account_does_not_raise():
    store = InMemoryStore()
    tool = _capture_phrase_tool(store)
    # Should not raise even if the account id is unknown.
    msg = _call_tool(tool, phrase="in two weeks", account_id="GHOST")
    assert msg["status"] == "error"
    assert "not saved" in msg["content"][0]["text"]


def test_capture_payment_date_phrase_empty_phrase_asks_again():
    store = InMemoryStore()
    tool = _capture_phrase_tool(store)
    msg = _call_tool(tool, phrase="   ", account_id="ACC-1001")
    assert msg["status"] == "error"
    assert "again" in msg["content"][0]["text"]


# --- Controller-backed terminal tools --------------------------------------


def _end_conversation_tool(store, **kwargs):
    tools = build_tools(store, **kwargs)
    return next(t for t in tools if t.tool_name == "end_conversation")


def test_build_tools_includes_end_conversation():
    store = InMemoryStore()
    tools = build_tools(store, call_sid="CA_live")
    assert len(tools) == 4
    names = {t.tool_name for t in tools}
    assert "end_conversation" in names
    assert "begin_farewell" in names


def test_end_conversation_returns_controller_readiness_without_claiming_hangup():
    store = InMemoryStore()
    calls = []

    class Lifecycle:
        async def request_end(self, reason=None, tool_use_id=None, language="en"):
            calls.append((reason, tool_use_id, language))
            return {"status": "ready", "attempt_id": "attempt-test"}

    runtime = CallToolRuntime(lifecycle=Lifecycle())
    tool = _end_conversation_tool(store, call_sid="CA_live", runtime=runtime)
    msg = _call_tool(tool, reason="caller said goodbye")

    assert msg == {"status": "ready", "attempt_id": "attempt-test"}
    assert len(calls) == 1 and calls[0][0] == "caller said goodbye"
    assert calls[0][1]
    assert calls[0][2] == "en"


def test_end_conversation_guards_controller_failure():
    store = InMemoryStore()

    class Lifecycle:
        async def request_end(self, **kwargs):
            raise RuntimeError("SYNTHETIC_CONTROLLER_SECRET")

    tool = _end_conversation_tool(store, runtime=CallToolRuntime(lifecycle=Lifecycle()))
    msg = _call_tool(tool)
    assert msg["status"] == "error"
    assert "SYNTHETIC_CONTROLLER_SECRET" not in str(msg)


def test_end_conversation_without_controller_reports_unavailable():
    store = InMemoryStore()
    tool = _end_conversation_tool(store, call_sid="CA_live")
    msg = _call_tool(tool)
    assert msg["status"] == "error"
    assert "unavailable" in msg["content"][0]["text"]


def test_end_conversation_logs_tool_invocation(caplog):
    import logging

    store = InMemoryStore()
    tool = _end_conversation_tool(store, call_sid="CA_live")
    with caplog.at_level(logging.INFO):
        _call_tool(tool)
    recs = [
        r
        for r in caplog.records
        if getattr(r, "event_type", None) == "tool_invocation"
        and getattr(r, "tool", None) == "end_conversation"
    ]
    assert recs, "end_conversation must log event_type=tool_invocation tool=end_conversation"


def test_system_prompt_instructs_end_conversation():
    store = InMemoryStore()
    prompt = get_system_prompt(store.get_due("ACC-1001"))
    assert "end_conversation" in prompt
    assert "begin_farewell" in prompt and "attempt_id" in prompt
    assert "goodbye" in prompt.lower()
    assert "Only after `begin_farewell` confirms readiness" in prompt
    assert "Thank you for your time. Goodbye." not in prompt


def test_system_prompt_instructs_capturing_date_phrase():
    store = InMemoryStore()
    prompt = get_system_prompt(store.get_due("ACC-1001"))
    # Prompt now instructs the agent to capture the phrase via the new tool and
    # no longer references the removed compute_future_date tool.
    assert "capture_payment_date_phrase" in prompt
    assert "compute_future_date" not in prompt


def test_system_prompt_contains_debtor_amount_due_date_and_ai_identity():
    store = InMemoryStore()
    record = store.get_due("ACC-1001")
    prompt = get_system_prompt(record)

    assert record["debtor_name"] in prompt
    assert "428.57" in prompt  # amount due formatted
    assert record["due_date"] in prompt
    assert "AI assistant" in prompt


def test_identity_question_precedes_account_discussion_and_matches_kickoff():
    from app.sonic_bridge import DEFAULT_KICKOFF_TEXT

    record = InMemoryStore().get_due("ACC-1001")
    prompt = get_system_prompt(record)
    identity = prompt.split("[IDENTITY]", 1)[1].split("[ACCOUNT]", 1)[0]
    opening = identity.split("Begin in English with only:", 1)[1].split("Then stop", 1)[0]
    assert opening.strip() == f"Hi, I'm Ava, an AI assistant. Am I speaking with {record['debtor_name']}?"
    assert "428.57" not in opening and record["due_date"] not in opening
    assert "Then stop and wait for the caller's answer" in identity
    assert "only after the caller explicitly confirms" in identity
    assert "A hello, silence, or an unrelated answer is not confirmation" in identity
    assert "leave the due record unchanged" in identity
    assert "intended-person question" in DEFAULT_KICKOFF_TEXT
    assert "then wait for their answer" in DEFAULT_KICKOFF_TEXT
    assert "only after they confirm their identity" in DEFAULT_KICKOFF_TEXT
    assert "state the exact outstanding amount" not in DEFAULT_KICKOFF_TEXT


def test_prompt_separates_live_tool_dispatch_from_acknowledgment():
    prompt = get_system_prompt(InMemoryStore().get_due("ACC-1001"))
    assert "emit the tool call alone, without a simultaneous spoken acknowledgment" in prompt
    assert "Answer new caller questions naturally while results are pending" in prompt
    assert "While a write is pending, acknowledge" not in prompt
    assert "[FINAL CHECK]" in prompt
