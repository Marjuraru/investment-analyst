#!/usr/bin/env python3
"""Query stored market history without contacting providers."""

import argparse
import json
import sys
from datetime import UTC, datetime

from investment_analyst.analytics.market.bar_models import HistoricalBarQuery
from investment_analyst.analytics.market.bar_schemas import get_market_bar_schema
from investment_analyst.analytics.market.history_service import (
    HistoricalMarketDataService,
    MarketHistoryError,
)
from investment_analyst.application.cli import (
    add_storage_location_arguments,
    storage_location_from_namespace,
)
from investment_analyst.application.runtime import (
    ApplicationRuntime,
    ApplicationRuntimeError,
)
from investment_analyst.storage.errors import StorageError
from investment_analyst.workspace.models import WorkspaceAccessMode
from investment_analyst.workspace.service import WorkspaceError

_NOTICE = (
    "Analytical query of locally stored data only; no orders are executed and this is not "
    "financial advice."
)


def _date_utc(value: str) -> datetime:
    try:
        return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=UTC)
    except ValueError as error:
        raise argparse.ArgumentTypeError("expected date in YYYY-MM-DD format") from error


def _aware_datetime(value: str) -> datetime:
    normalized = f"{value[:-1]}+00:00" if value.endswith(("Z", "z")) else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as error:
        raise argparse.ArgumentTypeError("known-at must be a valid ISO 8601 datetime") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise argparse.ArgumentTypeError("known-at must include timezone information")
    return parsed.astimezone(UTC)


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("limit must be an integer") from error
    if parsed <= 0:
        raise argparse.ArgumentTypeError("limit must be positive")
    return parsed


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Query auditable stored daily market bars.")
    add_storage_location_arguments(parser)
    parser.add_argument("--asset-id", required=True)
    parser.add_argument("--source-id", required=True)
    parser.add_argument("--start", required=True, type=_date_utc)
    parser.add_argument("--end", required=True, type=_date_utc)
    parser.add_argument("--known-at", required=True, type=_aware_datetime)
    parser.add_argument("--limit", type=_positive_int, default=50)
    return parser


def main() -> int:
    """Execute the local historical query and print a bounded JSON result."""
    args = _parser().parse_args()
    try:
        query = HistoricalBarQuery(
            asset_id=args.asset_id,
            source_id=args.source_id,
            start=args.start,
            end=args.end,
            known_at=args.known_at,
        )
        get_market_bar_schema(query.source_id)
        runtime = ApplicationRuntime.create_default()
        with runtime.open_storage(
            storage_location_from_namespace(args),
            access_mode=WorkspaceAccessMode.READ_ONLY,
        ) as storage:
            series = HistoricalMarketDataService(storage).query(query)
        displayed = series.bars[: args.limit]
        payload = {
            "notice": _NOTICE,
            "query": series.query.model_dump(mode="json"),
            "coverage": series.coverage.model_dump(mode="json"),
            "bars": [bar.model_dump(mode="json") for bar in displayed],
            "truncated": len(displayed) < len(series.bars),
            "traceability_verified": series.traceability_verified,
        }
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    except (
        ApplicationRuntimeError,
        MarketHistoryError,
        StorageError,
        ValueError,
        WorkspaceError,
    ) as error:
        print(f"market history query failed: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
