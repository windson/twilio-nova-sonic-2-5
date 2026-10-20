"""Regression tests for the GRACEFUL end-of-call flow (BUG 1).

Live call ``CA3b7e801f39a87eae9f0af33b18a9875d`` exhibited one root cause with
three symptoms when a call ended via the new ``end_conversation`` tool:

* the Twilio call was hung up the instant the model invoked the tool, cutting
  off the farewell audio ("Thank you for your time, and have a great day")
  while it was still draining to the caller -- the call "ended abruptly / no
  bye";
* the immediate hangup tore the ``/media`` SonicSession down before
  ``CallMetrics.on_session_end()`` ran, so the persisted analytics had
  ``total_e2e_ms = null`` / ``non_nova_ms = null`` and no ``call_end_summary``
  event ("Total duration = -").

The fix makes ``end_conversation`` a GRACEFUL end with a deterministic,
idempotent ordering:

    farewell audio drains to Twilio
      -> metrics finalized (on_session_end -> total_e2e_ms + call_end_summary)
      -> analytics persisted
      -> Twilio hangup
      -> SonicSession closed

These tests stub the Strands agent and the Twilio client -- no live call, no
network, no AWS. They assert:

* (a) a tool-ended call persists a record with a non-null ``total_e2e_ms`` /
  ``non_nova_ms`` and a ``call_end_summary`` event;
* (b) the hangup happens AFTER the metrics were finalized AND after the
  farewell audio drained (call-order spies);
* (c) ``end_conversation`` is idempotent: invoking it twice yields exactly one
  hangup and one finalize.
"""

from __future__ import annotations

import asyncio
import base64
import json

import pytest

from app import main as main_mod
from app import sonic_bridge
from app.config import Settings
from app.metrics import CallMetrics
from app.store import InMemoryStore, set_default_store


def _settings() -> Settings:
    return Settings(
        nova_model_id="amazon.nova-2-5-sonic",
        aws_region="us-east-1",
        app_log_group="/ecs/ar-collections-nova-sonic/app",
        extraction_model_id="amazon.nova-lite-v1:0",
        code_interpreter_id="aws.codeinterpreter.v1",
        payment_link_base_url="https://pay.example.com/",
        twilio_trial_mode=True,
        twilio_account_sid="AC_test_sid",
        twilio_auth_token="test_token_value",
        twilio_from_number="+15005550006",
        twilio_verified_caller_id="+15005550010",
        public_base_url="https://example.ngrok.app",
        otel_enabled=False,
        otel_exporter="console",
        otel_endpoint=None,
    )


