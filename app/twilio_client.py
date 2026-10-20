"""Twilio outbound calling + TwiML generation for the Media Streams bridge.

Places an AI-initiated outbound collections call. The call's TwiML uses
``<Connect><Stream>`` to open a WebSocket back to this service's ``/media``
endpoint, carrying the due/account id as a Stream ``<Parameter>`` so the
WebSocket ``start`` event's ``start.customParameters`` contains it.

Credentials are read from the environment via :mod:`app.config`. The auth token
is never logged. Tests mock ``twilio.rest.Client`` so no network call happens.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Mapping
from urllib.parse import urlsplit, urlunsplit
from xml.sax.saxutils import quoteattr

from app.config import Settings, get_settings

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class OutboundCallResult:
    """Outcome of placing an outbound call."""

    call_sid: str
    to: str
    from_number: str
    account_id: str | None


@dataclass(frozen=True)
class SmsResult:
    """Outcome of sending an SMS."""

    message_sid: str
    to: str
    from_number: str


def _media_ws_url(public_base_url: str) -> str:
    """Derive the ``wss://<host>/media`` WebSocket URL from PUBLIC_BASE_URL.

    Accepts http(s):// or ws(s):// base URLs and normalizes the scheme to
    ``wss`` and the path to ``/media``.
    """
    base = (public_base_url or "").strip().rstrip("/")
    parts = urlsplit(base if "://" in base else f"https://{base}")
    netloc = parts.netloc or parts.path  # bare host fell into path
    return urlunsplit(("wss", netloc, "/media", "", ""))


def build_stream_twiml(
    public_base_url: str,
    *,
    account_id: str | None = None,
    extra_parameters: Mapping[str, str] | None = None,
) -> str:
    """Build the ``<Connect><Stream>`` TwiML document for an outbound call.

    Args:
        public_base_url: Public base URL of this service (PUBLIC_BASE_URL).
        account_id: Due/account id passed through as a Stream ``<Parameter>``.
        extra_parameters: Additional Stream parameters.

    Returns:
        A TwiML XML string.
    """
    ws_url = _media_ws_url(public_base_url)

    params: dict[str, str] = {}
    if account_id is not None:
        params["accountId"] = account_id
    if extra_parameters:
        params.update({k: str(v) for k, v in extra_parameters.items()})

    param_tags = "".join(
        f"<Parameter name={quoteattr(name)} value={quoteattr(value)}/>" for name, value in params.items()
    )

    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<Response>"
        "<Connect>"
        f"<Stream url={quoteattr(ws_url)}>"
        f"{param_tags}"
        "</Stream>"
        "</Connect>"
        "</Response>"
    )


def _make_client(settings: Settings, client_factory: Any | None = None) -> Any:
    """Instantiate a Twilio REST client (never logs the auth token)."""
    if client_factory is None:
        from twilio.rest import Client as client_factory  # type: ignore[no-redef]
    return client_factory(settings.twilio_account_sid, settings.twilio_auth_token)


def send_sms(
    to: str,
    body: str,
    *,
    settings: Settings | None = None,
    client_factory: Any | None = None,
) -> SmsResult:
    """Send an SMS using the same Twilio REST client + credentials.

    Used by the post-call PTP downstream trigger to deliver a payment link.

    Note: on a Twilio TRIAL account messages can only be delivered to the
    verified number; sending to any other destination raises a Twilio API
    error. Callers are expected to attempt the real send and catch/flag the
    trial restriction (``app.postcall`` records ``ptp_sms_status`` accordingly)
    rather than faking a success.

    Args:
        to: Destination phone number (E.164).
        body: Message body.
        settings: Settings override (defaults to :func:`get_settings`).
        client_factory: Injectable ``twilio.rest.Client`` factory (mocked in
            tests). Defaults to the real client.

    Returns:
        :class:`SmsResult` describing the created message.

    Raises:
        ValueError: If required Twilio configuration is missing.
    """
    settings = settings or get_settings()

    if not settings.twilio_configured:
        raise ValueError("Twilio is not configured (missing TWILIO_ACCOUNT_SID/TWILIO_AUTH_TOKEN)")
    if not settings.twilio_from_number:
        raise ValueError("TWILIO_FROM_NUMBER is not configured")

    client = _make_client(settings, client_factory=client_factory)
    message = client.messages.create(
        to=to,
        from_=settings.twilio_from_number,
        body=body,
    )
    message_sid = getattr(message, "sid", None) or ""
    logger.info("message_sid=<%s> | payment-link SMS sent", message_sid)
    return SmsResult(
        message_sid=message_sid,
        to=to,
        from_number=settings.twilio_from_number,
    )


def place_outbound_call(
    to: str,
    account_record: Mapping[str, Any] | None = None,
    *,
    settings: Settings | None = None,
    client_factory: Any | None = None,
) -> OutboundCallResult:
    """Place an AI-initiated outbound call bridged to Nova Sonic.

    Args:
        to: Callee phone number (E.164). For a Twilio trial/verified setup this
            is typically ``TWILIO_VERIFIED_CALLER_ID``.
        account_record: Due/account record; its ``id``/``account_id`` is passed
            to the Stream as the ``accountId`` parameter.
        settings: Settings override (defaults to :func:`get_settings`).
        client_factory: Injectable ``twilio.rest.Client`` factory (mocked in
            tests). Defaults to the real client.

    Returns:
        :class:`OutboundCallResult` describing the created call.

    Raises:
        ValueError: If required Twilio configuration is missing.
    """
    settings = settings or get_settings()

    if not settings.twilio_configured:
        raise ValueError("Twilio is not configured (missing TWILIO_ACCOUNT_SID/TWILIO_AUTH_TOKEN)")
    if not settings.twilio_from_number:
        raise ValueError("TWILIO_FROM_NUMBER is not configured")
    if not settings.public_base_url:
        raise ValueError("PUBLIC_BASE_URL is not configured")

    account_record = account_record or {}
    account_id = account_record.get("id") or account_record.get("account_id")
    account_id = str(account_id) if account_id is not None else None

    twiml = build_stream_twiml(settings.public_base_url, account_id=account_id)

    client = _make_client(settings, client_factory=client_factory)
    call = client.calls.create(
        to=to,
        from_=settings.twilio_from_number,
        twiml=twiml,
    )

    call_sid = getattr(call, "sid", None) or ""
    logger.info("call_sid=<%s>, account_id=<%s> | outbound call placed", call_sid, account_id)

    return OutboundCallResult(
        call_sid=call_sid,
        to=to,
        from_number=settings.twilio_from_number,
        account_id=account_id,
    )
