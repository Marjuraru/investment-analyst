#!/usr/bin/env python3
"""Query local Schedule 13D/13G evidence without network access."""

import argparse
import json
import sys
from datetime import UTC, datetime

from investment_analyst.application.cli import (
    add_storage_location_arguments,
    storage_location_from_namespace,
)
from investment_analyst.application.sec_beneficial_ownership import (
    SecBeneficialOwnershipApplication,
)
from investment_analyst.evidence.sec_beneficial_ownership.models import BeneficialOwnershipQuery


def _time(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise argparse.ArgumentTypeError("known-at must be ISO 8601") from error
    if parsed.tzinfo is None:
        raise argparse.ArgumentTypeError("known-at must include timezone")
    return parsed.astimezone(UTC)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    add_storage_location_arguments(parser)
    parser.add_argument("--asset-id", required=True)
    parser.add_argument("--known-at", required=True, type=_time)
    parser.add_argument("--form")
    parser.add_argument("--accession")
    parser.add_argument("--limit", type=int, default=100)
    arguments = parser.parse_args()
    try:
        result = SecBeneficialOwnershipApplication.create_default().query_beneficial_ownership(
            query=BeneficialOwnershipQuery(
                asset_id=arguments.asset_id,
                known_at=arguments.known_at,
                form=arguments.form,
                accession=arguments.accession,
                limit=arguments.limit,
            ),
            location=storage_location_from_namespace(arguments),
        )
        print(
            json.dumps(
                {
                    "state": "found" if result.statements else "missing",
                    "total_matching": result.total_matching,
                    "truncated": result.truncated,
                    "statements": [
                        {
                            "statement_id": str(item.statement_id),
                            "accession": item.document_revision.document.filing.accession,
                            "form": item.form,
                            "available_at": item.available_at.isoformat(),
                        }
                        for item in result.statements
                    ],
                },
                sort_keys=True,
            )
        )
        return 0
    except Exception as error:
        print(f"SEC beneficial ownership query failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
