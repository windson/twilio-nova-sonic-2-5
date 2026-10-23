"""Real pinned Strands scheduler + app tools/controller, with public provider I/O fakes.

No provider connection, telephone operation, SDK private state, or real store is
used. The real BidiAgent owns tool concurrency and tool-result event ordering.
"""

from __future__ import annotations

import asyncio
import base64
import json
import threading
from types import SimpleNamespace

import boto3
import pytest
from strands.bidi.models import BedrockNovaSonicModel, ConnectionTimeoutError
from strands.bidi.types.content import BidiMessage
from strands.bidi.types.events import (
    BidiAudioDeltaEvent, BidiAudioStartEvent, BidiAudioStopEvent,
    BidiConnectionStartEvent, BidiResponseStartEvent, BidiResponseStopEvent,
    BidiToolUseBlocksEvent, BidiTranscriptDeltaEvent, BidiTranscriptStartEvent,
    BidiTranscriptStopEvent,
)
from strands.bidi.types.media import AudioDelta
from strands.types.tools import ToolResultBlock
from strands.types._events import ToolResultMessageEvent

from app.call_lifecycle import CallLifecycleController, CallState, TerminationBounds
from app.collections_agent import CallToolRuntime, build_tools
from app.sonic_bridge import SonicSession


async def until(predicate):
    async def poll():
        while not predicate():
            await asyncio.sleep(0.001)
    await asyncio.wait_for(poll(), 2)


def tool_use(id_, name, **input_):
    return {"toolUseId": id_, "name": name, "input": input_}


def pcm_event(content_id):
    return BidiAudioDeltaEvent(audio=base64.b64encode(b"\x10\x00" * 480).decode("ascii"),
        format="pcm", sample_rate=24000, channels=1, content_id=content_id)


