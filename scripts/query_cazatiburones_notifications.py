#!/usr/bin/env python3
"""Query the explicit Cazatiburones local notification outbox."""

import argparse
import sys
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


def _location_arguments(parser: argparse.ArgumentParser) -> None:
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--workspace", type=Path)
    group.add_argument("--legacy-root", "--root", dest="legacy_root", type=Path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    _location_arguments(parser)
    parser.add_argument("--outbox-state", type=Path, required=True)
    parser.add_argument("--notification-id", type=UUID)
    arguments = parser.parse_args()

    try:
        result = CazatiburonesNotificationsApplication.create_default().query(
            location=StorageLocationRequest(
                workspace=arguments.workspace,
                legacy_root=arguments.legacy_root,
            ),
            outbox_state=arguments.outbox_state,
            notification_id=arguments.notification_id,
        )
    except (CazatiburonesNotificationError, ValueError, WorkspaceError) as error:
        print(f"Cazatiburones notification query failed: {error}", file=sys.stderr)
        return 1

    print(result.model_dump_json(indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
