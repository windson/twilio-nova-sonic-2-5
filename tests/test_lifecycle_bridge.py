"""Offline tests of public SDK observation, transport gating, and task ownership."""

from __future__ import annotations

import asyncio
import base64
from types import SimpleNamespace

import boto3
import pytest
from strands import tool
from strands.bidi.models import BedrockNovaSonicModel
from strands.bidi.types.content import BidiMessage
from strands.bidi.types.events import (
    BidiAudioDeltaEvent,
    BidiBargeInEvent,
    BidiConnectionRestartEvent,
    BidiConnectionStartEvent,
    BidiResponseStartEvent,
    BidiToolUseBlocksEvent,
    BidiTranscriptBlockEvent,
)
from strands.bidi.types.media import AudioDelta
from strands.types.content import TextBlock
from strands.types.tools import ToolResultBlock

from app import sonic_bridge as bridge


@pytest.fixture
def settings():
    return SimpleNamespace(
        nova_model_id="amazon.nova-2-5-sonic", aws_region="us-east-1",
        endpointing_sensitivity=None,
    )


@pytest.fixture
def offline_model(monkeypatch, settings):
    # No AWS credential resolution or model connection is needed for construction.
    monkeypatch.setattr(boto3, "Session", lambda: SimpleNamespace(region_name="us-east-1"))
    return bridge.build_model(settings)


class Observer:
    def __init__(self):
        self.provider = []
        self.groups = []
        self.submitted = []
        self.output = []
        self.discarded = []

    def observe_provider_event(self, event):
        self.provider.append(event)
        if isinstance(event, BidiToolUseBlocksEvent):
            self.groups.append(event.tool_uses)

    def results_submitted(self, tool_use_ids):
        self.submitted.append(tool_use_ids)

    def observe_event(self, event):
        self.output.append(event)

    def output_discarded(self, event):
        self.discarded.append(event)

    def accept_output(self, event):
        return True


def audio_event(content_id="audio-1"):
    return BidiAudioDeltaEvent(
        audio=base64.b64encode(b"\x10\x00" * 480).decode("ascii"),
        format="pcm", sample_rate=24000, channels=1, content_id=content_id,
    )


def result_message(*ids):
    return BidiMessage(content=[
        ToolResultBlock(tool_use_id=id_, status="success", content=[{"text": "saved"}])
        for id_ in ids
    ])


async def discard(_):
    return None


@pytest.mark.asyncio
async def test_public_receive_observes_each_event_before_yield(monkeypatch, offline_model):
    observer = Observer()
    offline_model.bind_lifecycle(observer)
    events = [
        BidiConnectionStartEvent(connection_id="connection-1", model="nova"),
        BidiResponseStartEvent(response_id="response-1"),
        audio_event(),
        BidiToolUseBlocksEvent(tool_uses=[{"toolUseId": "save-1", "name": "capture", "input": {}}]),
    ]

    async def receive(self):
        for event in events:
            yield event

    monkeypatch.setattr(BedrockNovaSonicModel, "receive", receive)
    delivered = []
    async for event in offline_model.receive():
        assert observer.provider == events[:len(delivered) + 1]
        delivered.append(event)
    assert delivered == events
    assert observer.groups == [events[-1].tool_uses]
    assert observer.output == []  # provider production is not socket delivery


