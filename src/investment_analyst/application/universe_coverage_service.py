"""Compose catalog declarations with verified local multi-domain evidence."""

import hashlib
import json
from datetime import UTC, datetime

from investment_analyst.analytics.fundamentals.research_models import AaplFundamentalResearchRequest
from investment_analyst.analytics.fundamentals.research_service import (
    SecIssuerFundamentalResearchService,
)
from investment_analyst.analytics.market.bar_models import HistoricalBarQuery
from investment_analyst.analytics.market.history_service import HistoricalMarketDataService
from investment_analyst.analytics.valuation.models import CorporateValuationRequest
from investment_analyst.analytics.valuation.service import CorporateValuationService
from investment_analyst.application.analysis_capabilities import analysis_capabilities_for
from investment_analyst.application.market_universe import (
    MarketAssetDescriptor,
    build_market_asset_universe,
)
from investment_analyst.application.peru_registry import (
    BvlRegistryAsset,
    BvlRegistryUniverseRequest,
    BvlRegistryUniverseService,
)
from investment_analyst.application.universe_coverage_models import (
    CoverageCapability,
    EvidenceState,
    UniverseBvlRegistryCoverage,
    UniverseCoverageAsset,
    UniverseCoverageRequest,
    UniverseCoverageResult,
    UniverseFundamentalCoverage,
    UniverseMarketCoverage,
    UniverseValuationCoverage,
)
from investment_analyst.catalog.models import CatalogAsset
from investment_analyst.catalog.provider_configuration import (
    resolve_alpaca_configuration,
    resolve_sec_configuration,
)
from investment_analyst.catalog.provider_context import (
    ProviderAssetContextResolver,
    ProviderAssetNotConfiguredError,
    ProviderCapabilityMissingError,
    ProviderNamespaceMissingError,
)
from investment_analyst.catalog.service import AssetCatalogService
from investment_analyst.core.models import AssetClass, DataFrequency
from investment_analyst.storage import LocalStorage
from investment_analyst.time_intervals import inclusive_utc_date_bounds


