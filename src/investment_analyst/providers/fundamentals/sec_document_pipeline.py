"""Append-only import of selected official SEC primary filing documents."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from investment_analyst.core.models import SourceDefinition, SourceType
from investment_analyst.evidence.sec_documents.models import (
    FINANCIAL_SEC_FORMS,
    REVISION_SCHEMA_VERSION_V2,
    SEC_DOCUMENT_SOURCE_ID,
    SecDocumentRevision,
    SecFiling,
    SecLogicalDocument,
)
from investment_analyst.evidence.sec_documents.repository import (
    SecDocumentRepository,
    revision_to_raw_record,
)
from investment_analyst.providers.asset_config import SecAssetConfiguration
from investment_analyst.providers.failure_reasons import ProviderFailureReason
from investment_analyst.providers.fundamentals.sec_document_client import SecDocumentClient
from investment_analyst.providers.fundamentals.sec_fact_models import SUBMISSIONS_SCHEMA_VERSION
from investment_analyst.providers.fundamentals.sec_filing_index import (
    AmbiguousSecFilingError,
    SecFilingIndex,
    SecFilingIndexError,
)
from investment_analyst.storage import LocalStorage, StorageError


class SecDocumentPipelineError(StorageError):
    """An import request is inconsistent with persisted submissions evidence."""

    def __init__(self, message: str, *, reason_code: str | None = None) -> None:
        self.reason_code = reason_code
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class SecDocumentImportRequest:
    forms: tuple[str, ...] = ()
    accessions: tuple[str, ...] = ()
    limit_per_form: int = 1

    def __post_init__(self) -> None:
        if bool(self.forms) == bool(self.accessions):
            raise SecDocumentPipelineError(
                "provide exactly one of forms or accessions",
                reason_code=ProviderFailureReason.SEC_DOCUMENT_REQUEST_INVALID,
            )
        if self.limit_per_form < 1:
            raise SecDocumentPipelineError(
                "limit_per_form must be positive",
                reason_code=ProviderFailureReason.SEC_DOCUMENT_REQUEST_INVALID,
            )
        if len(set(self.forms)) != len(self.forms) or len(set(self.accessions)) != len(
            self.accessions
        ):
            raise SecDocumentPipelineError(
                "document selection contains duplicate values",
                reason_code=ProviderFailureReason.SEC_DOCUMENT_SELECTION_INVALID,
            )
        if any(form not in FINANCIAL_SEC_FORMS for form in self.forms):
            raise SecDocumentPipelineError(
                "document selection includes an unsupported SEC form",
                reason_code=ProviderFailureReason.SEC_DOCUMENT_SELECTION_INVALID,
            )


@dataclass(frozen=True, slots=True)
class SecDocumentImportSummary:
    asset_id: str
    submissions_raw_record_id: str
    revisions_created: int
    revisions_reused: int
    blobs_created: int
    blobs_reused: int
    revisions: tuple[SecDocumentRevision, ...]
    accessions_fetched: tuple[str, ...] = ()
    accessions_reused: tuple[str, ...] = ()
    document_fetch_calls: int = 0

    def to_json_dict(self) -> dict[str, object]:
        return {
            "asset_id": self.asset_id,
            "submissions_raw_record_id": self.submissions_raw_record_id,
            "revisions_created": self.revisions_created,
            "revisions_reused": self.revisions_reused,
            "blobs_created": self.blobs_created,
            "blobs_reused": self.blobs_reused,
            "accessions_fetched": list(self.accessions_fetched),
            "accessions_reused": list(self.accessions_reused),
            "document_fetch_calls": self.document_fetch_calls,
            "revisions": [
                {
                    "accession": item.document.filing.accession,
                    "form": item.document.filing.form,
                    "revision_id": str(item.revision_id),
                    "raw_record_id": str(item.raw_record_id),
                    "content_sha256": item.content_sha256,
                    "content_size_bytes": item.content_size_bytes,
                    "available_at": item.available_at.isoformat(),
                    "source_url": item.source_url,
                }
                for item in self.revisions
            ],
        }


class SecDocumentPipeline:
    """Use a persisted Submissions snapshot to import bounded primary documents."""

    def __init__(
        self,
        storage: LocalStorage,
        client: SecDocumentClient,
        *,
        configuration: SecAssetConfiguration,
    ) -> None:
        self._storage = storage
        self._client = client
        self._configuration = configuration

    def run(self, request: SecDocumentImportRequest) -> SecDocumentImportSummary:
        self._storage.require_open()
        submissions = self._latest_submissions()
        try:
            index = SecFilingIndex.from_raw_record(submissions, self._configuration)
        except AmbiguousSecFilingError as error:
            raise SecDocumentPipelineError(
                "persisted SEC submissions snapshot has ambiguous filing identity",
                reason_code=ProviderFailureReason.SEC_DOCUMENT_SNAPSHOT_AMBIGUOUS,
            ) from error
        except SecFilingIndexError as error:
            raise SecDocumentPipelineError(
                "persisted SEC submissions snapshot failed index validation",
                reason_code=ProviderFailureReason.SEC_DOCUMENT_SNAPSHOT_INVALID,
            ) from error
        filings = self._select(index, request)
        repository = SecDocumentRepository(self._storage.raw_records, self._storage.documents)
        try:
            self._storage.sources.upsert(
                SourceDefinition(
                    source_id=SEC_DOCUMENT_SOURCE_ID,
                    provider_name="U.S. Securities and Exchange Commission",
                    dataset_name="EDGAR primary filing documents",
                    source_type=SourceType.DOCUMENTS,
                    base_url="https://www.sec.gov",
                    is_official=True,
                    coverage_notes="Selected primary 10-K, 10-Q, 20-F, and 40-F filings only.",
                )
            )
        except StorageError as error:
            raise SecDocumentPipelineError(
                "SEC document source persistence failed",
                reason_code=ProviderFailureReason.SEC_DOCUMENT_REVISION_PERSIST_FAILED,
            ) from error
        created = reused = blobs_created = blobs_reused = document_fetch_calls = 0
        revisions: list[SecDocumentRevision] = []
        accessions_fetched: list[str] = []
        accessions_reused: list[str] = []
        for metadata in filings:
            filing = SecFiling(
                filing_id=SecFiling.expected_id(self._configuration.cik, metadata.accession_number),
                filer_cik=self._configuration.cik,
                accession=metadata.accession_number,
                form=metadata.form,
                filing_date=metadata.filing_date,
                report_date=metadata.report_date,
                accepted_at=metadata.acceptance_at,
                is_amendment=metadata.is_amendment,
            )
            document = SecLogicalDocument(
                document_id=SecLogicalDocument.expected_id(
                    filing.filing_id, metadata.primary_document
                ),
                filing=filing,
                name=metadata.primary_document,
            )
            try:
                existing_candidates = repository.list_revisions(
                    asset_id=self._configuration.asset_id,
                    known_at=datetime.max.replace(tzinfo=UTC),
                    accession=metadata.accession_number,
                )
            except StorageError as error:
                raise SecDocumentPipelineError(
                    "SEC document revision lookup failed",
                    reason_code=ProviderFailureReason.SEC_DOCUMENT_REVISION_READ_FAILED,
                ) from error
            if existing_candidates:
                if len(existing_candidates) != 1:
                    raise SecDocumentPipelineError(
                        "existing SEC document accession is ambiguous",
                        reason_code=ProviderFailureReason.SEC_DOCUMENT_REVISION_AMBIGUOUS,
                    )
                existing = existing_candidates[0]
                if (
                    existing.asset_id != self._configuration.asset_id
                    or existing.document != document
                ):
                    raise SecDocumentPipelineError(
                        "existing SEC document revision conflicts",
                        reason_code=ProviderFailureReason.SEC_DOCUMENT_REVISION_CONFLICT,
                    )
                try:
                    repository.verify_revision(existing)
                except StorageError as error:
                    raise SecDocumentPipelineError(
                        "existing SEC document revision could not be verified",
                        reason_code=ProviderFailureReason.SEC_DOCUMENT_REVISION_VERIFY_FAILED,
                    ) from error
                revisions.append(existing)
                accessions_reused.append(metadata.accession_number)
                reused += 1
                blobs_reused += 1
                continue
            try:
                response = self._client.fetch(document)
            except Exception as error:  # noqa: BLE001 - keep provider failure as the cause
                raise SecDocumentPipelineError(
                    "SEC primary document fetch failed",
                    reason_code=ProviderFailureReason.SEC_DOCUMENT_FETCH_FAILED,
                ) from error
            document_fetch_calls += 1
            revision_id = SecDocumentRevision.expected_id(
                document.document_id, response.sha256, REVISION_SCHEMA_VERSION_V2
            )
            try:
                existing = repository.get_revision(revision_id)
            except StorageError as error:
                raise SecDocumentPipelineError(
                    "SEC document revision lookup failed",
                    reason_code=ProviderFailureReason.SEC_DOCUMENT_REVISION_READ_FAILED,
                ) from error
            if existing is not None:
                if (
                    existing.asset_id != self._configuration.asset_id
                    or existing.document != document
                    or existing.content_sha256 != response.sha256
                ):
                    raise SecDocumentPipelineError(
                        "existing SEC document revision conflicts",
                        reason_code=ProviderFailureReason.SEC_DOCUMENT_REVISION_CONFLICT,
                    )
                try:
                    repository.verify_revision(existing)
                except StorageError as error:
                    raise SecDocumentPipelineError(
                        "existing SEC document revision could not be verified",
                        reason_code=ProviderFailureReason.SEC_DOCUMENT_REVISION_VERIFY_FAILED,
                    ) from error
                revisions.append(existing)
                accessions_reused.append(metadata.accession_number)
                reused += 1
                blobs_reused += 1
                continue
            try:
                receipt = self._storage.documents.put(response.content)
            except StorageError as error:
                raise SecDocumentPipelineError(
                    "SEC document blob persistence failed",
                    reason_code=ProviderFailureReason.SEC_DOCUMENT_BLOB_PERSIST_FAILED,
                ) from error
            revision = SecDocumentRevision(
                revision_id=revision_id,
                asset_id=self._configuration.asset_id,
                document=document,
                raw_record_id=SecDocumentRevision.expected_raw_record_id(revision_id),
                discovery_raw_record_id=submissions.record_id,
                content_sha256=receipt.sha256,
                content_size_bytes=receipt.size_bytes,
                available_at=filing.accepted_at,
                retrieved_at=response.retrieved_at,
                source_url=response.url,
                revision_schema_version=REVISION_SCHEMA_VERSION_V2,
            )
            try:
                self._storage.raw_records.save(revision_to_raw_record(revision))
            except StorageError as error:
                raise SecDocumentPipelineError(
                    "SEC document revision persistence failed",
                    reason_code=ProviderFailureReason.SEC_DOCUMENT_REVISION_PERSIST_FAILED,
                ) from error
            try:
                repository.verify_revision(revision)
            except StorageError as error:
                raise SecDocumentPipelineError(
                    "SEC document revision could not be verified",
                    reason_code=ProviderFailureReason.SEC_DOCUMENT_REVISION_VERIFY_FAILED,
                ) from error
            revisions.append(revision)
            accessions_fetched.append(metadata.accession_number)
            created += 1
            blobs_created += int(receipt.created)
            blobs_reused += int(not receipt.created)
        return SecDocumentImportSummary(
            asset_id=self._configuration.asset_id,
            submissions_raw_record_id=str(submissions.record_id),
            revisions_created=created,
            revisions_reused=reused,
            blobs_created=blobs_created,
            blobs_reused=blobs_reused,
            revisions=tuple(revisions),
            accessions_fetched=tuple(accessions_fetched),
            accessions_reused=tuple(accessions_reused),
            document_fetch_calls=document_fetch_calls,
        )

    def _latest_submissions(self):
        try:
            records = self._storage.raw_records.list(
                asset_id=self._configuration.asset_id,
                source_id=self._configuration.submissions_source_id,
                schema_version=SUBMISSIONS_SCHEMA_VERSION,
            )
        except StorageError as error:
            raise SecDocumentPipelineError(
                "persisted SEC submissions snapshot could not be read",
                reason_code=ProviderFailureReason.SEC_DOCUMENT_SNAPSHOT_READ_FAILED,
            ) from error
        if not records:
            raise SecDocumentPipelineError(
                "no persisted SEC submissions snapshot is eligible",
                reason_code=ProviderFailureReason.SEC_DOCUMENT_SNAPSHOT_MISSING,
            )
        return max(
            records,
            key=lambda item: (item.available_at, item.received_at, str(item.record_id)),
        )

    def _select(self, index: SecFilingIndex, request: SecDocumentImportRequest):
        by_accession = {item.accession_number: item for item in index.all()}
        if request.accessions:
            selected = []
            for accession in request.accessions:
                try:
                    selected.append(by_accession[accession])
                except KeyError as error:
                    raise SecDocumentPipelineError(
                        "requested accession is absent or ineligible",
                        reason_code=ProviderFailureReason.SEC_DOCUMENT_SELECTION_MISSING,
                    ) from error
            return tuple(
                sorted(selected, key=lambda item: (item.acceptance_at, item.accession_number))
            )
        selected = []
        for form in sorted(request.forms):
            candidates = [item for item in index.all() if item.form == form]
            selected.extend(candidates[-request.limit_per_form :])
        return tuple(sorted(selected, key=lambda item: (item.acceptance_at, item.accession_number)))