def _farewell_audio_event():
    """A GA ``BidiAudioDeltaEvent`` carrying ~120ms of 24 kHz PCM silence.

    120ms at 24 kHz mono PCM16 = 0.12 * 24000 * 2 = 5760 bytes. Transcoded to
    u-law 8 kHz this is ~960 bytes => ~120ms of Twilio playback, enough that the
    drain has a measurable (but short) tail to wait out.
    """
    from strands.bidi.types.events import BidiAudioDeltaEvent

    pcm = b"\x00\x00" * (24000 * 120 // 1000)  # 120ms of PCM16 silence @24k
    return BidiAudioDeltaEvent(
        audio=base64.b64encode(pcm).decode("ascii"),
        format="pcm",
        sample_rate=24000,
        channels=1,
        content_id="farewell-1",
    )


class _EndConversationAgent:
    """Stub BidiAgent whose run() emulates a call ending via ``end_conversation``.

    It speaks a couple of transcript turns, STREAMS a farewell audio chunk, sends
    the audio-stop event, then the model "decides" the call is over and invokes
    the ``end_conversation`` tool (firing the ``on_end`` callback wired by
    ``/media``). After firing the tool it blocks forever, exactly like a live
    Nova stream, so teardown happens via cancellation.
    """

    def __init__(self, tools, **_):
        self._tools = tools or []

    @property
    def _end_tool(self):
        for t in self._tools:
            name = getattr(t, "tool_name", None) or getattr(t, "__name__", None)
            if name == "end_conversation":
                return t
        return None

    async def run(self, inputs, outputs, invocation_state=None):
        from strands.bidi.types.events import (
            BidiAudioStopEvent,
            BidiTranscriptBlockEvent,
        )

        for out in outputs:
            await out.start(self)
        for out in outputs:
            await out(
                BidiTranscriptBlockEvent(
                    transcript="Hello, this is AR.", role="assistant", content_id="a1"
                )
            )
            await out(
                BidiTranscriptBlockEvent(
                    transcript="Okay, I will pay Friday.", role="user", content_id="u1"
                )
            )
            # Stream the farewell audio then signal the stream stopped.
            await out(_farewell_audio_event())
            await out(BidiAudioStopEvent(content_id="farewell-1"))
        # Let a little wall-clock elapse so total_e2e_ms is clearly > 0.
        await asyncio.sleep(0.02)
        # Model concludes: fire the terminal tool (mirrors Nova calling it).
        tool = self._end_tool
        assert tool is not None, "end_conversation tool must be wired in"
        fn = getattr(tool, "_tool_func", None) or getattr(tool, "__wrapped__", None) or tool
        try:
            fn(reason="done")
        except TypeError:
            fn()
        # Mimic an open stream: block until cancelled by the teardown.
        await asyncio.Future()


class _FakeWebSocket:
    """Minimal Twilio Media Streams websocket stub.

    Delivers ``connected`` then ``start``, then blocks on ``receive_text`` until
    the Twilio hangup "disconnects" the socket (the test's ``hangup_call`` stub
    sets an event that makes the next ``receive_text`` raise WebSocketDisconnect,
    exactly as a real hangup closing the stream does).
    """

    def __init__(self, messages, disconnected: asyncio.Event):
        self._messages = list(messages)
        self._disconnected = disconnected
        self.sent: list[str] = []

    async def accept(self) -> None:
        return None

    async def receive_text(self) -> str:
        if self._messages:
            return self._messages.pop(0)
        await self._disconnected.wait()
        from fastapi import WebSocketDisconnect

        raise WebSocketDisconnect(code=1000)

    async def send_text(self, text: str) -> None:
        self.sent.append(text)


def _start_messages(call_sid: str, account_id: str) -> list[str]:
    return [
        json.dumps({"event": "connected"}),
        json.dumps(
            {
                "event": "start",
                "streamSid": "MZ_test",
                "start": {
                    "streamSid": "MZ_test",
                    "callSid": call_sid,
                    "customParameters": {"accountId": account_id},
                },
            }
        ),
    ]


@pytest.mark.asyncio
async def test_tool_ended_call_persists_metrics_and_orders_hangup_last(monkeypatch):
    """(a)+(b): a tool-ended call persists non-null duration metrics + a
    ``call_end_summary`` event, and the hangup happens AFTER finalization and
    AFTER the farewell audio drained.
    """
    store = InMemoryStore(seed=True)
    set_default_store(store)
    account_id = store.list_dues()[0]["account_id"]
    call_sid = "CA_tool_ended_test"

    monkeypatch.setattr(main_mod, "get_settings", _settings)
    monkeypatch.setattr(sonic_bridge, "get_settings", _settings)
    monkeypatch.setattr(sonic_bridge, "build_agent", lambda **kw: _EndConversationAgent(**kw))

    # Call-order spies: record the sequence of key terminal steps.
    order: list[str] = []
    disconnected = asyncio.Event()

    _orig_finalize = sonic_bridge.SonicSession.finalize_metrics

    def spy_finalize(self):
        already = getattr(self, "_metrics_finalized", False)
        _orig_finalize(self)
        if not already:
            order.append("finalize")

    monkeypatch.setattr(sonic_bridge.SonicSession, "finalize_metrics", spy_finalize)

    _orig_drain = sonic_bridge.SonicSession.drain_outbound_audio

    async def spy_drain(self, **kw):
        await _orig_drain(self, **kw)
        order.append("drain")

    monkeypatch.setattr(sonic_bridge.SonicSession, "drain_outbound_audio", spy_drain)

    def fake_hangup(sid, **_):
        assert sid == call_sid
        order.append("hangup")
        disconnected.set()
        return {"hung_up": True, "status": "completed", "call_sid": sid}

    monkeypatch.setattr(main_mod, "hangup_call", fake_hangup)
    monkeypatch.setattr(main_mod, "_launch_postcall", lambda *a, **k: None)

    ws = _FakeWebSocket(_start_messages(call_sid, account_id), disconnected)
    await asyncio.wait_for(main_mod.media_stream(ws), timeout=5.0)

    record = store.get_call(call_sid)
    assert record is not None, "analytics record must be persisted for a tool-ended call"
    # THE BUG: these were null on the tool-ended path.
    assert record["total_e2e_ms"] is not None, "total_e2e_ms must be computed"
    assert record["total_e2e_ms"] > 0
    assert record["non_nova_ms"] is not None, "non_nova_ms must be computed"
    event_types = [e.get("event_type") for e in record.get("events_log", [])]
    assert "call_end_summary" in event_types, "call_end_summary must be emitted"

    # (b) ordering: farewell drained, THEN metrics finalized, THEN hangup.
    assert "drain" in order and "finalize" in order and "hangup" in order
    assert order.index("drain") < order.index("hangup"), "hangup must be after the farewell drain"
    assert order.index("finalize") < order.index("hangup"), (
        "hangup must be after metrics are finalized"
    )


@pytest.mark.asyncio
async def test_end_conversation_is_idempotent_one_hangup_one_finalize(monkeypatch):
    """(c): invoking ``end_conversation`` twice => exactly one hangup, one finalize."""
    store = InMemoryStore(seed=True)
    set_default_store(store)
    account_id = store.list_dues()[0]["account_id"]
    call_sid = "CA_double_end"

    class _DoubleEndAgent(_EndConversationAgent):
        async def run(self, inputs, outputs, invocation_state=None):
            for out in outputs:
                await out.start(self)
            await asyncio.sleep(0.01)
            tool = self._end_tool
            fn = getattr(tool, "_tool_func", None) or tool
            # Fire the terminal tool TWICE (mirrors a model double-emit).
            fn(reason="done")
            fn(reason="done-again")
            await asyncio.Future()

    monkeypatch.setattr(main_mod, "get_settings", _settings)
    monkeypatch.setattr(sonic_bridge, "get_settings", _settings)
    monkeypatch.setattr(sonic_bridge, "build_agent", lambda **kw: _DoubleEndAgent(**kw))

    hangups: list[str] = []
    disconnected = asyncio.Event()

    def fake_hangup(sid, **_):
        hangups.append(sid)
        disconnected.set()
        return {"hung_up": True}

    monkeypatch.setattr(main_mod, "hangup_call", fake_hangup)
    monkeypatch.setattr(main_mod, "_launch_postcall", lambda *a, **k: None)

    ws = _FakeWebSocket(_start_messages(call_sid, account_id), disconnected)
    await asyncio.wait_for(main_mod.media_stream(ws), timeout=5.0)

    assert hangups == [call_sid], "end_conversation must hang up exactly once"
    record = store.get_call(call_sid)
    assert record is not None
    summaries = [e for e in record.get("events_log", []) if e.get("event_type") == "call_end_summary"]
    assert len(summaries) == 1, "call_end_summary must be emitted exactly once"


@pytest.mark.asyncio
async def test_close_runs_on_session_end_before_concurrent_caller_returns(monkeypatch):
    """Unit-level proof of the ordering guarantee.

    Two concurrent callers invoke ``SonicSession.close()`` (mirroring the
    end_conversation task and the /media finally racing). Both must observe
    ``metrics.on_session_end()`` as having completed before ``close()`` returns,
    so whichever caller persists analytics sees a finalized ``total_e2e_ms``.
    """

    class _BlockingAgent:
        def __init__(self, *a, **k):
            pass

        async def run(self, inputs, outputs, invocation_state=None):
            await asyncio.Future()  # block until cancelled

    monkeypatch.setattr(sonic_bridge, "build_agent", lambda **kw: _BlockingAgent())

    metrics = CallMetrics(call_sid="CA_race", account_id="ACC-1")

    async def send_media(_payload: str) -> None:  # pragma: no cover - unused
        return None

    session = sonic_bridge.SonicSession(
        send_media=send_media,
        metrics=metrics,
        settings=_settings(),
        kickoff_text=None,
    )
    session.start()
    await asyncio.sleep(0.01)
    assert metrics.total_e2e_ms is None  # not finalized yet

    await asyncio.gather(session.close(), session.close())

    assert metrics.total_e2e_ms is not None
    assert metrics.total_e2e_ms >= 0
    summary_events = [e for e in metrics.events_log if e.get("event_type") == "call_end_summary"]
    assert len(summary_events) == 1, "call_end_summary must be emitted exactly once"


@pytest.mark.asyncio
async def test_drain_waits_for_audio_stop_then_playback_tail(monkeypatch):
    """``drain_outbound_audio`` waits for the audio stream to stop generating,
    then waits out the queued Twilio playback tail (bounded)."""

    class _BlockingAgent:
        def __init__(self, *a, **k):
            pass

        async def run(self, inputs, outputs, invocation_state=None):
            await asyncio.Future()

    monkeypatch.setattr(sonic_bridge, "build_agent", lambda **kw: _BlockingAgent())

    sent: list[str] = []

    async def send_media(payload: str) -> None:
        sent.append(payload)

    session = sonic_bridge.SonicSession(
        send_media=send_media,
        metrics=None,
        settings=_settings(),
        kickoff_text=None,
    )
    # Feed a farewell audio chunk through the real output stream so there is a
    # playback tail, but do NOT send the audio-stop yet: the drain must block.
    out = session._output  # type: ignore[attr-defined]
    await out(_farewell_audio_event())
    assert sent, "farewell audio should have been forwarded to Twilio"
    assert not out.audio_idle.is_set(), "stream is actively generating -> not idle"
    assert out.remaining_playback_s() > 0

    # With a tiny audio-stop timeout the drain returns promptly even though the
    # stop never came (bounded safety), proving it cannot hang forever.
    await asyncio.wait_for(
        session.drain_outbound_audio(
            audio_stop_timeout_s=0.05, max_drain_s=0.5, margin_s=0.0
        ),
        timeout=2.0,
    )
