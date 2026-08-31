#!/usr/bin/env python3
"""Query local as-filed Form 13F semantics without network access."""

import argparse
import json
import sys
from datetime import UTC, datetime
from uuid import UUID

from investment_analyst.application.cli import (
    add_storage_location_arguments,
    storage_location_from_namespace,
)
from investment_analyst.application.sec_institutional_semantics import (
    SecInstitutionalSemanticsApplication,
)
from investment_analyst.evidence.sec_institutional_semantics.models import (
    InstitutionalHoldingsSemanticsQuery,
)


def _time(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise argparse.ArgumentTypeError("known-at must be ISO 8601") from error
    if parsed.tzinfo is None:
        raise argparse.ArgumentTypeError("known-at must include timezone")
    return parsed.astimezone(UTC)


def _uuid(value: str) -> UUID:
    try:
        return UUID(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("report-id must be a UUID") from error


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    add_storage_location_arguments(parser)
    parser.add_argument("--filer-cik", required=True)
    parser.add_argument("--report-id", required=True, action="append", type=_uuid)
    parser.add_argument("--known-at", required=True, type=_time)
    parser.add_argument("--cusip")
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--limit", type=int, default=1000)
    arguments = parser.parse_args()
    try:
        result = SecInstitutionalSemanticsApplication.create_default().query(
            query=InstitutionalHoldingsSemanticsQuery(
                manager_cik=arguments.filer_cik,
                report_ids=tuple(arguments.report_id),
                known_at=arguments.known_at,
                cusip=arguments.cusip,
                offset=arguments.offset,
                limit=arguments.limit,
            ),
            location=storage_location_from_namespace(arguments),
        )
    except Exception as error:
        print(f"SEC institutional semantics query failed: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result.model_dump(mode="json"), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
