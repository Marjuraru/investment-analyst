#!/usr/bin/env python3
"""Import public Coinbase BTC-USD daily candles into local storage."""

import argparse
import json
import sys
from datetime import UTC, date, datetime, time
from pathlib import Path

from investment_analyst.providers.crypto.coinbase_exchange import CoinbaseExchangeClient
from investment_analyst.providers.crypto.coinbase_pipeline import CoinbaseHistoricalPipeline
from investment_analyst.providers.http import UrlLibHttpTransport
from investment_analyst.storage import LocalStorage, StoragePaths


def _parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("dates must use YYYY-MM-DD") from error


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Import public Coinbase Exchange BTC-USD daily market data."
    )
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--start", required=True, type=_parse_date)
    parser.add_argument("--end", required=True, type=_parse_date)
    return parser


def main() -> int:
    """Run the importer and print one readable JSON document."""
    arguments = _build_parser().parse_args()
    start = datetime.combine(arguments.start, time.min, tzinfo=UTC)
    end = datetime.combine(arguments.end, time.min, tzinfo=UTC)
    if start >= end:
        print("error: --start must be earlier than --end", file=sys.stderr)
        return 2
    if end > datetime.now(UTC):
        print("error: --end must not be in the future", file=sys.stderr)
        return 2

    try:
        paths = StoragePaths.from_root(arguments.root)
        client = CoinbaseExchangeClient(UrlLibHttpTransport())
        with LocalStorage(paths) as storage:
            summary = CoinbaseHistoricalPipeline(storage, client).run(start, end)
        output = {
            "notice": (
                "Market-data analysis only. This command does not execute trades and does not "
                "constitute financial advice."
            ),
            "summary": summary.to_json_dict(),
        }
        print(json.dumps(output, indent=2, sort_keys=True))
    except Exception as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
