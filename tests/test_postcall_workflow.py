"""Post-disconnect admission and worker isolation; no provider/network I/O."""
from __future__ import annotations

import asyncio
import copy
import threading

import pytest

from app import main, postcall, postcall_workflow as workflow
from app.store import InMemoryStore


def _record(**overrides):
    record = {
        "call_sid": "CA_postcall_workflow", "account_id": "ACC-1001",
        "nova_model_id": "amazon.nova-2-5-sonic", "aws_region": "us-east-1",
        "termination": {"telephone_completion_confirmed": True,
                        "timestamps": {"telephone_completed": "2026-10-22T10:00:00Z"}},
        "total_e2e_ms": 1234, "usage": {}, "events_log": [], "conversation_turns": [],
    }
    record.update(overrides)
    return record


@pytest.mark.parametrize("termination", [None, {}, {"telephone_completion_confirmed": False},
                                        {"telephone_completion_confirmed": "true"}])
async def test_no_work_is_admitted_without_confirmed_telephone_completion(monkeypatch, termination):
    async def forbidden(*args, **kwargs):
        pytest.fail("unconfirmed telephone must not start background work")
    monkeypatch.setattr(workflow, "run_postcall_blocking", forbidden)
    record = _record(termination=termination)
    result = await workflow.run_postcall_workflow(record, object(), object(), extract=forbidden)
    assert result["status"] == "withheld_unconfirmed_termination"
    assert main._launch_postcall(record, object(), object()) is None


async def test_cost_is_saved_before_failed_extraction_without_changing_live_metrics(monkeypatch):
    record = _record()
    original = copy.deepcopy(record)
    store = InMemoryStore()
    store.save_call_analytics(record)
    cost = {"status": "estimated", "total_cost_usd": "0.0012"}

    def calculate(snapshot, store_arg):
        assert threading.current_thread().name.startswith("postcall")
        store_arg.update_call_analytics(snapshot["call_sid"], sonic_cost=cost)
        return cost

    async def extract(snapshot, store_arg, settings):
        assert store_arg.get_call(record["call_sid"])["sonic_cost"] == cost
        raise RuntimeError("private-provider-detail")

    monkeypatch.setattr(workflow, "_calculate_and_persist_cost", calculate)
    with pytest.raises(RuntimeError):
        await workflow.run_postcall_workflow(record, store, object(), extract=extract)
    saved = store.get_call(record["call_sid"])
    assert saved["sonic_cost"] == cost
    state = saved["postcall_workflow"]
    assert state["phase"] == "post_call" and state["status"] == "failed"
    assert state["cost"]["completed_at"] <= state["intelligence"]["started_at"]
    assert state["intelligence"]["error_type"] == "RuntimeError"
    assert "private-provider-detail" not in str(state)
    assert saved["events_log"] == [] and saved["total_e2e_ms"] == 1234
    assert record == original


async def test_failed_cost_phase_does_not_suppress_intelligence(monkeypatch):
    record = _record()
    store = InMemoryStore()
    store.save_call_analytics(record)
    phases = []

    def fail_cost(*args):
        phases.append("cost")
        raise ValueError("calculation failed")

    async def extract(*args):
        phases.append("intelligence")
        return {"intelligence_status": "ready"}

    monkeypatch.setattr(workflow, "_calculate_and_persist_cost", fail_cost)
    result = await workflow.run_postcall_workflow(record, store, object(), extract=extract)
    assert phases == ["cost", "intelligence"]
    assert result["status"] == "completed_with_errors"
    assert result["cost"]["status"] == "failed"
    assert result["intelligence"]["status"] == "ready"


async def test_cost_uses_call_bound_model_and_region_not_new_deployment_settings(monkeypatch):
    from app import sonic_pricing

    record = _record(aws_region="eu-west-1", nova_model_id="recorded-model")
    store = InMemoryStore()
    store.save_call_analytics(record)
    seen = []

    def calculate(usage, **kwargs):
        seen.append(kwargs)
        return {"status": "unavailable", "reason": "unsupported_model"}

    async def extract(*args):
        return {"intelligence_status": "ready"}

    monkeypatch.setattr(sonic_pricing, "calculate_sonic_cost", calculate)
    await workflow.run_postcall_workflow(record, store, object(), extract=extract)
    assert seen == [{"model_id": "recorded-model", "region": "eu-west-1", "usage_source": "bidi_usage"}]
    assert store.get_call(record["call_sid"])["sonic_cost"]["status"] == "unavailable"


async def test_postcall_saturation_does_not_take_live_io_executor_workers():
    entered = [threading.Event(), threading.Event()]
    release = threading.Event()

    def blocking(index):
        entered[index].set()
        assert release.wait(3)

    jobs = [asyncio.create_task(workflow.run_postcall_blocking(blocking, i)) for i in range(2)]
    try:
        for event in entered:
            assert await asyncio.to_thread(event.wait, 1)
        # Both post-call workers are occupied. Live-call I/O still has its own
        # executor, and the event loop can continue to receive/forward audio.
        assert await asyncio.wait_for(asyncio.to_thread(lambda: "live I/O"), .5) == "live I/O"
        assert await asyncio.wait_for(main.healthz(), .5) == {"status": "ok"}
        assert all(not job.done() for job in jobs)
    finally:
        release.set()
        await asyncio.gather(*jobs)


async def test_extraction_uses_dedicated_postcall_worker(monkeypatch):
    monkeypatch.setattr(postcall, "_extract_and_apply_sync",
                        lambda *args, **kwargs: {"worker": threading.current_thread().name})
    result = await postcall.extract_and_apply({}, object(), object())
    assert result["worker"].startswith("postcall")


async def test_shutdown_grace_timeout_retains_pending_work():
    release = asyncio.Event()
    task = asyncio.create_task(release.wait())
    main._POSTCALL_TASKS.add(task)
    try:
        await main._drain_postcall_tasks(.01)
        assert not task.done() and task in main._POSTCALL_TASKS
        release.set()
        await main._drain_postcall_tasks(.5)
        assert task.done() and not task.cancelled()
    finally:
        release.set()
        await task
        main._POSTCALL_TASKS.discard(task)
