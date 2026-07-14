#!/usr/bin/env python3
"""List assets from the packaged static catalog."""

import argparse
import json
import sys

from pydantic import ValidationError

from investment_analyst.catalog.models import CatalogAsset
from investment_analyst.catalog.service import AssetCatalogError, AssetCatalogService
from investment_analyst.core.models import AssetClass


def _asset_type(value: str) -> AssetClass:
    try:
        return AssetClass(value.casefold())
    except ValueError as error:
        allowed = ", ".join(item.value for item in AssetClass)
        raise argparse.ArgumentTypeError(f"asset-type must be one of: {allowed}") from error


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--asset-type", type=_asset_type)
    parser.add_argument("--capability")
    return parser


def _asset_payload(asset: CatalogAsset) -> dict[str, object]:
    aliases = asset.aliases
    bindings = asset.provider_bindings
    return {
        "asset_id": asset.asset_id,
        "name": asset.name,
        "type": asset.asset_class.value,
        "aliases": list(aliases),
        "providers": sorted({binding.provider for binding in bindings}),
        "capabilities": sorted(
            {capability for binding in bindings for capability in binding.capabilities}
        ),
    }


def main() -> int:
    """Load, filter, and print the default catalog."""
    arguments = _parser().parse_args()
    try:
        service = AssetCatalogService.load_default()
        assets = service.list_assets(
            asset_type=arguments.asset_type,
            capability=arguments.capability,
        )
        payload = {
            "catalog_version": service.catalog_version,
            "count": len(assets),
            "assets": [_asset_payload(asset) for asset in assets],
        }
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    except (AssetCatalogError, ValidationError, ValueError) as error:
        print(f"asset catalog listing failed: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
