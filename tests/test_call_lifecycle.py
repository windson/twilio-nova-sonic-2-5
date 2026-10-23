"""Deterministic telephone state-machine races; no provider or telephone I/O."""
import asyncio
import base64
from dataclasses import replace

import pytest
from strands.bidi.types.events import (
    BidiAudioDeltaEvent, BidiAudioStartEvent, BidiAudioStopEvent, BidiBargeInEvent,
    BidiConnectionRestartEvent, BidiConnectionStartEvent, BidiResponseStartEvent,
    BidiResponseStopEvent, BidiTranscriptStartEvent, BidiTranscriptDeltaEvent,
    BidiTranscriptStopEvent, BidiTranscriptBlockEvent,
)
from strands.types._events import ToolResultMessageEvent
from strands.types.events import ToolResultEvent

from app.call_lifecycle import CallLifecycleController, CallState, TerminationBounds
from app.collections_agent import CallToolRuntime
from app.metrics import CallMetrics


class Session:
    def __init__(self):
        self.marks = []
        self.prompts = []
        self.output_closed = False
        self.run_task = None
        self.close_count = 0
        self.mark_ready = asyncio.Event()
        self.close_release = None

    def start(self):
        self.run_task = asyncio.create_task(asyncio.Event().wait())
        return self.run_task

    def close_output(self):
        self.output_closed = True

    def notify_transport_closed(self):
        self.output_closed = True

    async def close(self):
        self.close_count += 1
        if self.close_release:
            await self.close_release.wait()
        if self.run_task:
            self.run_task.cancel()
            await asyncio.gather(self.run_task, return_exceptions=True)

    async def send_playback_mark(self, name):
        self.marks.append(name)
        self.mark_ready.set()

    async def send_recovery_prompt(self, text):
        self.prompts.append(text)


class Harness:
    def __init__(self, *, bounds=None, confirmed=True):
        self.runtime = CallToolRuntime()
        self.metrics = CallMetrics(call_sid="CA_fake", account_id="ACC-1001")
        self.hangups, self.saved, self.ws_closes = [], [], []
        async def hangup():
            self.hangups.append(True)
            assert self.session.output_closed
            return {"confirmed_terminal": confirmed, "status": "completed" if confirmed else None,
                    "outcome": "confirmed_terminal" if confirmed else "unknown", "attempts": 1}
        async def persist(terminal):
            self.saved.append((terminal, self.metrics.to_call_analytics()))
        async def close_ws():
            self.ws_closes.append(True)
        self.controller = CallLifecycleController(call_sid="CA_fake", stream_sid="MZ_fake",
            runtime=self.runtime, metrics=self.metrics, hangup=hangup, persist=persist,
            close_websocket=close_ws, bounds=bounds or TerminationBounds(.2, .2, .2, .03, .03))
        self.session = Session()
        self.controller.bind_session(self.session)
        self.emit(BidiConnectionStartEvent(connection_id="connection-1", model="fake"))

    def emit(self, event, *, provider=True):
        if provider:
            self.controller.observe_provider_event(event)
        self.controller.observe_event(event)

    def register(self, *tools):
        self.controller.register_tool_group([{"toolUseId": identity, "name": name, "input": {}}
                                             for identity, name in tools])

    def results(self, *identities, submitted=True, status="success"):
        results = [{"toolUseId": identity, "status": status, "content": [{"text": "result"}]}
                   for identity in identities]
        for result in results:
            self.emit(ToolResultEvent(result), provider=False)
        self.emit(ToolResultMessageEvent({"role": "user", "content": [{"toolResult": r} for r in results]}),
                  provider=False)
        if submitted:
            self.controller.results_submitted(list(identities))

    async def ready(self, *, end_id="end", begin_id="begin", submit_begin=True, language="en"):
        self.register((end_id, "end_conversation"))
        result = await self.controller.request_end(tool_use_id=end_id, language=language)
        assert result["ready"]
        self.results(end_id)
        self.register((begin_id, "begin_farewell"))
        grant = await self.controller.begin_farewell(result["attempt_id"], tool_use_id=begin_id)
        assert grant["ready"]
        self.results(begin_id, submitted=submit_begin)
        return result["attempt_id"]

    def farewell(self, *, response="response", audio="audio", text="Thank you for your time. Goodbye.",
                 start=True, text_first=False, complete=True):
        if start:
            self.emit(BidiResponseStartEvent(response))
            self.emit(BidiAudioStartEvent(audio))
        def transcript():
            self.emit(BidiTranscriptStartEvent("assistant", "transcript"))
            self.emit(BidiTranscriptDeltaEvent(text, "assistant", "transcript"))
            self.emit(BidiTranscriptStopEvent("assistant", "transcript"))
            self.emit(BidiTranscriptBlockEvent(text, "assistant", "transcript"), provider=False)
        if text_first:
            transcript()
        self.emit(BidiAudioDeltaEvent(base64.b64encode(b"\0\0" * 480).decode(), "pcm", 24000, 1, audio))
        if not text_first:
            transcript()
        if complete:
            self.emit(BidiAudioStopEvent(audio))
            self.emit(BidiResponseStopEvent(response))

    async def mark(self):
        await asyncio.wait_for(self.session.mark_ready.wait(), .5)
        # Let the successful send observer run before acknowledging.
        for _ in range(3):
            await asyncio.sleep(0)
        return self.session.marks[-1]

    async def finish(self):
        await self.controller.start_close(reason="test_cleanup", outcome="caller_disconnected")