@pytest.mark.asyncio
async def test_real_sdk_dispatch_registers_group_while_output_is_backlogged(monkeypatch, offline_model):
    """The tool can start before output consumes its event, but never before registration."""
    observer = Observer()
    tool_started = asyncio.Event()
    output_blocked = asyncio.Event()
    release_output = asyncio.Event()
    model_result_sent = asyncio.Event()
    group = BidiToolUseBlocksEvent(tool_uses=[{"toolUseId": "save-1", "name": "capture", "input": {}}])

    async def provider_start(self, *args, **kwargs):
        return None

    async def provider_stop(self):
        return None

    async def provider_receive(self):
        yield BidiConnectionStartEvent(connection_id="connection-1", model="nova")
        await output_blocked.wait()
        yield group
        await asyncio.Future()

    async def provider_send(self, content):
        model_result_sent.set()

    @tool
    async def capture() -> str:
        """Capture the requested update."""
        assert observer.groups == [group.tool_uses]
        assert group not in observer.output
        tool_started.set()
        return "saved"

    class BlockedOutput:
        async def start(self, agent):
            return None

        async def stop(self):
            return None

        async def __call__(self, event):
            output_blocked.set()
            await release_output.wait()
            observer.observe_event(event)

    monkeypatch.setattr(BedrockNovaSonicModel, "start", provider_start)
    monkeypatch.setattr(BedrockNovaSonicModel, "stop", provider_stop)
    monkeypatch.setattr(BedrockNovaSonicModel, "receive", provider_receive)
    monkeypatch.setattr(BedrockNovaSonicModel, "send", provider_send)
    agent = bridge.build_agent(model=offline_model, tools=[capture], lifecycle=observer)
    task = asyncio.create_task(agent.run(inputs=[], outputs=[BlockedOutput()]))
    try:
        await asyncio.wait_for(tool_started.wait(), 1)
        assert observer.output == []
        assert observer.submitted == []
        # The SDK's bounded output queue can also backpressure result events.
        # Releasing output lets the real result-submission path finish.
        release_output.set()
        await asyncio.wait_for(model_result_sent.wait(), 1)
        assert observer.submitted == [["save-1"]]
    finally:
        release_output.set()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_results_observed_only_after_successful_public_send(monkeypatch, offline_model):
    observer = Observer()
    offline_model.bind_lifecycle(observer)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def send(self, content):
        entered.set()
        await release.wait()

    monkeypatch.setattr(BedrockNovaSonicModel, "send", send)
    task = asyncio.create_task(offline_model.send(result_message("save-1", "end-1")))
    await entered.wait()
    assert observer.submitted == []
    release.set()
    await task
    assert observer.submitted == [["save-1", "end-1"]]
    await offline_model.send(BidiMessage(content=[TextBlock(text="continue")]))
    await offline_model.send(AudioDelta(format="pcm", source={"bytes": b"\0\0"}))
    assert observer.submitted == [["save-1", "end-1"]]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [RuntimeError("provider send failed"), asyncio.CancelledError()])
async def test_failed_or_cancelled_send_never_reports_submission(monkeypatch, offline_model, failure):
    observer = Observer()
    offline_model.bind_lifecycle(observer)

    async def send(self, content):
        raise failure

    monkeypatch.setattr(BedrockNovaSonicModel, "send", send)
    with pytest.raises(type(failure)):
        await offline_model.send(result_message("save-1"))
    assert observer.submitted == []


@pytest.mark.asyncio
async def test_closed_model_blocks_new_sends_and_dispatch_but_reports_inflight_send(monkeypatch, offline_model):
    observer = Observer()
    offline_model.bind_lifecycle(observer)
    entered = asyncio.Event()
    release = asyncio.Event()
    sends = []

    async def send(self, content):
        sends.append(content)
        entered.set()
        await release.wait()

    async def receive(self):
        yield BidiToolUseBlocksEvent(tool_uses=[{"toolUseId": "late", "name": "capture", "input": {}}])

    monkeypatch.setattr(BedrockNovaSonicModel, "send", send)
    monkeypatch.setattr(BedrockNovaSonicModel, "receive", receive)
    in_flight = asyncio.create_task(offline_model.send(result_message("save-1")))
    await entered.wait()
    offline_model.close_submissions()
    with pytest.raises(bridge.SessionClosedError):
        await offline_model.send(result_message("save-2"))
    assert [event async for event in offline_model.receive()] == []
    release.set()
    await in_flight
    assert len(sends) == 1
    assert observer.submitted == [["save-1"]]
    assert observer.groups == []


