#!/usr/bin/env python3
"""Compute persisted descriptive metrics over declared insider and 13D/13G activity."""

import argparse
from datetime import UTC, datetime

from investment_analyst.application.cazatiburones_activity_metrics import (
    CazatiburonesActivityMetricsApplication,
)
from investment_analyst.application.cli import (
    add_storage_location_arguments,
    storage_location_from_namespace,
)


def _time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00"))
    if parsed.tzinfo is None:
        raise argparse.ArgumentTypeError("known-at must include timezone")
    return parsed.astimezone(UTC)


def main() -> None:
    parser = argparse.ArgumentParser()
    add_storage_location_arguments(parser)
    parser.add_argument("--asset-id", required=True)
    parser.add_argument("--known-at", required=True, type=_time)
    args = parser.parse_args()
    result = CazatiburonesActivityMetricsApplication.create_default().compute(
        asset_id=args.asset_id,
        known_at=args.known_at,
        location=storage_location_from_namespace(args),
    )
    print(result.model_dump_json())


if __name__ == "__main__":
    main()