@pytest.mark.asyncio
@pytest.mark.parametrize("text_first", [False, True])
async def test_farewell_requires_audio_completed_transcript_and_ack_then_one_finalization(text_first):
    h = Harness()
    await h.ready()
    h.farewell(text_first=text_first)
    mark = await h.mark()
    assert not h.hangups and not h.saved
    assert not h.controller.acknowledge_playback(mark, "wrong-stream")
    assert not h.controller.acknowledge_playback("unknown", "MZ_fake")
    assert h.controller.acknowledge_playback(mark, "MZ_fake")
    assert not h.controller.tool_admission_open and h.session.output_closed
    await h.controller.closed.wait()
    await h.finish()
    assert len(h.hangups) == len(h.saved) == len(h.ws_closes) == h.session.close_count == 1
    summary = h.saved[0][1]["termination"]
    assert summary["outcome"] == "farewell_played"
    assert summary["telephone_completion_confirmed"] is True
    assert summary["closing_handshake_ms"] >= 0
    assert h.saved[0][1]["total_e2e_ms"] is not None


@pytest.mark.asyncio
async def test_idle_and_delayed_first_audio_never_authorize_hangup():
    h = Harness()
    await h.ready()
    h.emit(BidiResponseStartEvent("response"))
    await asyncio.sleep(.01)
    assert not h.session.marks and not h.hangups
    h.farewell(start=False)
    mark = await h.mark()
    h.controller.acknowledge_playback(mark, "MZ_fake")
    await h.finish()


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["Thank you", "", "See you later"])
async def test_missing_configured_closing_phrase_resumes_with_one_nova_prompt(text):
    h = Harness(bounds=TerminationBounds(.1, .02, .02, .03, .03))
    await h.ready()
    h.farewell(text=text)
    await asyncio.sleep(.04)
    assert h.controller.state == CallState.ACTIVE
    assert not h.session.marks and not h.hangups
    assert len(h.session.prompts) == 1
    assert h.controller.summary["reason"] == "closing_generation_timeout"
    await h.finish()


@pytest.mark.asyncio
async def test_normalized_good_bye_and_reused_audio_id_after_tool_continuation():
    h = Harness()
    h.emit(BidiResponseStartEvent("response"))
    h.emit(BidiAudioStartEvent("audio"))
    await h.ready()
    h.farewell(start=False, text="Thank you. GOOD - BYE!")
    await h.mark()
    await h.finish()


