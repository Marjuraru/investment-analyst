#!/usr/bin/env python3
"""Query public-effective Form 13F holdings from read-only local evidence."""

import argparse
from datetime import UTC, date, datetime

from investment_analyst.application.cazatiburones_institutional_effective_holdings import (
    CazatiburonesInstitutionalEffectiveHoldingsApplication,
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


def _date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("report-period must be YYYY-MM-DD") from error


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    add_storage_location_arguments(parser)
    parser.add_argument("--manager-cik", required=True)
    parser.add_argument("--report-period", required=True, type=_date)
    parser.add_argument("--known-at", required=True, type=_time)
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--limit", type=int, default=1000)
    arguments = parser.parse_args()
    result = CazatiburonesInstitutionalEffectiveHoldingsApplication.create_default().query(
        manager_cik=arguments.manager_cik,
        report_period=arguments.report_period,
        known_at=arguments.known_at,
        offset=arguments.offset,
        limit=arguments.limit,
        location=storage_location_from_namespace(arguments),
    )
    print(result.model_dump_json())


if __name__ == "__main__":
    main()
