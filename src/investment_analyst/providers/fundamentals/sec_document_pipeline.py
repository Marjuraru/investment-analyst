"""Append-only import of selected official SEC primary filing documents."""

from __future__ import annotations

from dataclasses import dataclass

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
from investment_analyst.providers.fundamentals.sec_document_client import SecDocumentClient
from investment_analyst.providers.fundamentals.sec_fact_models import SUBMISSIONS_SCHEMA_VERSION
from investment_analyst.providers.fundamentals.sec_filing_index import SecFilingIndex
from investment_analyst.storage import LocalStorage, StorageError


class SecDocumentPipelineError(StorageError):
    """An import request is inconsistent with persisted submissions evidence."""


@dataclass(frozen=True, slots=True)
class SecDocumentImportRequest:
    forms: tuple[str, ...] = ()
    accessions: tuple[str, ...] = ()
    limit_per_form: int = 1

    def __post_init__(self) -> None:
        if bool(self.forms) == bool(self.accessions):
            raise SecDocumentPipelineError("provide exactly one of forms or accessions")
        if self.limit_per_form < 1:
            raise SecDocumentPipelineError("limit_per_form must be positive")
        if len(set(self.forms)) != len(self.forms) or len(set(self.accessions)) != len(
            self.accessions
        ):
            raise SecDocumentPipelineError("document selection contains duplicate values")
        if any(form not in FINANCIAL_SEC_FORMS for form in self.forms):
            raise SecDocumentPipelineError("document selection includes an unsupported SEC form")


@dataclass(frozen=True, slots=True)
class SecDocumentImportSummary:
    asset_id: str
    submissions_raw_record_id: str
    revisions_created: int
    revisions_reused: int
    blobs_created: int
    blobs_reused: int
    revisions: tuple[SecDocumentRevision, ...]

    def to_json_dict(self) -> dict[str, object]:
        return {
            "asset_id": self.asset_id,
            "submissions_raw_record_id": self.submissions_raw_record_id,
            "revisions_created": self.revisions_created,
            "revisions_reused": self.revisions_reused,
            "blobs_created": self.blobs_created,
            "blobs_reused": self.blobs_reused,
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
        index = SecFilingIndex.from_raw_record(submissions, self._configuration)
        filings = self._select(index, request)
        repository = SecDocumentRepository(self._storage.raw_records, self._storage.documents)
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
        created = reused = blobs_created = blobs_reused = 0
        revisions: list[SecDocumentRevision] = []
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
            response = self._client.fetch(document)
            revision_id = SecDocumentRevision.expected_id(
                document.document_id, response.sha256, REVISION_SCHEMA_VERSION_V2
            )
            existing = repository.get_revision(revision_id)
            if existing is not None:
                if (
                    existing.asset_id != self._configuration.asset_id
                    or existing.document != document
                    or existing.content_sha256 != response.sha256
                ):
                    raise SecDocumentPipelineError("existing SEC document revision conflicts")
                repository.verify_revision(existing)
                revisions.append(existing)
                reused += 1
                blobs_reused += 1
                continue
            receipt = self._storage.documents.put(response.content)
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
            self._storage.raw_records.save(revision_to_raw_record(revision))
            repository.verify_revision(revision)
            revisions.append(revision)
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
        )

    def _latest_submissions(self):
        records = self._storage.raw_records.list(
            asset_id=self._configuration.asset_id,
            source_id=self._configuration.submissions_source_id,
            schema_version=SUBMISSIONS_SCHEMA_VERSION,
        )
        if not records:
            raise SecDocumentPipelineError("no persisted SEC submissions snapshot is eligible")
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
                        "requested accession is absent or ineligible"
                    ) from error
            return tuple(
                sorted(selected, key=lambda item: (item.acceptance_at, item.accession_number))
            )
        selected = []
        for form in sorted(request.forms):
            candidates = [item for item in index.all() if item.form == form]
            selected.extend(candidates[-request.limit_per_form :])
        return tuple(sorted(selected, key=lambda item: (item.acceptance_at, item.accession_number)))
