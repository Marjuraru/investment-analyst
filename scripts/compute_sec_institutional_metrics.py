#!/usr/bin/env python3
"""Compute append-only institutional descriptive metrics in an explicit workspace."""

import argparse
from datetime import UTC, datetime

from investment_analyst.application.cazatiburones_institutional_metrics import (
    CazatiburonesInstitutionalMetricsApplication,
)
from investment_analyst.application.cli import (
    add_storage_location_arguments,
    storage_location_from_namespace,
)


def _time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise argparse.ArgumentTypeError("known-at must include timezone")
    return parsed.astimezone(UTC)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    add_storage_location_arguments(parser)
    parser.add_argument("--asset-id", required=True)
    parser.add_argument("--manager-cik", required=True)
    parser.add_argument("--known-at", required=True, type=_time)
    args = parser.parse_args()
    result = CazatiburonesInstitutionalMetricsApplication.create_default().compute(
        asset_id=args.asset_id,
        manager_cik=args.manager_cik,
        known_at=args.known_at,
        location=storage_location_from_namespace(args),
    )
    print(result.model_dump_json())


if __name__ == "__main__":
    main()