@pytest.mark.asyncio
async def test_pregrant_queued_audio_and_transcript_do_not_count_as_farewell():
    h = Harness(bounds=TerminationBounds(.1, .03, .03, .03, .03))
    await h.ready(submit_begin=False)
    h.emit(BidiResponseStartEvent("response"))
    queued = BidiAudioDeltaEvent(base64.b64encode(b"\0\0" * 480).decode(), "pcm", 24000, 1, "audio")
    h.controller.observe_provider_event(queued)
    h.controller.results_submitted(["begin"])
    h.controller.observe_event(queued)
    h.emit(BidiAudioStopEvent("audio"))
    h.emit(BidiResponseStopEvent("response"))
    await asyncio.sleep(.05)
    assert not h.session.marks and not h.hangups
    await h.finish()


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["settling", "preparing", "playback"])
@pytest.mark.parametrize("signal", ["barge", "transcript"])
async def test_interruption_each_precommit_phase_invalidates_before_clear_echo(phase, signal):
    h = Harness()
    pending = None
    if phase == "settling":
        h.register(("end", "end_conversation"), ("write", "update_due_record"))
        pending = asyncio.create_task(h.controller.request_end(tool_use_id="end"))
        await asyncio.sleep(0)
    else:
        await h.ready()
    if phase == "playback":
        h.farewell()
        mark = await h.mark()
    event = BidiBargeInEvent() if signal == "barge" else BidiTranscriptStartEvent("user", "caller")
    # Intentionally leave the SDK output queue blocked: receipt alone fences ACK.
    h.controller.observe_provider_event(event)
    assert h.controller.state == CallState.ACTIVE
    if phase == "playback":
        assert not h.controller.acknowledge_playback(mark, "MZ_fake")
    h.controller.observe_event(event)
    assert h.controller.activity == 1
    if pending:
        assert not (await pending)["ready"]
    assert not h.session.prompts and not h.hangups
    await h.finish()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["audio", "response", "text"])
async def test_late_provider_output_invalidates_mark_before_output_queue_consumes_it(kind):
    h = Harness()
    await h.ready()
    h.farewell()
    mark = await h.mark()
    if kind == "audio":
        event = BidiAudioDeltaEvent(base64.b64encode(b"\0\0" * 480).decode(), "pcm", 24000, 1, "audio")
    elif kind == "response":
        event = BidiResponseStartEvent("later-response")
    else:
        event = BidiTranscriptStartEvent("assistant", "later-text")
    h.controller.observe_provider_event(event)
    assert not h.controller.acknowledge_playback(mark, "MZ_fake")
    assert not h.hangups
    h.controller.observe_event(event)
    await h.finish()


@pytest.mark.asyncio
async def test_nonce_replay_wrong_attempt_and_unsubmitted_results_are_not_ready():
    h = Harness()
    h.register(("end", "end_conversation"))
    result = await h.controller.request_end(tool_use_id="end")
    h.results("end", submitted=False)
    h.register(("begin", "begin_farewell"))
    assert not (await h.controller.begin_farewell("wrong", tool_use_id="begin"))["ready"]
    pending = asyncio.create_task(h.controller.begin_farewell(result["attempt_id"], tool_use_id="begin"))
    await asyncio.sleep(0)
    assert not pending.done()
    assert not (await h.controller.begin_farewell(result["attempt_id"], tool_use_id="begin"))["ready"]
    h.controller.results_submitted(["end"])
    assert (await pending)["ready"]
    assert not (await h.controller.begin_farewell(result["attempt_id"], tool_use_id="begin"))["ready"]
    await h.finish()


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["audio", "farewell", "writes"])
async def test_restart_invalidates_old_attempt_and_marks_retaining_tools(phase):
    h = Harness()
    if phase == "writes":
        h.register(("write", "update_due_record"), ("end", "end_conversation"))
        h.controller.tool_started("write")
        waiting = asyncio.create_task(h.controller.request_end(tool_use_id="end"))
        await asyncio.sleep(0)
    else:
        await h.ready()
        h.farewell(complete=phase == "farewell")
        if phase == "farewell":
            await h.mark()
    h.emit(BidiConnectionRestartEvent("scheduled", turn_interrupted=True), provider=False)
    h.emit(BidiConnectionStartEvent("replacement", "fake"))
    assert h.controller.state == CallState.ACTIVE
    assert h.controller.connection_ready and h.controller._response is None
    assert not h.session.prompts
    if phase == "writes":
        assert "write" in h.controller.tools
        assert not (await waiting)["ready"]
    await h.finish()


