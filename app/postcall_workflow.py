"""A fixed workflow admitted only after confirmed telephone disconnection.

Strands' Workflow pattern fits these known dependencies: calculate Sonic cost,
then run the Luna specialist's extraction and follow-up work. No LLM needs to
choose a route or perform pricing arithmetic. Neither specialist is exposed as
a Bidi tool. This is an application workflow, not a Graph/Swarm invocation.
Pattern: https://strandsagents.com/docs/user-guide/sdk/multi-agent/multi-agent-patterns/

The dedicated executor bounds blocking post-call work to two workers and keeps
it out of the default executor used for live call storage I/O. It still shares
process CPU and memory; an in-process task is not a durable queue. The caller
retains the workflow task and observes its completion.
"""

from __future__ import annotations

import asyncio
import contextvars
import functools
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Mapping, TypeVar

logger = logging.getLogger(__name__)
_T = TypeVar("_T")
_POSTCALL_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="postcall")


async def run_postcall_blocking(function: Callable[..., _T], *args: Any, **kwargs: Any) -> _T:
    """Offload blocking post-call I/O without consuming a live-call worker.

    As with asyncio.to_thread, cancellation of the awaiting coroutine cannot
    undo an SDK request already running in a thread.
    """
    context = contextvars.copy_context()
    invoke = functools.partial(function, *args, **kwargs)
    return await asyncio.get_running_loop().run_in_executor(
        _POSTCALL_EXECUTOR, context.run, invoke,
    )


def telephone_completion_confirmed(record: Mapping[str, Any]) -> bool:
    termination = record.get("termination")
    return isinstance(termination, Mapping) and termination.get("telephone_completion_confirmed") is True


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _persist_workflow(store: Any, call_id: str, state: Mapping[str, Any]) -> None:
    try:
        store.update_call_analytics(call_id, postcall_workflow=dict(state))
    except Exception as exc:
        logger.warning("post-call workflow status persistence failed (%s)", type(exc).__name__)


def _calculate_and_persist_cost(record: Mapping[str, Any], store: Any) -> dict[str, Any]:
    from app.sonic_pricing import calculate_sonic_cost

    # Use call-bound model/region, never a possibly changed deployment default.
    # Missing historical metadata must be recovered by the cost-only backfill.
    cost = calculate_sonic_cost(
        record.get("usage") or {},
        model_id=record.get("nova_model_id") or "",
        region=record.get("aws_region") or "",
        usage_source="bidi_usage",
    )
    store.update_call_analytics(str(record["call_sid"]), sonic_cost=cost)
    return cost


async def run_postcall_workflow(
    record: Mapping[str, Any],
    store: Any,
    settings: Any,
    *,
    extract: Callable[..., Awaitable[dict[str, Any]]],
) -> dict[str, Any]:
    """Cost and analyze a disconnected call, with independent phase outcomes.

    The gate precedes all inference, pricing, and storage work. A failed cost
    write does not suppress intelligence; a failed extraction cannot erase or
    suppress the cost saved first. Historical costing must call its dedicated
    backfill, never replay this workflow's follow-up side effects.
    """
    if not telephone_completion_confirmed(record):
        return {"phase": "post_call", "status": "withheld_unconfirmed_termination"}
    call_id = record.get("call_sid")
    if not isinstance(call_id, str) or not call_id:
        return {"phase": "post_call", "status": "withheld_missing_call_id"}

    state: dict[str, Any] = {
        "phase": "post_call", "pattern": "fixed_workflow", "status": "running",
        "started_at": _utc(), "cost": {"status": "running", "started_at": _utc()},
    }
    await run_postcall_blocking(_persist_workflow, store, call_id, state)
    try:
        cost = await run_postcall_blocking(_calculate_and_persist_cost, record, store)
        state["cost"].update(status=cost.get("status", "unknown"), completed_at=_utc())
    except Exception as exc:
        state["cost"].update(status="failed", completed_at=_utc(), error_type=type(exc).__name__)
        logger.warning("post-call Sonic costing failed (%s)", type(exc).__name__)

    state["intelligence"] = {"status": "running", "started_at": _utc()}
    await run_postcall_blocking(_persist_workflow, store, call_id, state)
    try:
        result = await extract(record, store, settings)
        state["intelligence"].update(
            status=result.get("intelligence_status", "unknown"), completed_at=_utc(),
        )
    except Exception as exc:
        state["intelligence"].update(status="failed", completed_at=_utc(), error_type=type(exc).__name__)
        state.update(status="failed", completed_at=_utc())
        await run_postcall_blocking(_persist_workflow, store, call_id, state)
        raise
    state.update(
        status="completed" if state["cost"]["status"] != "failed"
        and state["intelligence"]["status"] != "failed" else "completed_with_errors",
        completed_at=_utc(),
    )
    await run_postcall_blocking(_persist_workflow, store, call_id, state)
    return state
