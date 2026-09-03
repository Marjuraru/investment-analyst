#!/usr/bin/env python3
"""CLI script for materializing descriptive institutional 13F events into storage."""

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from investment_analyst.application.cazatiburones_institutional_events import (
    CazatiburonesInstitutionalEventsApplication,
)
from investment_analyst.application.runtime import StorageLocationRequest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--asset-id", required=True)
    parser.add_argument("--manager-cik", required=True)
    parser.add_argument("--known-at", required=True)
    parser.add_argument("--workspace", type=Path, default=None)
    parser.add_argument("--legacy-root", type=Path, default=None)
    arguments = parser.parse_args()

    known_at = datetime.fromisoformat(arguments.known_at.replace("Z", "+00:00")).astimezone(UTC)
    location = StorageLocationRequest(
        workspace=arguments.workspace, legacy_root=arguments.legacy_root
    )

    app = CazatiburonesInstitutionalEventsApplication.create_default()
    summary = app.materialize(
        asset_id=arguments.asset_id,
        manager_cik=arguments.manager_cik,
        known_at=known_at,
        location=location,
    )
    print(json.dumps(summary.model_dump(mode="json"), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
