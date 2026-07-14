#!/usr/bin/env python3
"""Query normalized Apple SEC facts from local storage point-in-time."""

import argparse
import json
import sys
from datetime import UTC, date, datetime
from pathlib import Path

from investment_analyst.core.models import DataFrequency
from investment_analyst.providers.fundamentals.sec_point_in_time_service import (
    SecAaplFundamentalPointInTimeService,
    SecFundamentalQueryError,
)
from investment_analyst.providers.fundamentals.sec_query_models import SecFundamentalQuery
from investment_analyst.storage import LocalStorage, StorageError, StoragePaths

_NOTICE = (
    "Local point-in-time query of five Apple SEC fundamental fields. The current revision is "
    "selected using known_at, incomplete periods remain incomplete, no ratios are calculated, "
    "no operations are executed, and this is not financial advice."
)


def _aware_datetime(value: str) -> datetime:
    normalized = f"{value[:-1]}+00:00" if value.endswith(("Z", "z")) else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as error:
        raise argparse.ArgumentTypeError("known-at must be a valid ISO 8601 datetime") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise argparse.ArgumentTypeError("known-at must include timezone information")
    return parsed.astimezone(UTC)


def _date_value(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("expected date in YYYY-MM-DD format") from error


def _frequency(value: str) -> DataFrequency:
    mapping = {
        "annual": DataFrequency.ANNUAL,
        "quarterly": DataFrequency.QUARTERLY,
    }
    try:
        return mapping[value.casefold()]
    except KeyError as error:
        raise argparse.ArgumentTypeError("frequency must be annual or quarterly") from error


def _limit(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("limit must be an integer") from error
    if not 1 <= parsed <= 500:
        raise argparse.ArgumentTypeError("limit must be between 1 and 500")
    return parsed


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Query point-in-time Apple SEC facts from local normalized observations."
    )
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--known-at", required=True, type=_aware_datetime)
    parser.add_argument("--frequency", required=True, type=_frequency)
    parser.add_argument("--start", type=_date_value)
    parser.add_argument("--end", type=_date_value)
    parser.add_argument("--limit", type=_limit)
    return parser


def main() -> int:
    """Execute the read-only local query and print deterministic JSON."""
    args = _parser().parse_args()
    try:
        request = SecFundamentalQuery(
            known_at=args.known_at,
            frequency=args.frequency,
            start_period_end=args.start,
            end_period_end=args.end,
            limit=args.limit,
        )
        with LocalStorage(StoragePaths.from_root(args.root)) as storage:
            result = SecAaplFundamentalPointInTimeService(storage).query(request)
        print(
            json.dumps(
                {"notice": _NOTICE, "result": result.to_json_dict()},
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    except (SecFundamentalQueryError, StorageError, ValueError) as error:
        print(f"SEC fundamental query failed: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
