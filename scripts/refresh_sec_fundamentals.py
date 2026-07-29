#!/usr/bin/env python3
"""Actualiza fundamentales SEC de un emisor configurado en el catálogo."""

import argparse
import json
import os
import sys
from datetime import UTC, datetime

from investment_analyst.application.cli import (
    add_storage_location_arguments,
    storage_location_from_namespace,
)
from investment_analyst.application.facade import InvestmentAnalystApplication
from investment_analyst.application.runtime import ApplicationRuntimeError
from investment_analyst.application.sec_fundamental_refresh import (
    SecIssuerFundamentalRefreshError,
)
from investment_analyst.application.sec_fundamental_refresh_models import (
    SecIssuerFundamentalRefreshRequest,
)
from investment_analyst.catalog.service import AssetCatalogError
from investment_analyst.core.models import DataFrequency
from investment_analyst.providers.fundamentals.sec_edgar import SecEdgarIdentity
from investment_analyst.storage import StorageError
from investment_analyst.workspace.service import WorkspaceError


def _aware_datetime(value: str) -> datetime:
    normalized = f"{value[:-1]}+00:00" if value.endswith(("Z", "z")) else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as error:
        raise argparse.ArgumentTypeError("known-at must be a valid ISO 8601 datetime") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise argparse.ArgumentTypeError("known-at must include timezone information")
    return parsed.astimezone(UTC)


def _frequency(value: str) -> DataFrequency:
    mapping = {
        "annual": DataFrequency.ANNUAL,
        "quarterly": DataFrequency.QUARTERLY,
    }
    try:
        return mapping[value.casefold()]
    except KeyError as error:
        raise argparse.ArgumentTypeError("frequency must be annual or quarterly") from error


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    add_storage_location_arguments(parser)
    parser.add_argument("--asset-id", required=True)
    parser.add_argument("--frequency", required=True, type=_frequency)
    parser.add_argument("--known-at", type=_aware_datetime)
    return parser


def main() -> int:
    """Run one catalog-backed SEC refresh and print its compact audit summary."""
    arguments = _parser().parse_args()
    user_agent = os.environ.get("SEC_USER_AGENT", "")
    if not user_agent.strip():
        print("SEC_USER_AGENT is required and was not provided.", file=sys.stderr)
        return 2

    try:
        result = InvestmentAnalystApplication.create_default().refresh_sec_fundamentals(
            SecIssuerFundamentalRefreshRequest(
                asset_id=arguments.asset_id,
                frequency=arguments.frequency,
                requested_known_at=arguments.known_at,
            ),
            location=storage_location_from_namespace(arguments),
            sec_identity=SecEdgarIdentity(user_agent=user_agent),
        )
        print(
            json.dumps(
                {
                    "notice": (
                        "Fuente oficial SEC EDGAR. El resultado es descriptivo, mantiene mercado "
                        "y fundamentales separados, no recomienda operaciones ni ejecuta órdenes."
                    ),
                    "summary": result.to_json_dict(),
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    except (
        ApplicationRuntimeError,
        AssetCatalogError,
        SecIssuerFundamentalRefreshError,
        StorageError,
        ValueError,
        WorkspaceError,
    ) as error:
        print(f"SEC fundamental refresh failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
