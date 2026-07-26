#!/usr/bin/env python3
"""Import a bounded Coinbase BTC-USD one-minute range into local storage."""

import argparse
import json
import sys
from datetime import UTC, datetime, timedelta

from investment_analyst.application.cli import (
    add_storage_location_arguments,
    storage_location_from_namespace,
)
from investment_analyst.application.runtime import (
    ApplicationRuntime,
    ApplicationRuntimeError,
)
from investment_analyst.catalog.provider_configuration import (
    resolve_coinbase_intraday_configuration,
)
from investment_analyst.providers.crypto.coinbase_exchange import CoinbaseExchangeClient
from investment_analyst.providers.crypto.coinbase_pipeline import CoinbaseIntradayPipeline
from investment_analyst.providers.http import UrlLibHttpTransport
from investment_analyst.storage import StorageError
from investment_analyst.workspace.models import WorkspaceAccessMode
from investment_analyst.workspace.service import WorkspaceError

MAX_IMPORT_DURATION = timedelta(hours=24)


def _aware_minute(value: str) -> datetime:
    normalized = f"{value[:-1]}+00:00" if value.endswith(("Z", "z")) else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as error:
        raise argparse.ArgumentTypeError("expected an ISO 8601 datetime") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise argparse.ArgumentTypeError("datetime must include timezone information")
    utc_value = parsed.astimezone(UTC)
    if utc_value.second != 0 or utc_value.microsecond != 0:
        raise argparse.ArgumentTypeError("datetime must align to a whole UTC minute")
    return utc_value


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Import a bounded Coinbase Exchange BTC-USD one-minute range."
    )
    add_storage_location_arguments(parser)
    parser.add_argument("--start", required=True, type=_aware_minute)
    parser.add_argument("--end", required=True, type=_aware_minute)
    return parser


def main() -> int:
    """Run the append-only minute importer and print an auditable summary."""
    arguments = _build_parser().parse_args()
    if arguments.start >= arguments.end:
        print("error: --start must be earlier than --end", file=sys.stderr)
        return 2
    if arguments.end - arguments.start > MAX_IMPORT_DURATION:
        print("error: one intraday import may cover at most 24 hours", file=sys.stderr)
        return 2
    if arguments.end > datetime.now(UTC):
        print("error: --end must not be in the future", file=sys.stderr)
        return 2

    try:
        runtime = ApplicationRuntime.create_default()
        configuration = resolve_coinbase_intraday_configuration(runtime.provider_resolver)
        client = CoinbaseExchangeClient(UrlLibHttpTransport())
        with runtime.open_storage(
            storage_location_from_namespace(arguments),
            access_mode=WorkspaceAccessMode.READ_WRITE,
        ) as storage:
            summary = CoinbaseIntradayPipeline(
                storage,
                client,
                configuration=configuration,
            ).run(arguments.start, arguments.end)
        print(
            json.dumps(
                {
                    "notice": (
                        "Market-data collection only. No orders are executed and this is not "
                        "financial advice."
                    ),
                    "interval": "1m",
                    "summary": summary.to_json_dict(),
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    except (ApplicationRuntimeError, StorageError, WorkspaceError) as error:
        print(f"Coinbase intraday import failed: {error}", file=sys.stderr)
        return 1
    except Exception as error:
        print(f"Coinbase intraday import failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
