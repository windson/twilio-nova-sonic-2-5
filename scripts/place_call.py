#!/usr/bin/env python3
"""CLI to initiate an AI-first outbound collections call.

Triggers the same end-to-end flow as ``POST /api/calls`` from the terminal:
look up a due/account record in the store and place an outbound Twilio call
whose TwiML opens a Media Streams WebSocket back to this service's ``/media``
endpoint, which bridges the caller to Amazon Bedrock Nova Sonic 2.5.

Usage::

    python scripts/place_call.py --account-id ACC-1001
    python scripts/place_call.py              # defaults to the seeded demo id
    python scripts/place_call.py --dry-run    # no live Twilio call
    python scripts/place_call.py --help

IMPORTANT: ``PUBLIC_BASE_URL`` must point at a publicly reachable host/tunnel
(e.g. an ngrok URL for local dev, or the ALB/public DNS on AWS) so Twilio can
open the ``/media`` WebSocket. For a Twilio trial/verified setup the callee is
``TWILIO_VERIFIED_CALLER_ID`` and the caller id is ``TWILIO_FROM_NUMBER``.
Credentials are read from the environment via :mod:`app.config`; secrets are
never printed.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Allow running as a plain script (python scripts/place_call.py) by making the
# repo root importable.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from app.config import get_settings  # noqa: E402
from app.store import get_default_store  # noqa: E402
from app.twilio_client import place_outbound_call  # noqa: E402

DEFAULT_ACCOUNT_ID = "ACC-1001"


def _make_dry_run_client():
    """A fake Twilio client factory for --dry-run (no network call)."""

    class _FakeCall:
        sid = "CA_dry_run_000000000000000000000000"

    class _FakeCalls:
        def create(self, **kwargs):
            print("[dry-run] would call twilio.calls.create with:")
            for key in ("to", "from_"):
                print(f"[dry-run]   {key}={kwargs.get(key)!r}")
            twiml = kwargs.get("twiml", "")
            print(f"[dry-run]   twiml={twiml}")
            return _FakeCall()

    class _FakeClient:
        def __init__(self, *_args, **_kwargs):
            self.calls = _FakeCalls()

    return _FakeClient


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="place_call.py",
        description="Initiate an AI-first outbound collections call via Nova Sonic 2.5.",
    )
    parser.add_argument(
        "--account-id",
        default=DEFAULT_ACCOUNT_ID,
        help=f"Due/account id to call about (default: {DEFAULT_ACCOUNT_ID}).",
    )
    parser.add_argument(
        "--to",
        default=None,
        help=(
            "Override the callee phone number (E.164). Defaults to "
            "TWILIO_VERIFIED_CALLER_ID for a trial/verified setup."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Build the request and TwiML without placing a live Twilio call.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = get_settings()
    store = get_default_store()

    due_record = store.get_due(args.account_id)
    if due_record is None:
        available = ", ".join(d["account_id"] for d in store.list_dues()) or "<none>"
        print(
            f"error: unknown account id {args.account_id!r}. "
            f"Available seeded records: {available}",
            file=sys.stderr,
        )
        return 2

    callee = args.to or settings.twilio_verified_caller_id
    if not callee and not args.dry_run:
        print(
            "error: no callee number. Set TWILIO_VERIFIED_CALLER_ID or pass --to. "
            "For a Twilio trial/verified setup the callee is TWILIO_VERIFIED_CALLER_ID.",
            file=sys.stderr,
        )
        return 2

    client_factory = None
    if args.dry_run:
        client_factory = _make_dry_run_client()
        callee = callee or "+10000000000"
        # Dry-run still validates required config but tolerates missing creds by
        # supplying placeholders so TwiML can be generated and printed.
        if not settings.twilio_configured or not settings.twilio_from_number or not settings.public_base_url:
            from app.config import Settings

            settings = Settings(
                nova_model_id=settings.nova_model_id,
                aws_region=settings.aws_region,
                twilio_account_sid=settings.twilio_account_sid or "AC_dry_run",
                twilio_auth_token=settings.twilio_auth_token or "dry_run_token",
                twilio_from_number=settings.twilio_from_number or "+15005550006",
                twilio_verified_caller_id=settings.twilio_verified_caller_id or callee,
                public_base_url=settings.public_base_url or "https://dry-run.example",
            )
            print(
                "[dry-run] note: using placeholder values for missing Twilio/"
                "PUBLIC_BASE_URL config so TwiML can be generated."
            )

    if not settings.public_base_url:
        print(
            "error: PUBLIC_BASE_URL is not set. It must point at a publicly "
            "reachable host/tunnel (e.g. ngrok URL or the AWS ALB/public DNS) "
            "so Twilio can open the /media WebSocket.",
            file=sys.stderr,
        )
        return 2

    try:
        result = place_outbound_call(
            callee,
            due_record,
            settings=settings,
            client_factory=client_factory,
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(
        f"outbound call placed: call_sid={result.call_sid} "
        f"account_id={result.account_id} to={result.to} from={result.from_number}"
    )
    print(
        f"debtor={due_record['debtor_name']} "
        f"amount_due={due_record['amount_due']} {due_record.get('currency', 'USD')} "
        f"due_date={due_record['due_date']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
