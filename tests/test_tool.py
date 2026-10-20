"""Tests for the collections agent: tool mutation + system prompt content.

The in-call date tool now only CAPTURES the raw natural-language phrase; the
concrete calendar date is resolved off-path by the AgentCore Code Interpreter
(see tests/test_code_interpreter.py and tests/test_postcall.py). So there is no
longer any in-process date math to assert here.
"""

from __future__ import annotations

from app.collections_agent import build_tools, get_system_prompt
from app.store import InMemoryStore


def _call_tool(decorated_tool, **kwargs):
    """Invoke a Strands @tool directly via its wrapped original function."""
    func = getattr(decorated_tool, "__wrapped__", decorated_tool)
    return func(**kwargs)


def test_update_due_record_mutates_store_and_returns_confirmation():
    store = InMemoryStore()
    tools = build_tools(store)
    assert len(tools) == 2
    tool_names = {t.tool_name for t in tools}
    assert "update_due_record" in tool_names
    assert "capture_payment_date_phrase" in tool_names
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


def test_update_due_record_invalid_status_falls_back_to_open():
    store = InMemoryStore()
    update_tool = build_tools(store)[0]
    _call_tool(update_tool, account_id="ACC-1002", new_status="not-a-status")
    assert store.get_due("ACC-1002")["status"] == "open"


def test_update_due_record_unknown_account_returns_message_not_raises():
    store = InMemoryStore()
    update_tool = build_tools(store)[0]
    msg = _call_tool(update_tool, account_id="GHOST", new_status="paid")
    assert "GHOST" in msg
    assert "could not" in msg.lower() or "unable" in msg.lower()


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
    assert isinstance(msg, str) and "in two weeks" in msg


def test_capture_payment_date_phrase_empty_phrase_asks_again():
    store = InMemoryStore()
    tool = _capture_phrase_tool(store)
    msg = _call_tool(tool, phrase="   ", account_id="ACC-1001")
    assert "again" in msg.lower() or "didn't" in msg.lower()


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