class SchedulerHarness:
    def __init__(self, monkeypatch, *, block_write=False, fail_write=False, restart_after_s=None,
                 restart_failure=False):
        self.events = asyncio.Queue()
        self.sent_results = {}
        self.send_gates = {}
        self.send_started = set()
        self.inbound = []
        self.media = []
        self.media_gate = None
        self.media_send_started = asyncio.Event()
        self.marks = []
        self.hangups = []
        self.persisted = []
        self.lifecycle_events = []
        self.connection_count = 0
        self.restart_calls = []
        self.stop_calls = []
        self.clears = []
        self.restart_failure = restart_failure
        self.write_started = threading.Event()
        self.write_release = threading.Event()
        if not block_write:
            self.write_release.set()
        self.writes = []
        self.fail_write = fail_write
        self.runtime = CallToolRuntime(call_sid="test-call")
        metrics = SimpleNamespace(
            on_session_start=lambda: None, on_session_end=lambda: None,
            record_event=lambda event: None,
            record_lifecycle_event=lambda event, **fields: self.lifecycle_events.append((event, fields)),
        )

        async def hangup():
            self.hangups.append(True)
            return {"confirmed_terminal": True, "status": "completed", "attempts": 1}

        async def persist(confirmed):
            self.persisted.append(confirmed)

        async def close_socket():
            return None

        self.controller = CallLifecycleController(call_sid="test-call", stream_sid="test-stream",
            runtime=self.runtime, metrics=metrics, hangup=hangup, persist=persist,
            close_websocket=close_socket,
            bounds=TerminationBounds(tools=2, generation=2, playback=2, sdk_cleanup=1, twilio_request=1))

        harness = self

        class Store:
            def update_due(self, account_id, **changes):
                harness.write_started.set()
                if not harness.write_release.wait(timeout=3):
                    raise RuntimeError("test write was not released")
                if harness.fail_write:
                    raise RuntimeError("test write failed")
                harness.writes.append((account_id, changes))
                return changes

        async def start(model, *args, **kwargs):
            harness.connection_count += 1

        async def stop(model):
            harness.stop_calls.append(True)
            harness.events.put_nowait(None)

        async def restart(model, *args, **kwargs):
            harness.restart_calls.append({"messages": kwargs.get("messages", args[2] if len(args) > 2 else None),
                                          "options": kwargs})
            if harness.restart_failure:
                raise RuntimeError("test provider recovery failed")
            old_events = harness.events
            harness.events = asyncio.Queue()
            harness.connection_count += 1
            old_events.put_nowait(None)

        async def receive(model):
            events = harness.events
            yield BidiConnectionStartEvent(connection_id=f"connection-{harness.connection_count}", model="nova")
            while True:
                event = await events.get()
                if event is None:
                    return
                if isinstance(event, Exception):
                    raise event
                yield event

        async def send(model, content):
            if isinstance(content, AudioDelta):
                harness.inbound.append(content)
            elif isinstance(content, BidiMessage):
                results = [block for block in content.content if isinstance(block, ToolResultBlock)]
                for block in results:
                    harness.send_started.add(block.tool_use_id)
                    gate = harness.send_gates.get(block.tool_use_id)
                    if gate is not None:
                        await gate.wait()
                for block in results:
                    harness.sent_results[block.tool_use_id] = block

        async def send_media(payload):
            self.media_send_started.set()
            if self.media_gate is not None:
                await self.media_gate.wait()
            self.media.append(payload)

        async def send_mark(name):
            self.marks.append(name)

        async def clear():
            harness.clears.append(True)

        monkeypatch.setattr(boto3, "Session", lambda: SimpleNamespace(region_name="us-east-1"))
        monkeypatch.setattr(BedrockNovaSonicModel, "start", start)
        monkeypatch.setattr(BedrockNovaSonicModel, "stop", stop)
        monkeypatch.setattr(BedrockNovaSonicModel, "restart", restart)
        monkeypatch.setattr(BedrockNovaSonicModel, "receive", receive)
        monkeypatch.setattr(BedrockNovaSonicModel, "send", send)
        self.session = SonicSession(send_media=send_media, send_mark=send_mark, on_interrupt=clear,
            settings=SimpleNamespace(nova_model_id="amazon.nova-2-5-sonic", aws_region="us-east-1",
                                     endpointing_sensitivity=None),
            tools=build_tools(store=Store(), runtime=self.runtime, call_sid="test-call"),
            metrics=metrics, lifecycle=self.controller, kickoff_text=None)
        self.controller.bind_session(self.session)
        if restart_after_s is not None:
            # Public provider configuration drives the REAL SDK restart timer.
            # Production build_model retains its tested 420-second default.
            self.session.agent.model.update_config(connection={"restart_after_s": restart_after_s})
        self.task = None

    async def start(self):
        self.task = self.session.start()
        await until(lambda: self.controller.connection_ready)
        return self

    async def emit(self, *events):
        for event in events:
            self.events.put_nowait(event)
        # Let the real reader consume asynchronously; no app observer is called directly.
        await asyncio.sleep(0)

    async def group(self, *uses):
        await self.emit(BidiToolUseBlocksEvent(tool_uses=list(uses)))

    async def result(self, tool_id):
        await until(lambda: tool_id in self.sent_results)
        block = self.sent_results[tool_id]
        payload = next((item["json"] for item in block.content if "json" in item), {})
        if not payload:
            for item in block.content:
                try:
                    payload = json.loads(item.get("text", ""))
                    break
                except (ValueError, TypeError):
                    pass
        return block, payload

    async def request_ready(self, *, end_id="end-1", begin_id="begin-1"):
        await self.group(tool_use(end_id, "end_conversation"))
        end, payload = await self.result(end_id)
        assert end.status == "success" and payload["ready"] is True
        await self.group(tool_use(begin_id, "begin_farewell", attempt_id=payload["attempt_id"]))
        begin, grant = await self.result(begin_id)
        assert begin.status == "success" and grant["ready"] is True
        await until(lambda: self.controller.attempt is not None
                    and self.controller.attempt.grant_sequence is not None)
        return payload["attempt_id"]

    async def closing_audio(self, response="closing-response", audio="closing-audio", transcript="closing-text",
                            *, response_start=True):
        if response_start:
            await self.emit(BidiResponseStartEvent(response_id=response))
        await self.emit(
            BidiTranscriptStartEvent(role="assistant", content_id=transcript),
            BidiTranscriptDeltaEvent(delta="Thank you for your time. Goodbye.", role="assistant", content_id=transcript),
            BidiAudioStartEvent(content_id=audio), pcm_event(audio), BidiAudioStopEvent(content_id=audio),
            BidiTranscriptStopEvent(role="assistant", content_id=transcript),
            BidiResponseStopEvent(response_id=response),
        )

    async def close(self):
        self.write_release.set()
        for gate in self.send_gates.values():
            gate.set()
        if self.media_gate is not None:
            self.media_gate.set()
        self.controller.transport_closed()
        self.controller.start_close(reason="test_cleanup", outcome="caller_disconnected")
        await asyncio.wait_for(self.controller.closed.wait(), 3)
        if self.task is not None:
            await asyncio.gather(self.task, return_exceptions=True)


