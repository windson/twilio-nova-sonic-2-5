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


@dataclass(frozen=True)
class Settings:
    """Application settings sourced from os.environ.

    Secret-bearing fields (Twilio credentials) are read lazily and are never
    rendered by ``repr``/logging helpers in this module.
    """

    nova_model_id: str
    aws_region: str
    app_log_group: str
    twilio_account_sid: str | None
    twilio_auth_token: str | None
    twilio_from_number: str | None
    twilio_verified_caller_id: str | None
    public_base_url: str | None

    @property
    def twilio_configured(self) -> bool:
        """True when the minimum Twilio credentials are present."""
        return bool(self.twilio_account_sid and self.twilio_auth_token)

    def safe_summary(self) -> dict[str, object]:
        """Non-secret summary safe to log or render in the UI."""
        return {
            "nova_model_id": self.nova_model_id,
            "aws_region": self.aws_region,
            # Non-secret CloudWatch log group name so the dashboard UI can build
            # a per-call Logs Insights deep link client-side (no IAM needed).
            "app_log_group": self.app_log_group,
            "twilio_configured": self.twilio_configured,
            "from_number_set": bool(self.twilio_from_number),
            "verified_caller_id_set": bool(self.twilio_verified_caller_id),
            "public_base_url_set": bool(self.public_base_url),
        }


def get_settings() -> Settings:
    """Build a Settings instance from the current environment."""
    return Settings(
        nova_model_id=os.environ.get("NOVA_MODEL_ID", DEFAULT_NOVA_MODEL_ID),
        aws_region=os.environ.get(
            "AWS_REGION", os.environ.get("AWS_DEFAULT_REGION", DEFAULT_AWS_REGION)
        ),
        app_log_group=os.environ.get("APP_LOG_GROUP", DEFAULT_APP_LOG_GROUP),
        twilio_account_sid=os.environ.get("TWILIO_ACCOUNT_SID") or None,
        twilio_auth_token=os.environ.get("TWILIO_AUTH_TOKEN") or None,
        twilio_from_number=os.environ.get("TWILIO_FROM_NUMBER") or None,
        twilio_verified_caller_id=os.environ.get("TWILIO_VERIFIED_CALLER_ID") or None,
        public_base_url=os.environ.get("PUBLIC_BASE_URL") or None,
    )
