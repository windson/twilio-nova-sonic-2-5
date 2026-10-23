"""Twilio outbound calling + TwiML generation for the Media Streams bridge.

Places an AI-initiated outbound collections call. The call's TwiML uses
``<Connect><Stream>`` to open a WebSocket back to this service's ``/media``
endpoint, carrying the due/account id as a Stream ``<Parameter>`` so the
WebSocket ``start`` event's ``start.customParameters`` contains it.

Credentials are read from the environment via :mod:`app.config`. The auth token
is never logged. Tests mock ``twilio.rest.Client`` so no network call happens.
"""

from __future__ import annotations

import asyncio
import logging
import math
from dataclasses import dataclass
from typing import Any, Generator, Mapping
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


# Call-resource attributes retrieved after telephone completion through
# GET /2010-04-01/Accounts/{sid}/Calls/{CallSid}.json. Advanced Voice Insights
# QoS requires a separate integration and is not inferred from these fields.
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

# Call-resource fields do not include advanced Voice Insights QoS. Report
# This integration boundary without making claims about account entitlements.
ADVANCED_QOS_UNAVAILABLE_REASON = (
    "Advanced Voice Insights QoS (RTT/jitter/packet-loss/MOS) is not retrieved "
    "by this application; advanced QoS is unavailable."
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

    This runs POST-CALL only (wired from :mod:`app.postcall`), never on
    the real-time audio path. It reads the authentic Call-resource fields
    (duration, start/end timestamps, from/to, status, price, direction, ...) via
    the existing ``twilio.rest.Client`` using the Secrets-Manager-stored
    credentials; the auth token is never logged. It ALWAYS degrades gracefully:
    on missing creds / missing call_sid / any API error it returns an explicit
    ``available=False`` marker rather than raising, so a post-call failure can
    never crash the pipeline and we never fabricate data.

    Advanced Voice Insights QoS (RTT/jitter/packet-loss/MOS) is outside the
    Call-resource response. This helper marks it ``advanced_qos_available=False``
    with a human-readable reason.

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
        # The type name and call sid are recorded.
        logger.warning(
            "call_sid=<%s> | twilio call insights fetch failed (%s)",
            call_sid,
            type(exc).__name__,
        )
    return base


_TERMINAL_CALL_STATUSES = frozenset({"completed", "canceled", "failed", "busy", "no-answer"})
# A timed-out to_thread task keeps running. Retain it until completion instead
# of claiming that cancelling our wait cancelled the underlying HTTP request.
_HANGUP_WORKERS: set[asyncio.Task[Any]] = set()


@dataclass(frozen=True)
class _HangupRequestResult:
    status: str | None = None
    error_type: str | None = None
    rejected: bool = False
    pending: bool = False


def _prepare_hangup(
    call_sid: str | None,
    settings: Settings | None,
    request_timeout_s: float,
) -> tuple[dict[str, Any], Settings | None]:
    marker: dict[str, Any] = {
        "hung_up": False,
        "confirmed_terminal": False,
        "outcome": "request_failed",
        "status": None,
        "attempts": 0,
        "pending_request": False,
        "call_sid": call_sid,
    }
    if not call_sid:
        marker["error_type"] = "no_call_sid"
        return marker, None
    if not isinstance(request_timeout_s, (int, float)) or not math.isfinite(request_timeout_s) or request_timeout_s <= 0:
        marker["error_type"] = "invalid_request_timeout"
        return marker, None
    try:
        settings = settings or get_settings()
        if not settings.twilio_configured:
            marker["error_type"] = "twilio_not_configured"
            return marker, None
    except Exception as exc:  # noqa: BLE001 - configuration must not break teardown
        marker["error_type"] = type(exc).__name__
        return marker, None
    return marker, settings


def _perform_hangup_request(
    operation: str,
    call_sid: str,
    settings: Settings,
    client_factory: Any | None,
    request_timeout_s: float,
) -> _HangupRequestResult:
    """Perform one HTTP request without hidden retries or exception payloads."""
    from twilio.base.exceptions import TwilioRestException
    from twilio.http.http_client import TwilioHttpClient

    http_client = None
    request_started = False
    try:
        # Each request owns its session: a timed-out worker may still be using
        # its session when the async caller starts the read-back or retry.
        http_client = TwilioHttpClient(timeout=request_timeout_s, max_retries=0)
        if client_factory is None:
            from twilio.rest import Client as client_factory
        client = client_factory(
            settings.twilio_account_sid,
            settings.twilio_auth_token,
            http_client=http_client,
        )
        context = client.calls(call_sid)
        request_started = True
        call = context.update(status="completed") if operation == "update" else context.fetch()
        status = getattr(call, "status", None)
        return _HangupRequestResult(status=status if isinstance(status, str) else None)
    except Exception as exc:  # noqa: BLE001 - never expose SDK exception payloads
        # A 4xx (except a request timeout) explicitly rejects this request.
        # Transport failures and 5xx may follow a server-side state change.
        rejected = not request_started or (
            isinstance(exc, TwilioRestException)
            and isinstance(exc.status, int)
            and 400 <= exc.status < 500
            and exc.status != 408
        )
        return _HangupRequestResult(error_type=type(exc).__name__, rejected=rejected)
    finally:
        if http_client is not None and http_client.session is not None:
            try:
                http_client.session.close()
            except Exception:  # noqa: BLE001 - cleanup cannot change the result
                pass


def _hangup_steps(marker: dict[str, Any]) -> Generator[str, _HangupRequestResult, dict[str, Any]]:
    """Shared update/read-back/single-retry policy for both public helpers."""
    def observe(result: _HangupRequestResult) -> bool:
        if result.status is not None:
            marker["status"] = result.status
        if result.error_type is not None:
            marker["error_type"] = result.error_type
        if result.status in _TERMINAL_CALL_STATUSES:
            marker.update(hung_up=True, confirmed_terminal=True, outcome="confirmed_terminal")
            marker.pop("error_type", None)
            return True
        return False

    marker["attempts"] = 1
    update = yield "update"
    if observe(update):
        return marker
    if update.rejected:
        return marker

    marker["outcome"] = "unknown"
    # A successful read-back of a nonterminal status resolves an earlier
    # uncertain write, unless that write is still executing in another thread.
    uncertain_write = update.pending or update.error_type is not None or update.status is None
    fetched = yield "fetch"
    if observe(fetched):
        return marker
    if fetched.status is not None and not update.pending:
        uncertain_write = False

    marker["attempts"] = 2
    retry = yield "update"
    if observe(retry):
        return marker
    if retry.rejected and not uncertain_write:
        marker["outcome"] = "request_failed"
    return marker


def hangup_call(
    call_sid: str | None,
    *,
    settings: Settings | None = None,
    client_factory: Any | None = None,
    request_timeout_s: float = 10.0,
) -> dict[str, Any]:
    """Request call termination and confirm only an observed terminal status.

    At most two update requests and one intervening fetch are made. ``attempts``
    counts updates (0..2), including uncertain failures. ``status`` is the last
    observed Twilio status, or None; no status is inferred from sending a request.
    ``hung_up`` is the legacy alias of ``confirmed_terminal``. ``outcome`` is
    ``confirmed_terminal``, ``request_failed`` for a known rejection/configuration
    failure, or ``unknown`` when termination cannot be established. Failures may
    include a safe ``error_type``, never an exception message.

    The injected factory receives Twilio's ``http_client`` keyword. Every HTTP
    request has retries disabled and a positive socket timeout. Requests' socket
    timeout is NOT a total wall-clock deadline (DNS/connect/read may take longer).
    Use :func:`hangup_call_async` when the caller needs bounded waiting.
    """
    marker, settings = _prepare_hangup(call_sid, settings, request_timeout_s)
    if settings is None:
        return marker
    steps = _hangup_steps(marker)
    operation = next(steps)
    while True:
        result = _perform_hangup_request(operation, call_sid, settings, client_factory, request_timeout_s)
        try:
            operation = steps.send(result)
        except StopIteration as completed:
            return completed.value


def _observe_hangup_worker(worker: asyncio.Task[_HangupRequestResult]) -> None:
    """Observe late completion safely and release the retained strong reference."""
    _HANGUP_WORKERS.discard(worker)
    if worker.cancelled():
        return
    try:
        worker.result()
    except BaseException as exc:  # no exception repr/message in logs
        logger.warning("twilio hangup worker failed (%s)", type(exc).__name__)


async def hangup_call_async(
    call_sid: str | None,
    *,
    settings: Settings | None = None,
    client_factory: Any | None = None,
    request_timeout_s: float = 10.0,
) -> dict[str, Any]:
    """Same result/policy as ``hangup_call``, with bounded per-request waits.

    Each of at most three requests gets ``request_timeout_s`` of async waiting
    (30 seconds at the default, plus local scheduling overhead). A timeout does
    not cancel the HTTP worker: it remains retained and its completion observed.
    ``pending_request`` reports whether such a worker is still running at return.
    Any overlapping update has the same idempotent terminal intent. Caller
    cancellation propagates while workers remain retained until completion.
    """
    marker, settings = _prepare_hangup(call_sid, settings, request_timeout_s)
    if settings is None:
        return marker
    workers: list[asyncio.Task[_HangupRequestResult]] = []
    steps = _hangup_steps(marker)
    operation = next(steps)
    while True:
        worker = asyncio.create_task(
            asyncio.to_thread(
                _perform_hangup_request,
                operation,
                call_sid,
                settings,
                client_factory,
                request_timeout_s,
            ),
            name="twilio-hangup-request",
        )
        workers.append(worker)
        _HANGUP_WORKERS.add(worker)
        worker.add_done_callback(_observe_hangup_worker)
        try:
            result = await asyncio.wait_for(asyncio.shield(worker), timeout=request_timeout_s)
        except TimeoutError:
            result = _HangupRequestResult(error_type="TimeoutError", pending=True)
        try:
            operation = steps.send(result)
        except StopIteration as completed:
            marker = completed.value
            marker["pending_request"] = any(not request.done() for request in workers)
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
