"""Persist Apple SEC EDGAR foundation documents as auditable raw snapshots."""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from investment_analyst.core.models import Asset, RawRecord
from investment_analyst.providers.asset_config import SecAssetConfiguration
from investment_analyst.providers.fundamentals.sec_edgar import (
    APPLE_CIK,
    APPLE_ENTITY_NAME,
    APPLE_TICKER,
    SecAaplFetchResult,
    SecDocumentType,
    SecEdgarClient,
    SecEdgarDocument,
)
from investment_analyst.providers.fundamentals.sec_raw_records import (
    ASSET_ID,
    COMPANY_FACTS_SOURCE_ID,
    SUBMISSIONS_SOURCE_ID,
    create_sec_aapl_asset,
    expected_record_id,
    get_sec_source_definitions,
    sec_document_to_raw_record,
)
from investment_analyst.storage import LocalStorage
from investment_analyst.storage.errors import RecordNotFoundError, StorageError


@dataclass(frozen=True, slots=True)
class SecAaplImportSummary:
    """Compact result of one two-document Apple SEC snapshot import."""

    asset_id: str
    cik: str
    entity_name: str
    retrieved_at: datetime
    documents_received: int
    submissions_record_id: UUID
    companyfacts_record_id: UUID
    raw_records_created: int
    raw_records_reused: int
    observations_created: int
    metric_results_created: int
    diagnostics_created: int
    content_changed: bool
    traceability_verified: bool

    def to_json_dict(self) -> dict[str, object]:
        """Return an explicit JSON-compatible representation."""
        return {
            "asset_id": self.asset_id,
            "cik": self.cik,
            "entity_name": self.entity_name,
            "retrieved_at": self.retrieved_at.isoformat(),
            "documents_received": self.documents_received,
            "submissions_record_id": str(self.submissions_record_id),
            "companyfacts_record_id": str(self.companyfacts_record_id),
            "raw_records_created": self.raw_records_created,
            "raw_records_reused": self.raw_records_reused,
            "observations_created": self.observations_created,
            "metric_results_created": self.metric_results_created,
            "diagnostics_created": self.diagnostics_created,
            "content_changed": self.content_changed,
            "traceability_verified": self.traceability_verified,
        }