@pytest.mark.asyncio
async def test_end_first_group_waits_for_write_while_audio_flows_and_allows_open_tool_response(monkeypatch):
    harness = await SchedulerHarness(monkeypatch, block_write=True).start()
    try:
        await harness.emit(BidiResponseStartEvent(response_id="tool-response"))
        await harness.group(
            tool_use("end-1", "end_conversation"),
            tool_use("write-1", "update_due_record", account_id="account-1", new_status="promised_to_pay"),
        )
        await until(lambda: harness.write_started.is_set())
        await until(lambda: harness.controller.state == CallState.SETTLING_TOOLS)
        await harness.emit(BidiAudioStartEvent(content_id="interim-audio"), pcm_event("interim-audio"),
                           BidiAudioStopEvent(content_id="interim-audio"))
        await harness.session.feed_twilio_audio(base64.b64encode(b"\xff" * 160).decode("ascii"))
        await until(lambda: bool(harness.media) and bool(harness.inbound))
        assert harness.runtime.pending_write_count == 1
        assert "end-1" not in harness.sent_results
        assert harness.hangups == []
        harness.write_release.set()
        _, ready = await harness.result("end-1")
        assert ready["ready"] is True and harness.writes
        await harness.group(tool_use("begin-1", "begin_farewell", attempt_id=ready["attempt_id"]))
        _, grant = await harness.result("begin-1")
        assert grant["ready"] is True
        # Native async tools may finish inside the still-open provider response.
        # A tool-only response stop is not a prerequisite for the second step.
        await harness.closing_audio(response="tool-response", response_start=False)
        await until(lambda: bool(harness.marks))
        assert harness.hangups == []
        assert harness.controller.acknowledge_playback(harness.marks[-1], "test-stream")
        await asyncio.wait_for(harness.controller.closed.wait(), 2)
        assert harness.hangups == [True]
        assert harness.persisted == [True]
        assert harness.controller.summary["outcome"] == "farewell_played"
    finally:
        await harness.close()


@pytest.mark.asyncio
async def test_recorded_result_with_blocked_public_send_keeps_begin_not_ready(monkeypatch):
    harness = await SchedulerHarness(monkeypatch).start()
    try:
        harness.send_gates["end-1"] = asyncio.Event()
        await harness.group(tool_use("end-1", "end_conversation"))
        await until(lambda: "end-1" in harness.send_started)
        tracked = harness.controller.tools["end-1"]
        assert tracked.completed and tracked.recorded and not tracked.submitted
        attempt_id = harness.controller.attempt.id
        await harness.group(tool_use("begin-early", "begin_farewell", attempt_id=attempt_id))
        await until(lambda: "begin-early" in harness.controller.tools)
        await asyncio.sleep(0.01)
        assert "begin-early" not in harness.sent_results
        assert harness.controller.attempt.grant_sequence is None
        harness.send_gates["end-1"].set()
        await harness.result("end-1")
        accepted, payload = await harness.result("begin-early")
        assert accepted.status == "success" and payload["ready"] is True
        await harness.group(tool_use("begin-later", "begin_farewell", attempt_id=attempt_id))
        rejected, _ = await harness.result("begin-later")
        assert rejected.status == "error"
        assert harness.hangups == []
    finally:
        await harness.close()


