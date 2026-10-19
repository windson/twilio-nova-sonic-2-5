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

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse

from app.config import get_settings
from app.sonic_bridge import SonicSession

logger = logging.getLogger(__name__)

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
                # FEAT-003 will inject tools + the collections system prompt and a
                # metrics sink here; defaults are used for FEAT-002.
                session = SonicSession(
                    send_media=send_media,
                    on_interrupt=clear_playback,
                )
                session.start()

            elif event == "media":
                if session is not None:
                    payload = message.get("media", {}).get("payload")
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
