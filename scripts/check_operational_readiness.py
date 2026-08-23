#!/usr/bin/env python3
"""Check durable operational readiness without modifying the workspace."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from investment_analyst.application.operational_readiness import (
    OperationalReadinessDecision,
    OperationalReadinessError,
    OperationalReadinessService,
)

_MAX_MIN_LOCAL_DATES = 366


def _utc_datetime(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise argparse.ArgumentTypeError("since must be an ISO-8601 UTC timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise argparse.ArgumentTypeError("since must include an explicit UTC offset")
    if parsed.utcoffset() != timedelta(0):
        raise argparse.ArgumentTypeError("since must use UTC")
    return parsed.astimezone(UTC)


def _minimum(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("min-local-dates must be an integer") from error
    if not 1 <= parsed <= _MAX_MIN_LOCAL_DATES:
        raise argparse.ArgumentTypeError(
            f"min-local-dates must be between 1 and {_MAX_MIN_LOCAL_DATES}"
        )
    return parsed


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--since", type=_utc_datetime, required=True)
    parser.add_argument("--min-local-dates", type=_minimum, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        report = OperationalReadinessService().check(
            workspace=arguments.workspace,
            since=arguments.since,
            min_local_dates=arguments.min_local_dates,
        )
    except OperationalReadinessError as error:
        print(
            json.dumps(
                {
                    "schema_version": "operational-readiness-error-v1",
                    "error": error.code,
                },
                separators=(",", ":"),
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2
    print(
        json.dumps(
            report.to_json_dict(),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    )
    return 0 if report.decision is OperationalReadinessDecision.PASS else 3


if __name__ == "__main__":
    raise SystemExit(main())