@pytest.mark.asyncio
async def test_concurrent_groups_duplicate_end_and_new_business_invalidate_once(monkeypatch):
    harness = await SchedulerHarness(monkeypatch, block_write=True).start()
    try:
        await harness.group(tool_use("write-1", "update_due_record", account_id="account-1", new_status="open"))
        await until(lambda: harness.write_started.is_set())
        await harness.group(tool_use("end-1", "end_conversation"))
        await until(lambda: harness.controller.attempt is not None)
        first_attempt = harness.controller.attempt.id
        await harness.group(tool_use("end-duplicate", "end_conversation"))
        duplicate, payload = await harness.result("end-duplicate")
        assert duplicate.status == "success" and payload["status"] == "pending"
        assert payload["attempt_id"] == first_attempt
        assert harness.controller.attempt_count == 1
        await harness.group(tool_use("write-2", "update_due_record", account_id="account-1", new_status="callback"))
        await until(lambda: harness.controller.attempt is None)
        assert harness.controller.summary["reason"] == "new_tool_operation"
        harness.write_release.set()
        end, _ = await harness.result("end-1")
        await harness.result("write-2")
        assert end.status == "error"
        assert harness.hangups == [] and harness.marks == []
    finally:
        await harness.close()


@pytest.mark.asyncio
async def test_failed_native_group_write_does_not_grant_farewell(monkeypatch):
    harness = await SchedulerHarness(monkeypatch, block_write=True, fail_write=True).start()
    try:
        await harness.group(
            tool_use("end-1", "end_conversation"),
            tool_use("write-1", "update_due_record", account_id="account-1", new_status="paid"),
        )
        await until(lambda: harness.write_started.is_set())
        harness.write_release.set()
        failed_write, _ = await harness.result("write-1")
        failed_end, _ = await harness.result("end-1")
        assert failed_write.status == failed_end.status == "error"
        assert harness.controller.summary["reason"] == "tool_write_failed"
        assert harness.controller.state == CallState.ACTIVE
        assert harness.marks == [] and harness.hangups == []
    finally:
        await harness.close()


@pytest.mark.asyncio
async def test_duplicate_begin_nonce_does_not_grant_second_farewell(monkeypatch):
    harness = await SchedulerHarness(monkeypatch).start()
    try:
        attempt_id = await harness.request_ready()
        sequence = harness.controller.attempt.grant_sequence
        await harness.group(tool_use("begin-duplicate", "begin_farewell", attempt_id=attempt_id))
        rejected, _ = await harness.result("begin-duplicate")
        assert rejected.status == "error"
        assert harness.controller.attempt.grant_sequence == sequence
        assert harness.controller.attempt_count == 1
        assert harness.marks == [] and harness.hangups == []
    finally:
        await harness.close()


