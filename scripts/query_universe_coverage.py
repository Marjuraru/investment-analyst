#!/usr/bin/env python3
"""Query catalog coverage and local evidence without contacting providers."""

import argparse
import json
import sys
from datetime import UTC, date, datetime
from pathlib import Path

from investment_analyst.application.runtime import StorageLocationRequest
from investment_analyst.application.universe_coverage import UniverseCoverageApplication
from investment_analyst.application.universe_coverage_models import UniverseCoverageRequest
from investment_analyst.storage.errors import StorageError
from investment_analyst.workspace.service import WorkspaceError


def _date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("expected YYYY-MM-DD") from error


def _known_at(value: str) -> datetime:
    normalized = f"{value[:-1]}+00:00" if value.endswith(("Z", "z")) else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as error:
        raise argparse.ArgumentTypeError("known-at must be ISO 8601") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise argparse.ArgumentTypeError("known-at must include timezone information")
    return parsed.astimezone(UTC)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Query local universe coverage.")
    parser.add_argument("--workspace", required=True, type=Path)
    parser.add_argument("--known-at", required=True, type=_known_at)
    parser.add_argument("--market-start", required=True, type=_date)
    parser.add_argument("--market-end", required=True, type=_date)
    parser.add_argument("--fundamental-start", required=True, type=_date)
    parser.add_argument("--fundamental-end", required=True, type=_date)
    parser.add_argument("--frequency", choices=("annual", "quarterly"), default="annual")
    parser.add_argument("--asset-id", action="append", default=[])
    return parser


def main() -> int:
    """Print one deterministic read-only coverage response."""
    args = _parser().parse_args()
    try:
        request = UniverseCoverageRequest(
            known_at=args.known_at,
            market_start=args.market_start,
            market_end=args.market_end,
            fundamental_start=args.fundamental_start,
            fundamental_end=args.fundamental_end,
            frequency=args.frequency,
            asset_ids=tuple(sorted(args.asset_id)),
        )
        result = UniverseCoverageApplication.create_default().query(
            StorageLocationRequest(workspace=args.workspace),
            request,
        )
        print(json.dumps(result.model_dump(mode="json"), indent=2, sort_keys=True))
        return 0
    except (StorageError, ValueError, WorkspaceError) as error:
        print(f"universe coverage query failed: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