class UniverseCoverageService:
    """Read catalog-scoped evidence once without provider clients or writes."""

    def __init__(
        self,
        storage: LocalStorage,
        catalog: AssetCatalogService,
        resolver: ProviderAssetContextResolver,
    ) -> None:
        self._storage = storage
        self._catalog = catalog
        self._resolver = resolver

    def query(self, request: UniverseCoverageRequest) -> UniverseCoverageResult:
        """Return stable local evidence for every catalog asset or explicit subset."""
        market_universe = build_market_asset_universe(self._catalog, self._resolver)
        descriptors = {item.asset_id: item for item in market_universe.assets}
        catalog_assets = {item.asset_id: item for item in self._catalog.list_assets()}
        asset_ids = request.asset_ids or tuple(sorted(catalog_assets))
        unknown = tuple(item for item in asset_ids if item not in catalog_assets)
        if unknown:
            raise ValueError(f"asset is not configured in the catalog: {unknown[0]}")
        start, end = inclusive_utc_date_bounds(request.market_start, request.market_end)
        bvl = self._bvl_views(request, asset_ids)
        history = HistoricalMarketDataService(self._storage)
        assets = tuple(
            self._asset_view(
                catalog_assets[asset_id],
                descriptors.get(asset_id),
                request,
                start,
                end,
                history,
                bvl.get(asset_id),
            )
            for asset_id in asset_ids
        )
        return UniverseCoverageResult(
            catalog_version=self._catalog.catalog_version,
            catalog_sha256=_catalog_sha256(self._catalog),
            request=request,
            assets=assets,
        )

    def _bvl_views(
        self, request: UniverseCoverageRequest, asset_ids: tuple[str, ...]
    ) -> dict[str, BvlRegistryAsset]:
        bvl_ids = tuple(
            asset_id for asset_id in asset_ids if self._catalog.get(asset_id).exchange == "BVL"
        )
        if not bvl_ids:
            return {}
        result = BvlRegistryUniverseService(self._storage, self._catalog, self._resolver).query(
            BvlRegistryUniverseRequest(known_at=request.known_at, asset_ids=bvl_ids)
        )
        return {item.asset_id: item for item in result.assets}

    def _asset_view(
        self,
        asset: CatalogAsset,
        descriptor: MarketAssetDescriptor | None,
        request: UniverseCoverageRequest,
        start: datetime,
        end: datetime,
        history: HistoricalMarketDataService,
        bvl_view: BvlRegistryAsset | None,
    ) -> UniverseCoverageAsset:
        return UniverseCoverageAsset(
            asset_id=asset.asset_id,
            symbol=asset.symbol,
            name=asset.name,
            asset_class=asset.asset_class,
            exchange=asset.exchange or "UNKNOWN",
            quote_currency=asset.quote_currency,
            market=self._market_view(descriptor, request, start, end, history),
            fundamentals=self._fundamental_view(asset, request),
            corporate_valuation=self._valuation_view(asset, request),
            bvl_registry=self._bvl_registry_view(asset, bvl_view, request),
            additional_capabilities_not_queried=_additional_capabilities(asset),
            limitations=_limitations(asset, descriptor),
        )

    def _market_view(
        self,
        descriptor: MarketAssetDescriptor | None,
        request: UniverseCoverageRequest,
        start: datetime,
        end: datetime,
        history: HistoricalMarketDataService,
    ) -> UniverseMarketCoverage:
        if descriptor is None:
            return UniverseMarketCoverage(
                capability=CoverageCapability.NOT_CONFIGURED,
                evidence=EvidenceState.NOT_QUERIED,
                bar_count=0,
                candidate_versions=0,
                discarded_revisions=0,
            )
        series = history.query(
            HistoricalBarQuery(
                asset_id=descriptor.asset_id,
                source_id=descriptor.source_id,
                start=start,
                end=end,
                known_at=request.known_at,
            )
        )
        latest = series.bars[-1] if series.bars else None
        return UniverseMarketCoverage(
            capability=CoverageCapability.SUPPORTED,
            evidence=EvidenceState.PRESENT if latest else EvidenceState.MISSING,
            source_id=descriptor.source_id,
            volume_unit=descriptor.volume_unit,
            history_start=descriptor.default_market_start,
            bar_count=series.coverage.bar_count,
            candidate_versions=series.coverage.candidate_versions,
            discarded_revisions=series.coverage.discarded_revisions,
            earliest_timestamp=series.coverage.earliest_timestamp,
            latest_timestamp=series.coverage.latest_timestamp,
            latest_available_at=latest.available_at if latest else None,
            latest_raw_record_id=latest.raw_record_id if latest else None,
            latest_observation_ids=tuple(sorted(latest.observation_ids.values(), key=str))
            if latest
            else (),
            reference_at=latest.timestamp if latest else None,
            reference_age_days=_age_days(request.known_at, latest.timestamp if latest else None),
            latest_input_available_at=latest.available_at if latest else None,
            latest_input_age_days=_age_days(
                request.known_at, latest.available_at if latest else None
            ),
        )

    def _fundamental_view(
        self, asset: CatalogAsset, request: UniverseCoverageRequest
    ) -> UniverseFundamentalCoverage:
        if asset.asset_class is not AssetClass.EQUITY:
            return _empty_fundamentals(CoverageCapability.NOT_APPLICABLE)
        try:
            configuration = resolve_sec_configuration(self._resolver, asset_id=asset.asset_id)
        except (
            ProviderAssetNotConfiguredError,
            ProviderCapabilityMissingError,
            ProviderNamespaceMissingError,
        ):
            return _empty_fundamentals(CoverageCapability.NOT_CONFIGURED)
        result = SecIssuerFundamentalResearchService(self._storage, configuration).query(
            AaplFundamentalResearchRequest(
                known_at=request.known_at,
                frequency=DataFrequency(request.frequency),
                start_period_end=request.fundamental_start,
                end_period_end=request.fundamental_end,
            )
        )
        latest_period = result.periods[-1] if result.periods else None
        latest_available_at = (
            max((metric.available_at for metric in latest_period.metrics), default=None)
            if latest_period
            else None
        )
        return UniverseFundamentalCoverage(
            capability=CoverageCapability.SUPPORTED,
            evidence=EvidenceState.PRESENT if latest_period else EvidenceState.MISSING,
            source_id=result.source_id,
            frequency=request.frequency,
            definition_keys=tuple(item.metric_key for item in result.definitions),
            source_periods=result.coverage.source_periods,
            output_periods=result.coverage.output_periods,
            metrics_returned=result.coverage.metrics_returned,
            skipped_counts=result.coverage.skipped_counts,
            latest_period_end=latest_period.period_end if latest_period else None,
            latest_input_available_at=latest_available_at,
            reference_at=latest_period.period_end if latest_period else None,
            reference_age_days=_age_days(
                request.known_at, latest_period.period_end if latest_period else None
            ),
            latest_input_age_days=_age_days(request.known_at, latest_available_at),
        )

    def _valuation_view(
        self, asset: CatalogAsset, request: UniverseCoverageRequest
    ) -> UniverseValuationCoverage:
        if asset.asset_class is not AssetClass.EQUITY:
            return _empty_valuation(CoverageCapability.NOT_APPLICABLE)
        try:
            market = resolve_alpaca_configuration(self._resolver, asset_id=asset.asset_id)
            fundamental = resolve_sec_configuration(self._resolver, asset_id=asset.asset_id)
        except (
            ProviderAssetNotConfiguredError,
            ProviderCapabilityMissingError,
            ProviderNamespaceMissingError,
        ):
            return _empty_valuation(CoverageCapability.NOT_CONFIGURED)
        snapshot = CorporateValuationService(
            self._storage,
            capabilities=analysis_capabilities_for(asset),
            market_source_id=market.source_id,
            fundamental_source_id=fundamental.companyfacts_source_id,
            price_currency=asset.quote_currency,
            security_unit_factor=asset.security_unit_factor,
            security_unit_basis=asset.security_unit_basis,
            security_unit_basis_version=asset.security_unit_basis_version,
            security_unit_market_adjustment=asset.security_unit_market_adjustment,
        ).query(
            CorporateValuationRequest(
                asset_id=asset.asset_id,
                known_at=request.known_at,
                valuation_date=request.market_end,
            ),
            computed_at=request.known_at,
        )
        reasons = tuple(
            sorted(
                {
                    item.reason_code.value
                    for item in snapshot.metrics
                    if item.reason_code is not None
                }
            )
        )
        latest_input = max((item.available_at for item in snapshot.inputs), default=None)
        reference_at = snapshot.valuation_as_of or snapshot.annual_period_end
        return UniverseValuationCoverage(
            capability=CoverageCapability.SUPPORTED,
            evidence=EvidenceState.PRESENT if snapshot.inputs else EvidenceState.MISSING,
            status=snapshot.status.value,
            reason_codes=reasons,
            price_source_id=snapshot.price_source_id,
            fundamental_source_id=snapshot.fundamental_source_id,
            valuation_as_of=snapshot.valuation_as_of,
            latest_input_available_at=latest_input,
            reference_at=reference_at,
            reference_age_days=_age_days(request.known_at, reference_at),
            latest_input_age_days=_age_days(request.known_at, latest_input),
        )

    def _bvl_registry_view(
        self,
        asset: CatalogAsset,
        bvl_view: BvlRegistryAsset | None,
        request: UniverseCoverageRequest,
    ) -> UniverseBvlRegistryCoverage:
        if asset.exchange != "BVL":
            return _empty_bvl(CoverageCapability.NOT_APPLICABLE)
        if bvl_view is None:
            return _empty_bvl(CoverageCapability.NOT_CONFIGURED)
        available = tuple(
            value
            for value in (bvl_view.company_available_at, bvl_view.securities_available_at)
            if value is not None
        )
        latest = max(available) if available else None
        return UniverseBvlRegistryCoverage(
            capability=CoverageCapability.SUPPORTED,
            evidence=EvidenceState.MISSING
            if bvl_view.status.value == "not_imported"
            else EvidenceState.PRESENT,
            status=bvl_view.status.value,
            company_raw_record_ids=tuple(sorted(bvl_view.company_raw_record_ids, key=str)),
            securities_raw_record_ids=tuple(sorted(bvl_view.securities_raw_record_ids, key=str)),
            latest_input_available_at=latest,
            reference_at=latest,
            reference_age_days=_age_days(request.known_at, latest),
            latest_input_age_days=_age_days(request.known_at, latest),
        )


