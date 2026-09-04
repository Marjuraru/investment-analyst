"""Read-only selection and point-in-time ordering service for SEC document timelines."""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta

from investment_analyst.evidence.sec_documents.models import (
    SEC_DOCUMENT_SCHEMA_VERSION,
    SEC_DOCUMENT_SOURCE_ID,
    SecDocumentRevision,
    SecFilerDocumentRevision,
)
from investment_analyst.evidence.sec_documents.repository import SecDocumentRepository
from investment_analyst.evidence.sec_documents.timeline_models import (
    SecDocumentTimelineEntry,
    SecDocumentTimelineQuery,
    SecDocumentTimelineResult,
)
from investment_analyst.evidence.sec_institutional_holdings.document_repository import (
    SecFilerDocumentRepository,
)
from investment_analyst.storage import LocalStorage, StorageError


class SecDocumentTimelineServiceError(StorageError):
    """A document timeline query failed or was executed with invalid storage."""


class SecDocumentTimelineService:
    """Select and order point-in-time SEC document revisions across families."""

    def __init__(self, storage: LocalStorage) -> None:
        self._storage = storage
        self._asset_repository = SecDocumentRepository(storage.raw_records, storage.documents)
        self._filer_repository = SecFilerDocumentRepository(storage.raw_records, storage.documents)

    def query(self, query: SecDocumentTimelineQuery) -> SecDocumentTimelineResult:
        """Execute a point-in-time document timeline query in strict read-only mode."""
        self._storage.require_open()
        if not self._storage.read_only:
            raise SecDocumentTimelineServiceError(
                "document timeline query requires read-only storage"
            )

        raw_candidates: list[SecDocumentTimelineEntry] = []
        legacy_records_excluded = 0

        # 1. Asset document family
        for asset_id in query.asset_ids:
            asset_revisions = self._asset_repository.list_revisions(
                asset_id=asset_id,
                known_at=query.known_at,
                accession=query.accession,
            )
            for rev in asset_revisions:
                raw_candidates.append(self._asset_revision_to_entry(rev))

            legacy_records_excluded += self._storage.raw_records.count(
                asset_id=asset_id,
                source_id=SEC_DOCUMENT_SOURCE_ID,
                schema_version=SEC_DOCUMENT_SCHEMA_VERSION,
            )

        # 2. Filer document family
        for filer_cik in query.filer_ciks:
            filer_revisions = self._filer_repository.list_revisions(
                available_to=query.known_at,
                filer_cik=filer_cik,
                accession=query.accession,
            )
            for rev in filer_revisions:
                raw_candidates.append(self._filer_revision_to_entry(rev))

        # 3. Filtering
        from_bound = (
            datetime.combine(query.available_from, time.min, tzinfo=UTC)
            if query.available_from is not None
            else None
        )
        to_bound = (
            datetime.combine(query.available_to + timedelta(days=1), time.min, tzinfo=UTC)
            if query.available_to is not None
            else None
        )

        matched_entries: list[SecDocumentTimelineEntry] = []
        for entry in raw_candidates:
            # Point-in-time cut invariant
            if entry.available_at > query.known_at:
                continue
            # Form filtering
            if query.forms and entry.form not in query.forms:
                continue
            # Public date range (inclusive, converted to half-open UTC interval)
            if from_bound is not None and entry.available_at < from_bound:
                continue
            if to_bound is not None and entry.available_at >= to_bound:
                continue
            matched_entries.append(entry)

        # 4. Total and deterministic ordering across families
        matched_entries.sort(
            key=lambda item: (item.available_at, item.family, str(item.revision_id))
        )

        # 5. Coherent truncation and result assembly
        matched_count = len(matched_entries)
        if matched_count == 0:
            return SecDocumentTimelineResult(
                state="missing",
                known_at=query.known_at,
                entries=(),
                matched_count=0,
                returned_count=0,
                legacy_records_excluded=legacy_records_excluded,
                truncated=False,
            )

        if query.limit is not None and matched_count > query.limit:
            returned_entries = tuple(matched_entries[: query.limit])
            truncated = True
        else:
            returned_entries = tuple(matched_entries)
            truncated = False

        return SecDocumentTimelineResult(
            state="found",
            known_at=query.known_at,
            entries=returned_entries,
            matched_count=matched_count,
            returned_count=len(returned_entries),
            legacy_records_excluded=legacy_records_excluded,
            truncated=truncated,
        )

    @staticmethod
    def _asset_revision_to_entry(rev: SecDocumentRevision) -> SecDocumentTimelineEntry:
        return SecDocumentTimelineEntry(
            family="asset_document",
            revision_id=rev.revision_id,
            asset_id=rev.asset_id,
            filer_cik=rev.document.filing.filer_cik,
            form=rev.document.filing.form,
            accession=rev.document.filing.accession,
            document_name=rev.document.name,
            filing_date=rev.document.filing.filing_date,
            report_date=rev.document.filing.report_date,
            accepted_at=rev.document.filing.accepted_at,
            available_at=rev.available_at,
            content_sha256=rev.content_sha256,
            content_size_bytes=rev.content_size_bytes,
            source_url=rev.source_url,
            is_amendment=rev.document.filing.is_amendment,
        )

    @staticmethod
    def _filer_revision_to_entry(rev: SecFilerDocumentRevision) -> SecDocumentTimelineEntry:
        return SecDocumentTimelineEntry(
            family="filer_document",
            revision_id=rev.revision_id,
            asset_id=None,
            filer_cik=rev.filer_cik,
            form=rev.document.filing.form,
            accession=rev.document.filing.accession,
            document_name=rev.document.name,
            filing_date=rev.document.filing.filing_date,
            report_date=rev.document.filing.report_date,
            accepted_at=rev.document.filing.accepted_at,
            available_at=rev.available_at,
            content_sha256=rev.content_sha256,
            content_size_bytes=rev.content_size_bytes,
            source_url=rev.source_url,
            is_amendment=rev.document.filing.is_amendment,
        )
