"""Tests for the collections agent: tool mutation + system prompt content."""

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
    assert "compute_future_date" in tool_names
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


def _compute_future_date_tool():
    tools = build_tools(InMemoryStore())
    return next(t for t in tools if t.tool_name == "compute_future_date")


def test_compute_future_date_next_month_first_monday():
    tool = _compute_future_date_tool()
    # Reference 2024-05-15 (a Wednesday). First Monday of next month (June 2024)
    # is 2024-06-03.
    msg = _call_tool(tool, phrase="next month 1st Monday", reference_date="2024-05-15")
    assert "2024-06-03" in msg

    # Both phrasings resolve identically.
    msg2 = _call_tool(
        tool, phrase="first Monday of next month", reference_date="2024-05-15"
    )
    assert "2024-06-03" in msg2


def test_compute_future_date_in_n_weeks_and_days():
    tool = _compute_future_date_tool()
    # "in two weeks" from 2024-05-15 -> 2024-05-29.
    assert "2024-05-29" in _call_tool(
        tool, phrase="in two weeks", reference_date="2024-05-15"
    )
    # Digit form: "in 10 days" from 2024-05-15 -> 2024-05-25.
    assert "2024-05-25" in _call_tool(
        tool, phrase="in 10 days", reference_date="2024-05-15"
    )


def test_compute_future_date_end_of_next_month_and_tomorrow():
    tool = _compute_future_date_tool()
    # End of next month from 2024-05-15 -> 2024-06-30.
    assert "2024-06-30" in _call_tool(
        tool, phrase="the end of next month", reference_date="2024-05-15"
    )
    # Tomorrow.
    assert "2024-05-16" in _call_tool(
        tool, phrase="tomorrow", reference_date="2024-05-15"
    )


def test_compute_future_date_next_weekday():
    tool = _compute_future_date_tool()
    # 2024-05-15 is Wednesday; "next Friday" -> 2024-05-17.
    assert "2024-05-17" in _call_tool(
        tool, phrase="next Friday", reference_date="2024-05-15"
    )


def test_compute_future_date_unparseable_returns_graceful_message():
    tool = _compute_future_date_tool()
    msg = _call_tool(tool, phrase="whenever I feel like it", reference_date="2024-05-15")
    assert "couldn't" in msg.lower() or "specific" in msg.lower()


def test_system_prompt_mentions_compute_future_date():
    store = InMemoryStore()
    prompt = get_system_prompt(store.get_due("ACC-1001"))
    assert "compute_future_date" in prompt


def test_system_prompt_contains_debtor_amount_due_date_and_ai_identity():
    store = InMemoryStore()
    record = store.get_due("ACC-1001")
    prompt = get_system_prompt(record)

    assert record["debtor_name"] in prompt
    assert "428.57" in prompt  # amount due formatted
    assert record["due_date"] in prompt
    assert "AI assistant" in prompt
