"""FastAPI WebSocket integration with deterministic Nova/Twilio substitutes.

Real pinned scheduler semantics are exercised in test_lifecycle_scheduler;
these tests verify endpoint ownership, ordering and exactly-once side effects.
"""
import asyncio
import base64
import json

import pytest
from fastapi import WebSocketDisconnect
from strands.bidi.types.events import (
    BidiConnectionStartEvent, BidiResponseStartEvent, BidiAudioStartEvent,
    BidiAudioDeltaEvent, BidiAudioStopEvent, BidiResponseStopEvent,
    BidiTranscriptStartEvent, BidiTranscriptDeltaEvent, BidiTranscriptStopEvent,
    BidiTranscriptBlockEvent,
)
from strands.types.events import ToolResultEvent
from strands.types._events import ToolResultMessageEvent

from app import main
from app.store import InMemoryStore
from app.call_lifecycle import TerminationBounds


class FakeWebSocket:
    def __init__(self, *, probe=False, auto_ack=True):
        self.incoming = asyncio.Queue()
        self.sent = []
        self.auto_ack = auto_ack
        self.accepted = False
        self.closed = False
        self.mark_sent = asyncio.Event()
        self.incoming.put_nowait({"event": "connected"})
        if probe:
            self.incoming.put_nowait(None)
        else:
            self.incoming.put_nowait({"event": "start", "streamSid": "MZ_test",
                "start": {"callSid": "CA_test", "customParameters": {"accountId": "ACC-1001"}}})

    async def accept(self):
        self.accepted = True

    async def receive_text(self):
        event = await self.incoming.get()
        if event is None:
            raise WebSocketDisconnect()
        return json.dumps(event)

    async def send_text(self, payload):
        message = json.loads(payload)
        self.sent.append(message)
        if message["event"] == "mark":
            self.mark_sent.set()
            if self.auto_ack:
                self.incoming.put_nowait(message)

    async def close(self):
        self.closed = True
        self.incoming.put_nowait(None)


class ScriptedSession:
    def __init__(self, *, lifecycle, send_media, send_mark, metrics, mode="normal", order, **kwargs):
        self.controller = lifecycle
        self.metrics = metrics
        self.send_media = send_media
        self.send_mark = send_mark
        self.mode = mode
        self.order = order
        self.output_closed = False
        self.task = None

    def event(self, event, provider=True):
        if provider:
            self.controller.observe_provider_event(event)
        self.metrics.record_event(event)
        self.controller.observe_event(event)

    def result(self, identity):
        result = {"toolUseId": identity, "status": "success", "content": [{"text": "result"}]}
        self.event(ToolResultEvent(result), False)
        self.event(ToolResultMessageEvent({"role": "user", "content": [{"toolResult": result}]}), False)
        self.controller.results_submitted([identity])

    async def run(self):
        self.event(BidiConnectionStartEvent("test-connection", "fake"))
        if self.mode == "sdk_failure":
            raise RuntimeError("SYNTHETIC_PRIVATE_EXCEPTION")
        c = self.controller
        c.register_tool_group([{"toolUseId": "end", "name": "end_conversation", "input": {}}])
        result = await c.request_end(tool_use_id="end")
        self.result("end")
        if self.mode == "duplicate":
            duplicate = await c.request_end(tool_use_id="end")
            assert duplicate["status"] == "pending"
        c.register_tool_group([{"toolUseId": "begin", "name": "begin_farewell", "input": {}}])
        assert (await c.begin_farewell(result["attempt_id"], tool_use_id="begin"))["ready"]
        self.result("begin")
        if self.mode == "no_audio":
            await asyncio.Future()
        self.event(BidiResponseStartEvent("response"))
        self.event(BidiAudioStartEvent("audio"))
        audio = BidiAudioDeltaEvent(base64.b64encode(b"\0\0" * 480).decode(), "pcm", 24000, 1, "audio")
        c.observe_provider_event(audio)
        await self.send_media(base64.b64encode(b"\xff" * 160).decode())
        self.event(audio, False)
        self.event(BidiTranscriptStartEvent("assistant", "text"))
        self.event(BidiTranscriptDeltaEvent("Thank you for your time. Goodbye.", "assistant", "text"))
        self.event(BidiTranscriptStopEvent("assistant", "text"))
        self.event(BidiTranscriptBlockEvent("Thank you for your time. Goodbye.", "assistant", "text"), False)
        self.event(BidiAudioStopEvent("audio"))
        self.event(BidiResponseStopEvent("response"))
        await asyncio.Future()

    def start(self):
        self.task = asyncio.create_task(self.run())
        return self.task

    def close_output(self):
        self.output_closed = True

    def notify_transport_closed(self):
        self.output_closed = True

    async def close(self):
        self.order.append("sdk_close")
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)

    async def send_playback_mark(self, name):
        assert not self.output_closed
        await self.send_mark(name)

    async def send_recovery_prompt(self, text):
        self.order.append("recovery_prompt")

    async def feed_twilio_audio(self, payload):
        self.order.append("inbound_audio")


