"""Coordinate a fresh SEC Submissions snapshot with primary-document reuse."""

from datetime import datetime
from typing import Protocol

from investment_analyst.application.sec_document_refresh_models import (
    SecPrimaryDocumentRefreshRequest,
    SecPrimaryDocumentRefreshSummary,
)
from investment_analyst.core.models import Asset, RawRecord
from investment_analyst.evidence.sec_documents.models import SEC_DOCUMENT_SOURCE_ID
from investment_analyst.providers.asset_config import SecAssetConfiguration
from investment_analyst.providers.fundamentals.sec_document_pipeline import (
    SecDocumentImportRequest,
    SecDocumentImportSummary,
    SecDocumentPipeline,
    SecDocumentPipelineError,
)
from investment_analyst.providers.fundamentals.sec_edgar import (
    SecEdgarClient,
    SecEdgarDocument,
)
from investment_analyst.providers.fundamentals.sec_filing_index import SecFilingIndex
from investment_analyst.providers.fundamentals.sec_raw_records import (
    create_sec_asset,
    create_sec_submissions_source,
    sec_document_to_raw_record,
)
from investment_analyst.storage import LocalStorage
from investment_analyst.storage.errors import RecordNotFoundError, StorageError


class SecPrimaryDocumentRefreshError(RuntimeError):
    """A fresh SEC snapshot or its document coverage cannot be safely reconciled."""


class _IssuerSnapshotClient(Protocol):
    def fetch_submissions(self) -> SecEdgarDocument:
        """Fetch exactly one validated SEC Submissions snapshot."""
        ...


class _DocumentPipeline(Protocol):
    def run(self, request: SecDocumentImportRequest) -> SecDocumentImportSummary:
        """Import selected document revisions append-only."""
        ...


class SecPrimaryDocumentRefreshService:
    """Refresh the newest eligible primary document for every supported SEC form."""

    def __init__(
        self,
        storage: LocalStorage,
        *,
        configuration: SecAssetConfiguration,
        issuer_client: _IssuerSnapshotClient,
        document_pipeline: _DocumentPipeline,
    ) -> None:
        storage.require_open()
        self._storage = storage
        self._configuration = configuration
        self._issuer_client = issuer_client
        self._document_pipeline = document_pipeline

    def run(
        self,
        request: SecPrimaryDocumentRefreshRequest,
    ) -> SecPrimaryDocumentRefreshSummary:
        """Persist one current Submissions snapshot, then fill only missing documents."""
        self._storage.require_open()
        if request.asset_id != self._configuration.asset_id:
            raise SecPrimaryDocumentRefreshError("request asset_id does not match SEC issuer")
        submissions, checked_at, created, reused = self._persist_fresh_submissions()
        index = SecFilingIndex.from_raw_record(submissions, self._configuration)
        forms = tuple(sorted(self._configuration.supported_forms))
        by_form = {form: tuple(item for item in index.all() if item.form == form) for form in forms}
        forms_missing = tuple(form for form in forms if not by_form[form])
        selected = tuple(candidate[-1] for form in forms if (candidate := by_form[form]))
        selected = tuple(
            sorted(selected, key=lambda item: (item.acceptance_at, item.accession_number))
        )
        try:
            documents = (
                self._document_pipeline.run(
                    SecDocumentImportRequest(
                        accessions=tuple(item.accession_number for item in selected)
                    )
                )
                if selected
                else None
            )
        except (SecDocumentPipelineError, StorageError, ValueError) as error:
            raise SecPrimaryDocumentRefreshError(
                "primary document coverage could not be verified"
            ) from error
        if documents is None:
            revisions_created = revisions_reused = blobs_created = blobs_reused = calls = 0
            fetched: tuple[str, ...] = ()
            document_reused: tuple[str, ...] = ()
            traceability_verified = True
        else:
            revisions_created = documents.revisions_created
            revisions_reused = documents.revisions_reused
            blobs_created = documents.blobs_created
            blobs_reused = documents.blobs_reused
            calls = documents.document_fetch_calls
            fetched = documents.accessions_fetched
            document_reused = documents.accessions_reused
            traceability_verified = len(documents.revisions) == len(selected)
        return SecPrimaryDocumentRefreshSummary(
            asset_id=self._configuration.asset_id,
            request=request,
            source_id=SEC_DOCUMENT_SOURCE_ID,
            submissions_source_id=self._configuration.submissions_source_id,
            submissions_raw_record_id=str(submissions.record_id),
            submissions_checked_at=checked_at,
            submissions_record_available_at=submissions.available_at,
            forms_evaluated=forms,
            forms_missing=forms_missing,
            accessions_selected=tuple(item.accession_number for item in selected),
            accessions_fetched=fetched,
            accessions_reused=document_reused,
            submissions_created=created,
            submissions_reused=reused,
            revisions_created=revisions_created,
            revisions_reused=revisions_reused,
            blobs_created=blobs_created,
            blobs_reused=blobs_reused,
            document_fetch_calls=calls,
            coverage_complete=traceability_verified,
            traceability_verified=traceability_verified,
        )

    def _persist_fresh_submissions(self) -> tuple[RawRecord, datetime, int, int]:
        submissions_document = self._issuer_client.fetch_submissions()
        candidate = sec_document_to_raw_record(submissions_document, self._configuration)
        existing_asset = self._existing_asset()
        self._storage.assets.upsert(create_sec_asset(self._configuration, existing_asset))
        self._storage.sources.upsert(create_sec_submissions_source(self._configuration))
        try:
            record = self._storage.raw_records.get(candidate.record_id)
            reused = 1
            created = 0
        except RecordNotFoundError:
            self._storage.raw_records.save(candidate)
            record = self._storage.raw_records.get(candidate.record_id)
            created = 1
            reused = 0
        if (
            record.record_id != candidate.record_id
            or record.asset_id != candidate.asset_id
            or record.source.source_id != candidate.source.source_id
            or record.payload != candidate.payload
            or record.schema_version != candidate.schema_version
        ):
            raise SecPrimaryDocumentRefreshError("stored Submissions snapshot conflicts")
        return record, candidate.received_at, created, reused

    def _existing_asset(self) -> Asset | None:
        try:
            return self._storage.assets.get(self._configuration.asset_id)
        except RecordNotFoundError:
            return None


def build_sec_primary_document_refresh_service(
    storage: LocalStorage,
    *,
    configuration: SecAssetConfiguration,
    issuer_client: SecEdgarClient,
    document_pipeline: SecDocumentPipeline,
) -> SecPrimaryDocumentRefreshService:
    """Keep the facade composition concise while retaining typed dependencies."""
    return SecPrimaryDocumentRefreshService(
        storage,
        configuration=configuration,
        issuer_client=issuer_client,
        document_pipeline=document_pipeline,
    )


__all__ = [
    "SecPrimaryDocumentRefreshError",
    "SecPrimaryDocumentRefreshService",
    "build_sec_primary_document_refresh_service",
]
