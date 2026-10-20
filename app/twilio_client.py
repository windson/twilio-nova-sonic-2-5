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


# Authentic Twilio Call-resource attributes we persist for a completed call
# (ASK 2, FEAT-001 finding b). These are real transport/billing facts obtainable
# via GET /2010-04-01/Accounts/{sid}/Calls/{CallSid}.json. We deliberately do
# NOT include advanced Voice Insights QoS (RTT/jitter/packet-loss/MOS): that is
# a paid add-on NOT enabled on the trial account, so it is marked unavailable
# rather than fabricated.
_CALL_RESOURCE_FIELDS = (
    "sid",
    "parent_call_sid",
    "to",
    "to_formatted",
    "from_",
    "from_formatted",
    "status",
    "start_time",
    "end_time",
    "duration",
    "price",
    "price_unit",
    "direction",
    "answered_by",
    "queue_time",
    "date_created",
    "date_updated",
)

# Marker string used when the paid Voice Insights Advanced Features add-on is
# not enabled (the trial-account reality, per FEAT-001). The UI shows this
# verbatim so the limitation is stated honestly instead of fabricating QoS.
ADVANCED_QOS_UNAVAILABLE_REASON = (
    "Voice Insights Advanced Features (RTT/jitter/packet-loss/MOS) is a paid "
    "add-on not enabled on this account; advanced QoS is unavailable."
)


def _coerce_call_field(value: Any) -> Any:
    """Make a Twilio Call attribute JSON/DynamoDB-serializable.

    Twilio returns ``datetime`` for the timestamp fields and ``Decimal``/str for
    price. We stringify datetimes to ISO-8601 and leave scalars as-is so the
    stored record contains plain JSON-friendly values (never the auth token).
    """
    if value is None:
        return None
    isoformat = getattr(value, "isoformat", None)
    if callable(isoformat):
        try:
            return isoformat()
        except Exception:  # noqa: BLE001 - defensive, never crash enrichment
            return str(value)
    if isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def fetch_call_insights(
    call_sid: str | None,
    *,
    settings: Settings | None = None,
    client_factory: Any | None = None,
) -> dict[str, Any]:
    """Fetch the REAL Twilio Call resource for a completed call (OFF-PATH).

    ASK 2/6: this runs POST-CALL only (wired from :mod:`app.postcall`), never on
    the real-time audio path. It reads the authentic Call-resource fields
    (duration, start/end timestamps, from/to, status, price, direction, ...) via
    the existing ``twilio.rest.Client`` using the Secrets-Manager-stored
    credentials; the auth token is never logged. It ALWAYS degrades gracefully:
    on missing creds / missing call_sid / any API error it returns an explicit
    ``available=False`` marker rather than raising, so a post-call failure can
    never crash the pipeline and we never fabricate data.

    Advanced Voice Insights QoS (RTT/jitter/packet-loss/MOS) is a paid add-on
    NOT enabled on the trial account (FEAT-001 finding b); it is always marked
    ``advanced_qos_available=False`` with a human-readable reason, never faked.

    Args:
        call_sid: The completed Twilio Call sid to fetch.
        settings: Settings override (defaults to :func:`get_settings`).
        client_factory: Injectable ``twilio.rest.Client`` factory (mocked in
            tests). Defaults to the real client.

    Returns:
        A ``twilio_insights`` dict: ``{available, source, advanced_qos_available,
        advanced_qos_reason, fields}``. ``fields`` holds the authentic
        Call-resource values when available, else is empty.
    """
    settings = settings or get_settings()

    base: dict[str, Any] = {
        "available": False,
        "source": "twilio_call_resource",
        "advanced_qos_available": False,
        "advanced_qos_reason": ADVANCED_QOS_UNAVAILABLE_REASON,
        "fields": {},
    }

    if not call_sid:
        base["source"] = "unavailable"
        base["error"] = "no_call_sid"
        return base
    if not settings.twilio_configured:
        base["source"] = "unavailable"
        base["error"] = "twilio_not_configured"
        return base

    try:
        client = _make_client(settings, client_factory=client_factory)
        call = client.calls(call_sid).fetch()
        fields: dict[str, Any] = {}
        for name in _CALL_RESOURCE_FIELDS:
            # ``from_``/``to`` etc. live as attributes on the Twilio Call
            # instance; map ``from_`` to a friendly ``from`` key for the UI.
            value = getattr(call, name, None)
            key = "from" if name == "from_" else name
            fields[key] = _coerce_call_field(value)
        base["available"] = True
        base["fields"] = fields
        logger.info(
            "call_sid=<%s>, status=<%s> | twilio call insights fetched",
            call_sid,
            fields.get("status"),
        )
    except Exception as exc:  # noqa: BLE001 - never crash post-call enrichment
        base["available"] = False
        base["source"] = "unavailable"
        base["error"] = type(exc).__name__
        # Log WITHOUT the exception payload detail that might echo creds; only
        # the type name and call sid are recorded.
        logger.warning(
            "call_sid=<%s> | twilio call insights fetch failed (%s)",
            call_sid,
            type(exc).__name__,
        )
    return base


def hangup_call(
    call_sid: str | None,
    *,
    settings: Settings | None = None,
    client_factory: Any | None = None,
) -> dict[str, Any]:
    """Hang up a live Twilio call by completing it (FEAT-002, terminal action).

    The end-of-conversation ``end_conversation`` tool (built in
    :mod:`app.collections_agent`) drives this: updating an in-progress Call with
    ``Status=completed`` terminates it via
    ``client.calls(call_sid).update(status='completed')`` (confirmed against the
    Twilio Call-resource API). This is the single intended terminal REST action;
    it is allowed off the real-time audio path.

    It is deliberately FAILURE-SAFE and never raises into the caller (the model
    thread must never crash on a hangup): a missing ``call_sid`` returns a benign
    marker, and a Twilio error because the call already completed / is gone is
    caught and returned as a marker. It mirrors the defensive logging in
    :func:`fetch_call_insights` - only the exception type name and call sid are
    logged, never the auth token.

    Args:
        call_sid: The live Twilio Call sid to hang up.
        settings: Settings override (defaults to :func:`get_settings`).
        client_factory: Injectable ``twilio.rest.Client`` factory (mocked in
            tests). Defaults to the real client.

    Returns:
        A marker dict: ``{"hung_up": bool, "status": str, "call_sid": ...}`` and,
        on failure, an ``error`` (exception type name). ``hung_up`` is ``True``
        only when the Call update was actually issued.
    """
    marker: dict[str, Any] = {
        "hung_up": False,
        "status": "noop",
        "call_sid": call_sid,
    }

    if not call_sid:
        marker["status"] = "no_call_sid"
        return marker

    settings = settings or get_settings()
    if not settings.twilio_configured:
        marker["status"] = "twilio_not_configured"
        return marker

    try:
        client = _make_client(settings, client_factory=client_factory)
        client.calls(call_sid).update(status="completed")
        marker["hung_up"] = True
        marker["status"] = "completed"
        logger.info("call_sid=<%s> | twilio call hung up (status=completed)", call_sid)
    except Exception as exc:  # noqa: BLE001 - never raise; call may already be gone
        marker["hung_up"] = False
        marker["status"] = "error"
        marker["error"] = type(exc).__name__
        # Log WITHOUT the exception payload (which could echo creds); only the
        # type name + call sid. A call that already completed / is gone lands
        # here and is treated as a benign no-op by the caller.
        logger.warning(
            "call_sid=<%s> | twilio hangup failed (%s); treating as already-ended",
            call_sid,
            type(exc).__name__,
        )
    return marker


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
