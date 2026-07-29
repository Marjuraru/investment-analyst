#!/usr/bin/env python3
"""Query locally stored FRED/ALFRED vintages without contacting the provider."""

import argparse
import json
import sys
from datetime import UTC, date, datetime

from investment_analyst.application.cli import (
    add_storage_location_arguments,
    storage_location_from_namespace,
)
from investment_analyst.application.facade import InvestmentAnalystApplication
from investment_analyst.application.runtime import ApplicationRuntimeError
from investment_analyst.providers.macro.fred_point_in_time import (
    FredPointInTimeError,
    FredPointInTimeQuery,
)
from investment_analyst.storage import StorageError
from investment_analyst.workspace.service import WorkspaceError


def _parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("dates must use YYYY-MM-DD") from error


def _aware_datetime(value: str) -> datetime:
    normalized = f"{value[:-1]}+00:00" if value.endswith(("Z", "z")) else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as error:
        raise argparse.ArgumentTypeError("known-at must be a valid ISO 8601 datetime") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise argparse.ArgumentTypeError("known-at must include timezone information")
    return parsed.astimezone(UTC)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Reconstruct one local FRED series at an explicit information cut."
    )
    add_storage_location_arguments(parser)
    parser.add_argument("--series-id", required=True)
    parser.add_argument("--known-at", required=True, type=_aware_datetime)
    parser.add_argument("--start", type=_parse_date)
    parser.add_argument("--end", type=_parse_date)
    return parser


def main() -> int:
    """Execute the local point-in-time query and print auditable JSON."""
    args = _parser().parse_args()
    try:
        request = FredPointInTimeQuery(
            series_id=args.series_id,
            known_at=args.known_at,
            observation_start=args.start,
            observation_end=args.end,
        )
        result = InvestmentAnalystApplication.create_default().query_fred_point_in_time(
            request,
            location=storage_location_from_namespace(args),
        )
        print(json.dumps(result.to_json_dict(), indent=2, sort_keys=True))
        return 0
    except (
        ApplicationRuntimeError,
        FredPointInTimeError,
        StorageError,
        ValueError,
        WorkspaceError,
    ) as error:
        print(f"FRED/ALFRED point-in-time query failed: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
