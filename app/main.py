"""FastAPI application entrypoint.

Serves the Twilio Media Streams WebSocket, call and analytics APIs, health
endpoint, and the React dashboard from ``web/``. Coordinates the Nova Sonic
session, call lifecycle, persistence, and post-call workflow.
"""

from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import asyncio

from app.call_lifecycle import CallLifecycleController, TerminationBounds, retain
from app.collections_agent import CallToolRuntime, build_tools, get_system_prompt
from app.config import get_settings
from app.dashboard import call_dashboard_url
from app.metrics import CallMetrics, configure_json_logging
from app.postcall import extract_and_apply
from app.postcall_workflow import run_postcall_workflow, telephone_completion_confirmed
from app.sonic_bridge import DEFAULT_KICKOFF_TEXT, SonicSession
from app.store import get_default_store
from app.telemetry import setup_telemetry
from app.twilio_client import hangup_call_async, place_outbound_call

logger = logging.getLogger(__name__)

# Emit CloudWatch-friendly JSON-line logs for call lifecycle/metrics events.
configure_json_logging()

# Configure GA Strands OTEL telemetry ONCE at import/startup, BEFORE any
# BidiAgent.run can execute (the agent loop only auto-emits its OTEL spans --
# session token usage, per-response TTFT, tool-call / connection-restart spans
# -- when a tracer provider is already configured). This is a one-time, guarded,
# idempotent setup: it never raises and never runs per call, so it cannot add
# latency to or crash the /media WebSocket audio path.
setup_telemetry()

@asynccontextmanager
async def _app_lifespan(application: FastAPI):
    try:
        yield
    finally:
        await _drain_postcall_tasks()


app = FastAPI(
    title="AR Collections Nova Sonic 2.5 Agent",
    version="0.1.0",
    lifespan=_app_lifespan,
    description=(
        "AI-first-initiated outbound Accounts Receivable Collections voice "
        "agent bridging Twilio Media Streams to Amazon Bedrock Nova Sonic 2.5."
    ),
)

_WEB_DIR = Path(__file__).resolve().parent.parent / "web"

_PLACEHOLDER_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>AR Collections Nova Sonic 2.5 Agent</title>
</head>
<body>
  <h1>AR Collections Nova Sonic 2.5 Agent</h1>
  <p>Service is running. Analytics UI is added in a later feature.</p>
  <p>Health: <a href="/healthz">/healthz</a></p>
