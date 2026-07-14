#!/usr/bin/env python3
"""Compute and persist descriptive statistics from locally stored market bars."""

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from investment_analyst.analytics.market.bar_models import HistoricalBarQuery
from investment_analyst.analytics.market.history_service import (
    HistoricalMarketDataService,
    MarketHistoryError,
)
from investment_analyst.analytics.market.statistics_engine import (
    MarketStatisticsEngine,
    MarketStatisticsError,
)
from investment_analyst.analytics.market.statistics_models import MarketStatisticsRequest
from investment_analyst.analytics.market.statistics_pipeline import (
    MarketStatisticsPipeline,
    MarketStatisticsPipelineError,
)
from investment_analyst.storage import LocalStorage, StoragePaths
from investment_analyst.storage.errors import StorageError

_NOTICE = (
    "Descriptive historical statistics for the selected source only; no orders are executed "
    "and this output is not financial advice."
)
_METRIC_KEYS = {
    "market.history.simple_return_1d",
    "market.history.sma",
    "market.history.rolling_daily_volatility",
    "market.history.relative_volume",
}


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
        raise argparse.ArgumentTypeError("known-at must be valid ISO 8601") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise argparse.ArgumentTypeError("known-at must include timezone information")
    return parsed.astimezone(UTC)


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("expected an integer") from error
    if parsed <= 0:
        raise argparse.ArgumentTypeError("value must be positive")
    return parsed


def _volatility_window(value: str) -> int:
    parsed = _positive_int(value)
    if parsed < 2:
        raise argparse.ArgumentTypeError("volatility window must be at least 2")
    return parsed


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Compute auditable historical market statistics.")
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--asset-id", required=True)
    parser.add_argument("--source-id", required=True)
    parser.add_argument("--start", required=True, type=_date_utc)
    parser.add_argument("--end", required=True, type=_date_utc)
    parser.add_argument("--known-at", required=True, type=_aware_datetime)
    parser.add_argument("--sma-window", action="append", type=_positive_int)
    parser.add_argument("--volatility-window", type=_volatility_window, default=20)
    parser.add_argument("--relative-volume-window", type=_positive_int, default=20)
    parser.add_argument("--output-limit", type=_positive_int, default=20)
    return parser


def _matches_request(result, request: MarketStatisticsRequest) -> bool:
    if result.metric_key not in _METRIC_KEYS:
        return False
    if result.parameters.get("source_id") != request.query.source_id:
        return False
    if result.parameters.get("known_at") != request.query.known_at.isoformat():
        return False
    if not request.query.start <= result.as_of < request.query.end:
        return False
    if result.metric_key == "market.history.sma":
        return result.parameters.get("window") in request.sma_windows
    if result.metric_key == "market.history.rolling_daily_volatility":
        return result.parameters.get("window") == request.volatility_window
    if result.metric_key == "market.history.relative_volume":
        return result.parameters.get("window") == request.relative_volume_window
    return True


def main() -> int:
    """Run the local statistics pipeline and print a bounded JSON result."""
    args = _parser().parse_args()
    try:
        query = HistoricalBarQuery(
            asset_id=args.asset_id,
            source_id=args.source_id,
            start=args.start,
            end=args.end,
            known_at=args.known_at,
        )
        request = MarketStatisticsRequest(
            query=query,
            sma_windows=tuple(args.sma_window or (5, 20)),
            volatility_window=args.volatility_window,
            relative_volume_window=args.relative_volume_window,
        )
        with LocalStorage(StoragePaths.from_root(args.root)) as storage:
            history = HistoricalMarketDataService(storage)
            pipeline = MarketStatisticsPipeline(storage, history, MarketStatisticsEngine())
            summary = pipeline.run(request)
            results = [
                item
                for item in storage.metric_results.list(asset_id=request.query.asset_id)
                if _matches_request(item, request)
            ]
        results.sort(
            key=lambda item: (
                item.as_of,
                item.metric_key,
                json.dumps(item.parameters, separators=(",", ":"), sort_keys=True),
            )
        )
        displayed = results[: args.output_limit]
        payload = {
            "notice": _NOTICE,
            "summary": summary.to_json_dict(),
            "results": [item.model_dump(mode="json") for item in displayed],
            "truncated": len(displayed) < len(results),
        }
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    except (
        MarketHistoryError,
        MarketStatisticsError,
        MarketStatisticsPipelineError,
        StorageError,
        ValueError,
    ) as error:
        print(f"market statistics failed: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