class SecAaplFundamentalsPipeline:
    """Fetch and persist only raw Apple SEC documents without analytics."""

    def __init__(
        self,
        storage: LocalStorage,
        client: SecEdgarClient,
        *,
        configuration: SecAssetConfiguration | None = None,
    ) -> None:
        self._storage = storage
        self._client = client
        self._configuration = configuration or SecAssetConfiguration(
            asset_id=ASSET_ID,
            cik=APPLE_CIK,
            ticker=APPLE_TICKER,
            submissions_source_id=SUBMISSIONS_SOURCE_ID,
            companyfacts_source_id=COMPANY_FACTS_SOURCE_ID,
        )
        expected = SecAssetConfiguration(
            asset_id=ASSET_ID,
            cik=APPLE_CIK,
            ticker=APPLE_TICKER,
            submissions_source_id=SUBMISSIONS_SOURCE_ID,
            companyfacts_source_id=COMPANY_FACTS_SOURCE_ID,
        )
        if self._configuration != expected:
            raise StorageError("SEC configuration does not match the current persisted identity")

    def run(self) -> SecAaplImportSummary:
        """Import both Apple SEC snapshots idempotently and verify storage integrity."""
        self._storage.require_open()
        observation_ids_before = {item.observation_id for item in self._storage.observations.list()}
        metric_ids_before = {item.result_id for item in self._storage.metric_results.list()}
        diagnostic_ids_before = {item.diagnostic_id for item in self._storage.diagnostics.list()}

        fetch = self._client.fetch_aapl_issuer_documents()
        candidates = self._prepare_candidates(fetch)
        existing_asset = self._get_existing_asset()
        target_asset = create_sec_aapl_asset(existing_asset)
        source_definitions = get_sec_source_definitions()

        prior_records = {
            source.source_id: tuple(self._storage.raw_records.list(source_id=source.source_id))
            for source in source_definitions
        }

        self._storage.assets.upsert(target_asset)
        for source in source_definitions:
            self._storage.sources.upsert(source)

        stored: dict[SecDocumentType, RawRecord] = {}
        created = 0
        reused = 0
        content_changed = False
        document_by_type = {document.document_type: document for document in fetch.documents}

        for document_type in (SecDocumentType.SUBMISSIONS, SecDocumentType.COMPANY_FACTS):
            candidate = candidates[document_type]
            try:
                record = self._storage.raw_records.get(candidate.record_id)
                reused += 1
            except RecordNotFoundError:
                if prior_records[candidate.source.source_id]:
                    content_changed = True
                self._storage.raw_records.save(candidate)
                record = self._storage.raw_records.get(candidate.record_id)
                created += 1
            self._verify_stored_record(record, document_by_type[document_type])
            stored[document_type] = record

        if {
            item.observation_id for item in self._storage.observations.list()
        } != observation_ids_before:
            raise StorageError("SEC raw import unexpectedly changed normalized observations")
        if {item.result_id for item in self._storage.metric_results.list()} != metric_ids_before:
            raise StorageError("SEC raw import unexpectedly changed metric results")
        if {
            item.diagnostic_id for item in self._storage.diagnostics.list()
        } != diagnostic_ids_before:
            raise StorageError("SEC raw import unexpectedly changed diagnostics")

        self._verify_round_trip(stored, target_asset)
        return SecAaplImportSummary(
            asset_id=ASSET_ID,
            cik=APPLE_CIK,
            entity_name=APPLE_ENTITY_NAME,
            retrieved_at=fetch.retrieved_at,
            documents_received=len(fetch.documents),
            submissions_record_id=stored[SecDocumentType.SUBMISSIONS].record_id,
            companyfacts_record_id=stored[SecDocumentType.COMPANY_FACTS].record_id,
            raw_records_created=created,
            raw_records_reused=reused,
            observations_created=0,
            metric_results_created=0,
            diagnostics_created=0,
            content_changed=content_changed,
            traceability_verified=True,
        )

    def _prepare_candidates(
        self,
        fetch: SecAaplFetchResult,
    ) -> dict[SecDocumentType, RawRecord]:
        if (
            fetch.cik != self._configuration.cik
            or fetch.ticker != self._configuration.ticker
            or fetch.entity_name.casefold() != APPLE_ENTITY_NAME.casefold()
        ):
            raise StorageError("SEC fetch result does not identify Apple")
        candidates: dict[SecDocumentType, RawRecord] = {}
        for document in fetch.documents:
            candidate = sec_document_to_raw_record(document)
            if candidate.record_id != expected_record_id(document):
                raise StorageError("SEC raw record UUID5 validation failed before persistence")
            self._validate_candidate(candidate, document)
            candidates[document.document_type] = candidate
        if set(candidates) != {SecDocumentType.SUBMISSIONS, SecDocumentType.COMPANY_FACTS}:
            raise StorageError("SEC fetch did not produce both required document types")
        return candidates

    def _get_existing_asset(self) -> Asset | None:
        try:
            return self._storage.assets.get(ASSET_ID)
        except RecordNotFoundError:
            return None

    def _validate_candidate(self, record: RawRecord, document: SecEdgarDocument) -> None:
        expected_source = (
            SUBMISSIONS_SOURCE_ID
            if document.document_type is SecDocumentType.SUBMISSIONS
            else COMPANY_FACTS_SOURCE_ID
        )
        if record.asset_id != ASSET_ID or record.source.source_id != expected_source:
            raise StorageError("SEC raw record asset or source is invalid")
        if record.source.checksum_sha256 != document.body_sha256:
            raise StorageError("SEC raw record source checksum is invalid")
        if record.source.raw_uri != document.request_url:
            raise StorageError("SEC raw record source URL is invalid")
        if record.event_time != document.retrieved_at:
            raise StorageError("SEC raw record event_time must equal retrieval time")
        if (
            record.available_at != document.retrieved_at
            or record.received_at != document.retrieved_at
        ):
            raise StorageError("SEC raw record availability timestamps are invalid")
        payload = record.payload
        if not isinstance(payload, dict):
            raise StorageError("SEC raw record payload must be an object")
        expected_payload = {
            "document_type": document.document_type.value,
            "cik": document.cik,
            "entity_name": document.entity_name,
            "body_sha256": document.body_sha256,
            "content_length": document.content_length,
            "document": document.body,
        }
        if payload != expected_payload:
            raise StorageError("SEC raw record payload does not match the validated document")
        if "user_agent" in str(payload).casefold():
            raise StorageError("SEC User-Agent must not appear in raw records")

    def _verify_stored_record(self, record: RawRecord, document: SecEdgarDocument) -> None:
        if record.record_id != expected_record_id(document):
            raise StorageError("stored SEC raw record has an unexpected identifier")
        if record.asset_id != ASSET_ID:
            raise StorageError("stored SEC raw record has an unexpected asset")
        if record.source.checksum_sha256 != document.body_sha256:
            raise StorageError("stored SEC raw record checksum does not match the response body")
        payload = record.payload
        if not isinstance(payload, dict):
            raise StorageError("stored SEC raw record payload must be an object")
        if payload.get("document") != document.body:
            raise StorageError("stored SEC document body differs from the fetched document")
        if payload.get("body_sha256") != document.body_sha256:
            raise StorageError("stored SEC payload checksum differs from the fetched checksum")
        if payload.get("document_type") != document.document_type.value:
            raise StorageError("stored SEC document type is inconsistent")

    def _verify_round_trip(
        self,
        records: dict[SecDocumentType, RawRecord],
        asset: Asset,
    ) -> None:
        if self._storage.assets.get(ASSET_ID) != asset:
            raise StorageError("SEC AAPL asset round-trip verification failed")
        for source in get_sec_source_definitions():
            if self._storage.sources.get(source.source_id) != source:
                raise StorageError("SEC source-definition round-trip verification failed")
        for record in records.values():
            if self._storage.raw_records.get(record.record_id) != record:
                raise StorageError("SEC raw-record round-trip verification failed")
