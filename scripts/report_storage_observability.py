#!/usr/bin/env python3
"""Report persisted storage observability growth without writing to the workspace."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

from investment_analyst.application.runtime import ApplicationRuntime
from investment_analyst.application.storage_observability import StorageObservabilityError
from investment_analyst.application.storage_observability_report import (
    DEFAULT_BUDGET_BYTES_PER_DAY,
    StorageObservabilityReportError,
    StorageObservabilityReportService,
)

_EXIT_BUDGET_EXCEEDED = 3
_EXIT_INVALID = 2


def _as_of(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("as-of must be an ISO-8601 UTC date") from error


def _budget(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("budget-bytes-per-day must be an integer") from error
    if parsed < 0:
        raise argparse.ArgumentTypeError("budget-bytes-per-day must not be negative")
    return parsed


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--as-of", type=_as_of, default=None)
    parser.add_argument(
        "--budget-bytes-per-day", type=_budget, default=DEFAULT_BUDGET_BYTES_PER_DAY
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        runtime = ApplicationRuntime.create_default()
        paths = runtime.workspace_service.resolve(arguments.workspace)
        if not paths.root.is_dir():
            raise StorageObservabilityReportError("workspace_path_invalid")
        report = StorageObservabilityReportService(
            state_root=paths.state_root,
            budget_bytes_per_day=arguments.budget_bytes_per_day,
        ).report(as_of=arguments.as_of)
    except (StorageObservabilityReportError, StorageObservabilityError) as error:
        print(
            json.dumps(
                {
                    "schema_version": "storage-observability-report-error-v1",
                    "error": str(error),
                },
                separators=(",", ":"),
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return _EXIT_INVALID
    print(
        json.dumps(
            report.to_json_dict(),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    )
    if report.budget is not None and report.budget.exceeded:
        return _EXIT_BUDGET_EXCEEDED
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
