#!/usr/bin/env python3
"""Fetch Apple IEX daily bars into an explicit local storage root."""

import argparse
import json
import os
import sys
from datetime import UTC, date, datetime
from pathlib import Path

from investment_analyst.catalog.provider_configuration import (
    resolve_alpaca_configuration,
)
from investment_analyst.catalog.provider_context import ProviderAssetContextResolver
from investment_analyst.catalog.service import AssetCatalogService
from investment_analyst.providers.http import UrlLibHttpTransport
from investment_analyst.providers.market.alpaca_pipeline import AlpacaHistoricalPipeline
from investment_analyst.providers.market.alpaca_stock import (
    AlpacaCredentials,
    AlpacaStockClient,
)
from investment_analyst.storage import LocalStorage, StoragePaths


def _parse_date(value: str) -> datetime:
    try:
        parsed = date.fromisoformat(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("dates must use YYYY-MM-DD") from error
    return datetime(parsed.year, parsed.month, parsed.day, tzinfo=UTC)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--start", required=True, type=_parse_date)
    parser.add_argument("--end", required=True, type=_parse_date)
    return parser


def main() -> int:
    """Run the read-only Alpaca Market Data import and print one JSON document."""
    arguments = _parser().parse_args()
    api_key = os.environ.get("ALPACA_API_KEY", "")
    secret_key = os.environ.get("ALPACA_API_SECRET", "")
    if not api_key.strip() or not secret_key.strip():
        print(
            "ALPACA_API_KEY and ALPACA_API_SECRET are required and were not provided.",
            file=sys.stderr,
        )
        return 2
    if arguments.start >= arguments.end:
        print("--start must be earlier than the exclusive --end date.", file=sys.stderr)
        return 2
    if arguments.end > datetime.now(UTC):
        print("--end must not be in the future.", file=sys.stderr)
        return 2

    try:
        catalog = AssetCatalogService.load_default()
        resolver = ProviderAssetContextResolver(catalog)
        configuration = resolve_alpaca_configuration(resolver)
        credentials = AlpacaCredentials(api_key=api_key, secret_key=secret_key)
        client = AlpacaStockClient(UrlLibHttpTransport(), credentials)
        with LocalStorage(StoragePaths.from_root(arguments.root)) as storage:
            summary = AlpacaHistoricalPipeline(
                storage,
                client,
                configuration=configuration,
            ).run(
                arguments.start,
                arguments.end,
            )
        output = {
            "notice": (
                "Data analysis only. Uses the partial Alpaca IEX feed, executes no orders, "
                "and is not financial advice."
            ),
            "summary": summary.to_json_dict(),
        }
        print(json.dumps(output, indent=2, sort_keys=True))
        return 0
    except Exception as error:  # noqa: BLE001
        print(f"Alpaca history import failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