@pytest.mark.asyncio
async def test_reused_ids_require_new_postgrant_audio_and_completion_evidence(monkeypatch):
    harness = await SchedulerHarness(monkeypatch).start()
    try:
        await harness.closing_audio(response="shared-response", audio="shared-audio", transcript="shared-text")
        await until(lambda: bool(harness.media))
        await harness.request_ready()
        # Old stop identities alone do not establish another closing response.
        await harness.emit(BidiAudioStopEvent(content_id="shared-audio"),
                           BidiResponseStopEvent(response_id="shared-response"))
        await asyncio.sleep(0.01)
        assert harness.marks == []
        assert harness.controller.attempt.audio == set()
        # Reuse is permitted when fresh post-grant media/transcript and matching
        # completion boundaries establish what the caller actually heard.
        await harness.closing_audio(response="shared-response", audio="shared-audio", transcript="shared-text")
        await until(lambda: bool(harness.marks))
        assert harness.hangups == []
        harness.controller.acknowledge_playback(harness.marks[-1], "test-stream")
        await asyncio.wait_for(harness.controller.closed.wait(), 2)
        assert harness.controller.summary["outcome"] == "farewell_played"
    finally:
        await harness.close()


@pytest.mark.asyncio
async def test_pregrant_audio_queued_at_transport_cannot_establish_postgrant_farewell(monkeypatch):
    harness = await SchedulerHarness(monkeypatch).start()
    try:
        await harness.group(tool_use("end-1", "end_conversation"))
        _, ready = await harness.result("end-1")
        harness.send_gates["begin-1"] = asyncio.Event()
        await harness.group(tool_use("begin-1", "begin_farewell", attempt_id=ready["attempt_id"]))
        await until(lambda: "begin-1" in harness.send_started)
        assert harness.controller.attempt.grant_sequence is None
        harness.media_gate = asyncio.Event()
        await harness.emit(BidiResponseStartEvent(response_id="queued-response"),
                           BidiAudioStartEvent(content_id="queued-audio"), pcm_event("queued-audio"))
        await asyncio.wait_for(harness.media_send_started.wait(), 1)
        # The audio was received and queued before the grant. Its socket send
        # finishes afterward, which must not change its production provenance.
        harness.send_gates["begin-1"].set()
        await harness.result("begin-1")
        harness.media_gate.set()
        await until(lambda: bool(harness.media))
        await harness.emit(
            BidiTranscriptStartEvent(role="assistant", content_id="new-text"),
            BidiTranscriptDeltaEvent(delta="Goodbye.", role="assistant", content_id="new-text"),
            BidiTranscriptStopEvent(role="assistant", content_id="new-text"),
            BidiAudioStopEvent(content_id="queued-audio"),
            BidiResponseStopEvent(response_id="queued-response"),
        )
        await asyncio.sleep(0.01)
        assert harness.controller.attempt.audio == set()
        assert harness.marks == [] and harness.hangups == []
    finally:
        await harness.close()


@pytest.mark.asyncio
async def test_native_begin_waits_for_prior_result_recording_when_output_is_backlogged(monkeypatch):
    harness = SchedulerHarness(monkeypatch)
    recording_started = asyncio.Event()
    release_recording = asyncio.Event()
    output = harness.session._output

    class DelayedRecordingOutput:
        async def start(self, agent):
            await output.start(agent)

        async def stop(self):
            await output.stop()

        def close_output(self):
            output.close_output()

        async def __call__(self, event):
            if isinstance(event, ToolResultMessageEvent):
                ids = [block.get("toolResult", {}).get("toolUseId")
                       for block in event.get("message", {}).get("content", [])]
                if "end-1" in ids:
                    recording_started.set()
                    await release_recording.wait()
            await output(event)

    harness.session._output = DelayedRecordingOutput()
    await harness.start()
    try:
        await harness.group(tool_use("end-1", "end_conversation"))
        _, ready = await harness.result("end-1")
        await asyncio.wait_for(recording_started.wait(), 1)
        assert harness.controller.tools["end-1"].submitted
        assert not harness.controller.tools["end-1"].recorded
        await harness.group(tool_use("begin-1", "begin_farewell", attempt_id=ready["attempt_id"]))
        await until(lambda: "begin-1" in harness.controller.tools)
        # Let the actual SDK dispatch begin while the prior recording callback
        # remains blocked. It must wait for prior evidence, not reject the grant.
        await asyncio.sleep(0.01)
        release_recording.set()
        begin, grant = await harness.result("begin-1")
        assert begin.status == "success" and grant["ready"] is True
        assert harness.controller.tools["end-1"].recorded
        assert harness.hangups == []
    finally:
        release_recording.set()
        await harness.close()


