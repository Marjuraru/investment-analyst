#!/usr/bin/env python3
"""Query local Section 16 ownership evidence without network."""

import argparse
import json
import sys
from datetime import UTC, datetime

from investment_analyst.application.cli import (
    add_storage_location_arguments,
    storage_location_from_namespace,
)
from investment_analyst.application.sec_ownership import SecOwnershipApplication
from investment_analyst.evidence.sec_ownership.models import OwnershipQuery


def _time(value):
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise argparse.ArgumentTypeError("known-at must be ISO 8601") from error
    if parsed.tzinfo is None:
        raise argparse.ArgumentTypeError("known-at must include timezone")
    return parsed.astimezone(UTC)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    add_storage_location_arguments(parser)
    parser.add_argument("--asset-id", required=True)
    parser.add_argument("--known-at", required=True, type=_time)
    parser.add_argument("--form")
    parser.add_argument("--accession")
    parser.add_argument("--reporting-owner-cik")
    parser.add_argument("--transaction-code")
    parser.add_argument("--limit", type=int, default=100)
    arguments = parser.parse_args()
    try:
        statements = SecOwnershipApplication.create_default().query_ownership(
            query=OwnershipQuery(
                asset_id=arguments.asset_id,
                known_at=arguments.known_at,
                form=arguments.form,
                accession=arguments.accession,
                reporting_owner_cik=arguments.reporting_owner_cik,
                transaction_code=arguments.transaction_code,
                limit=arguments.limit,
            ),
            location=storage_location_from_namespace(arguments),
        )
        print(
            json.dumps(
                {
                    "state": "found" if statements else "missing",
                    "statements": [
                        {
                            "statement_id": str(item.statement_id),
                            "accession": item.document_revision.document.filing.accession,
                            "form": item.form,
                            "owner_count": len(item.reporting_owners),
                            "entry_count": len(item.entries),
                            "available_at": item.available_at.isoformat(),
                        }
                        for item in statements
                    ],
                },
                sort_keys=True,
            )
        )
        return 0
    except Exception as error:
        print(f"SEC ownership query failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
