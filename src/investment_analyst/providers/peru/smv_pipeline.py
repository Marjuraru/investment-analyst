"""Append-only persistence pipeline for official SMV registry snapshots."""

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from investment_analyst.providers.peru.smv_open_data import (
    SmvOpenDataDataset,
    SmvOpenDataFetch,
    validate_legal_name,
)
from investment_analyst.providers.peru.smv_raw_records import (
    StoredSmvRegistrySnapshot,
    create_smv_source,
    smv_fetch_to_raw_record,
    smv_snapshot_sha256,
    smv_source_id,
    stored_smv_snapshot_from_raw_record,
)
from investment_analyst.storage import LocalStorage, StorageError


class SmvRegistryClient(Protocol):
    """Provider operations required by the registry persistence pipeline."""

    def fetch_registered_company(self, legal_name: str) -> SmvOpenDataFetch: ...

    def fetch_registered_securities(self, legal_name: str) -> SmvOpenDataFetch: ...


@dataclass(frozen=True, slots=True)
class SmvRegistryDatasetImport:
    """Outcome for one independently persisted SMV dataset."""

    dataset: str
    source_id: str
    retrieved_at: datetime
    records_received: int
    raw_records_created: int
    raw_records_reused: int
    raw_record_id: str
    semantic_sha256: str
    traceability_verified: bool

    def to_json_dict(self) -> dict[str, object]:
        """Return deterministic JSON primitives."""
        return {
            "dataset": self.dataset,
            "source_id": self.source_id,
            "retrieved_at": self.retrieved_at.isoformat(),
            "records_received": self.records_received,
            "raw_records_created": self.raw_records_created,
            "raw_records_reused": self.raw_records_reused,
            "raw_record_id": self.raw_record_id,
            "semantic_sha256": self.semantic_sha256,
            "traceability_verified": self.traceability_verified,
        }


@dataclass(frozen=True, slots=True)
class SmvRegistryImportSummary:
    """Compact auditable outcome for one exact legal-name registry refresh."""

    legal_name: str
    company: SmvRegistryDatasetImport
    securities: SmvRegistryDatasetImport
    raw_records_created: int
    raw_records_reused: int
    traceability_verified: bool

    def to_json_dict(self) -> dict[str, object]:
        """Return deterministic JSON primitives."""
        return {
            "legal_name": self.legal_name,
            "company": self.company.to_json_dict(),
            "securities": self.securities.to_json_dict(),
            "raw_records_created": self.raw_records_created,
            "raw_records_reused": self.raw_records_reused,
            "traceability_verified": self.traceability_verified,
        }