@pytest.mark.asyncio
async def test_playback_timeout_fresh_attempt_has_new_nonce_and_mark():
    h = Harness(bounds=TerminationBounds(.1, .1, .02, .03, .03))
    old = await h.ready()
    h.farewell()
    old_mark = await h.mark()
    await asyncio.sleep(.04)
    assert h.controller.state == CallState.ACTIVE and len(h.session.prompts) == 1
    new = await h.ready(end_id="end2", begin_id="begin2")
    assert old != new
    assert not h.controller.acknowledge_playback(old_mark, "MZ_fake")
    h.farewell(response="response2", audio="audio2")
    h.session.mark_ready.clear()
    await h.mark()
    assert old_mark != h.session.marks[-1]
    await h.finish()


@pytest.mark.asyncio
async def test_sdk_failure_with_open_websocket_terminates_as_technical_failure():
    h = Harness()
    async def fail():
        raise RuntimeError("synthetic")
    h.session.start = lambda: asyncio.create_task(fail())
    reader_cancelled = asyncio.Event()
    async def reader():
        try:
            await asyncio.Future()
        finally:
            reader_cancelled.set()
    await asyncio.wait_for(h.controller.run(reader()), .5)
    assert reader_cancelled.is_set()
    assert h.saved[0][1]["termination"]["outcome"] == "technical_failure"
    assert len(h.hangups) == 1 and h.ws_closes == [True]


@pytest.mark.asyncio
async def test_caller_disconnect_has_one_owner_and_unconfirmed_end_withholds_side_effects():
    h = Harness(confirmed=False)
    async def reader():
        return
    await h.controller.run(reader())
    assert h.saved[0][0] is False
    assert h.saved[0][1]["termination"]["outcome"] == "telephone_completion_unresolved"
    assert len(h.hangups) == len(h.saved) == 1


@pytest.mark.asyncio
async def test_sdk_cleanup_timeout_retains_work_but_ends_telephone():
    h = Harness()
    h.session.close_release = asyncio.Event()
    await asyncio.wait_for(h.finish(), .5)
    assert h.hangups and h.ws_closes
    assert h.saved[0][1]["termination"]["sdk_cleanup"] == "timeout_work_retained"
    h.session.close_release.set()
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_pending_storage_is_retained_and_final_snapshot_waits_for_actual_outcome():
    import threading
    h = Harness(bounds=TerminationBounds(.02, .1, .1, .03, .03))
    entered, release = threading.Event(), threading.Event()
    def worker():
        entered.set()
        assert release.wait(1)
    write = asyncio.create_task(h.runtime.execute("update_due_record", worker))
    assert await asyncio.to_thread(entered.wait, .5)
    write.cancel()
    await asyncio.gather(write, return_exceptions=True)
    await h.finish()
    assert h.hangups and h.ws_closes and not h.saved
    assert h.runtime.pending_write_count == 1
    release.set()
    await asyncio.wait_for(h.controller._finalization_task, .5)
    assert len(h.saved) == 1 and h.runtime.pending_write_count == 0
