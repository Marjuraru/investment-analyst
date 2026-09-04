#!/usr/bin/env python3
"""Acknowledge one Cazatiburones local notification at an explicit UTC time."""

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from investment_analyst.alerts.cazatiburones_notifications import (
    CazatiburonesNotificationError,
)
from investment_analyst.application.cazatiburones_notifications import (
    CazatiburonesNotificationsApplication,
)
from investment_analyst.application.runtime import StorageLocationRequest
from investment_analyst.workspace.service import WorkspaceError


def _time(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise argparse.ArgumentTypeError("recorded-at must be an ISO timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise argparse.ArgumentTypeError("recorded-at must include timezone")
    return parsed.astimezone(UTC)


def _location_arguments(parser: argparse.ArgumentParser) -> None:
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--workspace", type=Path)
    group.add_argument("--legacy-root", "--root", dest="legacy_root", type=Path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    _location_arguments(parser)
    parser.add_argument("--outbox-state", type=Path, required=True)
    parser.add_argument("--notification-id", type=UUID, required=True)
    parser.add_argument("--recorded-at", type=_time, required=True)
    arguments = parser.parse_args()

    try:
        result = CazatiburonesNotificationsApplication.create_default().acknowledge(
            location=StorageLocationRequest(
                workspace=arguments.workspace,
                legacy_root=arguments.legacy_root,
            ),
            outbox_state=arguments.outbox_state,
            notification_id=arguments.notification_id,
            recorded_at=arguments.recorded_at,
        )
    except (CazatiburonesNotificationError, ValueError, WorkspaceError) as error:
        print(f"Cazatiburones notification acknowledgement failed: {error}", file=sys.stderr)
        return 1

    print(result.model_dump_json(indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
