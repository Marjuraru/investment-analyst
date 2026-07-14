"""Unit coverage for provider-specific context resolution."""

import pytest
from pydantic import ValidationError

from investment_analyst.catalog.models import ProviderBinding
from investment_analyst.catalog.provider_context import (
    ProviderAssetContext,
    ProviderAssetContextResolver,
    ProviderAssetNotConfiguredError,
    ProviderCapabilityMissingError,
    ProviderNamespaceMissingError,
)
from investment_analyst.catalog.service import AssetCatalogService, AssetNotFoundError

APPLE_ASSET_ID = "equity:us:aapl"
COINBASE_ASSET_ID = "crypto:btc-usd"


def test_resolves_deterministic_provider_context_and_preserves_identifiers() -> None:
    resolver = ProviderAssetContextResolver(AssetCatalogService.load_default())

    alpaca = resolver.resolve(
        APPLE_ASSET_ID,
        provider=" ALPACA ",
        required_namespaces=("symbol",),
        required_capabilities=("market.daily_bars",),
    )
    sec = resolver.resolve(
        APPLE_ASSET_ID,
        provider="sec",
        required_namespaces=("cik", "ticker"),
    )

    assert alpaca.provider == "alpaca"
    assert alpaca.require_identifier("symbol") == "AAPL"
    assert alpaca.supports("market.daily_bars") is True
    assert sec.require_identifier("cik") == "0000320193"
    assert sec.require_identifier("ticker") == "AAPL"
    assert sec.capabilities == (
        "fundamentals.company_facts",
        "fundamentals.submissions",
    )
    assert isinstance(sec.bindings, tuple)


def test_missing_asset_provider_namespace_and_capability_are_typed() -> None:
    resolver = ProviderAssetContextResolver(AssetCatalogService.load_default())

    with pytest.raises(AssetNotFoundError):
        resolver.resolve("equity:us:missing", provider="alpaca")
    with pytest.raises(ProviderAssetNotConfiguredError):
        resolver.resolve(APPLE_ASSET_ID, provider="coinbase")
    with pytest.raises(ProviderAssetNotConfiguredError):
        resolver.resolve(COINBASE_ASSET_ID, provider="sec")
    with pytest.raises(ProviderNamespaceMissingError):
        resolver.resolve(
            APPLE_ASSET_ID,
            provider="alpaca",
            required_namespaces=("product_id",),
        )
    with pytest.raises(ProviderCapabilityMissingError):
        resolver.resolve(
            COINBASE_ASSET_ID,
            provider="coinbase",
            required_capabilities=("fundamentals.company_facts",),
        )


def test_context_rejects_mixed_provider_and_duplicate_namespaces() -> None:
    asset = AssetCatalogService.load_default().get(APPLE_ASSET_ID)
    alpaca = next(binding for binding in asset.provider_bindings if binding.provider == "alpaca")
    sec = next(binding for binding in asset.provider_bindings if binding.namespace == "cik")

    with pytest.raises(ValidationError):
        ProviderAssetContext(
            asset=asset,
            provider="alpaca",
            bindings=(alpaca, sec),
            capabilities=("market.daily_bars",),
        )

    duplicate = ProviderBinding(
        provider="alpaca",
        namespace="symbol",
        identifier="AAPL.X",
        capabilities=("market.daily_bars",),
    )
    with pytest.raises(ValidationError):
        ProviderAssetContext(
            asset=asset,
            provider="alpaca",
            bindings=(alpaca, duplicate),
            capabilities=("market.daily_bars",),
        )
