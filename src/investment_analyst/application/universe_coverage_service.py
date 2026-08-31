"""Compose catalog declarations with verified local market-bar evidence."""

import hashlib
import json
from datetime import datetime

from investment_analyst.analytics.market.bar_models import HistoricalBarQuery
from investment_analyst.analytics.market.history_service import HistoricalMarketDataService
from investment_analyst.application.market_universe import (
    MarketAssetDescriptor,
    build_market_asset_universe,
)
from investment_analyst.application.universe_coverage_models import (
    CoverageCapability,
    EvidenceState,
    UniverseCoverageAsset,
    UniverseCoverageRequest,
    UniverseCoverageResult,
    UniverseMarketCoverage,
)
from investment_analyst.catalog.provider_context import ProviderAssetContextResolver
from investment_analyst.catalog.service import AssetCatalogService
from investment_analyst.core.models import AssetClass
from investment_analyst.storage import LocalStorage
from investment_analyst.time_intervals import inclusive_utc_date_bounds


class UniverseCoverageService:
    """Read catalog-scoped coverage once without provider clients or writes."""

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
        """Return stable capability and local evidence summaries for selected assets."""
        universe = build_market_asset_universe(self._catalog, self._resolver)
        descriptors = {item.asset_id: item for item in universe.assets}
        asset_ids = request.asset_ids or tuple(sorted(descriptors))
        unknown = tuple(item for item in asset_ids if item not in descriptors)
        if unknown:
            raise ValueError(f"asset is not configured for daily market coverage: {unknown[0]}")
        start, end = inclusive_utc_date_bounds(request.market_start, request.market_end)
        history = HistoricalMarketDataService(self._storage)
        assets = tuple(
            self._asset_view(descriptors[asset_id], request, start, end, history)
            for asset_id in asset_ids
        )
        return UniverseCoverageResult(
            catalog_version=self._catalog.catalog_version,
            catalog_sha256=_catalog_sha256(self._catalog),
            request=request,
            assets=assets,
        )

    def _asset_view(
        self,
        market_descriptor: MarketAssetDescriptor,
        request: UniverseCoverageRequest,
        start: datetime,
        end: datetime,
        history: HistoricalMarketDataService,
    ) -> UniverseCoverageAsset:
        series = history.query(
            HistoricalBarQuery(
                asset_id=market_descriptor.asset_id,
                source_id=market_descriptor.source_id,
                start=start,
                end=end,
                known_at=request.known_at,
            )
        )
        latest = series.bars[-1] if series.bars else None
        asset = self._catalog.get(market_descriptor.asset_id)
        fundamentals = (
            CoverageCapability.SUPPORTED
            if market_descriptor.has_fundamentals
            else CoverageCapability.NOT_APPLICABLE
            if asset.asset_class is not AssetClass.EQUITY
            else CoverageCapability.NOT_CONFIGURED
        )
        valuation = (
            CoverageCapability.SUPPORTED
            if market_descriptor.has_corporate_valuation
            else CoverageCapability.NOT_APPLICABLE
            if asset.asset_class is not AssetClass.EQUITY
            else CoverageCapability.NOT_CONFIGURED
        )
        limitations = ["IEX market data is not consolidated SIP coverage"]
        if (
            valuation is CoverageCapability.NOT_CONFIGURED
            and asset.asset_class is AssetClass.EQUITY
        ):
            limitations.append("corporate valuation requires documented share-unit basis")
        return UniverseCoverageAsset(
            asset_id=asset.asset_id,
            symbol=asset.symbol,
            name=asset.name,
            asset_class=asset.asset_class,
            exchange=asset.exchange or "UNKNOWN",
            quote_currency=asset.quote_currency,
            market=UniverseMarketCoverage(
                capability=CoverageCapability.SUPPORTED,
                evidence=EvidenceState.PRESENT if series.bars else EvidenceState.MISSING,
                source_id=market_descriptor.source_id,
                volume_unit=market_descriptor.volume_unit,
                history_start=market_descriptor.default_market_start,
                bar_count=series.coverage.bar_count,
                candidate_versions=series.coverage.candidate_versions,
                discarded_revisions=series.coverage.discarded_revisions,
                earliest_timestamp=series.coverage.earliest_timestamp,
                latest_timestamp=series.coverage.latest_timestamp,
                latest_available_at=latest.available_at if latest else None,
            ),
            fundamentals=fundamentals,
            corporate_valuation=valuation,
            limitations=tuple(limitations),
        )


def _catalog_sha256(catalog: AssetCatalogService) -> str:
    document = catalog.document.model_dump(mode="json")
    payload = json.dumps(document, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
