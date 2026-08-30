#!/usr/bin/env python3
"""Materialize immutable descriptive activity-event snapshots."""

import argparse
from datetime import UTC, datetime

from investment_analyst.application.cazatiburones_activity_events import (
    CazatiburonesActivityEventsApplication,
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
    arguments = parser.parse_args()
    result = CazatiburonesActivityEventsApplication.create_default().materialize(
        asset_id=arguments.asset_id,
        known_at=arguments.known_at,
        location=storage_location_from_namespace(arguments),
    )
    print(result.model_dump_json())


if __name__ == "__main__":
    main()
