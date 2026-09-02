#!/usr/bin/env python3
"""Query local, read-only point-in-time declared 13F concentration."""

import argparse
from datetime import UTC, datetime

from investment_analyst.application.cazatiburones_institutional_concentration import (
    CazatiburonesInstitutionalConcentrationApplication,
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
    parser.add_argument("--manager-cik", required=True)
    parser.add_argument("--known-at", required=True, type=_time)
    arguments = parser.parse_args()
    results = CazatiburonesInstitutionalConcentrationApplication.create_default().query(
        manager_cik=arguments.manager_cik,
        known_at=arguments.known_at,
        location=storage_location_from_namespace(arguments),
    )
    print("[" + ",".join(item.model_dump_json() for item in results) + "]")


if __name__ == "__main__":
    main()