def _empty_fundamentals(capability: CoverageCapability) -> UniverseFundamentalCoverage:
    return UniverseFundamentalCoverage(
        capability=capability,
        evidence=EvidenceState.NOT_QUERIED,
        source_periods=0,
        output_periods=0,
        metrics_returned=0,
    )


def _empty_valuation(capability: CoverageCapability) -> UniverseValuationCoverage:
    return UniverseValuationCoverage(capability=capability, evidence=EvidenceState.NOT_QUERIED)


def _empty_bvl(capability: CoverageCapability) -> UniverseBvlRegistryCoverage:
    return UniverseBvlRegistryCoverage(capability=capability, evidence=EvidenceState.NOT_QUERIED)


def _additional_capabilities(asset: CatalogAsset) -> tuple[str, ...]:
    queried = {
        "market.daily_bars",
        "fundamentals.company_facts",
        "fundamentals.submissions",
        "registry.exchange_listing",
        "registry.issuer",
    }
    return tuple(
        sorted(
            {
                capability
                for binding in asset.provider_bindings
                for capability in binding.capabilities
                if capability not in queried
            }
        )
    )


def _limitations(asset: CatalogAsset, descriptor: MarketAssetDescriptor | None) -> tuple[str, ...]:
    limitations: list[str] = []
    if descriptor is not None and descriptor.provider == "alpaca":
        limitations.append("IEX market data is not consolidated SIP coverage")
    if descriptor is not None and descriptor.provider == "coinbase":
        limitations.append("Coinbase Exchange daily candles are venue-specific spot-market data")
    if asset.asset_class is AssetClass.EQUITY and asset.security_unit_factor is None:
        limitations.append("corporate valuation requires documented share-unit basis")
    return tuple(limitations)


def _age_days(known_at: datetime, reference_at: datetime | None) -> int | None:
    if reference_at is None:
        return None
    return max(0, (known_at.astimezone(UTC).date() - reference_at.astimezone(UTC).date()).days)


def _catalog_sha256(catalog: AssetCatalogService) -> str:
    document = catalog.document.model_dump(mode="json")
    payload = json.dumps(document, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
