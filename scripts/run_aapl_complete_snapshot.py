#!/usr/bin/env python3
"""Refresh Apple IEX market analytics and query separate persisted diagnostics."""

import argparse
import json
import os
import sys
from datetime import UTC, date, datetime
from pathlib import Path

from investment_analyst.analytics.aapl_snapshot_models import AaplCompleteSnapshotRequest
from investment_analyst.analytics.aapl_snapshot_pipeline import (
    AaplCompleteSnapshotPipeline,
    AaplCompleteSnapshotPipelineError,
    AaplSnapshotIncompleteError,
)
from investment_analyst.analytics.consolidated_diagnostic_service import (
    AaplConsolidatedDiagnosticService,
)
from investment_analyst.analytics.market.diagnostic_pipeline import MarketDiagnosticPipeline
from investment_analyst.analytics.market.diagnostic_rules import MarketDiagnosticEngine
from investment_analyst.analytics.market.diagnostic_selection import (
    MarketDiagnosticMetricSelector,
)
from investment_analyst.analytics.market.history_service import HistoricalMarketDataService
from investment_analyst.analytics.market.statistics_engine import MarketStatisticsEngine
from investment_analyst.analytics.market.statistics_pipeline import MarketStatisticsPipeline
from investment_analyst.catalog.provider_configuration import (
    resolve_alpaca_configuration,
)
from investment_analyst.catalog.provider_context import ProviderAssetContextResolver
from investment_analyst.catalog.service import AssetCatalogService
from investment_analyst.core.models import DataFrequency
from investment_analyst.providers.http import UrlLibHttpTransport
from investment_analyst.providers.market.alpaca_pipeline import AlpacaHistoricalPipeline
from investment_analyst.providers.market.alpaca_stock import AlpacaCredentials, AlpacaStockClient
from investment_analyst.storage import LocalStorage, StoragePaths

_NOTICE = (
    "Local auditable Apple run using Alpaca Market Data IEX. IEX is a single-exchange "
    "feed and is not equivalent to consolidated SIP coverage. Fundamental diagnostics are "
    "reused from local point-in-time data; market and fundamentals remain separate, no "
    "combined score is produced, no recommendation is made, and no orders are executed."
)


def _date_value(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("dates must use YYYY-MM-DD") from error


def _aware_datetime(value: str) -> datetime:
    normalized = f"{value[:-1]}+00:00" if value.endswith(("Z", "z")) else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as error:
        raise argparse.ArgumentTypeError("known-at must be valid ISO 8601") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise argparse.ArgumentTypeError("known-at must include timezone information")
    return parsed.astimezone(UTC)


def _frequency(value: str) -> DataFrequency:
    mapping = {
        "annual": DataFrequency.ANNUAL,
        "quarterly": DataFrequency.QUARTERLY,
    }
    try:
        return mapping[value.lower()]
    except KeyError as error:
        raise argparse.ArgumentTypeError("frequency must be annual or quarterly") from error


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--known-at", required=True, type=_aware_datetime)
    parser.add_argument("--market-start", required=True, type=_date_value)
    parser.add_argument("--market-end", required=True, type=_date_value)
    parser.add_argument("--fundamental-frequency", required=True, type=_frequency)
    parser.add_argument("--market-as-of", type=_date_value)
    parser.add_argument("--fundamental-as-of", type=_date_value)
    parser.add_argument("--require-complete", action="store_true")
    return parser


def _payload(summary) -> dict[str, object]:
    serialized = summary.to_json_dict()
    return {
        "notice": _NOTICE,
        "request": serialized["request"],
        "source": {
            "provider": serialized["source"],
            "feed": serialized["feed"],
            "coverage": "IEX single-exchange market-data feed; not consolidated SIP",
        },
        "stages": serialized["stages"],
        "consolidated": serialized["consolidated"],
        "summary": {
            "overall_status": serialized["overall_status"],
            "started_at": serialized["started_at"],
            "completed_at": serialized["completed_at"],
            "stages_completed": serialized["stages_completed"],
            "stages_reused": serialized["stages_reused"],
            "stages_skipped": serialized["stages_skipped"],
            "traceability_verified": serialized["traceability_verified"],
        },
    }


def _build_pipeline(storage: LocalStorage, credentials: AlpacaCredentials):
    catalog = AssetCatalogService.load_default()
    resolver = ProviderAssetContextResolver(catalog)
    configuration = resolve_alpaca_configuration(resolver)
    transport = UrlLibHttpTransport()
    client = AlpacaStockClient(transport, credentials)
    market_pipeline = AlpacaHistoricalPipeline(
        storage,
        client,
        configuration=configuration,
    )
    history = HistoricalMarketDataService(storage)
    statistics_pipeline = MarketStatisticsPipeline(
        storage,
        history,
        MarketStatisticsEngine(),
    )
    selector = MarketDiagnosticMetricSelector(storage)
    diagnostic_pipeline = MarketDiagnosticPipeline(
        storage,
        selector,
        MarketDiagnosticEngine(),
    )
    consolidated = AaplConsolidatedDiagnosticService(storage)
    return AaplCompleteSnapshotPipeline(
        storage,
        market_pipeline,
        statistics_pipeline,
        diagnostic_pipeline,
        consolidated,
    )


def main() -> int:
    """Build existing components once, execute the orchestrator, and print compact JSON."""
    arguments = _parser().parse_args()
    api_key = os.environ.get("ALPACA_API_KEY", "")
    secret_key = os.environ.get("ALPACA_API_SECRET", "")
    if not api_key.strip() or not secret_key.strip():
        print(
            "ALPACA_API_KEY and ALPACA_API_SECRET are required and were not provided.",
            file=sys.stderr,
        )
        return 2

    try:
        request = AaplCompleteSnapshotRequest(
            known_at=arguments.known_at,
            market_start=arguments.market_start,
            market_end=arguments.market_end,
            fundamental_frequency=arguments.fundamental_frequency,
            market_as_of=arguments.market_as_of,
            fundamental_as_of=arguments.fundamental_as_of,
            require_complete=arguments.require_complete,
        )
        credentials = AlpacaCredentials(api_key=api_key, secret_key=secret_key)
        with LocalStorage(StoragePaths.from_root(arguments.root)) as storage:
            summary = _build_pipeline(storage, credentials).run(request)
        print(json.dumps(_payload(summary), indent=2, sort_keys=True))
        return 0
    except AaplSnapshotIncompleteError as error:
        payload = _payload(error.summary)
        payload["error"] = str(error)
        print(json.dumps(payload, indent=2, sort_keys=True))
        print(f"Apple snapshot incomplete: {error}", file=sys.stderr)
        return 3
    except (AaplCompleteSnapshotPipelineError, ValueError) as error:
        print(f"Apple snapshot failed: {error}", file=sys.stderr)
        return 2
    except Exception as error:  # noqa: BLE001
        print(f"Apple snapshot failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
