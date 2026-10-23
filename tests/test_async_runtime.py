"""Concurrency contracts exercised with the real pinned Strands agent.

The model and SDK network I/O are fakes; no phone, SMS, or Bedrock call occurs.
Event barriers prove audio progresses before an outstanding write completes,
without relying on a claimed production latency benchmark.
"""
from __future__ import annotations

import asyncio
import base64
import threading
from dataclasses import replace

import pytest

from app import main as main_mod
from app.call_lifecycle import CallLifecycleController, CallState
from app.collections_agent import CallToolRuntime, build_tools
from app.config import get_settings
from app.metrics import CallMetrics
from app.sonic_bridge import build_model
from app.store import InMemoryStore
from strands.bidi import BidiAgent
from strands.bidi.models.model import BidiModel
from strands.bidi.types.events import BidiAudioDeltaEvent, BidiToolUseBlocksEvent
from strands.bidi.types.media import AudioDelta
from strands.types.tools import ToolResultBlock


class BlockingStore(InMemoryStore):
    def __init__(self):
        super().__init__()
        self.entered = threading.Event()
        self.release = threading.Event()
        self.calls = 0

    def update_due(self, account_id, **changes):
        self.calls += 1
        self.entered.set()
        if not self.release.wait(timeout=3):
            raise TimeoutError("test did not release the fake SDK")
        return super().update_due(account_id, **changes)


class FakeStreamingModel(BidiModel):
    """Actual BidiAgent scheduler with deterministic network-free model events."""
    def __init__(self, lifecycle=None):
        self.events = asyncio.Queue()
        self.sent = []
        self.result_sent = asyncio.Event()
        self.lifecycle = lifecycle

    def get_config(self):
        return {"model_id": "fake-sonic", "connection": {"auto_restart": False}}

    def update_config(self, **config):
        pass

    async def start(self, **kwargs):
        pass

    async def stop(self):
        pass

    async def send(self, content):
        self.sent.append(content)
        results = [block for block in getattr(content, "content", []) if isinstance(block, ToolResultBlock)]
        if results:
            if self.lifecycle is not None:
                self.lifecycle.results_submitted([block.tool_use_id for block in results])
            self.result_sent.set()

    async def receive(self):
        while True:
            event = await self.events.get()
            if self.lifecycle is not None:
                self.lifecycle.observe_provider_event(event)
            yield event


@pytest.mark.asyncio
async def test_real_strands_scheduler_keeps_audio_live_while_tool_write_is_pending():
    store = BlockingStore()
    metrics = CallMetrics(call_sid="CA_async_test", account_id="ACC-1001")
    metrics.on_session_start()
    runtime = CallToolRuntime(event_sink=metrics.record_tool_execution)
    model = FakeStreamingModel()
    agent = BidiAgent(model=model, tools=build_tools(store, runtime=runtime))
    audio_received = asyncio.Event()

    async def consume():
        async for event in agent.receive():
            metrics.record_event(event)
            if isinstance(event, BidiAudioDeltaEvent):
                audio_received.set()

    await agent.start()
    reader = asyncio.create_task(consume())
    try:
        await model.events.put(BidiToolUseBlocksEvent(tool_uses=[{
            "name": "update_due_record", "toolUseId": "tool-async-1",
            "input": {"account_id": "ACC-1001", "new_status": "paid"},
        }]))
        assert await asyncio.to_thread(store.entered.wait, 1)
        assert not model.result_sent.is_set()
        # The real SDK model reader and audio sender remain available while
        # the actual tool coroutine awaits its worker's commit.
        await model.events.put(BidiAudioDeltaEvent(
            audio=base64.b64encode(b"\0\0" * 480).decode(),
            format="pcm", sample_rate=24000, channels=1, content_id="audio-during-tool",
        ))
        await agent.send(AudioDelta(format="pcm", source={"bytes": b"\0\0" * 320}))
        await asyncio.wait_for(audio_received.wait(), 1)
        assert store.get_due("ACC-1001")["status"] == "open"
        assert not model.result_sent.is_set(), "success must wait for the actual write"
        assert metrics.tool_executions["tool-async-1"]["status"] == "running"
        store.release.set()
        await asyncio.wait_for(model.result_sent.wait(), 1)
        result_blocks = [block for message in model.sent for block in getattr(message, "content", [])
                         if isinstance(block, ToolResultBlock)]
        assert result_blocks[0].tool_use_id == "tool-async-1"
        assert result_blocks[0].status == "success"
        assert store.get_due("ACC-1001")["status"] == "paid"
        execution = metrics.tool_executions["tool-async-1"]
        assert execution["status"] == "success"
        assert execution["kind"] == "async"
        assert execution["execution_mode"] == "thread_offload"
        assert execution["duration_ms"] >= 0
        assert len(metrics._sync_tool_calls()) == 1, "toolUseId must eliminate duplicate invocation rows"
    finally:
        store.release.set()
        reader.cancel()
        await asyncio.gather(reader, return_exceptions=True)
        await agent.stop()
        await runtime.drain(close=True)
        # Finish the fake agent's buffered spans while pytest owns stdout.
        from opentelemetry import trace
        force_flush = getattr(trace.get_tracer_provider(), "force_flush", None)
        if force_flush is not None:
            await asyncio.to_thread(force_flush)


