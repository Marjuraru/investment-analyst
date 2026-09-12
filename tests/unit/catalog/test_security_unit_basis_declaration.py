"""Unit tests for the explicit security unit basis declaration across catalog assets."""

from decimal import Decimal

from investment_analyst.analytics.valuation.models import (
    CorporateValuationRequest,
    ValuationReasonCode,
    ValuationSnapshotStatus,
)
from investment_analyst.analytics.valuation.service import CorporateValuationService
from investment_analyst.application.analysis_capabilities import analysis_capabilities_for
from investment_analyst.catalog.models import CatalogAsset
from investment_analyst.catalog.provider_configuration import (
    resolve_alpaca_configuration,
    resolve_sec_configuration,
)
from investment_analyst.catalog.provider_context import ProviderAssetContextResolver
from investment_analyst.catalog.service import AssetCatalogService
from investment_analyst.core.models import AssetClass
from investment_analyst.storage import LocalStorage

_SIXTEEN_COMMON_SHARE_ISSUER_IDS = frozenset(
    {
        "equity:us:amzn",
        "equity:us:b",
        "equity:us:cat",
        "equity:us:cde",
        "equity:us:cvx",
        "equity:us:hymc",
        "equity:us:intc",
        "equity:us:jnj",
        "equity:us:msft",
        "equity:us:mstr",
        "equity:us:mu",
        "equity:us:mux",
        "equity:us:nem",
        "equity:us:nvda",
        "equity:us:pltr",
        "equity:us:scco",
    }
)

_PRIOR_EVALUABLE_COMMON_SHARE_ISSUER_IDS = frozenset(
    {
        "equity:us:aapl",
        "equity:us:amd",
    }
)

_ADR_ISSUER_IDS = frozenset(
    {
        "equity:us:bvn",
        "equity:us:tsm",
    }
)


class _EmptyStorage:
    """Minimal storage double for read-only valuation query initialization."""

    def __init__(self) -> None:
        self.observations = self
        self.raw_records = self

    def require_open(self) -> None:
        return None

    def list(self, **_kwargs: object) -> list[object]:
        return []

    def get_many(self, *args: object, **_kwargs: object) -> dict[object, object]:
        return {}


def test_sixteen_common_share_issuers_declare_explicit_share_basis() -> None:
    catalog = AssetCatalogService.load_default()
    for asset_id in _SIXTEEN_COMMON_SHARE_ISSUER_IDS:
        asset = catalog.get(asset_id)
        assert asset.security_unit_basis == "reported_common_share"
        assert asset.security_unit_basis_version == "security-unit-basis-v1"
        assert asset.security_unit_factor == Decimal("1")
        assert asset.security_unit_market_adjustment == "all"

    all_declared = {
        asset.asset_id
        for asset in catalog.list_assets(asset_type=AssetClass.EQUITY)
        if asset.security_unit_basis is not None
    }
    assert all_declared == (
        _SIXTEEN_COMMON_SHARE_ISSUER_IDS | _PRIOR_EVALUABLE_COMMON_SHARE_ISSUER_IDS
    )
    assert len(all_declared) == 18


def test_bvn_and_tsm_remain_without_basis_declaration() -> None:
    catalog = AssetCatalogService.load_default()
    for asset_id in _ADR_ISSUER_IDS:
        asset = catalog.get(asset_id)
        assert asset.security_unit_basis is None
        assert asset.security_unit_basis_version is None
        assert asset.security_unit_factor is None
        assert asset.security_unit_market_adjustment is None


def test_non_equity_and_peru_assets_declare_no_security_unit_contract() -> None:
    catalog = AssetCatalogService.load_default()
    for asset in catalog.list_assets():
        if (
            asset.asset_id.startswith("equity:pe:bvl:")
            or asset.asset_class is not AssetClass.EQUITY
        ):
            assert asset.security_unit_basis is None
            assert asset.security_unit_basis_version is None
            assert asset.security_unit_factor is None
            assert asset.security_unit_market_adjustment is None


def _valuation_service_for_asset(
    catalog: AssetCatalogService,
    resolver: ProviderAssetContextResolver,
    storage: LocalStorage,
    asset: CatalogAsset,
) -> CorporateValuationService:
    alpaca = resolve_alpaca_configuration(resolver, asset_id=asset.asset_id)
    sec = resolve_sec_configuration(resolver, asset_id=asset.asset_id)
    return CorporateValuationService(
        storage,
        capabilities=analysis_capabilities_for(asset),
        market_source_id=alpaca.source_id,
        fundamental_source_id=sec.companyfacts_source_id,
        price_currency=asset.quote_currency,
        security_unit_factor=asset.security_unit_factor,
        security_unit_basis=asset.security_unit_basis,
        security_unit_basis_version=asset.security_unit_basis_version,
        security_unit_market_adjustment=asset.security_unit_market_adjustment,
    )


def test_corporate_valuation_does_not_fail_with_share_basis_unavailable_for_sixteen_issuers() -> (
    None
):
    catalog = AssetCatalogService.load_default()
    resolver = ProviderAssetContextResolver(catalog)
    storage = _EmptyStorage()  # type: ignore[arg-type]

    for asset_id in _SIXTEEN_COMMON_SHARE_ISSUER_IDS:
        asset = catalog.get(asset_id)
        service = _valuation_service_for_asset(catalog, resolver, storage, asset)  # type: ignore[arg-type]
        snapshot = service.query(
            CorporateValuationRequest(
                asset_id=asset_id,
                known_at="2026-09-12T00:00:00Z",
                valuation_date="2026-09-11",
            )
        )
        assert snapshot.status is ValuationSnapshotStatus.NOT_EVALUABLE
        reason_codes = {metric.reason_code for metric in snapshot.metrics}
        assert ValuationReasonCode.SHARE_BASIS_UNAVAILABLE not in reason_codes
        assert snapshot.security_basis is not None
        assert snapshot.security_basis.basis == "reported_common_share"
        assert snapshot.security_basis.market_units_per_reported_share == Decimal("1")


def test_corporate_valuation_fails_with_share_basis_unavailable_for_adr_issuers() -> None:
    catalog = AssetCatalogService.load_default()
    resolver = ProviderAssetContextResolver(catalog)
    storage = _EmptyStorage()  # type: ignore[arg-type]

    for asset_id in _ADR_ISSUER_IDS:
        asset = catalog.get(asset_id)
        service = _valuation_service_for_asset(catalog, resolver, storage, asset)  # type: ignore[arg-type]
        snapshot = service.query(
            CorporateValuationRequest(
                asset_id=asset_id,
                known_at="2026-09-12T00:00:00Z",
                valuation_date="2026-09-11",
            )
        )
        assert snapshot.status is ValuationSnapshotStatus.NOT_EVALUABLE
        assert snapshot.security_basis is None
        for metric in snapshot.metrics:
            assert metric.reason_code is ValuationReasonCode.SHARE_BASIS_UNAVAILABLE
