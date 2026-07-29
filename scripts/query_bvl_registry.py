#!/usr/bin/env python3
"""Query configured BVL identities and local SMV evidence point-in-time."""

import argparse
import json
import sys
from datetime import UTC, datetime

from investment_analyst.application.cli import (
    add_storage_location_arguments,
    storage_location_from_namespace,
)
from investment_analyst.application.facade import InvestmentAnalystApplication
from investment_analyst.application.peru_registry import BvlRegistryUniverseRequest
from investment_analyst.application.runtime import ApplicationRuntimeError
from investment_analyst.catalog.service import AssetCatalogError
from investment_analyst.providers.peru.smv_point_in_time import SmvPointInTimeError
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


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Reconstruye el universo BVL/SMV local en un corte temporal explícito."
    )
    add_storage_location_arguments(parser)
    parser.add_argument("--known-at", required=True, type=_aware_datetime)
    parser.add_argument(
        "--asset-id",
        action="append",
        default=[],
        help="asset_id BVL opcional; puede repetirse.",
    )
    return parser


def main() -> int:
    """Execute a read-only local registry query and print deterministic JSON."""
    args = _parser().parse_args()
    try:
        request = BvlRegistryUniverseRequest(
            known_at=args.known_at,
            asset_ids=tuple(sorted(args.asset_id)),
        )
        result = InvestmentAnalystApplication.create_default().query_bvl_registry(
            request,
            location=storage_location_from_namespace(args),
        )
        print(json.dumps(result.to_json_dict(), ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    except (
        ApplicationRuntimeError,
        AssetCatalogError,
        SmvPointInTimeError,
        StorageError,
        ValueError,
        WorkspaceError,
    ) as error:
        print(f"SMV/BVL registry query failed: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
