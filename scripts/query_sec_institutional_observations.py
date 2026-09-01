#!/usr/bin/env python3
"""Query local institutional 13F observations without network access."""

import argparse
import json
import sys
from datetime import UTC, datetime
from uuid import UUID

from investment_analyst.application.cazatiburones_institutional_observations import (
    CazatiburonesInstitutionalObservationsApplication,
)
from investment_analyst.application.cli import (
    add_storage_location_arguments,
    storage_location_from_namespace,
)
from investment_analyst.evidence.sec_institutional_observations.models import (
    InstitutionalObservationQuery,
)


def _time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise argparse.ArgumentTypeError("known-at must include timezone")
    return parsed.astimezone(UTC)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    add_storage_location_arguments(parser)
    parser.add_argument("--asset-id", required=True)
    parser.add_argument("--known-at", required=True, type=_time)
    parser.add_argument("--filer-cik")
    parser.add_argument("--report-id")
    parser.add_argument("--cusip")
    parser.add_argument("--field-name")
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--limit", type=int, default=1000)
    args = parser.parse_args()
    try:
        result = CazatiburonesInstitutionalObservationsApplication.create_default().query(
            InstitutionalObservationQuery(
                asset_id=args.asset_id,
                known_at=args.known_at,
                manager_cik=args.filer_cik,
                report_id=UUID(args.report_id) if args.report_id else None,
                cusip=args.cusip,
                field_name=args.field_name,
                offset=args.offset,
                limit=args.limit,
            ),
            location=storage_location_from_namespace(args),
        )
    except Exception as error:
        print(f"institutional observation query failed: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result.model_dump(mode="json"), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
