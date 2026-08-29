#!/usr/bin/env python3
"""Query local Form 13F evidence without network access."""

import argparse
import json
import sys
from datetime import UTC, date, datetime

from investment_analyst.application.cli import (
    add_storage_location_arguments,
    storage_location_from_namespace,
)
from investment_analyst.application.sec_institutional_holdings import (
    SecInstitutionalHoldingsApplication,
)
from investment_analyst.evidence.sec_institutional_holdings.models import (
    InstitutionalHoldingsQuery,
)


def _time(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise argparse.ArgumentTypeError("known-at must be ISO 8601") from error
    if parsed.tzinfo is None:
        raise argparse.ArgumentTypeError("known-at must include timezone")
    return parsed.astimezone(UTC)


def _date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("period must be an ISO date") from error


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    add_storage_location_arguments(parser)
    parser.add_argument("--filer-cik", required=True)
    parser.add_argument("--known-at", required=True, type=_time)
    parser.add_argument("--period-from", type=_date)
    parser.add_argument("--period-to", type=_date)
    parser.add_argument("--limit", type=int, default=100)
    arguments = parser.parse_args()
    try:
        result = SecInstitutionalHoldingsApplication.create_default().query_institutional_holdings(
            query=InstitutionalHoldingsQuery(
                manager_cik=arguments.filer_cik,
                known_at=arguments.known_at,
                period_from=arguments.period_from,
                period_to=arguments.period_to,
                limit=arguments.limit,
            ),
            location=storage_location_from_namespace(arguments),
        )
        print(
            json.dumps(
                {
                    "state": "found" if result.reports else "missing",
                    "total_matching": result.total_matching,
                    "truncated": result.truncated,
                    "reports": [
                        {
                            "report_id": str(item.report_id),
                            "accession": item.cover_revision.document.filing.accession,
                            "report_period": (
                                None
                                if item.report_period is None
                                else item.report_period.isoformat()
                            ),
                            "available_at": item.available_at.isoformat(),
                            "value_total_matches": item.value_total_matches,
                        }
                        for item in result.reports
                    ],
                    "positions": [
                        {
                            "position_id": str(item.position_id),
                            "report_id": str(item.report_id),
                            "cusip": item.cusip,
                            "issuer_name": item.issuer_name,
                            "title_of_class": item.title_of_class,
                            "value": None if item.value is None else str(item.value),
                        }
                        for item in result.positions
                    ],
                },
                sort_keys=True,
            )
        )
        return 0
    except Exception as error:
        print(f"SEC institutional holdings query failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