@pytest.mark.asyncio
async def test_concurrent_begin_tools_share_one_nonce_while_prior_send_is_pending(monkeypatch):
    harness = await SchedulerHarness(monkeypatch).start()
    try:
        harness.send_gates["end-1"] = asyncio.Event()
        await harness.group(tool_use("end-1", "end_conversation"))
        await until(lambda: "end-1" in harness.send_started)
        attempt_id = harness.controller.attempt.id
        await harness.group(
            tool_use("begin-1", "begin_farewell", attempt_id=attempt_id),
            tool_use("begin-duplicate", "begin_farewell", attempt_id=attempt_id),
        )
        await until(lambda: "begin-duplicate" in harness.controller.tools)
        await asyncio.sleep(0.01)
        assert harness.controller.attempt.grant_sequence is None
        harness.send_gates["end-1"].set()
        first, first_payload = await harness.result("begin-1")
        second, second_payload = await harness.result("begin-duplicate")
        assert sorted([first.status, second.status]) == ["error", "success"]
        assert sum(payload.get("ready") is True for payload in (first_payload, second_payload)) == 1
        assert harness.controller.attempt.begin_id in {"begin-1", "begin-duplicate"}
        assert harness.controller.attempt_count == 1
        assert harness.marks == [] and harness.hangups == []
    finally:
        await harness.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("late_start", [
    BidiResponseStartEvent(response_id="closing-response"),
    BidiAudioStartEvent(content_id="late-audio"),
    BidiTranscriptStartEvent(role="assistant", content_id="late-text"),
])
async def test_late_start_cannot_reuse_previous_response_completion_for_new_mark(monkeypatch, late_start):
    harness = await SchedulerHarness(monkeypatch).start()
    try:
        await harness.request_ready()
        await harness.closing_audio()
        await until(lambda: bool(harness.marks))
        old_mark = harness.marks[-1]
        await harness.emit(late_start)
        await asyncio.sleep(0.03)
        assert harness.marks == [old_mark], "late generation needs new completion before another playback mark"
        assert not harness.controller.acknowledge_playback(old_mark, "test-stream")
        assert harness.hangups == []
    finally:
        await harness.close()


@pytest.mark.asyncio
async def test_duplicate_old_stop_cannot_finish_new_audio_start_without_any_new_media(monkeypatch):
    harness = await SchedulerHarness(monkeypatch).start()
    try:
        await harness.request_ready()
        await harness.closing_audio()
        await until(lambda: bool(harness.marks))
        old_mark = harness.marks[-1]
        await harness.emit(BidiAudioStartEvent(content_id="unfinished-late-audio"),
                           BidiResponseStopEvent(response_id="closing-response"))
        await asyncio.sleep(0.03)
        assert harness.marks == [old_mark], "a stale stop cannot finish a new audio start"
        assert not harness.controller.acknowledge_playback(old_mark, "test-stream")
        assert harness.hangups == []
    finally:
        await harness.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("restart_kind", ["scheduled", "timeout"])