def test_inherits_public_provider_recovery_and_audio_defaults(offline_model):
    assert bridge.ObservedNovaSonicModel.__init__ is BedrockNovaSonicModel.__init__
    assert bridge.ObservedNovaSonicModel.restart is BedrockNovaSonicModel.restart
    assert bridge.ObservedNovaSonicModel.start is BedrockNovaSonicModel.start
    assert bridge.ObservedNovaSonicModel.get_config is BedrockNovaSonicModel.get_config
    assert bridge.ObservedNovaSonicModel.get_audio_config is BedrockNovaSonicModel.get_audio_config
    assert offline_model.get_connection_config()["restart_after_s"] == 420
    assert offline_model.get_connection_config().get("auto_restart", True) is True
    audio = offline_model.get_audio_config()
    assert audio["input"]["sample_rate"] == 16000
    assert audio["output"]["sample_rate"] == 24000
    assert audio["input"]["channels"] == audio["output"]["channels"] == 1
    assert offline_model.get_config()["params"] == {}


@pytest.mark.asyncio
async def test_audio_observed_after_delivery_and_not_when_send_fails():
    order = []
    observer = Observer()
    event = audio_event()
    observer.observe_event = lambda seen: order.append(("observed", seen))

    async def send(payload):
        order.append(("sent", payload))

    output = bridge._TwilioAudioOutput(send, lifecycle=observer)
    await output(event)
    assert [label for label, _ in order] == ["sent", "observed"]
    assert len(base64.b64decode(order[0][1])) == 160  # 20 ms 8 kHz mu-law, no header

    async def fail(payload):
        raise RuntimeError("socket closed")

    observer = Observer()
    output = bridge._TwilioAudioOutput(fail, lifecycle=observer)
    with pytest.raises(RuntimeError):
        await output(audio_event())
    assert observer.output == []


@pytest.mark.asyncio
async def test_empty_transcoded_audio_discards_provider_stamp_without_delivery():
    observer = Observer()
    sent = []

    async def send(payload):
        sent.append(payload)

    output = bridge._TwilioAudioOutput(send, lifecycle=observer)
    output._out = SimpleNamespace(process_bytes=lambda pcm: b"")
    event = audio_event()
    await output(event)
    assert observer.discarded == [event]
    assert observer.output == sent == []


@pytest.mark.asyncio
async def test_rejected_output_is_discarded_before_metrics_or_transcoding():
    observer = Observer()
    observer.accept_output = lambda event: False
    recorded = []
    output = bridge._TwilioAudioOutput(discard, lifecycle=observer,
        metrics=SimpleNamespace(record_event=lambda event: recorded.append(event)))
    event = BidiAudioDeltaEvent(audio="not base64; rejected before decode", format="pcm",
                               sample_rate=24000, channels=1, content_id="old-audio")
    await output(event)
    assert observer.discarded == [event]
    assert observer.output == recorded == []


@pytest.mark.asyncio
async def test_output_gate_drops_new_media_and_interrupts():
    sent = []
    cleared = []
    observer = Observer()

    async def send(payload):
        sent.append(payload)

    async def clear():
        cleared.append(True)

    output = bridge._TwilioAudioOutput(send, on_interrupt=clear, lifecycle=observer)
    output.close_output()
    await output(audio_event())
    await output(BidiBargeInEvent())
    assert sent == cleared == observer.output == []


@pytest.mark.asyncio
async def test_gate_during_inflight_delivery_still_reports_actual_delivery():
    observer = Observer()
    entered = asyncio.Event()
    release = asyncio.Event()
    sent = []

    async def send(payload):
        entered.set()
        await release.wait()
        sent.append(payload)

    output = bridge._TwilioAudioOutput(send, lifecycle=observer)
    event = audio_event()
    pending = asyncio.create_task(output(event))
    await entered.wait()
    output.close_output()
    release.set()
    await pending
    await output(audio_event("late-audio"))
    assert observer.output == [event]
    assert len(sent) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("event,clear_expected", [
    (BidiBargeInEvent(), True),
    (BidiConnectionRestartEvent(reason="timeout", turn_interrupted=True), True),
    (BidiConnectionRestartEvent(reason="scheduled", turn_interrupted=False), False),
    (BidiConnectionStartEvent(connection_id="replacement", model="nova"), False),
])
async def test_interrupt_restart_reset_and_observe_before_clear(event, clear_expected):
    order = []
    observer = Observer()
    observer.observe_event = lambda event: order.append("observe")

    async def clear():
        order.append("clear")

    output = bridge._TwilioAudioOutput(discard, on_interrupt=clear, lifecycle=observer)
    output._out = SimpleNamespace(reset=lambda: order.append("reset"))
    await output(event)
    assert order == (["reset", "observe", "clear"] if clear_expected else ["reset", "observe"])


