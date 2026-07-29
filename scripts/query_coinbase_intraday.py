#!/usr/bin/env python3
"""Query and aggregate stored Coinbase one-minute bars without provider access."""

import argparse
import json
import sys
from datetime import UTC, datetime, timedelta

from investment_analyst.analytics.market.bar_models import HistoricalBarQuery
from investment_analyst.analytics.market.history_service import (
    HistoricalMarketDataService,
    MarketHistoryError,
)
from investment_analyst.analytics.market.intraday_models import (
    IntradayAggregationRequest,
    IntradayInterval,
)
from investment_analyst.analytics.market.intraday_service import (
    IntradayAggregationError,
    IntradayAggregationService,
)
from investment_analyst.application.cli import (
    add_storage_location_arguments,
    storage_location_from_namespace,
)
from investment_analyst.application.runtime import (
    ApplicationRuntime,
    ApplicationRuntimeError,
)
from investment_analyst.providers.crypto.coinbase_intraday_normalizer import SOURCE_ID
from investment_analyst.providers.crypto.coinbase_normalizer import ASSET_ID
from investment_analyst.storage import StorageError
from investment_analyst.workspace.models import WorkspaceAccessMode
from investment_analyst.workspace.service import WorkspaceError

MAX_QUERY_DURATION = timedelta(days=7)


def _aware_datetime(value: str) -> datetime:
    normalized = f"{value[:-1]}+00:00" if value.endswith(("Z", "z")) else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as error:
        raise argparse.ArgumentTypeError("expected an ISO 8601 datetime") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise argparse.ArgumentTypeError("datetime must include timezone information")
    return parsed.astimezone(UTC)


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("limit must be an integer") from error
    if parsed <= 0:
        raise argparse.ArgumentTypeError("limit must be positive")
    return parsed


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Aggregate stored BTC-USD minute bars into fixed UTC intervals."
    )
    add_storage_location_arguments(parser)
    parser.add_argument("--start", required=True, type=_aware_datetime)
    parser.add_argument("--end", required=True, type=_aware_datetime)
    parser.add_argument("--known-at", required=True, type=_aware_datetime)
    parser.add_argument(
        "--interval",
        required=True,
        choices=tuple(interval.value for interval in IntradayInterval),
    )
    parser.add_argument("--limit", type=_positive_int, default=100)
    return parser


def main() -> int:
    """Reconstruct point-in-time minute evidence and aggregate it locally."""
    arguments = _build_parser().parse_args()
    if arguments.start >= arguments.end:
        print("error: --start must be earlier than --end", file=sys.stderr)
        return 2
    if arguments.end - arguments.start > MAX_QUERY_DURATION:
        print("error: one intraday query may cover at most 7 days", file=sys.stderr)
        return 2

    try:
        query = HistoricalBarQuery(
            asset_id=ASSET_ID,
            source_id=SOURCE_ID,
            start=arguments.start,
            end=arguments.end,
            known_at=arguments.known_at,
        )
        request = IntradayAggregationRequest(
            query=query,
            interval=IntradayInterval(arguments.interval),
        )
        runtime = ApplicationRuntime.create_default()
        with runtime.open_storage(
            storage_location_from_namespace(arguments),
            access_mode=WorkspaceAccessMode.READ_ONLY,
        ) as storage:
            minute_series = HistoricalMarketDataService(storage).query(query)
        result = IntradayAggregationService().aggregate(minute_series, request)
        displayed = result.bars[: arguments.limit]
        print(
            json.dumps(
                {
                    "notice": (
                        "Local descriptive market analysis only. No orders are executed and "
                        "this is not financial advice."
                    ),
                    "request": result.request.model_dump(mode="json"),
                    "source_bar_count": result.source_bar_count,
                    "complete_interval_count": result.complete_interval_count,
                    "incomplete_interval_count": result.incomplete_interval_count,
                    "bars": [bar.model_dump(mode="json") for bar in displayed],
                    "truncated": len(displayed) < len(result.bars),
                    "traceability_verified": result.traceability_verified,
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    except (
        ApplicationRuntimeError,
        IntradayAggregationError,
        MarketHistoryError,
        StorageError,
        ValueError,
        WorkspaceError,
    ) as error:
        print(f"Coinbase intraday query failed: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
