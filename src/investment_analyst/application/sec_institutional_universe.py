"""Application service for refreshing and querying the SEC 13F manager universe."""

from __future__ import annotations

from collections.abc import Callable

from investment_analyst.application.runtime import (
    ApplicationRuntime,
    StorageLocationRequest,
)
from investment_analyst.application.sec_institutional_universe_models import (
    SecInstitutionalUniverseQueryRequest,
    SecInstitutionalUniverseQueryResult,
    SecInstitutionalUniverseRefreshRequest,
    SecInstitutionalUniverseRefreshResult,
)
from investment_analyst.catalog.service import AssetCatalogService
from investment_analyst.evidence.sec_documents.models import normalize_cik
from investment_analyst.evidence.sec_institutional_universe.repository import (
    SecInstitutionalUniverseRepository,
)
from investment_analyst.evidence.sec_institutional_universe.service import (
    SecInstitutionalUniverseService,
)
from investment_analyst.providers.fundamentals.sec_edgar import SecEdgarIdentity
from investment_analyst.providers.http import HttpTransport, UrlLibHttpTransport
from investment_analyst.providers.institutional_holdings.sec_13f_data_sets import (
    Sec13FDataSetDownload,
    Sec13FDataSetsClient,
)
from investment_analyst.workspace.models import WorkspaceAccessMode


class SecInstitutionalUniverseApplicationError(RuntimeError):
    """Failure executing manager universe application operations."""


def resolve_catalog_sec_cusip_mappings(catalog: AssetCatalogService) -> dict[str, str]:
    """Extract all active sec/cusip bindings from catalog as {cusip: asset_id}."""
    mappings: dict[str, str] = {}
    for asset in catalog.list_assets():
        for binding in asset.provider_bindings:
            if binding.provider == "sec" and binding.namespace == "cusip":
                cusip = binding.identifier.upper().strip()
                if cusip in mappings and mappings[cusip] != asset.asset_id:
                    raise SecInstitutionalUniverseApplicationError(
                        f"Ambiguous CUSIP {cusip} maps to multiple assets: "
                        f"{mappings[cusip]}, {asset.asset_id}"
                    )
                mappings[cusip] = asset.asset_id

    if not mappings:
        raise SecInstitutionalUniverseApplicationError(
            "Catalog exposes zero assets with sec/cusip bindings"
        )
    return mappings


class SecInstitutionalUniverseApplication:
    """Isolated application facade for official Form 13F manager universe operations."""

    def __init__(
        self,
        runtime: ApplicationRuntime,
        *,
        transport_factory: Callable[[], HttpTransport] = UrlLibHttpTransport,
        service: SecInstitutionalUniverseService | None = None,
    ) -> None:
        self._runtime = runtime
        self._transport_factory = transport_factory
        self._service = service or SecInstitutionalUniverseService()

    @classmethod
    def create_default(cls) -> SecInstitutionalUniverseApplication:
        return cls(ApplicationRuntime.create_default())

    def refresh_with_storage(
        self,
        storage,
        request: SecInstitutionalUniverseRefreshRequest,
        *,
        sec_identity: SecEdgarIdentity,
        download: Sec13FDataSetDownload | None = None,
    ) -> SecInstitutionalUniverseRefreshResult:
        """Validate, select, and persist Form 13F manager universe under an open connection."""
        del request
        catalog_cusips = resolve_catalog_sec_cusip_mappings(self._runtime.catalog)
        if download is None:
            client = Sec13FDataSetsClient(
                identity=sec_identity,
                transport=self._transport_factory(),
            )
            download = client.fetch_latest_dataset()

        repository = SecInstitutionalUniverseRepository(storage.raw_records, storage.documents)
        receipt = repository.save_blob(download.content)

        from investment_analyst.evidence.sec_institutional_universe.identity import (
            dataset_revision_id,
        )

        expected_rev_id = dataset_revision_id(
            download.period_start,
            download.period_end,
            download.sha256,
        )
        existing_rev = repository.get_dataset_revision(expected_rev_id)
        if existing_rev is not None:
            revision = existing_rev
            snapshot = self._service.build_universe_snapshot(
                download.content,
                dataset_revision=revision,
                catalog_cusips=catalog_cusips,
                catalog_version=self._runtime.catalog.catalog_version,
            )
            existing_snap = repository.get_snapshot(snapshot.snapshot_id)
            if existing_snap is not None:
                snapshot = existing_snap
            else:
                repository.save_snapshot(snapshot)
        else:
            revision, snapshot = self._service.build_universe_from_download(
                download,
                catalog_cusips=catalog_cusips,
                catalog_version=self._runtime.catalog.catalog_version,
            )
            repository.save_dataset_revision(revision)
            repository.save_snapshot(snapshot)

        return SecInstitutionalUniverseRefreshResult(
            revision_id=revision.revision_id,
            snapshot_id=snapshot.snapshot_id,
            period_start=snapshot.period_start,
            period_end=snapshot.period_end,
            dataset_url=download.url,
            dataset_sha256=snapshot.dataset_sha256,
            size_bytes=download.size_bytes,
            retrieved_at=snapshot.retrieved_at,
            available_at=snapshot.available_at,
            eligible_asset_count=snapshot.eligible_asset_count,
            matched_asset_count=snapshot.matched_asset_count,
            candidate_manager_count=snapshot.candidate_manager_count,
            selected_manager_count=snapshot.selected_manager_count,
            unselected_manager_count=snapshot.unselected_manager_count,
            coverage_complete=snapshot.coverage_complete,
            created=receipt.created,
        )

    def refresh(
        self,
        request: SecInstitutionalUniverseRefreshRequest,
        *,
        sec_identity: SecEdgarIdentity,
        location: StorageLocationRequest | None = None,
    ) -> SecInstitutionalUniverseRefreshResult:
        """Fetch latest official SEC dataset archive, validate, select, and persist append-only."""
        storage_req = location or StorageLocationRequest()
        with self._runtime.open_storage(
            storage_req, access_mode=WorkspaceAccessMode.READ_WRITE
        ) as storage:
            return self.refresh_with_storage(
                storage,
                request,
                sec_identity=sec_identity,
            )

    def query(
        self,
        request: SecInstitutionalUniverseQueryRequest,
        *,
        location: StorageLocationRequest | None = None,
    ) -> SecInstitutionalUniverseQueryResult:
        """Point-in-time read-only query of the latest available manager universe snapshot."""
        storage_req = location or StorageLocationRequest()

        with self._runtime.open_storage(
            storage_req, access_mode=WorkspaceAccessMode.READ_ONLY
        ) as storage:
            repository = SecInstitutionalUniverseRepository(storage.raw_records, storage.documents)
            snapshot = repository.find_latest_snapshot(known_at=request.known_at)

            if snapshot is None:
                cut_iso = request.known_at.isoformat()
                raise SecInstitutionalUniverseApplicationError(
                    f"No Form 13F manager universe snapshot available at or before {cut_iso}"
                )

            filtered = list(snapshot.candidates)
            if request.asset_id is not None:
                filtered = [c for c in filtered if c.asset_id == request.asset_id]
            if request.cik is not None:
                normalized_target_cik = normalize_cik(request.cik)
                filtered = [c for c in filtered if c.manager_cik == normalized_target_cik]

            return SecInstitutionalUniverseQueryResult(
                snapshot=snapshot,
                filtered_candidates=tuple(filtered),
            )