@pytest.mark.asyncio
async def test_sdk_generated_transcript_block_reaches_output_observer():
    observer = Observer()
    output = bridge._TwilioAudioOutput(discard, lifecycle=observer)
    event = BidiTranscriptBlockEvent(transcript="Test closing text.", role="assistant", content_id="text-1")
    await output(event)
    assert observer.output == [event]
    assert observer.provider == []


def stub_session(monkeypatch, settings, agent, **kwargs):
    monkeypatch.setattr(bridge, "build_agent", lambda **kwargs: agent)
    return bridge.SonicSession(send_media=discard, settings=settings, **kwargs)


@pytest.mark.asyncio
async def test_session_mark_and_nova_recovery_transport_and_closed_gates(monkeypatch, settings, offline_model):
    sent_text = []
    marks = []

    async def send(text):
        sent_text.append(text)

    async def mark(name):
        marks.append(name)

    session = stub_session(monkeypatch, settings, SimpleNamespace(model=offline_model, send=send), send_mark=mark)
    await session.send_playback_mark("farewell-1")
    await session.send_recovery_prompt("Please complete your closing response.")
    assert marks == ["farewell-1"]
    assert sent_text == ["Please complete your closing response."]
    session.close_output()
    assert offline_model.submissions_closed
    for operation in (session.send_playback_mark("late"), session.send_recovery_prompt("late")):
        with pytest.raises(bridge.SessionClosedError):
            await operation
    await session.feed_twilio_audio("not decoded after gate")
    assert session._queue.empty()
    await session.close()


@pytest.mark.asyncio
async def test_transport_disconnect_gates_output_and_input_without_finalizing_metrics(monkeypatch, settings):
    finalized = []
    metrics = SimpleNamespace(on_session_start=lambda: None, record_event=lambda event: None,
                              on_session_end=lambda: finalized.append(True))
    session = stub_session(monkeypatch, settings, SimpleNamespace(), metrics=metrics)
    session.notify_transport_closed()
    await session.feed_twilio_audio("not decoded after disconnect")
    with pytest.raises(bridge.SessionClosedError):
        await session.send_playback_mark("late")
    await session.close()
    assert finalized == []


@pytest.mark.asyncio
@pytest.mark.parametrize("fail", [False, True])
async def test_owned_run_reports_outcome_without_ending_telephone_metrics(monkeypatch, settings, fail):
    started = []
    finalized = []

    async def run(**kwargs):
        if fail:
            raise RuntimeError("provider failed")

    metrics = SimpleNamespace(on_session_start=lambda: started.append(True), record_event=lambda event: None,
                              on_session_end=lambda: finalized.append(True))
    session = stub_session(monkeypatch, settings, SimpleNamespace(run=run), metrics=metrics)
    task = session.start()
    assert session.start() is task
    if fail:
        with pytest.raises(RuntimeError, match="provider failed"):
            await task
    else:
        await task
    await session.close()
    assert started == []  # Telephone lifetime begins in the controller, not the SDK run.
    assert finalized == []
    with pytest.raises(bridge.SessionClosedError):
        session.start()


@pytest.mark.asyncio
async def test_shared_cleanup_survives_cancelled_awaiter_and_finishes_once(monkeypatch, settings):
    running = asyncio.Event()
    cleanup_started = asyncio.Event()
    cleanup_release = asyncio.Event()
    stops = []

    async def run(**kwargs):
        running.set()
        try:
            await asyncio.Future()
        finally:
            cleanup_started.set()
            await cleanup_release.wait()
            stops.append(True)

    session = stub_session(monkeypatch, settings, SimpleNamespace(run=run))
    run_task = session.start()
    await running.wait()
    first = asyncio.create_task(session.close())
    await cleanup_started.wait()
    second = asyncio.create_task(session.close())
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    assert not second.done()
    cleanup_release.set()
    await asyncio.wait_for(second, 1)
    assert run_task.done()
    assert stops == [True]
    assert session._queue.empty()
    await session.close()
    assert stops == [True]
