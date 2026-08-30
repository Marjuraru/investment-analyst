#!/usr/bin/env python3
# ruff: noqa: E501
"""Query descriptive activity-rule evaluations without writing evidence."""

import argparse
from datetime import UTC, datetime

from investment_analyst.application.cazatiburones_activity_rules import (
    CazatiburonesActivityRulesApplication,
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
    subject = parser.add_mutually_exclusive_group(required=True)
    subject.add_argument("--asset-id", help="Query insider and beneficial ownership rules.")
    subject.add_argument("--manager-cik", help="Query institutional (Form 13F) rules.")
    parser.add_argument("--known-at", required=True, type=_time)
    args = parser.parse_args()
    application = CazatiburonesActivityRulesApplication.create_default()
    location = storage_location_from_namespace(args)
    if args.asset_id is not None:
        result = application.query_declared_activity(
            asset_id=args.asset_id, known_at=args.known_at, location=location
        )
    else:
        result = application.query_institutional(
            manager_cik=args.manager_cik, known_at=args.known_at, location=location
        )
    print(result.model_dump_json())


if __name__ == "__main__":
    main()