def setup(monkeypatch, ws, mode="normal", confirmed=True):
    order, saved, sessions = [], [], []
    class Store(InMemoryStore):
        def save_call_analytics(self, record):
            order.append("save")
            saved.append(record)
            return super().save_call_analytics(record)
    store = Store()
    monkeypatch.setattr(main, "get_default_store", lambda: store)
    monkeypatch.setattr(main, "_launch_postcall", lambda *args: order.append("postcall"))
    def factory(**kwargs):
        session = ScriptedSession(**kwargs, mode=mode, order=order)
        sessions.append(session)
        return session
    monkeypatch.setattr(main, "SonicSession", factory)
    monkeypatch.setattr(main, "TerminationBounds", lambda: TerminationBounds(.1, .1, .1, .03, .03))
    async def hangup(*args, **kwargs):
        order.append("hangup")
        assert not sessions or sessions[0].output_closed
        ws.incoming.put_nowait(None)
        return {"confirmed_terminal": confirmed, "outcome": "confirmed_terminal" if confirmed else "unknown",
                "status": "completed" if confirmed else None, "attempts": 1}
    monkeypatch.setattr(main, "hangup_call_async", hangup)
    return order, saved, sessions


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["normal", "duplicate"])
async def test_confirmed_farewell_cancels_sdk_then_hangs_up_and_persists_once(monkeypatch, mode):
    ws = FakeWebSocket()
    order, saved, sessions = setup(monkeypatch, ws, mode)
    await asyncio.wait_for(main.media_stream(ws), 1)
    assert order == ["sdk_close", "hangup", "save", "postcall"]
    assert ws.closed and ws.accepted
    assert len(saved) == 1 and saved[0]["total_e2e_ms"] is not None
    assert saved[0]["termination"]["outcome"] == "farewell_played"
    assert sum(event["event_type"] == "call_end_summary" for event in saved[0]["events_log"]) == 1
    assert sessions[0].task.cancelled()


@pytest.mark.asyncio
async def test_wrong_stream_unknown_marks_do_not_hang_up(monkeypatch):
    ws = FakeWebSocket(auto_ack=False)
    order, saved, _ = setup(monkeypatch, ws)
    task = asyncio.create_task(main.media_stream(ws))
    await ws.mark_sent.wait()
    mark = next(message for message in ws.sent if message["event"] == "mark")
    ws.incoming.put_nowait({**mark, "streamSid": "MZ_wrong"})
    ws.incoming.put_nowait({**mark, "mark": {"name": "unknown"}})
    await asyncio.sleep(.01)
    assert not order and not saved
    ws.incoming.put_nowait(mark)
    await asyncio.wait_for(task, 1)
    assert order.count("hangup") == 1


@pytest.mark.asyncio
async def test_sdk_failure_while_reader_open_is_supervised(monkeypatch, caplog):
    ws = FakeWebSocket()
    order, saved, _ = setup(monkeypatch, ws, "sdk_failure")
    await asyncio.wait_for(main.media_stream(ws), 1)
    assert order == ["sdk_close", "hangup", "save", "postcall"]
    assert saved[0]["termination"]["outcome"] == "technical_failure"
    assert "SYNTHETIC_PRIVATE_EXCEPTION" not in caplog.text


@pytest.mark.asyncio
async def test_caller_disconnect_during_generation_converges_on_same_cleanup(monkeypatch):
    ws = FakeWebSocket()
    order, saved, sessions = setup(monkeypatch, ws, "no_audio")
    task = asyncio.create_task(main.media_stream(ws))
    while not sessions:
        await asyncio.sleep(0)
    await asyncio.sleep(.01)
    ws.incoming.put_nowait({"event": "stop", "streamSid": "MZ_test"})
    await asyncio.wait_for(task, 1)
    assert order.count("hangup") == len(saved) == 1
    assert saved[0]["termination"]["outcome"] == "caller_disconnected"


@pytest.mark.asyncio
async def test_unknown_telephone_completion_withholds_postcall(monkeypatch):
    ws = FakeWebSocket()
    order, saved, _ = setup(monkeypatch, ws, confirmed=False)
    await asyncio.wait_for(main.media_stream(ws), 1)
    assert order == ["sdk_close", "hangup", "save"]
    assert saved[0]["termination"]["outcome"] == "telephone_completion_unresolved"
    assert saved[0]["intelligence_status"] == "withheld_unconfirmed_termination"
    assert ws.closed


@pytest.mark.asyncio
async def test_protocol_probe_constructs_no_session_and_makes_no_telephone_request(monkeypatch):
    ws = FakeWebSocket(probe=True)
    order, saved, sessions = setup(monkeypatch, ws)
    await main.media_stream(ws)
    assert ws.closed and not order and not saved and not sessions


@pytest.mark.asyncio
async def test_initial_store_failure_is_owned_and_terminates_phone(monkeypatch):
    ws = FakeWebSocket()
    order, saved, sessions = setup(monkeypatch, ws)
    store = main.get_default_store()
    monkeypatch.setattr(store, "get_due", lambda *_: (_ for _ in ()).throw(RuntimeError("private")))
    await asyncio.wait_for(main.media_stream(ws), 1)
    assert not sessions and ws.closed
    assert order == ["hangup", "save", "postcall"]
    assert saved[0]["termination"]["outcome"] == "technical_failure"
