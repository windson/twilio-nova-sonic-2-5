"""Tests for the Nova Sonic bridge wiring and the /media WebSocket endpoint.

No AWS credentials or network access: the Nova Sonic model is stubbed and the
agent run loop is exercised via the Strands custom-IO protocols directly.
"""

from __future__ import annotations

import asyncio
import base64

import pytest

from app import sonic_bridge
from app.config import Settings
from app.main import app
from strands.experimental.bidi.models import BidiNovaSonicModel
from strands.experimental.bidi.types.events import (
    BidiAudioInputEvent,
    BidiAudioStreamEvent,
    BidiInterruptionEvent,
)


def _settings() -> Settings:
    return Settings(
        nova_model_id="amazon.nova-2-5-sonic",
        aws_region="us-west-2",
        twilio_account_sid=None,
        twilio_auth_token=None,
        twilio_from_number=None,
        twilio_verified_caller_id=None,
        public_base_url=None,
    )


def test_build_model_sets_model_id_and_voice(monkeypatch):
    """build_model constructs BidiNovaSonicModel with model_id + tiffany voice.

    We stub BidiNovaSonicModel.__init__ to capture args so no boto session or
    network call is created.
    """
    captured = {}

    def fake_init(self, model_id="x", provider_config=None, client_config=None, **kwargs):
        captured["model_id"] = model_id
        captured["provider_config"] = provider_config
        captured["client_config"] = client_config
        # Minimal attribute surface used elsewhere.
        self.model_id = model_id
        self.config = provider_config

    monkeypatch.setattr(BidiNovaSonicModel, "__init__", fake_init)

    model = sonic_bridge.build_model(settings=_settings())

    assert captured["model_id"] == "amazon.nova-2-5-sonic"
    assert captured["provider_config"]["audio"]["voice"] == "tiffany"
    assert captured["provider_config"]["audio"]["input_rate"] == 16000
    assert captured["provider_config"]["audio"]["output_rate"] == 24000
    assert captured["client_config"]["region"] == "us-west-2"
    assert isinstance(model, BidiNovaSonicModel)


def test_build_agent_wires_model_tools_and_prompt(monkeypatch):
    """build_agent passes model/tools/system_prompt to BidiAgent."""

    def fake_init(self, model_id="x", provider_config=None, client_config=None, **kwargs):
        self.model_id = model_id
        self.config = provider_config

    monkeypatch.setattr(BidiNovaSonicModel, "__init__", fake_init)

    agent = sonic_bridge.build_agent(
        tools=[],
        system_prompt="be concise",
        settings=_settings(),
    )
    assert agent.system_prompt == "be concise"
    assert agent.tool_names == []
    assert isinstance(agent.model, BidiNovaSonicModel)
    assert agent.model.model_id == "amazon.nova-2-5-sonic"


def test_media_websocket_route_registered():
    paths = [getattr(r, "path", None) for r in app.routes]
    assert "/media" in paths
    assert "/healthz" in paths


@pytest.mark.asyncio
async def test_queue_input_wraps_pcm_as_audio_event():
    queue: asyncio.Queue = asyncio.Queue()
    inp = sonic_bridge._QueueAudioInput(queue)
    await queue.put(b"\x01\x02\x03\x04")
    event = await inp()
    assert isinstance(event, BidiAudioInputEvent)
    assert event.format == "pcm"
    assert event.sample_rate == 16000
    assert event.channels == 1
    assert base64.b64decode(event.audio) == b"\x01\x02\x03\x04"


@pytest.mark.asyncio
async def test_twilio_output_sends_transcoded_media_and_handles_barge_in():
    sent: list[str] = []
    cleared: list[bool] = []
    recorded: list[str] = []

    class Metrics:
        def on_session_start(self):
            pass

        def record_event(self, event):
            recorded.append(type(event).__name__)

        def on_session_end(self):
            pass

    async def send_media(payload: str) -> None:
        sent.append(payload)

    async def on_interrupt() -> None:
        cleared.append(True)

    out = sonic_bridge._TwilioAudioOutput(send_media, on_interrupt=on_interrupt, metrics=Metrics())

    # Nova 24k PCM audio chunk -> should transcode and send a base64 payload.
    pcm24k = b"\x00\x00" * 480
    await out(
        BidiAudioStreamEvent(
            audio=base64.b64encode(pcm24k).decode("ascii"),
            format="pcm",
            sample_rate=24000,
            channels=1,
        )
    )
    assert len(sent) == 1 and sent[0]

    # Barge-in -> clear playback invoked.
    await out(BidiInterruptionEvent(reason="user_speech"))
    assert cleared == [True]

    assert "BidiAudioStreamEvent" in recorded
    assert "BidiInterruptionEvent" in recorded


@pytest.mark.asyncio
async def test_session_feeds_audio_and_runs_with_stub_agent(monkeypatch):
    """SonicSession.run drives agent.run; close() tears it down cleanly.

    The real BidiAgent is replaced with a stub that consumes inputs and emits a
    single audio event, so the full input/output wiring is exercised without a
    network connection to Bedrock.
    """
    sent: list[str] = []

    async def send_media(payload: str) -> None:
        sent.append(payload)

    class StubAgent:
        def __init__(self, *a, **k):
            pass

        async def run(self, inputs, outputs, invocation_state=None):
            # Pull one input frame (proves the queue path works).
            inp = inputs[0]
            await inp.start(self)
            # Emit a synthetic Nova audio event to the output channel.
            pcm24k = b"\x00\x00" * 480
            evt = BidiAudioStreamEvent(
                audio=base64.b64encode(pcm24k).decode("ascii"),
                format="pcm",
                sample_rate=24000,
                channels=1,
            )
            for out in outputs:
                await out.start(self)
                await out(evt)
            # Then block until cancelled (mimics an open stream).
            await asyncio.Future()

    monkeypatch.setattr(sonic_bridge, "build_agent", lambda **kwargs: StubAgent())

    session = sonic_bridge.SonicSession(send_media=send_media, settings=_settings())
    # Feed an inbound Twilio frame.
    import audioop

    ulaw = audioop.lin2ulaw(b"\x00\x00" * 160, 2)
    payload = base64.b64encode(ulaw).decode("ascii")
    await session.feed_twilio_audio(payload)

    task = session.start()
    await asyncio.sleep(0.05)  # let the stub emit its audio event
    assert sent, "expected outbound media to be sent"

    await session.close()
    assert task.done()
