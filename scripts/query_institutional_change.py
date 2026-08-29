#!/usr/bin/env python3
import argparse
from datetime import UTC, datetime

from investment_analyst.application.cazatiburones_institutional_change import (
    CazatiburonesInstitutionalChangeApplication,
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
    parser.add_argument("--filer-cik", required=True)
    parser.add_argument("--known-at", required=True, type=_time)
    args = parser.parse_args()
    result = CazatiburonesInstitutionalChangeApplication.create_default().query(
        manager_cik=args.filer_cik,
        known_at=args.known_at,
        location=storage_location_from_namespace(args),
    )
    print("[" + ",".join(item.model_dump_json() for item in result) + "]")


if __name__ == "__main__":
    main()
