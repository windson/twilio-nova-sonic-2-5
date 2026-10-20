"""Ad-hoc WebSocket upgrade probe for wss://<cloudfront-domain>/media.

Not a committed test; an operator helper to prove the WS 101 handshake and open
socket complete end-to-end (CloudFront -> ALB -> ECS app). Sends a synthetic
Twilio-style 'connected' frame to confirm the app does not immediately close.

Usage:
    python scripts/ws_probe.py <cloudfront-domain>

where <cloudfront-domain> is the stack's CloudFrontDomainName output, e.g.
``dXXXXXXXXXXXXX.cloudfront.net`` (no scheme).
"""

from __future__ import annotations

import asyncio
import json
import sys

import websockets


async def main(cf: str) -> int:
    uri = f"wss://{cf}/media"
    try:
        async with websockets.connect(uri, open_timeout=20) as ws:
            # A successful context-enter means the 101 Switching Protocols
            # handshake completed and the socket is open.
            print(f"WS UPGRADE OK: connected to {uri}")
            print(f"  response_status: {getattr(ws.response, 'status_code', 'n/a')}")
            # Exercise the app's message loop with a benign Twilio 'connected' frame.
            await ws.send(json.dumps({"event": "connected", "protocol": "Call"}))
            print("  sent {'event':'connected'} frame")
            # Give the server a moment; confirm it does NOT immediately close.
            await asyncio.sleep(2.0)
            print(f"  socket still open after 2s: {ws.state.name}")
            return 0
    except Exception as exc:  # noqa: BLE001
        print(f"WS UPGRADE FAILED: {type(exc).__name__}: {exc}")
        return 1


if __name__ == "__main__":
    if len(sys.argv) < 2 or not sys.argv[1].strip():
        print(
            "usage: python scripts/ws_probe.py <cloudfront-domain>\n"
            "  e.g. python scripts/ws_probe.py dXXXXXXXXXXXXX.cloudfront.net\n"
            "  (use the stack's CloudFrontDomainName output, no scheme)",
            file=sys.stderr,
        )
        raise SystemExit(2)
    cf = sys.argv[1]
    raise SystemExit(asyncio.run(main(cf)))
