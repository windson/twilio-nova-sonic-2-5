"""GA Strands OpenTelemetry (OTEL) telemetry wiring.

The GA ``strands.bidi`` agent loop auto-emits OTEL spans for each bidi session,
model response, tool call and connection restart once a tracer provider /
exporter is configured -- but ONLY if ``StrandsTelemetry`` is configured BEFORE
``BidiAgent.run`` is ever called. This module does that once, at FastAPI
startup, so every ``/media`` call that follows benefits without touching the
real-time audio path.

What the GA spans give us (verified against the installed ``strands`` package
``strands/bidi/_telemetry.py``):

- ``bidi_session`` span: ``gen_ai.usage.input_tokens`` /
  ``gen_ai.usage.output_tokens`` / ``gen_ai.usage.total_tokens`` /
  ``gen_ai.usage.cache_read.input_tokens`` -- the session's reported token
  usage, authoritatively, straight from the model.
- ``bidi_response`` span: ``gen_ai.server.time_to_first_audio`` -- native TTFT
  (ms from response start to the first audio chunk).
- ``bidi_connection_restart`` span and ``bidi_barge_in`` span events.

Design constraints (see FEAT-003):

- Configure EXACTLY ONCE and be idempotent (safe to call repeatedly; only the
  first call wires exporters).
- NEVER raise: a telemetry setup failure must not crash app startup nor affect
  the ``/media`` WebSocket audio path. All failures are swallowed and logged.
- Default to the CONSOLE exporter: spans are written to stdout, which the ECS
  ``awslogs`` driver already ships to CloudWatch Logs -- so session token usage
  and per-response TTFT become visible with NO new infrastructure and NO new
  IAM. An OTLP collector can be opted into via config when one is available.
"""

from __future__ import annotations

import logging
import threading

from app.config import Settings, get_settings

logger = logging.getLogger(__name__)

# Guards one-time, idempotent setup. ``_configured`` flips to True after the
# first (successful or not) attempt so we never double-register exporters.
_lock = threading.Lock()
_configured = False


def _reset_for_tests() -> None:
    """Reset the one-time guard (test-only helper)."""
    global _configured
    with _lock:
        _configured = False


def setup_telemetry(settings: Settings | None = None) -> bool:
    """Configure GA Strands OTEL telemetry once, before any ``BidiAgent.run``.

    Returns ``True`` if an exporter was wired on this call, ``False`` otherwise
    (telemetry disabled, already configured, exporter ``none``, or a swallowed
    failure). Never raises.

    The selected exporter is driven by non-secret config (:class:`Settings`):

    - ``otel_enabled`` False or ``otel_exporter == "none"`` -> no-op.
    - ``otel_exporter == "console"`` (default) -> spans to stdout -> CloudWatch
      Logs via the existing awslogs driver (no new IAM/infra).
    - ``otel_exporter == "otlp"`` -> OTLP exporter. The endpoint/headers are
      taken from the standard ``OTEL_EXPORTER_OTLP_*`` environment variables by
      the OpenTelemetry SDK itself.
    """
    global _configured

    settings = settings or get_settings()

    with _lock:
        if _configured:
            return False
        # Mark configured up-front so that even if setup throws, we do not retry
        # on every request and we remain strictly idempotent.
        _configured = True

        if not settings.otel_enabled or settings.otel_exporter == "none":
            logger.info(
                "otel_enabled=<%s> otel_exporter=<%s> | OTEL telemetry disabled",
                settings.otel_enabled,
                settings.otel_exporter,
            )
            return False

        try:
            # Imported lazily so a telemetry/import problem cannot break module
            # import or app startup.
            from strands.telemetry import StrandsTelemetry

            telemetry = StrandsTelemetry()
            if settings.otel_exporter == "otlp":
                telemetry.setup_otlp_exporter()
                logger.info(
                    "endpoint_set=<%s> | GA Strands OTEL telemetry configured (otlp)",
                    bool(settings.otel_endpoint),
                )
            else:
                # Default / console: spans -> stdout -> CloudWatch Logs.
                telemetry.setup_console_exporter()
                logger.info("GA Strands OTEL telemetry configured (console)")
            return True
        except Exception:  # noqa: BLE001 - telemetry must never break startup
            logger.exception("failed to configure GA Strands OTEL telemetry")
            return False
