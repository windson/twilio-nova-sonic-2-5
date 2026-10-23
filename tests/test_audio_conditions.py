"""Synthetic duplex transport conditions, not an acoustic echo detector test.

The application has no AEC or waveform classifier. The fake provider explicitly
emits barge-in; waveforms only exercise the real codecs and native SDK I/O.
"""

from __future__ import annotations

import asyncio
import audioop
import base64
import json
import math
import struct
from types import SimpleNamespace

import pytest
from strands.bidi.models.model import BidiModel
from strands.bidi.types.content import BidiMessage
from strands.bidi.types.events import (
    BidiAudioDeltaEvent, BidiAudioStartEvent, BidiAudioStopEvent, BidiBargeInEvent,
    BidiConnectionStartEvent, BidiResponseStartEvent, BidiResponseStopEvent,
    BidiToolUseBlocksEvent, BidiTranscriptDeltaEvent, BidiTranscriptStartEvent,
    BidiTranscriptStopEvent,
)
from strands.bidi.types.media import AudioDelta
from strands.types.tools import ToolResultBlock

from app.audio import (
    NOVA_INPUT_RATE, NOVA_OUTPUT_RATE, TWILIO_RATE,
    NovaToTwilioBridge, TwilioToNovaBridge, pcm16_to_ulaw, ulaw_to_pcm16,
)
from app.call_lifecycle import CallLifecycleController, CallState, TerminationBounds
from app.collections_agent import CallToolRuntime, build_tools
from app.sonic_bridge import SonicSession
from app.store import InMemoryStore


def _tone(rate, duration, frequency=440, amplitude=6000):
    return struct.pack(f"<{int(rate * duration)}h", *[
        int(amplitude * math.sin(2 * math.pi * frequency * index / rate))
        for index in range(int(rate * duration))
    ])


def _echo_and_speech(outbound, *, delay_ms, gain, speech):
    # Build the echo from audio after the actual 24 kHz -> 8 kHz G.711 path.
    playback = ulaw_to_pcm16(NovaToTwilioBridge().process_bytes(outbound))
    echo = struct.unpack(f"<{len(playback) // 2}h", playback)
    delay = int(TWILIO_RATE * delay_ms / 1000)
    count = int(TWILIO_RATE * 0.32)
    mixed = []
    for index in range(count):
        reflected = gain * echo[index - delay] if 0 <= index - delay < len(echo) else 0
        # Deterministic speech-like near-end tone/envelope; this is not speech
        # recognition ground truth and does not pretend to model a whole room.
        near_end = (4500 * math.sin(2 * math.pi * 880 * index / TWILIO_RATE)
                    if speech and 0.08 <= index / TWILIO_RATE < 0.26 else 0)
        mixed.append(max(-32768, min(32767, int(reflected + near_end))))
    return pcm16_to_ulaw(struct.pack(f"<{count}h", *mixed))


async def _until(predicate):
    async def wait():
        while not predicate():
            await asyncio.sleep(0.001)
    await asyncio.wait_for(wait(), 2)


class _Provider(BidiModel):
    """Only public provider I/O is fake; Strands owns dispatch and tool groups."""

    def __init__(self, lifecycle):
        self.lifecycle = lifecycle
        self.events = asyncio.Queue()
        self.inbound = []
        self.results = {}

    def get_config(self):
        return {"model_id": "synthetic-audio", "connection": {"auto_restart": False}}

    def update_config(self, **config):
        pass

    async def start(self, **kwargs):
        pass

    async def stop(self):
        pass

    async def receive(self):
        self.events.put_nowait(BidiConnectionStartEvent(connection_id="synthetic-connection", model="nova"))
        while True:
            event = await self.events.get()
            self.lifecycle.observe_provider_event(event)
            yield event

    async def send(self, content):
        if isinstance(content, AudioDelta):
            self.inbound.append(content.source["bytes"])
        elif isinstance(content, BidiMessage):
            blocks = [block for block in content.content if isinstance(block, ToolResultBlock)]
            self.results.update({block.tool_use_id: block for block in blocks})
            if blocks:
                self.lifecycle.results_submitted([block.tool_use_id for block in blocks])


class _Call:
    def __init__(self):
        self.media, self.marks, self.clears, self.hangups = [], [], [], []
        self.runtime = CallToolRuntime()
        metrics = SimpleNamespace(
            on_session_start=lambda: None, on_session_end=lambda: None,
            record_event=lambda event: None, record_lifecycle_event=lambda event, **fields: None,
        )

        async def hangup():
            self.hangups.append(True)
            return {"confirmed_terminal": True, "status": "completed"}

        async def no_io(*args):
            pass

        self.controller = CallLifecycleController(
            call_sid="CA_synthetic_duplex", stream_sid="MZ_synthetic_duplex", runtime=self.runtime,
            metrics=metrics, hangup=hangup, persist=no_io, close_websocket=no_io,
            bounds=TerminationBounds(tools=2, generation=2, playback=2, sdk_cleanup=1, twilio_request=1),
        )
        self.provider = _Provider(self.controller)

        async def media(payload):
            self.media.append(payload)

        async def mark(name):
            self.marks.append(name)

        async def clear():
            # Any echo of a cleared mark must already have lost its authority.
            self.clears.append((self.controller.attempt, self.controller.state))

        self.session = SonicSession(
            model=self.provider, send_media=media, send_mark=mark, on_interrupt=clear,
            tools=build_tools(InMemoryStore(), runtime=self.runtime), lifecycle=self.controller,
            metrics=metrics, kickoff_text=None,
        )
        self.controller.bind_session(self.session)

    async def start(self):
        self.task = self.session.start()
        await _until(lambda: self.controller.connection_ready)
        return self

    async def close(self):
        self.controller.transport_closed()
        await asyncio.wait_for(self.controller.start_close(reason="synthetic_cleanup", outcome="caller_disconnected"), 3)
        await asyncio.gather(self.task, return_exceptions=True)
        # Other test modules may enable the console exporter. Finish the fake
        # session's spans while pytest still captures output from this test.
        from opentelemetry import trace
        force_flush = getattr(trace.get_tracer_provider(), "force_flush", None)
        if force_flush is not None:
            await asyncio.to_thread(force_flush)

    async def tool(self, name, identity, **arguments):
        self.provider.events.put_nowait(BidiToolUseBlocksEvent(tool_uses=[
            {"name": name, "toolUseId": identity, "input": arguments},
        ]))
        await _until(lambda: identity in self.provider.results)
        block = self.provider.results[identity]
        assert block.status == "success"
        return json.loads(block.content[0]["text"])


