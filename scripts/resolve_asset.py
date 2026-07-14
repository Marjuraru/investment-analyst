#!/usr/bin/env python3
"""Resolve one canonical asset or alias from the packaged static catalog."""

import argparse
import json
import sys

from pydantic import ValidationError

from investment_analyst.catalog.models import CatalogAsset, ProviderBinding
from investment_analyst.catalog.service import AssetCatalogError, AssetCatalogService


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    selector = parser.add_mutually_exclusive_group(required=True)
    selector.add_argument("--asset-id")
    selector.add_argument("--alias")
    parser.add_argument("--provider")
    parser.add_argument("--namespace")
    return parser


def _asset_payload(asset: CatalogAsset) -> dict[str, object]:
    return {
        "asset_id": asset.asset_id,
        "symbol": asset.symbol,
        "name": asset.name,
        "type": asset.asset_class.value,
        "quote_currency": asset.quote_currency,
        "exchange": asset.exchange,
        "aliases": list(asset.aliases),
    }


def _binding_payload(binding: ProviderBinding) -> dict[str, object]:
    return {
        "provider": binding.provider,
        "namespace": binding.namespace,
        "identifier": binding.identifier,
        "capabilities": list(binding.capabilities),
    }


def main() -> int:
    """Resolve one asset and optionally one provider binding."""
    arguments = _parser().parse_args()
    if (arguments.provider is None) != (arguments.namespace is None):
        print(
            "asset resolution failed: provider and namespace must be supplied together",
            file=sys.stderr,
        )
        return 2
    try:
        service = AssetCatalogService.load_default()
        asset = (
            service.get(arguments.asset_id)
            if arguments.asset_id is not None
            else service.resolve_alias(arguments.alias)
        )
        payload: dict[str, object] = {
            "catalog_version": service.catalog_version,
            "asset": _asset_payload(asset),
        }
        if arguments.provider is not None:
            payload["binding"] = _binding_payload(
                service.get_binding(
                    asset.asset_id,
                    provider=arguments.provider,
                    namespace=arguments.namespace,
                )
            )
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    except (AssetCatalogError, ValidationError, ValueError) as error:
        print(f"asset resolution failed: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
