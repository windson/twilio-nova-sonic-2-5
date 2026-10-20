"""Runtime configuration.

Reads settings from the process environment. Never prints or logs secret
values. Defaults are provided only for non-secret operational settings
(model id and AWS region).
"""

from __future__ import annotations

import os
from dataclasses import dataclass

DEFAULT_NOVA_MODEL_ID = "amazon.nova-2-5-sonic"
DEFAULT_AWS_REGION = "us-east-1"
DEFAULT_APP_LOG_GROUP = "/ecs/ar-collections-nova-sonic/app"
# TEXT model used by the off-path post-call intelligence extraction (Bedrock
# Converse). Non-secret; overridable via EXTRACTION_MODEL_ID.
DEFAULT_EXTRACTION_MODEL_ID = "amazon.nova-lite-v1:0"
# Base URL used to build the payment link embedded in the PTP SMS. Non-secret
# placeholder; overridable via PAYMENT_LINK_BASE_URL.
DEFAULT_PAYMENT_LINK_BASE_URL = "https://pay.example.com/"
# Managed AWS Bedrock AgentCore Code Interpreter identifier used (off the
# real-time path) to resolve natural-language payment dates. Non-secret;
# overridable via CODE_INTERPRETER_ID. The session runs in ``aws_region``.
DEFAULT_CODE_INTERPRETER_ID = "aws.codeinterpreter.v1"
# Twilio TRIAL accounts may only message/dial the single verified number. When
# trial mode is on (the default, matching the account this POC runs against),
# the PTP payment-link SMS is pinned to TWILIO_VERIFIED_CALLER_ID regardless of
# the debtor's real number (sending to any other destination raises). Set
# TWILIO_TRIAL_MODE=false on a full account to address the debtor's own number
# (falling back to the verified id only when the record carries no phone).
DEFAULT_TWILIO_TRIAL_MODE = True
# GA Strands OTEL telemetry (non-secret). When enabled, StrandsTelemetry is
# configured once at startup so the GA bidi agent loop auto-emits OTEL spans
# (session token usage, per-response time-to-first-audio/TTFT, tool-call and
# connection-restart spans). The default console exporter writes spans to
# stdout, which the ECS awslogs driver ships to CloudWatch Logs -- no new infra
# and no new IAM. Set OTEL_EXPORTER=otlp (and OTEL_EXPORTER_OTLP_ENDPOINT) to
# send to an OTLP collector instead, or OTEL_EXPORTER=none / OTEL_ENABLED=false
# to turn telemetry off.
DEFAULT_OTEL_ENABLED = True
DEFAULT_OTEL_EXPORTER = "console"
_VALID_OTEL_EXPORTERS = ("console", "otlp", "none")


def _env_bool(name: str, default: bool) -> bool:
    """Parse a boolean env var (true/1/yes/on -> True), else the default."""
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Settings:
    """Application settings sourced from os.environ.

    Secret-bearing fields (Twilio credentials) are read lazily and are never
    rendered by ``repr``/logging helpers in this module.
    """

    nova_model_id: str
    aws_region: str
    app_log_group: str
    extraction_model_id: str
    code_interpreter_id: str
    payment_link_base_url: str
    twilio_trial_mode: bool
    twilio_account_sid: str | None
    twilio_auth_token: str | None
    twilio_from_number: str | None
    twilio_verified_caller_id: str | None
    public_base_url: str | None
    # GA Strands OTEL telemetry knobs (non-secret).
    otel_enabled: bool
    otel_exporter: str
    otel_endpoint: str | None

    @property
    def twilio_configured(self) -> bool:
        """True when the minimum Twilio credentials are present."""
        return bool(self.twilio_account_sid and self.twilio_auth_token)

    def safe_summary(self) -> dict[str, object]:
        """Non-secret summary safe to log or render in the UI."""
        return {
            "nova_model_id": self.nova_model_id,
            "extraction_model_id": self.extraction_model_id,
            # Non-secret marker that natural-language dates are resolved by the
            # AgentCore Code Interpreter (off the real-time path).
            "code_interpreter": "agentcore",
            "code_interpreter_id": self.code_interpreter_id,
            "aws_region": self.aws_region,
            # Non-secret CloudWatch log group name so the dashboard UI can build
            # a per-call Logs Insights deep link client-side (no IAM needed).
            "app_log_group": self.app_log_group,
            "twilio_configured": self.twilio_configured,
            "twilio_trial_mode": self.twilio_trial_mode,
            "from_number_set": bool(self.twilio_from_number),
            "verified_caller_id_set": bool(self.twilio_verified_caller_id),
            "public_base_url_set": bool(self.public_base_url),
            # Non-secret markers so the dashboard/API can surface that GA Strands
            # OTEL telemetry is on and which exporter it uses (console spans land
            # in CloudWatch Logs; otlp ships to a collector).
            "otel_enabled": self.otel_enabled,
            "otel_exporter": self.otel_exporter,
            "otel_endpoint_set": bool(self.otel_endpoint),
        }


def get_settings() -> Settings:
    """Build a Settings instance from the current environment."""
    return Settings(
        nova_model_id=os.environ.get("NOVA_MODEL_ID", DEFAULT_NOVA_MODEL_ID),
        aws_region=os.environ.get(
            "AWS_REGION", os.environ.get("AWS_DEFAULT_REGION", DEFAULT_AWS_REGION)
        ),
        app_log_group=os.environ.get("APP_LOG_GROUP", DEFAULT_APP_LOG_GROUP),
        extraction_model_id=os.environ.get(
            "EXTRACTION_MODEL_ID", DEFAULT_EXTRACTION_MODEL_ID
        ),
        code_interpreter_id=os.environ.get(
            "CODE_INTERPRETER_ID", DEFAULT_CODE_INTERPRETER_ID
        ),
        payment_link_base_url=os.environ.get(
            "PAYMENT_LINK_BASE_URL", DEFAULT_PAYMENT_LINK_BASE_URL
        ),
        twilio_trial_mode=_env_bool("TWILIO_TRIAL_MODE", DEFAULT_TWILIO_TRIAL_MODE),
        twilio_account_sid=os.environ.get("TWILIO_ACCOUNT_SID") or None,
        twilio_auth_token=os.environ.get("TWILIO_AUTH_TOKEN") or None,
        twilio_from_number=os.environ.get("TWILIO_FROM_NUMBER") or None,
        twilio_verified_caller_id=os.environ.get("TWILIO_VERIFIED_CALLER_ID") or None,
        public_base_url=os.environ.get("PUBLIC_BASE_URL") or None,
        otel_enabled=_env_bool("OTEL_ENABLED", DEFAULT_OTEL_ENABLED),
        otel_exporter=_otel_exporter(),
        otel_endpoint=os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT") or None,
    )


def _otel_exporter() -> str:
    """Parse OTEL_EXPORTER, falling back to the default for unknown values."""
    raw = (os.environ.get("OTEL_EXPORTER") or DEFAULT_OTEL_EXPORTER).strip().lower()
    return raw if raw in _VALID_OTEL_EXPORTERS else DEFAULT_OTEL_EXPORTER