@pytest.mark.asyncio
@pytest.mark.parametrize("delay_ms,gain,speech,jitter_ms", [
    (20, 0.25, False, (0, 1, 0, 3)),
    (60, 0.50, True, (3, 0, 2, 1)),
    (100, 0.75, True, (0, 5, 1, 0)),
])
async def test_delayed_echo_and_double_talk_are_forwarded_under_arrival_jitter(delay_ms, gain, speech, jitter_ms):
    call = await _Call().start()
    outbound = _tone(NOVA_OUTPUT_RATE, 0.24)
    incoming = _echo_and_speech(outbound, delay_ms=delay_ms, gain=gain, speech=speech)
    expected = TwilioToNovaBridge().process_bytes(incoming)
    try:
        call.provider.events.put_nowait(BidiResponseStartEvent(response_id="speaking"))
        call.provider.events.put_nowait(BidiAudioStartEvent(content_id="outbound"))
        for index in range(0, len(incoming), 160):  # ordered 20 ms G.711 frames
            if index < 12 * 160:
                offset = index // 160 * 960  # 20 ms 24 kHz signed-16 PCM
                call.provider.events.put_nowait(BidiAudioDeltaEvent(
                    audio=base64.b64encode(outbound[offset:offset + 960]).decode(),
                    format="pcm", sample_rate=NOVA_OUTPUT_RATE, channels=1, content_id="outbound",
                ))
            await asyncio.sleep(jitter_ms[(index // 160) % len(jitter_ms)] / 1000)
            await call.session.feed_twilio_audio(base64.b64encode(incoming[index:index + 160]).decode())
        await _until(lambda: sum(map(len, call.provider.inbound)) == len(expected))
        assert b"".join(call.provider.inbound) == expected
        assert call.media and audioop.rms(expected, 2) > 0
        assert (TWILIO_RATE, NOVA_INPUT_RATE, NOVA_OUTPUT_RATE) == (8000, 16000, 24000)
        assert abs(len(expected) // 2 - len(incoming) * 2) <= 1
        assert call.controller.activity == 0 and call.clears == []
        assert call.hangups == [], "waveforms alone must not authorize a call end"
    finally:
        await call.close()


@pytest.mark.asyncio
async def test_provider_barge_during_double_talk_invalidates_mark_before_clear_and_keeps_input_live():
    call = await _Call().start()
    try:
        ready = await call.tool("end_conversation", "end-echo")
        await call.tool("begin_farewell", "begin-echo", attempt_id=ready["attempt_id"])
        outbound = _tone(NOVA_OUTPUT_RATE, 0.10)
        for event in [
            BidiResponseStartEvent(response_id="farewell"),
            BidiTranscriptStartEvent(role="assistant", content_id="farewell-text"),
            BidiTranscriptDeltaEvent(delta="Thank you for your time. Goodbye.", role="assistant", content_id="farewell-text"),
            BidiAudioStartEvent(content_id="farewell-audio"),
            BidiAudioDeltaEvent(audio=base64.b64encode(outbound).decode(), format="pcm",
                                sample_rate=NOVA_OUTPUT_RATE, channels=1, content_id="farewell-audio"),
            BidiAudioStopEvent(content_id="farewell-audio"),
            BidiTranscriptStopEvent(role="assistant", content_id="farewell-text"),
            BidiResponseStopEvent(response_id="farewell"),
        ]:
            call.provider.events.put_nowait(event)
        await _until(lambda: bool(call.marks))
        old_mark = call.marks[-1]
        incoming = _echo_and_speech(outbound, delay_ms=60, gain=0.4, speech=True)
        await call.session.feed_twilio_audio(base64.b64encode(incoming[:320]).decode())
        # The provider supplies this signal. The test does not infer it from
        # echo, distinguish a real human from echo, or evaluate acoustic AEC.
        call.provider.events.put_nowait(BidiBargeInEvent())
        await _until(lambda: bool(call.clears))
        assert call.clears == [(None, CallState.ACTIVE)]
        assert not call.controller.acknowledge_playback(old_mark, "MZ_synthetic_duplex")
        await call.session.feed_twilio_audio(base64.b64encode(incoming[320:]).decode())
        expected = TwilioToNovaBridge().process_bytes(incoming)
        await _until(lambda: sum(map(len, call.provider.inbound)) == len(expected))
        assert b"".join(call.provider.inbound) == expected
        assert call.hangups == [] and call.runtime.lifecycle.tool_admission_open
    finally:
        await call.close()
