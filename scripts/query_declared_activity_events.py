#!/usr/bin/env python3
"""Read one immutable descriptive activity-event snapshot."""

import argparse
from datetime import UTC, datetime
from uuid import UUID

from investment_analyst.application.cazatiburones_activity_events import (
    CazatiburonesActivityEventsApplication,
)
from investment_analyst.application.cli import (
    add_storage_location_arguments,
    storage_location_from_namespace,
)


def _time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise argparse.ArgumentTypeError("known-at must include timezone")
    return parsed.astimezone(UTC)


def main() -> None:
    parser = argparse.ArgumentParser()
    add_storage_location_arguments(parser)
    parser.add_argument("--asset-id", required=True)
    parser.add_argument("--known-at", required=True, type=_time)
    parser.add_argument("--snapshot-id", required=True, type=UUID)
    arguments = parser.parse_args()
    result = CazatiburonesActivityEventsApplication.create_default().query(
        asset_id=arguments.asset_id,
        known_at=arguments.known_at,
        snapshot_id=arguments.snapshot_id,
        location=storage_location_from_namespace(arguments),
    )
    print("null" if result is None else result.model_dump_json())


if __name__ == "__main__":
    main()
