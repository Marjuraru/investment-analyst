#!/usr/bin/env python3
"""Import one official FRED/ALFRED series vintage into local storage."""

import argparse
import json
import os
import sys
from datetime import date

from investment_analyst.application.cli import (
    add_storage_location_arguments,
    storage_location_from_namespace,
)
from investment_analyst.application.runtime import ApplicationRuntime, ApplicationRuntimeError
from investment_analyst.providers.http import UrlLibHttpTransport
from investment_analyst.providers.macro.fred_alfred import (
    FredAlfredClient,
    FredAlfredError,
    FredApiKey,
)
from investment_analyst.providers.macro.fred_pipeline import FredVintagePipeline
from investment_analyst.storage import StorageError
from investment_analyst.workspace.models import WorkspaceAccessMode
from investment_analyst.workspace.service import WorkspaceError


def _parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("dates must use YYYY-MM-DD") from error


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Import one FRED series snapshot using an explicit ALFRED vintage date."
    )
    add_storage_location_arguments(parser)
    parser.add_argument("--series-id", required=True)
    parser.add_argument("--vintage-date", required=True, type=_parse_date)
    parser.add_argument("--start", required=True, type=_parse_date)
    parser.add_argument("--end", required=True, type=_parse_date)
    return parser


def main() -> int:
    """Fetch, persist, and print one secret-free import summary."""
    args = _parser().parse_args()
    api_key = os.environ.get("FRED_API_KEY")
    if api_key is None:
        print("error: FRED_API_KEY is required", file=sys.stderr)
        return 2
    try:
        runtime = ApplicationRuntime.create_default()
        client = FredAlfredClient(UrlLibHttpTransport(), FredApiKey(api_key))
        with runtime.open_storage(
            storage_location_from_namespace(args),
            access_mode=WorkspaceAccessMode.READ_WRITE,
        ) as storage:
            summary = FredVintagePipeline(storage, client).run(
                args.series_id,
                vintage_date=args.vintage_date,
                observation_start=args.start,
                observation_end=args.end,
            )
        print(
            json.dumps(
                {
                    "notice": (
                        "Datos macro descriptivos y point-in-time; no se generan órdenes ni "
                        "recomendaciones de inversión."
                    ),
                    "summary": summary.to_json_dict(),
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    except (
        ApplicationRuntimeError,
        FredAlfredError,
        StorageError,
        ValueError,
        WorkspaceError,
    ) as error:
        print(f"FRED/ALFRED import failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
