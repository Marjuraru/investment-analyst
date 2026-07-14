#!/usr/bin/env python3
"""Compute and persist Apple SEC fundamental metrics from local observations."""

import argparse
import json
import sys
from datetime import UTC, date, datetime
from pathlib import Path

from investment_analyst.core.models import DataFrequency
from investment_analyst.providers.fundamentals.sec_metric_engine import (
    SecFundamentalMetricEngine,
    SecFundamentalMetricError,
)
from investment_analyst.providers.fundamentals.sec_metric_models import (
    SecFundamentalMetricRequest,
)
from investment_analyst.providers.fundamentals.sec_metric_pipeline import (
    SecAaplFundamentalMetricPipeline,
    SecFundamentalMetricPipelineError,
)
from investment_analyst.providers.fundamentals.sec_point_in_time_service import (
    SecAaplFundamentalPointInTimeService,
    SecFundamentalQueryError,
)
from investment_analyst.storage import LocalStorage, StorageError, StoragePaths

_NOTICE = (
    "Fundamental ratios computed from local Apple SEC observations using point-in-time known_at "
    "selection and Decimal arithmetic. Ratios are not probabilities or recommendations, and no "
    "operations are executed."
)


def _aware_datetime(value: str) -> datetime:
    normalized = f"{value[:-1]}+00:00" if value.endswith(("Z", "z")) else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as error:
        raise argparse.ArgumentTypeError("known-at must be valid ISO 8601") from error
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
    if not 1 <= parsed <= 100:
        raise argparse.ArgumentTypeError("limit must be between 1 and 100")
    return parsed


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Compute auditable Apple fundamental metrics from local SEC observations."
    )
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--known-at", required=True, type=_aware_datetime)
    parser.add_argument("--frequency", required=True, type=_frequency)
    parser.add_argument("--start", type=_date_value)
    parser.add_argument("--end", type=_date_value)
    parser.add_argument("--limit", type=_limit)
    return parser


def main() -> int:
    """Run the local fundamental metric pipeline and print a compact summary."""
    args = _parser().parse_args()
    try:
        request = SecFundamentalMetricRequest(
            known_at=args.known_at,
            frequency=args.frequency,
            start_period_end=args.start,
            end_period_end=args.end,
            limit=args.limit,
        )
        with LocalStorage(StoragePaths.from_root(args.root)) as storage:
            service = SecAaplFundamentalPointInTimeService(storage)
            pipeline = SecAaplFundamentalMetricPipeline(
                storage,
                service,
                SecFundamentalMetricEngine(),
            )
            summary = pipeline.run(request)
        print(
            json.dumps(
                {"notice": _NOTICE, "summary": summary.to_json_dict()},
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    except (
        SecFundamentalMetricError,
        SecFundamentalMetricPipelineError,
        SecFundamentalQueryError,
        StorageError,
        ValueError,
    ) as error:
        print(f"SEC fundamental metrics failed: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
