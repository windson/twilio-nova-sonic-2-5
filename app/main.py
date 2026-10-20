"""FastAPI application entrypoint.

FEAT-001 establishes a runnable skeleton with a health endpoint and a
placeholder landing page. Later features add the Twilio Media Streams
WebSocket, the Nova Sonic bridge, metrics APIs, and the analytics UI, and
mount ``web/`` as static files.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import asyncio

from app.collections_agent import build_tools, get_system_prompt
from app.config import get_settings
from app.metrics import CallMetrics, configure_json_logging
from app.postcall import extract_and_apply
from app.sonic_bridge import DEFAULT_KICKOFF_TEXT, SonicSession
from app.store import get_default_store
from app.twilio_client import place_outbound_call

logger = logging.getLogger(__name__)

# Emit CloudWatch-friendly JSON-line logs for call lifecycle/metrics events.
configure_json_logging()

app = FastAPI(
    title="AR Collections Nova Sonic 2.5 Agent",
    version="0.1.0",
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


def _log_postcall_task_result(task: "asyncio.Task[Any]") -> None:
    """Done-callback for the fire-and-forget post-call extraction task.

    :func:`app.postcall.extract_and_apply` never raises, but we still retrieve
    the task result so a cancellation/unexpected error is logged rather than
    swallowed as a "task exception was never retrieved" warning.
    """
    try:
        task.result()
    except asyncio.CancelledError:  # pragma: no cover - teardown race
        pass
    except Exception:  # noqa: BLE001 - defensive; extract_and_apply guards itself
        logger.exception("post-call intelligence task raised")


def _call_summary(record: dict[str, Any]) -> dict[str, Any]:
    """Project a CallAnalytics record to a compact list summary."""
    return {
        "call_sid": record.get("call_sid"),
        "account_id": record.get("account_id"),
        "total_e2e_ms": record.get("total_e2e_ms"),
        "nova_total_ms": record.get("nova_total_ms"),
        "twilio_latency_ms": record.get("twilio_latency_ms"),
        "ttft_ms": record.get("ttft_ms"),
        "turn_count": record.get("turn_count"),
        "nova_response_count": record.get("nova_response_count"),
        "barge_in_count": record.get("barge_in_count"),
        # Post-call intelligence (FEAT-002): surfaced in the list so the UI can
        # show the disposition tag and whether extraction is still pending.
        "disposition": record.get("disposition"),
        "intelligence_status": record.get("intelligence_status"),
    }


@app.get("/api/dues")
async def list_dues() -> dict[str, Any]:
    """List all due/account records."""
    return {"dues": get_default_store().list_dues()}


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

    due_record = store.get_due(body.account_id)
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
        result = place_outbound_call(callee, due_record, settings=settings)
    except ValueError as exc:
        # Defensive: config validation inside the client.
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - surface as 502, not an unhandled 500
        logger.exception("account_id=<%s> | failed to place outbound call", body.account_id)
        raise HTTPException(
            status_code=502,
            detail={"error": "twilio_call_failed", "message": str(exc)},
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
    calls = [_call_summary(r) for r in get_default_store().list_calls()]
    return {"calls": calls}


@app.get("/api/calls/{call_id}")
async def get_call(call_id: str) -> dict[str, Any]:
    """Return the full CallAnalytics record (turns, transcripts, events_log)."""
    record = get_default_store().get_call(call_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"Unknown call_id: {call_id}")
    return record


@app.websocket("/media")
async def media_stream(websocket: WebSocket) -> None:
    """Twilio Media Streams WebSocket <-> Nova Sonic bi-di bridge.

    Twilio opens this socket from the TwiML ``<Connect><Stream>`` of an outbound
    call. Message lifecycle (per Twilio Media Streams):

    - ``connected``: protocol handshake (logged).
    - ``start``: carries ``streamSid``, ``callSid``, ``mediaFormat`` and
      ``start.customParameters`` (our ``accountId``). We create a per-call
      :class:`SonicSession` and start the agent run.
    - ``media``: base64 u-law payload -> transcode -> enqueue to Nova.
    - ``mark``: playback marker (logged).
    - ``stop``: finalize + tear down.

    Outbound Nova audio is sent back as ``{"event":"media","streamSid":...,
    "media":{"payload":<base64 ulaw>}}``; on barge-in we send
    ``{"event":"clear","streamSid":...}`` to flush queued playback.
    """
    await websocket.accept()

    stream_sid: str | None = None
    session: SonicSession | None = None
    metrics: CallMetrics | None = None
    store = get_default_store()

    async def send_media(payload: str) -> None:
        if stream_sid is None:
            return
        await websocket.send_text(
            json.dumps({"event": "media", "streamSid": stream_sid, "media": {"payload": payload}})
        )

    async def clear_playback() -> None:
        if stream_sid is None:
            return
        await websocket.send_text(json.dumps({"event": "clear", "streamSid": stream_sid}))

    try:
        while True:
            raw = await websocket.receive_text()
            message = json.loads(raw)
            event = message.get("event")

            if event == "connected":
                logger.info("twilio media stream connected")

            elif event == "start":
                start = message.get("start", {})
                stream_sid = message.get("streamSid") or start.get("streamSid")
                call_sid = start.get("callSid")
                custom = start.get("customParameters", {}) or {}
                account_id = custom.get("accountId")
                logger.info(
                    "stream_sid=<%s>, call_sid=<%s>, account_id=<%s> | twilio stream start",
                    stream_sid,
                    call_sid,
                    account_id,
                )
                # Inject the collections brain: system prompt built from the due
                # record, the update-due tool bound to the store, and a per-call
                # metrics collector tagged to this call/account.
                due_record = store.get_due(account_id) if account_id else None
                system_prompt = get_system_prompt(due_record) if due_record else None
                tools = build_tools(store)
                metrics = CallMetrics(call_sid=call_sid, account_id=account_id)

                session = SonicSession(
                    send_media=send_media,
                    on_interrupt=clear_playback,
                    tools=tools,
                    system_prompt=system_prompt,
                    metrics=metrics,
                    # AI-first: prime Nova to open the conversation (greet the
                    # debtor and state the amount due) without waiting for the
                    # caller to speak. Uses the default kickoff instruction.
                    kickoff_text=DEFAULT_KICKOFF_TEXT,
                )
                session.start()

            elif event == "media":
                if session is not None:
                    media = message.get("media", {}) or {}
                    payload = media.get("payload")
                    if metrics is not None:
                        metrics.record_inbound_media(media.get("timestamp"))
                    if payload:
                        await session.feed_twilio_audio(payload)

            elif event == "mark":
                logger.debug("mark=<%s> | twilio mark", message.get("mark"))

            elif event == "stop":
                logger.info("stream_sid=<%s> | twilio stream stop", stream_sid)
                break

    except WebSocketDisconnect:
        logger.info("stream_sid=<%s> | twilio websocket disconnected", stream_sid)
    except Exception:  # noqa: BLE001 - ensure teardown on any error
        logger.exception("stream_sid=<%s> | media stream error", stream_sid)
    finally:
        if session is not None:
            await session.close()
        # Persist post-call analytics so the UI/API (FEAT-004) can read history.
        if metrics is not None:
            saved_record: dict[str, Any] | None = None
            try:
                saved_record = store.save_call_analytics(metrics.to_call_analytics())
            except Exception:  # noqa: BLE001 - persistence must not crash teardown
                logger.exception("stream_sid=<%s> | failed to persist analytics", stream_sid)

            # Launch the post-call intelligence extraction as a FIRE-AND-FORGET
            # asyncio task AFTER the call ended and analytics were saved. It
            # runs entirely off the real-time audio/turn path, so it cannot add
            # to call latency or to the metrics already frozen above. It is
            # guarded so a missing config / scheduling issue never raises into
            # the websocket teardown.
            if saved_record is not None:
                try:
                    settings = get_settings()
                    task = asyncio.create_task(
                        extract_and_apply(saved_record, store, settings)
                    )
                    # Avoid "task was never retrieved" warnings / silent errors.
                    task.add_done_callback(_log_postcall_task_result)
                except Exception:  # noqa: BLE001 - scheduling must not crash teardown
                    logger.exception(
                        "stream_sid=<%s> | failed to launch post-call intelligence",
                        stream_sid,
                    )


# Mount the vanilla-JS dashboard assets (web/app.js, web/styles.css) as static
# files. GET / above serves web/index.html when present (the real dashboard),
# falling back to the placeholder only if web/ has not been built yet.
if _WEB_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=str(_WEB_DIR)), name="static")
