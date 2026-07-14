#!/usr/bin/env python3
"""Compute one diagnostic from already persisted market statistics."""

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from investment_analyst.analytics.market.bar_models import HistoricalBarQuery
from investment_analyst.analytics.market.diagnostic_models import MarketDiagnosticRequest
from investment_analyst.analytics.market.diagnostic_pipeline import (
    MarketDiagnosticPipeline,
    MarketDiagnosticPipelineError,
)
from investment_analyst.analytics.market.diagnostic_rules import (
    MarketDiagnosticEngine,
    MarketDiagnosticRuleError,
    diagnostic_result_id,
)
from investment_analyst.analytics.market.diagnostic_selection import (
    MarketDiagnosticMetricSelector,
    MarketDiagnosticSelectionError,
)
from investment_analyst.storage import LocalStorage, StoragePaths
from investment_analyst.storage.errors import StorageError

_NOTICE = (
    "Descriptive market diagnostic from persisted metrics only. Context confidence is not a "
    "probability; no operations are executed and this output is not financial advice."
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
    parser = argparse.ArgumentParser(
        description="Compute an auditable market diagnostic from persisted statistics."
    )
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--asset-id", required=True)
    parser.add_argument("--source-id", required=True)
    parser.add_argument("--start", required=True, type=_date_utc)
    parser.add_argument("--end", required=True, type=_date_utc)
    parser.add_argument("--known-at", required=True, type=_aware_datetime)
    parser.add_argument("--short-sma-window", type=_positive_int, default=5)
    parser.add_argument("--long-sma-window", type=_positive_int, default=20)
    parser.add_argument("--volatility-window", type=_volatility_window, default=20)
    parser.add_argument("--relative-volume-window", type=_positive_int, default=20)
    return parser


def main() -> int:
    """Run the local diagnostic pipeline and print one JSON document."""
    args = _parser().parse_args()
    try:
        request = MarketDiagnosticRequest(
            query=HistoricalBarQuery(
                asset_id=args.asset_id,
                source_id=args.source_id,
                start=args.start,
                end=args.end,
                known_at=args.known_at,
            ),
            short_sma_window=args.short_sma_window,
            long_sma_window=args.long_sma_window,
            volatility_window=args.volatility_window,
            relative_volume_window=args.relative_volume_window,
        )
        with LocalStorage(StoragePaths.from_root(args.root)) as storage:
            selector = MarketDiagnosticMetricSelector(storage)
            pipeline = MarketDiagnosticPipeline(storage, selector, MarketDiagnosticEngine())
            summary = pipeline.run(request)
            identifier = diagnostic_result_id(
                request,
                selected_metric_result_ids=summary.selected_metric_result_ids,
                missing_requirements=summary.missing_requirements,
                final_score=summary.final_score,
                confidence=summary.confidence,
                verdict=summary.verdict,
                quality=summary.quality,
            )
            diagnostic = storage.diagnostics.get(identifier)
        payload = {
            "notice": _NOTICE,
            "summary": summary.to_json_dict(),
            "diagnostic": diagnostic.model_dump(mode="json"),
        }
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    except (
        MarketDiagnosticSelectionError,
        MarketDiagnosticRuleError,
        MarketDiagnosticPipelineError,
        StorageError,
        ValueError,
    ) as error:
        print(f"market diagnostic failed: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
