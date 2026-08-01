#!/usr/bin/env python3
"""Refresh the configured BVL/SMV identity universe through official HTTPS pages."""

import argparse
import json
import sys

from investment_analyst.application.cli import (
    add_storage_location_arguments,
    storage_location_from_namespace,
)
from investment_analyst.application.facade import InvestmentAnalystApplication
from investment_analyst.application.peru_registry import BvlRegistryRefreshRequest
from investment_analyst.application.runtime import ApplicationRuntimeError
from investment_analyst.catalog.service import AssetCatalogError
from investment_analyst.providers.http import HttpRequestError
from investment_analyst.providers.peru.smv_open_data import SmvOpenDataError
from investment_analyst.storage import StorageError
from investment_analyst.workspace.service import WorkspaceError


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Actualiza identidad registral SMV para todas las cotizaciones BVL configuradas "
            "o para una selección explícita."
        )
    )
    add_storage_location_arguments(parser)
    parser.add_argument(
        "--asset-id",
        action="append",
        default=[],
        help="asset_id BVL; puede repetirse. Sin este argumento se actualiza todo el lote.",
    )
    return parser


def main() -> int:
    """Execute one resumable registry batch and print compact auditable JSON."""
    args = _parser().parse_args()
    try:
        request = BvlRegistryRefreshRequest(asset_ids=tuple(sorted(args.asset_id)))
        summary = InvestmentAnalystApplication.create_default().refresh_bvl_registry(
            request,
            location=storage_location_from_namespace(args),
        )
        print(
            json.dumps(
                {
                    "notice": (
                        "Identidad registral descriptiva; no crea precios, métricas, "
                        "recomendaciones ni órdenes."
                    ),
                    "summary": summary.to_json_dict(),
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    except (
        ApplicationRuntimeError,
        AssetCatalogError,
        HttpRequestError,
        SmvOpenDataError,
        StorageError,
        ValueError,
        WorkspaceError,
    ) as error:
        print(f"SMV/BVL registry refresh failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
