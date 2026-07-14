"""Unit tests for strict immutable asset catalog models."""

import pytest
from pydantic import ValidationError

from investment_analyst.catalog.models import (
    AssetCatalogDocument,
    CatalogAsset,
    ProviderBinding,
)
from investment_analyst.core.models import AssetClass


def _binding(**updates: object) -> ProviderBinding:
    values: dict[str, object] = {
        "provider": "alpaca",
        "namespace": "symbol",
        "identifier": "AAPL",
        "capabilities": ("market.daily_bars",),
    }
    values.update(updates)
    return ProviderBinding(**values)


def _asset(**updates: object) -> CatalogAsset:
    values: dict[str, object] = {
        "asset_id": "equity:us:aapl",
        "symbol": "AAPL",
        "name": "Apple Inc.",
        "asset_class": AssetClass.EQUITY,
        "quote_currency": "USD",
        "exchange": "NASDAQ",
        "provider_symbols": {"alpaca_iex": "AAPL"},
        "is_active": True,
        "aliases": ("AAPL", "Apple"),
        "provider_bindings": (_binding(),),
    }
    values.update(updates)
    return CatalogAsset(**values)


def test_valid_document_and_binding_identity_are_immutable() -> None:
    binding = _binding()
    document = AssetCatalogDocument(catalog_version=1, assets=(_asset(),))
    assert binding.identity == ("alpaca", "symbol", "AAPL")
    assert document.assets[0].asset_class is AssetClass.EQUITY
    with pytest.raises(ValidationError):
        binding.identifier = "OTHER"
    with pytest.raises(ValidationError):
        document.catalog_version = 2


def test_catalog_version_requires_positive_strict_integer() -> None:
    with pytest.raises(ValidationError, match="positive"):
        AssetCatalogDocument(catalog_version=0, assets=(_asset(),))
    with pytest.raises(ValidationError):
        AssetCatalogDocument(catalog_version=True, assets=(_asset(),))


def test_models_reject_additional_fields_and_duplicate_asset_ids() -> None:
    with pytest.raises(ValidationError):
        _binding(extra="forbidden")
    with pytest.raises(ValidationError, match="asset IDs must be unique"):
        AssetCatalogDocument(catalog_version=1, assets=(_asset(), _asset()))


@pytest.mark.parametrize(
    ("updates", "message"),
    [
        ({"provider": "Alpaca Feed"}, "lower-case slugs"),
        ({"namespace": ""}, "String should have at least 1 character"),
        ({"identifier": ""}, "String should have at least 1 character"),
        ({"capabilities": ()}, "must not be empty"),
        (
            {"capabilities": ("market.daily_bars", "market.daily_bars")},
            "must not contain duplicates",
        ),
        (
            {"capabilities": ("market.trades", "market.daily_bars")},
            "must be sorted",
        ),
    ],
)
def test_binding_validation(updates: dict[str, object], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        _binding(**updates)


def test_identifier_preserves_leading_zeroes_and_rejects_surrounding_space() -> None:
    binding = _binding(provider="sec", namespace="cik", identifier="0000320193")
    assert binding.identifier == "0000320193"
    with pytest.raises(ValidationError, match="surrounding whitespace"):
        _binding(identifier=" AAPL")


def test_aliases_are_case_insensitively_unique_and_bindings_nonempty() -> None:
    with pytest.raises(ValidationError, match="unique ignoring case"):
        _asset(aliases=("AAPL", "aapl"))
    with pytest.raises(ValidationError, match="must not be empty"):
        _asset(provider_bindings=())


def test_catalog_asset_rejects_duplicate_binding_identity() -> None:
    binding = _binding()
    with pytest.raises(ValidationError, match="duplicate identities"):
        _asset(provider_bindings=(binding, binding))


def test_document_rejects_external_binding_shared_by_different_assets() -> None:
    crypto = _asset(
        asset_id="crypto:test",
        symbol="TEST",
        name="Test",
        asset_class=AssetClass.CRYPTO,
        exchange=None,
        aliases=("TEST",),
    )
    with pytest.raises(ValidationError, match="globally unique"):
        AssetCatalogDocument(catalog_version=1, assets=(crypto, _asset()))