async def test_native_sdk_renews_with_pending_write_and_invalidates_old_end(monkeypatch, restart_kind):
    harness = await SchedulerHarness(monkeypatch, block_write=True,
        restart_after_s=1 if restart_kind == "scheduled" else None).start()
    try:
        await harness.emit(BidiResponseStartEvent(response_id="before-restart"))
        await harness.group(
            tool_use("end-before-restart", "end_conversation"),
            tool_use("write-before-restart", "update_due_record", account_id="account-1", new_status="callback"),
        )
        await until(lambda: harness.write_started.is_set() and harness.controller.attempt is not None)
        old_attempt = harness.controller.attempt
        old_generation = harness.controller.generation
        await harness.emit(
            BidiTranscriptStartEvent(role="assistant", content_id="before-restart-text"),
            BidiTranscriptDeltaEvent(delta="I am recording that update.", role="assistant", content_id="before-restart-text"),
            BidiAudioStartEvent(content_id="before-restart-audio"), pcm_event("before-restart-audio"),
        )
        if restart_kind == "timeout":
            # Public ConnectionTimeoutError activates native reactive recovery.
            await harness.emit(ConnectionTimeoutError("synthetic provider timeout"))
        else:
            # Nova's AUDIO END_TURN yields these public boundaries. The SDK can
            # take this turn boundary while the native async write remains live.
            await harness.emit(BidiAudioStopEvent(content_id="before-restart-audio"),
                BidiTranscriptStopEvent(role="assistant", content_id="before-restart-text"),
                BidiResponseStopEvent(response_id="before-restart"))
        await until(lambda: harness.connection_count == 2 and harness.controller.connection_ready
                    and harness.controller.generation > old_generation)
        assert len(harness.restart_calls) == 1
        assert harness.controller.attempt is None
        assert old_attempt.invalid_reason == "connection_restart"
        assert harness.runtime.pending_write_count == 1
        assert harness.writes == [] and harness.hangups == []
        assert harness.controller._response is None
        assert harness.controller._transcripts == {}
        assert harness.clears == ([True] if restart_kind == "timeout" else [])
        harness.write_release.set()
        update, _ = await harness.result("write-before-restart")
        end, _ = await harness.result("end-before-restart")
        assert update.status == "success" and end.status == "error"
        assert len(harness.writes) == 1
        assert harness.runtime.pending_write_count == 0
        assert harness.controller.tools["write-before-restart"].submitted
        assert not harness.task.done()
        assert harness.marks == [] and harness.hangups == []
        fresh_attempt = await harness.request_ready(end_id="end-after-restart", begin_id="begin-after-restart")
        assert fresh_attempt != old_attempt.id
        assert harness.controller.attempt.generation == harness.controller.generation
    finally:
        await harness.close()


@pytest.mark.asyncio
async def test_native_scheduled_renewal_invalidates_playback_mark_without_clearing_completed_audio(monkeypatch):
    harness = await SchedulerHarness(monkeypatch, restart_after_s=1).start()
    try:
        await harness.request_ready()
        await harness.closing_audio()
        await until(lambda: bool(harness.marks))
        old_mark = harness.marks[-1]
        old_generation = harness.controller.generation
        await until(lambda: harness.connection_count == 2 and harness.controller.connection_ready
                    and harness.controller.generation > old_generation)
        assert not harness.controller.acknowledge_playback(old_mark, "test-stream")
        assert harness.controller.attempt is None
        assert harness.clears == []  # Native restart reported turn_interrupted=False.
        assert harness.hangups == [] and not harness.task.done()
    finally:
        await harness.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["unrecoverable", "restart_failed"])
async def test_native_ultimate_sdk_failure_closes_still_open_websocket(monkeypatch, failure):
    harness = SchedulerHarness(monkeypatch, restart_failure=failure == "restart_failed")
    reader_running = asyncio.Event()
    reader_finished = asyncio.Event()

    async def open_websocket_reader():
        reader_running.set()
        try:
            await asyncio.Future()
        finally:
            reader_finished.set()

    owner = asyncio.create_task(harness.controller.run(open_websocket_reader()))
    try:
        await until(lambda: harness.controller.connection_ready and reader_running.is_set())
        harness.task = harness.controller.sdk_task
        await harness.emit(ConnectionTimeoutError("synthetic recoverable timeout")
                           if failure == "restart_failed" else RuntimeError("synthetic provider failure"))
        await asyncio.wait_for(owner, 2)
        assert harness.hangups == [True]
        assert harness.persisted == [True]
        assert reader_finished.is_set()
        assert harness.controller.state == CallState.CLOSED
        assert harness.controller.summary["reason"] == "sdk_unexpected_exit"
        assert harness.controller.summary["outcome"] == "technical_failure"
        assert any(event == "sdk_run_failed" for event, _ in harness.lifecycle_events)
        assert len(harness.restart_calls) == (1 if failure == "restart_failed" else 0)
    finally:
        await harness.close()
        await asyncio.gather(owner, return_exceptions=True)