class SmvRegistryPipeline:
    """Persist company and security snapshots while preserving earlier progress."""

    def __init__(self, storage: LocalStorage, client: SmvRegistryClient) -> None:
        self._storage = storage
        self._client = client

    def run(self, legal_name: str) -> SmvRegistryImportSummary:
        """Refresh both datasets sequentially using one writer connection."""
        self._storage.require_open()
        canonical_name = validate_legal_name(legal_name)
        assets_before = tuple(self._storage.assets.list_all())
        observations_before = self._storage.observations.count()
        metric_definitions_before = tuple(self._storage.metric_definitions.list_all())
        metric_results_before = self._storage.metric_results.count()
        diagnostics_before = self._storage.diagnostics.count()

        company = self._persist_fetch(
            self._client.fetch_registered_company(canonical_name),
            expected_dataset=SmvOpenDataDataset.REGISTERED_COMPANIES,
            expected_legal_name=canonical_name,
        )
        securities = self._persist_fetch(
            self._client.fetch_registered_securities(canonical_name),
            expected_dataset=SmvOpenDataDataset.REGISTERED_SECURITIES,
            expected_legal_name=canonical_name,
        )
        self._verify_analytical_isolation(
            assets_before=assets_before,
            observations_before=observations_before,
            metric_definitions_before=metric_definitions_before,
            metric_results_before=metric_results_before,
            diagnostics_before=diagnostics_before,
        )
        return SmvRegistryImportSummary(
            legal_name=canonical_name,
            company=company,
            securities=securities,
            raw_records_created=(company.raw_records_created + securities.raw_records_created),
            raw_records_reused=(company.raw_records_reused + securities.raw_records_reused),
            traceability_verified=True,
        )

    def _persist_fetch(
        self,
        fetch: SmvOpenDataFetch,
        *,
        expected_dataset: SmvOpenDataDataset,
        expected_legal_name: str,
    ) -> SmvRegistryDatasetImport:
        if (
            fetch.snapshot.dataset is not expected_dataset
            or fetch.snapshot.query_legal_name != expected_legal_name
        ):
            raise StorageError("SMV fetch result does not match the requested registry scope")
        source = create_smv_source(expected_dataset)
        self._storage.sources.upsert(source)
        semantic_sha256 = smv_snapshot_sha256(fetch.snapshot)
        equivalent = self._find_equivalent(
            dataset=expected_dataset,
            legal_name=expected_legal_name,
            semantic_sha256=semantic_sha256,
        )
        created = 0
        reused = 0
        if equivalent is None:
            candidate = smv_fetch_to_raw_record(fetch)
            self._storage.raw_records.save(candidate)
            stored = self._storage.raw_records.get(candidate.record_id)
            verified = stored_smv_snapshot_from_raw_record(stored)
            created = 1
        else:
            verified = equivalent
            reused = 1
        if (
            verified.snapshot != fetch.snapshot
            or verified.record.source.source_id != source.source_id
        ):
            raise StorageError("SMV persisted snapshot does not match the fetched semantics")
        if self._storage.sources.get(source.source_id) != source:
            raise StorageError("SMV source definition round-trip verification failed")
        records_received = (
            len(verified.snapshot.companies)
            if expected_dataset is SmvOpenDataDataset.REGISTERED_COMPANIES
            else len(verified.snapshot.securities)
        )
        return SmvRegistryDatasetImport(
            dataset=expected_dataset.value,
            source_id=source.source_id,
            retrieved_at=verified.record.received_at,
            records_received=records_received,
            raw_records_created=created,
            raw_records_reused=reused,
            raw_record_id=str(verified.record.record_id),
            semantic_sha256=semantic_sha256,
            traceability_verified=True,
        )

    def _find_equivalent(
        self,
        *,
        dataset: SmvOpenDataDataset,
        legal_name: str,
        semantic_sha256: str,
    ) -> StoredSmvRegistrySnapshot | None:
        matches: list[StoredSmvRegistrySnapshot] = []
        for record in self._storage.raw_records.list(source_id=smv_source_id(dataset)):
            verified = stored_smv_snapshot_from_raw_record(record)
            if (
                verified.metadata.query_legal_name == legal_name
                and verified.metadata.semantic_sha256 == semantic_sha256
            ):
                matches.append(verified)
        if not matches:
            return None
        matches.sort(key=lambda item: (item.record.available_at, str(item.record.record_id)))
        first = matches[0]
        if any(item.snapshot != first.snapshot for item in matches[1:]):
            raise StorageError("equivalent SMV semantic identities contain conflicting snapshots")
        return first

    def _verify_analytical_isolation(
        self,
        *,
        assets_before: tuple[object, ...],
        observations_before: int,
        metric_definitions_before: tuple[object, ...],
        metric_results_before: int,
        diagnostics_before: int,
    ) -> None:
        if tuple(self._storage.assets.list_all()) != assets_before:
            raise StorageError("SMV registry refresh must not mutate persisted assets")
        if self._storage.observations.count() != observations_before:
            raise StorageError("SMV registry refresh must not create observations")
        if tuple(self._storage.metric_definitions.list_all()) != metric_definitions_before:
            raise StorageError("SMV registry refresh must not create metric definitions")
        if self._storage.metric_results.count() != metric_results_before:
            raise StorageError("SMV registry refresh must not create metric results")
        if self._storage.diagnostics.count() != diagnostics_before:
            raise StorageError("SMV registry refresh must not create diagnostics")


__all__ = [
    "SmvRegistryClient",
    "SmvRegistryDatasetImport",
    "SmvRegistryImportSummary",
    "SmvRegistryPipeline",
]
