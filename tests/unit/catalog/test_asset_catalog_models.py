"""Unit tests for strict immutable asset catalog models."""

from datetime import date
from decimal import Decimal

import pytest
from pydantic import ValidationError

from investment_analyst.catalog.models import (
    AssetCatalogDocument,
    CatalogAsset,
    CatalogCryptoProfile,
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


def test_security_unit_contract_is_exact_complete_and_equity_only() -> None:
    configured = _asset(
        security_unit_factor="1",
        security_unit_basis="reported_common_share",
        security_unit_basis_version="security-unit-basis-v1",
        security_unit_market_adjustment="all",
    )
    assert configured.security_unit_factor == Decimal("1")

    with pytest.raises(ValidationError, match="must use Decimal-compatible text"):
        _asset(
            security_unit_factor=1.0,
            security_unit_basis="reported_common_share",
            security_unit_basis_version="security-unit-basis-v1",
            security_unit_market_adjustment="all",
        )
    with pytest.raises(ValidationError, match="declared together"):
        _asset(security_unit_factor="1")
    with pytest.raises(ValidationError, match="only valid for equities"):
        _asset(
            asset_id="fund:test",
            asset_class=AssetClass.ETF,
            security_unit_factor="1",
            security_unit_basis="reported_common_share",
            security_unit_basis_version="security-unit-basis-v1",
            security_unit_market_adjustment="all",
        )
    with pytest.raises(ValidationError):
        _asset(
            security_unit_factor="1",
            security_unit_basis="reported_common_share",
            security_unit_basis_version="security-unit-basis-v1",
            security_unit_market_adjustment="raw",
        )
    with pytest.raises(ValidationError):
        _asset(
            security_unit_factor="1",
            security_unit_basis="unknown_basis",
            security_unit_basis_version="security-unit-basis-v1",
            security_unit_market_adjustment="all",
        )


def _revision(**updates: object) -> dict[str, object]:
    values: dict[str, object] = {
        "basis": "depositary_receipt",
        "factor": "0.2",
        "adjustment": "all",
        "valid_from": "2026-04-17",
        "available_at": "2026-04-17T00:00:00Z",
        "reported_share_unit": "common_share",
        "cik": "0001046179",
        "accession_number": "0001628280-26-025362",
        "source_url": "https://www.sec.gov/Archives/edgar/data/1046179/example.htm",
        "source_sha256": ("9b4a4e6cd176e5f55976dbe067d006b395aab2570115d943e6c089560805a141"),
    }
    values.update(updates)
    return values


def test_security_unit_revision_is_deterministic_and_time_bounded() -> None:
    from investment_analyst.catalog.models import SecurityUnitBasisRevision

    revision = SecurityUnitBasisRevision.model_validate(_revision())
    assert revision.basis == "depositary_receipt"
    assert revision.factor == Decimal("0.2")
    assert revision.valid_from == date(2026, 4, 17)
    assert revision.valid_to is None
    assert revision.reported_share_unit == "common_share"
    assert len(revision.revision) == 64
    assert revision.covers(date(2026, 4, 17))
    assert revision.covers(date(2030, 1, 1))

    assert SecurityUnitBasisRevision.model_validate(_revision()).revision == (revision.revision)
    assert revision.model_dump(mode="json")["revision"] == revision.revision
    assert (
        SecurityUnitBasisRevision.model_validate(_revision(factor="0.25")).revision
        != revision.revision
    )
    asserted = _revision(valid_to="2026-06-01")
    closed = SecurityUnitBasisRevision.model_validate(asserted)
    assert not closed.covers(date(2026, 6, 1))
    assert closed.covers(date(2026, 5, 31))

    with pytest.raises(ValidationError, match="canonical semantic digest"):
        SecurityUnitBasisRevision.model_validate(_revision(revision="0" * 64))
    with pytest.raises(ValidationError, match="valid_to must be after valid_from"):
        SecurityUnitBasisRevision.model_validate(
            _revision(valid_from="2026-06-01", valid_to="2026-06-01")
        )
    with pytest.raises(ValidationError, match="must use Decimal-compatible text"):
        SecurityUnitBasisRevision.model_validate(_revision(factor=0.2))
    with pytest.raises(ValidationError, match="lower-case hexadecimal"):
        SecurityUnitBasisRevision.model_validate(_revision(source_sha256="ABC"))


def test_catalog_asset_rejects_contradictory_or_unsorted_revisions() -> None:
    from investment_analyst.catalog.models import SecurityUnitBasisRevision

    first = SecurityUnitBasisRevision.model_validate(_revision())
    later = SecurityUnitBasisRevision.model_validate(
        _revision(valid_from="2026-05-01", available_at="2026-05-01T00:00:00Z")
    )

    declared = _asset(security_unit_basis_revisions=(first,))
    assert declared.security_unit_basis_revisions == (first,)

    with pytest.raises(ValidationError, match="must not coexist"):
        _asset(
            security_unit_factor="1",
            security_unit_basis="reported_common_share",
            security_unit_basis_version="security-unit-basis-v1",
            security_unit_market_adjustment="all",
            security_unit_basis_revisions=(first,),
        )
    with pytest.raises(ValidationError, match="sorted by validity"):
        _asset(security_unit_basis_revisions=(later, first))
    with pytest.raises(ValidationError, match="only valid for equities"):
        _asset(
            asset_id="fund:test",
            asset_class=AssetClass.ETF,
            security_unit_basis_revisions=(first,),
        )


def test_document_rejects_external_binding_shared_by_different_assets() -> None:
    crypto = _asset(
        asset_id="crypto:test",
        symbol="TEST",
        name="Test",
        asset_class=AssetClass.CRYPTO,
        crypto_profile=CatalogCryptoProfile.ALTCOIN,
        exchange=None,
        aliases=("TEST",),
    )
    with pytest.raises(ValidationError, match="globally unique"):
        AssetCatalogDocument(catalog_version=1, assets=(crypto, _asset()))


def test_crypto_catalog_assets_require_an_explicit_isolated_profile() -> None:
    with pytest.raises(ValidationError, match="require an explicit crypto_profile"):
        _asset(
            asset_id="crypto:unclassified",
            symbol="UNKNOWN",
            name="Unclassified crypto",
            asset_class=AssetClass.CRYPTO,
            exchange="SYNTHETIC",
            aliases=("UNKNOWN",),
        )

    profiles = tuple(
        _asset(
            asset_id=f"crypto:{profile.value}",
            symbol=profile.value.upper(),
            name=profile.value,
            asset_class=AssetClass.CRYPTO,
            crypto_profile=profile,
            exchange="SYNTHETIC",
            aliases=(profile.value,),
        ).crypto_profile
        for profile in CatalogCryptoProfile
    )
    assert profiles == tuple(CatalogCryptoProfile)


def test_document_allows_explicit_non_unique_external_relationship() -> None:
    shared = _binding(
        provider="smv",
        namespace="legal_name",
        identifier="EMISOR S.A.A.",
        capabilities=("registry.issuer",),
        is_unique=False,
    )
    first = _asset(provider_bindings=(shared,))
    second = _asset(
        asset_id="equity:pe:bvl:second",
        symbol="SECOND",
        name="Second listing",
        aliases=("SECOND",),
        provider_bindings=(shared,),
    )

    document = AssetCatalogDocument(
        catalog_version=1,
        assets=tuple(sorted((first, second), key=lambda item: item.asset_id)),
    )

    assert document.assets[0].provider_bindings[0].is_unique is False
