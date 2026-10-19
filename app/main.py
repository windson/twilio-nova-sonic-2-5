"""FastAPI application entrypoint.

FEAT-001 establishes a runnable skeleton with a health endpoint and a
placeholder landing page. Later features add the Twilio Media Streams
WebSocket, the Nova Sonic bridge, metrics APIs, and the analytics UI, and
mount ``web/`` as static files.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse

from app.config import get_settings

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
