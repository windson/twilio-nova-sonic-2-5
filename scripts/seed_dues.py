#!/usr/bin/env python3
"""Seed the demo due/account records into the DynamoDB dues table.

On AWS the app reads due records from DynamoDB (the ``DUES_TABLE`` created by
the CloudFormation stack). A fresh deploy starts with an EMPTY dues table, so
the dashboard shows no accounts and there is nothing to call about until the
demo records are written. Previously this was a manual ``aws dynamodb`` step;
this script automates it idempotently.

It writes the same demo records the in-memory store seeds locally
(``ACC-1001`` / ``ACC-1002``), so the AWS deploy behaves like local dev out of
the box. It is **idempotent** and safe to re-run: ``put_item`` (insert-or-
replace) is used, so re-running simply rewrites the same records.

It never touches the real-time audio path and makes no account-/table-specific
assumptions: the table name comes from the environment or ``--dues-table`` (not
hard-coded), and the region from ``--region`` / ``AWS_REGION``.

Usage::

    # Table name + region from the environment (as the ECS task sees them):
    DUES_TABLE=<dues-table-name> AWS_REGION=us-east-1 \
        python scripts/seed_dues.py

    # Or resolve the table name from the CloudFormation stack output first:
    export DUES_TABLE=$(aws cloudformation describe-stacks \
        --stack-name ar-collections-nova-sonic --region us-east-1 \
        --query "Stacks[0].Outputs[?OutputKey=='DuesTableName'].OutputValue|[0]" \
        --output text)
    python scripts/seed_dues.py --region us-east-1

    # Explicit table name, dry run (prints what it would write, no AWS call):
    python scripts/seed_dues.py --dues-table <dues-table-name> --dry-run
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

# Allow running as a plain script (python scripts/seed_dues.py) by making the
# repo root importable so we reuse the canonical demo records + the store.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from app.store import DynamoDbStore, _seed_dues  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="seed_dues.py",
        description="Idempotently seed the demo due records into the DynamoDB dues table.",
    )
    parser.add_argument(
        "--dues-table",
        default=os.environ.get("DUES_TABLE"),
        help="DynamoDB dues table name (default: $DUES_TABLE). Use the stack's "
        "DuesTableName output; never a hard-coded physical name.",
    )
    parser.add_argument(
        "--region",
        default=os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION"),
        help="AWS region (default: $AWS_REGION / $AWS_DEFAULT_REGION).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the records that would be written without calling AWS.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    # The canonical demo records are the same ones the in-memory store seeds, so
    # the AWS deploy matches local dev exactly.
    records = list(_seed_dues().values())

    if args.dry_run:
        print(f"[dry-run] would seed {len(records)} due record(s):")
        for rec in records:
            print("[dry-run]   " + json.dumps(rec, default=str, sort_keys=True))
        print(f"[dry-run] target table: {args.dues_table or '<unset>'} "
              f"region: {args.region or '<unset>'}")
        return 0

    if not args.dues_table:
        print(
            "error: no dues table. Set DUES_TABLE or pass --dues-table "
            "(use the stack's DuesTableName output).",
            file=sys.stderr,
        )
        return 2

    # The calls table is irrelevant to seeding dues; pass a harmless placeholder
    # so the store constructs. add_due only touches the dues table.
    store = DynamoDbStore(
        dues_table=args.dues_table,
        calls_table=args.dues_table,
        region_name=args.region,
    )

    written = 0
    for rec in records:
        saved = store.add_due(rec)  # put_item: insert-or-replace, idempotent
        print(
            f"seeded account_id={saved['account_id']} "
            f"debtor={saved.get('debtor_name')} "
            f"amount_due={saved.get('amount_due')} {saved.get('currency', 'USD')}"
        )
        written += 1

    print(f"OK: seeded {written} due record(s) into {args.dues_table} ({args.region}).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
