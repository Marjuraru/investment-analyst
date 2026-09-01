#!/usr/bin/env python3
"""Normalize linked, local Form 13F rows into append-only observations."""

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
    InstitutionalObservationRequest,
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
    parser.add_argument("--filer-cik", required=True)
    parser.add_argument("--report-id", required=True, action="append", type=UUID)
    parser.add_argument("--known-at", required=True, type=_time)
    args = parser.parse_args()
    try:
        result = CazatiburonesInstitutionalObservationsApplication.create_default().normalize(
            InstitutionalObservationRequest(
                asset_id=args.asset_id,
                manager_cik=args.filer_cik,
                report_ids=tuple(args.report_id),
                known_at=args.known_at,
            ),
            location=storage_location_from_namespace(args),
        )
    except Exception as error:
        print(f"institutional observation normalization failed: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result.model_dump(mode="json"), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
