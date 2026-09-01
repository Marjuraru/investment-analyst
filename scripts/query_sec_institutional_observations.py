#!/usr/bin/env python3
"""Query local institutional 13F observations without network access."""

import argparse
import json
import sys
from datetime import UTC, datetime

from investment_analyst.application.cazatiburones_institutional_observations import (
    CazatiburonesInstitutionalObservationsApplication,
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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    add_storage_location_arguments(parser)
    parser.add_argument("--asset-id", required=True)
    parser.add_argument("--known-at", required=True, type=_time)
    args = parser.parse_args()
    try:
        rows = CazatiburonesInstitutionalObservationsApplication.create_default().query(
            asset_id=args.asset_id,
            known_at=args.known_at,
            location=storage_location_from_namespace(args),
        )
    except Exception as error:
        print(f"institutional observation query failed: {error}", file=sys.stderr)
        return 1
    print(json.dumps([row.model_dump(mode="json") for row in rows], sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
