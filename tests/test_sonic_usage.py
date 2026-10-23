"""Real provider delta contracts, recovery accounting, and admission fences."""
import asyncio
from decimal import Decimal

import pytest
from strands.bidi.models import BedrockNovaSonicModel
from strands.bidi.types.events import BidiConnectionRestartEvent, BidiUsageEvent

from app.metrics import CallMetrics
from app.sonic_bridge import ObservedNovaSonicModel, SonicSession
from app.sonic_usage import UsageAccumulator


def event(text_in=10, audio_in=30, text_out=20, audio_out=40):
    return BidiUsageEvent(text_in + audio_in, text_out + audio_out,
                          text_in + audio_in + text_out + audio_out,
                          {"text": text_in, "audio": audio_in},
                          {"text": text_out, "audio": audio_out})


def test_call_usage_adds_deltas_once_and_does_not_reset_on_connection_restart():
    metrics = CallMetrics(nova_model_id="amazon.nova-2-5-sonic", aws_region="us-west-2")
    metrics.on_session_start()
    metrics.record_event(event())
    metrics.record_event(BidiConnectionRestartEvent("scheduled", turn_interrupted=False))
    metrics.record_event(event(text_in=0, audio_in=0, text_out=0, audio_out=20))
    usage = metrics.to_call_analytics()["usage"]
    assert usage["input_tokens"] == 40
    assert usage["output_tokens"] == 80
    assert usage["total_tokens"] == 120
    assert usage["event_count"] == 2 and usage["complete"]
    assert usage["modality_details"]["output"] == {"text": 20, "audio": 60}
    recorded = [e for e in metrics.events_log if e["event_type"] == "usage"]
    assert [e["output_tokens"] for e in recorded] == [60, 20], "logs remain deltas, not cumulative snapshots"
    assert all(e["usage_accounting"] == "delta" for e in recorded)
    assert metrics.to_call_analytics()["aws_region"] == "us-west-2"


def test_missing_positive_modality_is_incomplete_and_snapshots_are_detached():
    accumulator = UsageAccumulator()
    accumulator.add({"input_tokens": 5, "output_tokens": 0, "total_tokens": 5})
    snapshot = accumulator.snapshot()
    assert snapshot["complete"] is False
    assert snapshot["invalid_event_count"] == 1
    assert snapshot["modality_details"]["input"] == {}
    assert snapshot["modality_details"]["output"] == {"text": 0, "audio": 0}
    snapshot["modality_details"]["output"]["audio"] = 999
    assert accumulator.snapshot()["modality_details"]["output"]["audio"] == 0


@pytest.mark.parametrize("bad", [True, -1, float("nan"), float("inf"), 1.5, "10"])
def test_malformed_delta_cannot_be_reported_as_complete(bad):
    accumulator = UsageAccumulator()
    accumulator.add({"input_tokens": bad, "output_tokens": 0, "total_tokens": bad,
                     "modality_details": {"input": {"text": bad, "audio": 0}}})
    assert not accumulator.snapshot()["complete"]


def test_dynamo_decimal_usage_reconstruction():
    accumulator = UsageAccumulator()
    accumulator.add({"input_tokens": Decimal(10), "output_tokens": 0, "total_tokens": 10,
                     "modality_details": {"input": {"text": Decimal(10), "audio": Decimal(0)}}})
    assert accumulator.snapshot()["complete"]
    assert accumulator.snapshot()["input_tokens"] == 10


@pytest.mark.parametrize("cleanup", ["timeout_work_retained", "failed"])
def test_known_incomplete_sdk_cleanup_cannot_claim_full_usage(cleanup):
    from app.sonic_pricing import calculate_sonic_cost
    metrics = CallMetrics()
    metrics.record_event(event())
    metrics.termination = {"sdk_cleanup": cleanup, "telephone_completion_confirmed": True}
    usage = metrics.to_call_analytics()["usage"]
    assert usage["complete"] is False
    assert usage["coverage_reason"] == "sdk_cleanup_incomplete"
    cost = calculate_sonic_cost(usage, model_id="amazon.nova-2-5-sonic", region="us-east-1")
    assert cost["status"] == "partial" and cost["total_cost_usd"] is None


@pytest.mark.asyncio
async def test_public_provider_usage_captured_even_after_output_fence(monkeypatch):
    async def receive(self):
        yield event()
    monkeypatch.setattr(BedrockNovaSonicModel, "receive", receive)
    model = object.__new__(ObservedNovaSonicModel)
    metrics = CallMetrics()
    model.usage_sink = metrics.record_event
    model.close_submissions()
    assert [e async for e in model.receive()] == []
    assert metrics.usage["total_tokens"] == 100


@pytest.mark.asyncio
async def test_session_provider_and_output_do_not_double_count(monkeypatch):
    async def receive(self):
        yield event()
    monkeypatch.setattr(BedrockNovaSonicModel, "receive", receive)
    monkeypatch.setattr(BedrockNovaSonicModel, "__init__", lambda self, **kwargs: setattr(self, "_config", {"model_id": "fake"}))
    metrics = CallMetrics()
    session = SonicSession(send_media=lambda _: asyncio.sleep(0), metrics=metrics)
    async for received in session.agent.model.receive():
        await session._output(received)
    assert metrics.usage["event_count"] == 1
    assert metrics.usage["total_tokens"] == 100