@pytest.mark.asyncio
async def test_cancelled_tool_awaiter_retains_write_until_cleanup_and_records_real_outcome():
    store = BlockingStore()
    metrics = CallMetrics(call_sid="CA_cancel")
    metrics.on_session_start()
    runtime = CallToolRuntime(event_sink=metrics.record_tool_execution)
    update = build_tools(store, runtime=runtime)[0]
    task = asyncio.create_task(update(account_id="ACC-1001", new_status="paid"))
    try:
        assert await asyncio.to_thread(store.entered.wait, 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        cleanup = asyncio.create_task(runtime.drain(close=True))
        await asyncio.sleep(0)
        assert not cleanup.done()
        assert store.get_due("ACC-1001")["status"] == "open"
        store.release.set()
        await asyncio.wait_for(cleanup, 1)
        assert store.get_due("ACC-1001")["status"] == "paid"
        execution = next(iter(metrics.tool_executions.values()))
        assert execution["cancel_requested"] is True
        assert execution["result_delivered"] is False
        assert execution["status"] == "success", "completed SDK write cannot be reported rolled back"
        assert store.calls == 1
    finally:
        store.release.set()
        await runtime.drain(close=True)


@pytest.mark.asyncio
async def test_native_group_end_request_waits_for_write_without_waiting_for_its_own_submission():
    store = BlockingStore()
    metrics = CallMetrics(call_sid="CA_handshake_group")
    metrics.on_session_start()
    runtime = CallToolRuntime(event_sink=metrics.record_tool_execution)
    hangups = []

    async def hangup():
        hangups.append(True)
        return {"confirmed_terminal": True}

    async def no_io(*args):
        pass

    lifecycle = CallLifecycleController(
        call_sid="CA_handshake_group", stream_sid="MZ_handshake_group", runtime=runtime,
        metrics=metrics, hangup=hangup, persist=no_io, close_websocket=no_io,
    )
    lifecycle.observe_event({"type": "bidi_connection_start"})
    model = FakeStreamingModel(lifecycle=lifecycle)
    agent = BidiAgent(model=model, tools=build_tools(store, runtime=runtime))

    async def consume():
        async for event in agent.receive():
            metrics.record_event(event)
            lifecycle.observe_event(event)

    await agent.start()
    reader = asyncio.create_task(consume())
    try:
        await model.events.put(BidiToolUseBlocksEvent(tool_uses=[
            {"name": "update_due_record", "toolUseId": "group-write",
             "input": {"account_id": "ACC-1001", "new_status": "paid"}},
            {"name": "end_conversation", "toolUseId": "group-end", "input": {"reason": "complete"}},
        ]))
        assert await asyncio.to_thread(store.entered.wait, 1)
        assert lifecycle.state == CallState.SETTLING_TOOLS
        assert not model.result_sent.is_set() and not hangups
        assert runtime.pending_write_count == 1 and runtime.lifecycle.tool_admission_open
        store.release.set()
        await asyncio.wait_for(model.result_sent.wait(), 1)
        assert lifecycle.state == CallState.PREPARING_FAREWELL
        assert all(tool.completed and tool.recorded and tool.submitted for tool in lifecycle.tools.values())
        assert metrics.tool_executions["group-end"]["completion_semantics"] == "ready"
        assert store.get_due("ACC-1001")["status"] == "paid"
        assert not hangups, "end readiness must never mean the telephone already ended"
    finally:
        store.release.set()
        lifecycle.transport_closed()
        reader.cancel()
        await asyncio.gather(reader, return_exceptions=True)
        await agent.stop()
        await runtime.drain(close=True)
        from opentelemetry import trace
        force_flush = getattr(trace.get_tracer_provider(), "force_flush", None)
        if force_flush is not None:
            await asyncio.to_thread(force_flush)


@pytest.mark.asyncio
async def test_failed_write_returns_error_without_disclosing_exception_payload(caplog):
    class FailedStore(InMemoryStore):
        def update_due(self, *args, **kwargs):
            raise RuntimeError("SYNTHETIC_CREDENTIAL_SHOULD_NOT_LEAK")
    events = []
    runtime = CallToolRuntime(event_sink=events.append)
    update = build_tools(FailedStore(), runtime=runtime)[0]
    result = await update(account_id="ACC-1001", new_status="paid")
    assert result["status"] == "error"
    assert events[-1]["status"] == "error"
    assert events[-1]["error_type"] == "RuntimeError"
    assert "SYNTHETIC_CREDENTIAL_SHOULD_NOT_LEAK" not in str(result) + caplog.text + str(events)


@pytest.mark.asyncio
async def test_call_capture_records_timezone_and_call_provenance_after_write():
    store = InMemoryStore()
    metrics = CallMetrics(call_sid="CA_capture")
    runtime = CallToolRuntime(capture_sink=metrics.capture_payment_date_phrase,
                              payment_timezone="Asia/Kolkata", call_sid="CA_capture")
    capture = build_tools(store, runtime=runtime)[1]
    await capture(phrase="first Monday next month", account_id="ACC-1001")
    record = metrics.to_call_analytics()["payment_date_capture"]
    assert record["phrase"] == "first Monday next month"
    assert record["call_sid"] == "CA_capture"
    assert record["reference_timezone"] == "Asia/Kolkata"
    due = store.get_due("ACC-1001")
    assert due["promised_date_phrase_call_sid"] == "CA_capture"
    assert due["promised_date_reference_date"] == record["reference_date"]


@pytest.mark.asyncio
async def test_persistence_waits_once_without_blocking_health_handler(monkeypatch):
    entered = threading.Event()
    release = threading.Event()
    saves = []
    launches = []
    class SlowStore(InMemoryStore):
        def save_call_analytics(self, record):
            entered.set()
            assert release.wait(timeout=3)
            saves.append(record)
            return super().save_call_analytics(record)
    store = SlowStore()
    metrics = CallMetrics(call_sid="CA_persist")
    metrics.on_session_start()
    metrics.on_session_end()
    monkeypatch.setattr(main_mod, "_launch_postcall", lambda *args: launches.append(args))
    one = asyncio.create_task(main_mod._persist_analytics(metrics, store, "MZ_test"))
    two = asyncio.create_task(main_mod._persist_analytics(metrics, store, "MZ_test"))
    try:
        assert await asyncio.to_thread(entered.wait, 1)
        assert await asyncio.wait_for(main_mod.healthz(), 0.2) == {"status": "ok"}
        assert not one.done() and not two.done()
        release.set()
        await asyncio.wait_for(asyncio.gather(one, two), 1)
        assert len(saves) == len(launches) == 1
    finally:
        release.set()
        await asyncio.gather(one, two)


def test_documented_endpointing_option_is_passed_to_session_start():
    configured = build_model(replace(get_settings(), endpointing_sensitivity="HIGH")).get_config()
    assert configured["params"] == {"turnDetectionConfiguration": {"endpointingSensitivity": "HIGH"}}
    default = build_model(replace(get_settings(), endpointing_sensitivity=None)).get_config()
    assert "turnDetectionConfiguration" not in default["params"], "unset option must preserve provider default"
    assert default["connection"]["restart_after_s"] == 420


@pytest.mark.asyncio
async def test_overlapping_phrase_captures_commit_in_invocation_order():
    store = BlockingStore()
    metrics = CallMetrics(call_sid="CA_order")
    starts = asyncio.Event()
    start_count = 0
    def on_event(event):
        nonlocal start_count
        metrics.record_tool_execution(event)
        if event["event"] == "start":
            start_count += 1
            if start_count == 2:
                starts.set()
    runtime = CallToolRuntime(event_sink=on_event, capture_sink=metrics.capture_payment_date_phrase,
                              call_sid="CA_order")
    capture = build_tools(store, call_sid="CA_order", runtime=runtime)[1]
    first = asyncio.create_task(capture(phrase="next Friday", account_id="ACC-1001"))
    try:
        assert await asyncio.to_thread(store.entered.wait, 1)
        second = asyncio.create_task(capture(phrase="actually next Monday", account_id="ACC-1001"))
        await asyncio.wait_for(starts.wait(), 1)
        assert store.calls == 1, "the second write must wait for the first SDK result"
        store.release.set()
        await asyncio.wait_for(asyncio.gather(first, second), 1)
        assert store.get_due("ACC-1001")["promised_date_phrase"] == "actually next Monday"
        assert metrics.payment_date_capture["phrase"] == "actually next Monday"
    finally:
        store.release.set()
        await runtime.drain(close=True)
