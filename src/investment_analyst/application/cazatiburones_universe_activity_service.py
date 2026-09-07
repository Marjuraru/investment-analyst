"""Compose a read-only, point-in-time universe index for Cazatiburones evidence."""

import hashlib
import json
from collections.abc import Callable
from datetime import UTC, datetime

from investment_analyst.application.analysis_capabilities import AssetAnalysisFamily
from investment_analyst.application.cazatiburones_universe_activity_models import (
    CazatiburonesUniverseActivityAsset,
    CazatiburonesUniverseActivityFamily,
    CazatiburonesUniverseActivityRequest,
    CazatiburonesUniverseActivityResult,
)
from investment_analyst.application.market_universe import (
    MarketAssetDescriptor,
    build_market_asset_universe,
)
from investment_analyst.application.universe_coverage_models import (
    CoverageCapability,
    EvidenceState,
)
from investment_analyst.catalog.models import CatalogAsset
from investment_analyst.catalog.provider_context import ProviderAssetContextResolver
from investment_analyst.catalog.service import AssetCatalogService
from investment_analyst.core.models import NormalizedObservation
from investment_analyst.evidence.sec_beneficial_ownership.models import (
    BeneficialOwnershipStatement,
)
from investment_analyst.evidence.sec_beneficial_ownership.repository import (
    BeneficialOwnershipRepository,
)
from investment_analyst.evidence.sec_institutional_observations.definitions import SOURCE_ID
from investment_analyst.evidence.sec_ownership.models import OwnershipStatement
from investment_analyst.evidence.sec_ownership.repository import OwnershipRepository
from investment_analyst.storage import LocalStorage, StorageError

_LIMITATIONS = (
    "counts are as-filed evidence; amendments are not resolved",
    "features remain independent per asset and per family",
    "institutional counts use persisted 13F observations without parent verification",
)
_EvidenceRecord = OwnershipStatement | BeneficialOwnershipStatement | NormalizedObservation


class CazatiburonesUniverseActivityService:
    """Query three independent SEC evidence families without persistence or aggregation."""

    def __init__(
        self,
        storage: LocalStorage,
        catalog: AssetCatalogService,
        resolver: ProviderAssetContextResolver,
    ) -> None:
        self._storage = storage
        self._catalog = catalog
        self._resolver = resolver

    def query(
        self, request: CazatiburonesUniverseActivityRequest
    ) -> CazatiburonesUniverseActivityResult:
        """Return configured SEC issuers or an explicitly selected catalog subset."""
        if not self._storage.read_only:
            raise StorageError("universe activity query requires read-only storage")
        market_universe = build_market_asset_universe(self._catalog, self._resolver)
        descriptors = {item.asset_id: item for item in market_universe.assets}
        catalog_assets = {item.asset_id: item for item in self._catalog.list_assets()}
        asset_ids = request.asset_ids or tuple(
            sorted(
                item.asset_id
                for item in market_universe.assets
                if item.has_fundamentals
                and item.analysis.family is AssetAnalysisFamily.LISTED_COMPANY
            )
        )
        unknown = tuple(asset_id for asset_id in asset_ids if asset_id not in catalog_assets)
        if unknown:
            raise ValueError(f"asset is not configured in the catalog: {unknown[0]}")

        assets = tuple(
            self._asset_view(
                catalog_assets[asset_id],
                descriptors.get(asset_id),
                request.known_at,
            )
            for asset_id in asset_ids
        )
        return CazatiburonesUniverseActivityResult(
            catalog_version=self._catalog.catalog_version,
            catalog_sha256=_catalog_sha256(self._catalog),
            request=request,
            assets=assets,
        )

    def _asset_view(
        self,
        asset: CatalogAsset,
        descriptor: MarketAssetDescriptor | None,
        known_at: datetime,
    ) -> CazatiburonesUniverseActivityAsset:
        if not _is_sec_configured(descriptor):
            family = _not_configured_family()
            return CazatiburonesUniverseActivityAsset(
                asset_id=asset.asset_id,
                symbol=asset.symbol,
                name=asset.name,
                insider=family,
                beneficial=family,
                institutional=family,
                limitations=_LIMITATIONS,
            )

        insider = self._read_family(
            lambda: OwnershipRepository(self._storage.raw_records).list(
                asset_id=asset.asset_id,
                known_at=known_at,
            ),
            known_at,
        )
        beneficial = self._read_family(
            lambda: BeneficialOwnershipRepository(self._storage.raw_records).list(
                asset_id=asset.asset_id,
                known_at=known_at,
            ),
            known_at,
        )
        institutional = self._read_family(
            lambda: self._storage.observations.list(
                asset_id=asset.asset_id,
                source_id=SOURCE_ID,
                available_to=known_at,
            ),
            known_at,
        )
        return CazatiburonesUniverseActivityAsset(
            asset_id=asset.asset_id,
            symbol=asset.symbol,
            name=asset.name,
            insider=insider,
            beneficial=beneficial,
            institutional=institutional,
            limitations=_LIMITATIONS,
        )

    @staticmethod
    def _read_family(
        reader: Callable[[], list[_EvidenceRecord]],
        known_at: datetime,
    ) -> CazatiburonesUniverseActivityFamily:
        try:
            records = [record for record in reader() if record.available_at <= known_at]
        except (StorageError, ValueError):
            return CazatiburonesUniverseActivityFamily(
                capability=CoverageCapability.SUPPORTED,
                evidence=EvidenceState.NOT_QUERIED,
                statements=0,
                not_evaluable_reason="malformed_persisted_record",
            )
        latest = max((record.available_at for record in records), default=None)
        return CazatiburonesUniverseActivityFamily(
            capability=CoverageCapability.SUPPORTED,
            evidence=EvidenceState.PRESENT if records else EvidenceState.MISSING,
            statements=len(records),
            latest_available_at=latest,
            latest_age_days=_age_days(known_at, latest),
        )


def _is_sec_configured(descriptor: MarketAssetDescriptor | None) -> bool:
    return (
        descriptor is not None
        and descriptor.has_fundamentals
        and (descriptor.analysis.family is AssetAnalysisFamily.LISTED_COMPANY)
    )


def _not_configured_family() -> CazatiburonesUniverseActivityFamily:
    return CazatiburonesUniverseActivityFamily(
        capability=CoverageCapability.NOT_CONFIGURED,
        evidence=EvidenceState.NOT_QUERIED,
        statements=0,
    )


def _age_days(known_at: datetime, reference_at: datetime | None) -> int | None:
    if reference_at is None:
        return None
    return max(0, (known_at.astimezone(UTC).date() - reference_at.astimezone(UTC).date()).days)


def _catalog_sha256(catalog: AssetCatalogService) -> str:
    document = catalog.document.model_dump(mode="json")
    payload = json.dumps(document, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