@pytest.mark.asyncio
async def test_sdk_and_cancel_resistant_attempt_share_one_cleanup_budget(monkeypatch):
    harness = SchedulerHarness(monkeypatch)
    harness.controller.bounds = TerminationBounds(tools=1, generation=1, playback=1,
                                                  sdk_cleanup=0.08, twilio_request=1)
    stop_started = asyncio.Event()
    stop_release = asyncio.Event()
    attempt_started = asyncio.Event()
    attempt_cancelled = asyncio.Event()
    attempt_release = asyncio.Event()
    finished = []

    async def slow_public_provider_stop(model):
        stop_started.set()
        await stop_release.wait()
        finished.append("provider")

    async def resistant_terminal_wait():
        attempt_started.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            attempt_cancelled.set()
            await attempt_release.wait()
        finally:
            finished.append("attempt")

    monkeypatch.setattr(BedrockNovaSonicModel, "stop", slow_public_provider_stop)
    await harness.start()
    attempt_task = asyncio.create_task(resistant_terminal_wait())
    harness.controller._attempt_task = attempt_task
    await attempt_started.wait()
    try:
        started = asyncio.get_running_loop().time()
        harness.controller.start_close(reason="test_failure", outcome="technical_failure")
        await until(lambda: bool(harness.hangups))
        elapsed = asyncio.get_running_loop().time() - started
        # 80ms shared budget with 60ms scheduling headroom; two serial 80ms
        # allowances fail. This is cleanup timing, not a production latency claim.
        assert elapsed < 0.14
        assert stop_started.is_set() and attempt_cancelled.is_set()
        assert finished == []  # Timeout retains cleanup instead of claiming cancellation.
        assert harness.controller.summary["sdk_cleanup"] == "timeout_work_retained"
        stop_release.set()
        attempt_release.set()
        await asyncio.wait_for(attempt_task, 1)
        await until(lambda: sorted(finished) == ["attempt", "provider"])
        await asyncio.wait_for(harness.controller.closed.wait(), 1)
        assert harness.hangups == [True]
    finally:
        stop_release.set()
        attempt_release.set()
        await asyncio.gather(attempt_task, return_exceptions=True)
        await harness.close()


@pytest.mark.asyncio
async def test_bridge_discards_retired_generation_audio_before_send_or_receipt_contamination(monkeypatch):
    harness = SchedulerHarness(monkeypatch)
    output = harness.session._output
    try:
        initial = BidiConnectionStartEvent(connection_id="connection-old", model="nova")
        harness.controller.observe_provider_event(initial)
        await output(initial)
        old_audio = pcm_event("retired-audio")
        harness.controller.observe_provider_event(old_audio)
        replacement = BidiConnectionStartEvent(connection_id="connection-new", model="nova")
        harness.controller.observe_provider_event(replacement)
        await output(replacement)
        assert not harness.controller.accept_output(old_audio)
        await output(old_audio)
        assert harness.media == []
        assert id(old_audio) not in harness.controller._received
        assert id(old_audio) not in harness.controller._pending_output
        assert id(old_audio) not in harness.controller._stale_received
        # SDK-generated events lack a provider receipt; accept them normally.
        from strands.bidi.types.events import BidiConnectionWarningEvent
        warning = BidiConnectionWarningEvent(time_left_s=10)
        assert harness.controller.accept_output(warning)
        await output(warning)
        assert harness.controller.connection_ready
        assert harness.controller.attempt is None
    finally:
        await harness.close()
