#!/usr/bin/env python3
"""Run an explicit, local read-only smoke of the coverage query."""

import argparse
from datetime import UTC, date, datetime
from pathlib import Path

from investment_analyst.application.runtime import StorageLocationRequest
from investment_analyst.application.universe_coverage import UniverseCoverageApplication
from investment_analyst.application.universe_coverage_models import UniverseCoverageRequest


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Smoke the local universe coverage query.")
    parser.add_argument("--workspace", required=True, type=Path)
    parser.add_argument("--known-at", required=True)
    return parser


def _known_at(value: str) -> datetime:
    parsed = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("known-at must include timezone information")
    return parsed.astimezone(UTC)


def main() -> int:
    """Exercise all configured market assets without mutating the workspace."""
    args = _parser().parse_args()
    known_at = _known_at(args.known_at)
    request = UniverseCoverageRequest(
        known_at=known_at,
        market_start=date(2026, 8, 1),
        market_end=date(2026, 8, 28),
        fundamental_start=date(2020, 1, 1),
        fundamental_end=date(2026, 8, 28),
    )
    result = UniverseCoverageApplication.create_default().query(
        StorageLocationRequest(workspace=args.workspace),
        request,
    )
    if len(result.assets) != 31:
        raise RuntimeError("coverage smoke did not resolve the complete daily universe")
    print(f"universe coverage smoke: {len(result.assets)} configured assets")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