</body>
</html>
"""


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    """Liveness/readiness probe."""
    return {"status": "ok"}


@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    """Serve the landing page (web/index.html when present, else placeholder)."""
    index_file = _WEB_DIR / "index.html"
    if index_file.is_file():
        return HTMLResponse(index_file.read_text(encoding="utf-8"))
    return HTMLResponse(_PLACEHOLDER_HTML)


@app.get("/api/config")
async def config_summary() -> dict[str, object]:
    """Return a non-secret summary of the active configuration."""
    return get_settings().safe_summary()


# --- REST API: due records, call initiation, analytics -----------------------


class PlaceCallRequest(BaseModel):
    """Body for POST /api/calls."""

    account_id: str


# Strong references to in-flight fire-and-forget post-call extraction tasks.
# asyncio only keeps a WEAK reference to a bare ``create_task`` result, so a
# task held solely by a local variable can be garbage-collected mid-flight
# before it completes (and thus never run the PTP/Dispute triggers). We keep
# each task in this module-level set for the duration of its life and discard
# it in the done-callback. See https://docs.python.org/3/library/asyncio-task.html
# ("Important: Save a reference to the result of this function").
_POSTCALL_TASKS: "set[asyncio.Task[Any]]" = set()


async def _drain_postcall_tasks(timeout_s: float = 20.0) -> None:
    """Give retained workflows a bounded shutdown grace period.

    A timeout does not cancel or claim to roll back an already-running worker.
    This process-local grace period is not durable delivery across task loss.
    """
    pending = {task for task in _POSTCALL_TASKS if not task.done()}
    if pending:
        _, pending = await asyncio.wait(pending, timeout=timeout_s)
        if pending:
            logger.warning("shutdown with %s post-call workflows still pending", len(pending))


def _log_postcall_task_result(task: "asyncio.Task[Any]") -> None:
    """Done-callback for the fire-and-forget post-call extraction task.

    :func:`app.postcall.extract_and_apply` never raises, but we still retrieve
    the task result so a cancellation/unexpected error is logged rather than
    swallowed as a "task exception was never retrieved" warning. We also discard
    the task from :data:`_POSTCALL_TASKS` here so the strong reference that kept
    it alive is released once it has finished.
    """
    _POSTCALL_TASKS.discard(task)
    try:
        task.result()
    except asyncio.CancelledError:  # pragma: no cover - teardown race
        pass
    except Exception as exc:  # defensive: no SDK exception payloads in logs
        logger.warning("post-call intelligence task failed (%s)", type(exc).__name__)


def _launch_postcall(
    record: dict[str, Any],
    store: Any,
    settings: Any,
) -> "asyncio.Task[Any] | None":
    """Launch the gated post-call workflow as a retained background task.

    The controller invokes this after confirmed telephone completion and
    WebSocket cleanup. Check the stored proof here too, before admitting any
    pricing or intelligence work. Blocking work uses a separate bounded worker
    pool, keeping it out of live call I/O workers. Process resources are still
    shared. The done-callback observes errors and releases the task reference.

    Returns the created task (for testing) or ``None`` if scheduling failed.
    """
    if not telephone_completion_confirmed(record):
        return None
    try:
        task = asyncio.create_task(
            run_postcall_workflow(record, store, settings, extract=extract_and_apply),
            name="post-call-workflow",
        )
    except Exception as exc:  # scheduling must not crash teardown
        logger.warning("failed to launch post-call intelligence (%s)", type(exc).__name__)
        return None
    _POSTCALL_TASKS.add(task)
    task.add_done_callback(_log_postcall_task_result)
    return task


async def _persist_analytics(
    metrics: "CallMetrics | None",
    store: Any,
    stream_sid: str | None,
    *,
    telephone_confirmed: bool = True,
) -> None:
    """Await one shared persistence operation without blocking other calls.

    Both graceful hangup and WebSocket cleanup await the same task. A cancelled
    awaiter does not cancel the SDK write or launch duplicate post-call work.
    Metrics must be finalized and tool workers drained before taking a snapshot.
    """
    if metrics is None:
        return
    task = getattr(metrics, "_persistence_task", None)
    if task is None:
        snapshot = metrics.to_call_analytics()
        if not telephone_confirmed:
            snapshot["intelligence_status"] = "withheld_unconfirmed_termination"

        async def persist() -> None:
            try:
                saved_record = await asyncio.to_thread(store.save_call_analytics, snapshot)
            except Exception as exc:
                logger.warning("stream_sid=<%s> | analytics persistence failed (%s)",
                               stream_sid, type(exc).__name__)
                return
            if telephone_confirmed:
                _launch_postcall(saved_record, store, get_settings())

        task = asyncio.create_task(persist(), name="persist-call-analytics")
        metrics._persistence_task = task
    await asyncio.shield(task)


def _dashboard_url(call_sid: str | None) -> str | None:
    """Build the per-call CloudWatch dashboard deep-link from config.

    Pure string construction (no AWS call): opens the CONSOLIDATED per-call
    CloudWatch dashboard (``<stack>-per-call``, provisioned in infra) with its
    multi-widget view scoped to ``call_sid`` -- per-turn timeline, TTFT + turns,
    token usage, barge-ins, tool calls, Twilio/transport. ``None`` when the
    record has no call_sid. The stack name is derived from the app log group.
    """
    if not call_sid:
        return None
    settings = get_settings()
    return call_dashboard_url(
        call_sid,
        aws_region=settings.aws_region,
        app_log_group=settings.app_log_group,
    )


def _call_summary(record: dict[str, Any]) -> dict[str, Any]:
    """Project a CallAnalytics record to a compact list summary."""
    return {
        "call_sid": record.get("call_sid"),
        "account_id": record.get("account_id"),
        # Absolute wall-clock start time so the calls-under-dues view can
        # Render WHEN each call happened (absolute + relative) via a
        # client-side join on account_id against this list endpoint.
        "created_at": record.get("created_at"),
        "total_e2e_ms": record.get("total_e2e_ms"),
        "nova_total_ms": record.get("nova_total_ms"),
        # Transport estimate and its backward-compatible alias.
        "transport_delay_estimate_ms": record.get("transport_delay_estimate_ms"),
        "twilio_latency_ms": record.get("twilio_latency_ms"),
        "ttft_ms": record.get("ttft_ms"),
        "ttfa_ms": record.get("ttfa_ms", record.get("ttft_ms")),
        "text_ttft_ms": record.get("text_ttft_ms"),
        "turn_count": record.get("turn_count"),
        "nova_response_count": record.get("nova_response_count"),
        "barge_in_count": record.get("barge_in_count"),
        # Post-call intelligence: surfaced in the list so the UI can
        # show the disposition tag and whether extraction is still pending.
        "disposition": record.get("disposition"),
        "intelligence_status": record.get("intelligence_status"),
        # Each row links to the consolidated per-call CloudWatch dashboard.
        "dashboard_url": _dashboard_url(record.get("call_sid")),
    }


@app.get("/api/dues")
async def list_dues() -> dict[str, Any]:
    """List all due/account records."""
    return {"dues": await asyncio.to_thread(get_default_store().list_dues)}


@app.post("/api/calls")
async def create_call(body: PlaceCallRequest) -> dict[str, Any]:
    """Initiate an AI-first outbound collections call for a due record.

    Looks up the due record, then places an outbound call via
    :func:`app.twilio_client.place_outbound_call`. For a Twilio trial/verified
    setup the callee is ``TWILIO_VERIFIED_CALLER_ID``.

    Returns a clear 4xx/503 JSON error (not a 500 traceback) when the account
    is unknown or when Twilio credentials / ``PUBLIC_BASE_URL`` are missing.
    """
    store = get_default_store()
    settings = get_settings()

    due_record = await asyncio.to_thread(store.get_due, body.account_id)
    if due_record is None:
        raise HTTPException(status_code=404, detail=f"Unknown account_id: {body.account_id}")

    # Pre-flight config checks so we can return a clear, actionable error.
    missing: list[str] = []
    if not settings.twilio_configured:
        missing.append("TWILIO_ACCOUNT_SID/TWILIO_AUTH_TOKEN")
    if not settings.twilio_from_number:
        missing.append("TWILIO_FROM_NUMBER")
    if not settings.public_base_url:
        missing.append("PUBLIC_BASE_URL")
    callee = settings.twilio_verified_caller_id
    if not callee:
        missing.append("TWILIO_VERIFIED_CALLER_ID")
    if missing:
        raise HTTPException(
            status_code=503,
            detail={
                "error": "twilio_not_configured",
                "message": (
                    "Outbound calling is not configured. Set the following "
                    "environment variables and restart: " + ", ".join(missing) + ". "
                    "PUBLIC_BASE_URL must point at a publicly reachable host/tunnel "
                    "so Twilio can open the /media WebSocket."
                ),
                "missing": missing,
            },
        )

    try:
        result = await asyncio.to_thread(place_outbound_call, callee, due_record, settings=settings)
    except ValueError as exc:
        # SDK errors can echo credentials or URLs. Configuration checks above
        # already identify missing fields without exposing their values.
        logger.warning(
            "account_id=<%s> error_type=<%s> | outbound call configuration failed",
            body.account_id,
            type(exc).__name__,
        )
        raise HTTPException(
            status_code=503,
            detail="Outbound calling configuration is invalid.",
        ) from exc
    except Exception as exc:  # noqa: BLE001 - surface as 502, not an unhandled 500
        logger.warning(
            "account_id=<%s> error_type=<%s> | failed to place outbound call",
            body.account_id,
            type(exc).__name__,
        )
        raise HTTPException(
            status_code=502,
            detail={"error": "twilio_call_failed", "message": "Unable to place outbound call."},
        ) from exc

    return {
        "call_sid": result.call_sid,
        "session_id": result.call_sid,
        "account_id": result.account_id,
        "to": result.to,
        "from": result.from_number,
    }


@app.get("/api/calls")
async def list_calls() -> dict[str, Any]:
    """List post-call analytics summaries (most recent flavor last)."""
    records = await asyncio.to_thread(get_default_store().list_calls)
    calls = [_call_summary(r) for r in records]
    return {"calls": calls}


@app.get("/api/calls/{call_id}")
async def get_call(call_id: str) -> dict[str, Any]:
    """Return the full CallAnalytics record (turns, transcripts, events_log)."""
    record = await asyncio.to_thread(get_default_store().get_call, call_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"Unknown call_id: {call_id}")
    # Attach the per-call CloudWatch dashboard deep-link (config-built,
    # no live AWS call) so the detail view can open the consolidated call-scoped
    # dashboard. Non-destructive: we return a shallow copy with the field added.
    enriched = dict(record)
    enriched["dashboard_url"] = _dashboard_url(record.get("call_sid"))
    return enriched


@app.websocket("/media")
async def media_stream(websocket: WebSocket) -> None:
    """Bind a Twilio stream to one supervised telephone lifecycle."""
    await websocket.accept()
    controller: CallLifecycleController | None = None
    store = get_default_store()
    settings = get_settings()

    async def close_websocket() -> None:
        try:
            await websocket.close()
        except (RuntimeError, WebSocketDisconnect):
            pass  # The peer may have already closed the transport.

    try:
        # A protocol/health probe may connect and leave without starting a call.
        # Do not construct a Nova session or make a Twilio request in that case.
        while True:
            message = json.loads(await websocket.receive_text())
            if message.get("event") == "stop":
                return
            if message.get("event") == "start":
                break
        start = message.get("start") or {}
        stream_sid = message.get("streamSid") or start.get("streamSid")
        call_sid = start.get("callSid")
        if not isinstance(stream_sid, str) or not isinstance(call_sid, str):
            raise ValueError("Twilio start requires stream and call identifiers")
        account_id = (start.get("customParameters") or {}).get("accountId")
        logger.info("stream_sid=<%s>, call_sid=<%s> | twilio stream start", stream_sid, call_sid)
        metrics = CallMetrics(call_sid=call_sid, account_id=account_id,
                              nova_model_id=settings.nova_model_id, aws_region=settings.aws_region)
        runtime = CallToolRuntime(event_sink=metrics.record_tool_execution,
            capture_sink=metrics.capture_payment_date_phrase,
            payment_timezone=settings.payment_timezone, call_sid=call_sid)
        bounds = TerminationBounds()

        async def send_media(payload: str) -> None:
            await websocket.send_text(json.dumps({"event": "media", "streamSid": stream_sid,
                                                  "media": {"payload": payload}}))

        async def send_mark(name: str) -> None:
            await websocket.send_text(json.dumps({"event": "mark", "streamSid": stream_sid,
                                                  "mark": {"name": name}}))

        async def clear_playback() -> None:
            await websocket.send_text(json.dumps({"event": "clear", "streamSid": stream_sid}))

        async def hangup() -> dict[str, Any]:
            return await hangup_call_async(call_sid, settings=settings, request_timeout_s=bounds.twilio_request)

        async def persist(confirmed: bool) -> None:
            await _persist_analytics(metrics, store, stream_sid, telephone_confirmed=confirmed)

        controller = CallLifecycleController(call_sid=call_sid, stream_sid=stream_sid,
            runtime=runtime, metrics=metrics, hangup=hangup, persist=persist,
            close_websocket=close_websocket, bounds=bounds,
            closing_greeting=settings.closing_greeting, closing_phrase=settings.closing_phrase)
        # Initialize the lifecycle before optional store/model setup so failures
        # cannot leave a connected telephone without an owner.
        due_record = None
        if account_id:
            lookup = retain(asyncio.create_task(asyncio.to_thread(store.get_due, account_id), name="call-due-lookup"))
            due_record = await asyncio.wait_for(asyncio.shield(lookup), bounds.tools)
        system_prompt = get_system_prompt(due_record or {}, closing_greeting=settings.closing_greeting)
        session = SonicSession(send_media=send_media, on_interrupt=clear_playback, send_mark=send_mark,
            tools=build_tools(store, call_sid=call_sid, runtime=runtime), system_prompt=system_prompt,
            metrics=metrics, settings=settings, lifecycle=controller, kickoff_text=DEFAULT_KICKOFF_TEXT)
        controller.bind_session(session)

        async def reader() -> None:
            try:
                while True:
                    incoming = json.loads(await websocket.receive_text())
                    event = incoming.get("event")
                    if event in {"media", "mark", "stop"} and incoming.get("streamSid") != stream_sid:
                        continue
                    if event == "media":
                        media = incoming.get("media") or {}
                        metrics.record_inbound_media(media.get("timestamp"))
                        if media.get("payload"):
                            await session.feed_twilio_audio(media["payload"])
                    elif event == "mark":
                        name = (incoming.get("mark") or {}).get("name")
                        if isinstance(name, str):
                            controller.acknowledge_playback(name, incoming.get("streamSid"))
                    elif event == "stop":
                        return
                    elif event == "start":
                        raise ValueError("A media connection cannot replace its call identity")
            except WebSocketDisconnect:
                return
            finally:
                # Signal only. The supervising controller owns all awaited work.
                controller.transport_closed()

        await controller.run(reader())
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        logger.warning("media stream error (%s)", type(exc).__name__)
    finally:
        if controller is not None:
            await asyncio.shield(controller.start_close(reason="media_handler_exit", outcome="technical_failure"))
        else:
            await close_websocket()


# Mount the compiled React/shadcn dashboard bundle (built from frontend/ into
# web/ at image-build time: web/index.html + web/assets/*) as static files.
# GET / above serves web/index.html when present (the real dashboard), falling
# back to the placeholder only if web/ has not been built yet.
if _WEB_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=str(_WEB_DIR)), name="static")
