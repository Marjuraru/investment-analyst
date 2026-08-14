#!/usr/bin/env python3
"""Refresca evidencia descriptiva de derivados Deribit para BTC o ETH."""

import argparse
import json
import sys
from datetime import UTC, date, datetime

from investment_analyst.application.cli import (
    add_storage_location_arguments,
    storage_location_from_namespace,
)
from investment_analyst.application.crypto_derivatives import CryptoDerivativesRefreshError
from investment_analyst.application.crypto_derivatives_models import (
    CryptoDerivativesRefreshMode,
    CryptoDerivativesRefreshRequest,
)
from investment_analyst.application.facade import InvestmentAnalystApplication
from investment_analyst.application.runtime import ApplicationRuntimeError
from investment_analyst.catalog.service import AssetCatalogError
from investment_analyst.providers.crypto.deribit import DeribitError
from investment_analyst.storage import StorageError
from investment_analyst.workspace.service import WorkspaceError


def _date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("date must use YYYY-MM-DD") from error


def _aware_datetime(value: str) -> datetime:
    normalized = f"{value[:-1]}+00:00" if value.endswith(("Z", "z")) else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as error:
        raise argparse.ArgumentTypeError("known-at must be valid ISO 8601") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise argparse.ArgumentTypeError("known-at must include timezone information")
    return parsed.astimezone(UTC)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    add_storage_location_arguments(parser)
    parser.add_argument("--asset-id", required=True)
    parser.add_argument("--start", required=True, type=_date)
    parser.add_argument("--end", required=True, type=_date)
    parser.add_argument(
        "--refresh-mode",
        choices=tuple(item.value for item in CryptoDerivativesRefreshMode),
        default=CryptoDerivativesRefreshMode.AUTO.value,
    )
    parser.add_argument("--known-at", type=_aware_datetime)
    return parser


def main() -> int:
    """Run one public read-only-provider refresh into local append-only storage."""
    arguments = _parser().parse_args()
    try:
        summary = InvestmentAnalystApplication.create_default().refresh_crypto_derivatives(
            CryptoDerivativesRefreshRequest(
                asset_id=arguments.asset_id,
                start_date=arguments.start,
                end_date=arguments.end,
                refresh_mode=CryptoDerivativesRefreshMode(arguments.refresh_mode),
                known_at=arguments.known_at,
            ),
            location=storage_location_from_namespace(arguments),
        )
        print(
            json.dumps(
                {
                    "notice": (
                        "Deribit público; evidencia descriptiva local, no vintage garantizado, "
                        "señal, recomendación, asesoramiento ni ejecución."
                    ),
                    "summary": summary.to_json_dict(),
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    except (
        ApplicationRuntimeError,
        AssetCatalogError,
        CryptoDerivativesRefreshError,
        DeribitError,
        StorageError,
        ValueError,
        WorkspaceError,
    ) as error:
        print(f"Crypto derivatives refresh failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
