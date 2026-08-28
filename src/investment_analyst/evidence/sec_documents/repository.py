"""Metadata-only RawRecord lineage and point-in-time selection for SEC documents."""

from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import datetime
from uuid import UUID

from pydantic import ValidationError

from investment_analyst.core.models import RawRecord, SourceReference
from investment_analyst.evidence.sec_documents.models import (
    SEC_DOCUMENT_SCHEMA_VERSION,
    SEC_DOCUMENT_SCHEMA_VERSION_V2,
    SEC_DOCUMENT_SOURCE_ID,
    SecDocumentReplay,
    SecDocumentRevision,
)
from investment_analyst.storage import DocumentContentStore, RecordNotFoundError, StorageError
from investment_analyst.storage.raw_records import JsonRawRecordRepository


class SecDocumentRepositoryError(StorageError):
    """A primary-document record or its lineage is malformed."""


def revision_to_raw_record(revision: SecDocumentRevision) -> RawRecord:
    """Encode revision metadata without embedding the document body."""
    record_key = json.dumps(
        {"revision_id": str(revision.revision_id)}, separators=(",", ":"), sort_keys=True
    )
    return RawRecord(
        record_id=revision.raw_record_id,
        asset_id=revision.asset_id,
        source=SourceReference(
            source_id=SEC_DOCUMENT_SOURCE_ID,
            record_key=record_key,
            retrieved_at=revision.retrieved_at,
            raw_uri=revision.source_url,
            checksum_sha256=revision.content_sha256,
        ),
        event_time=revision.document.filing.accepted_at,
        available_at=revision.available_at,
        received_at=revision.retrieved_at,
        payload={
            "kind": "sec_document_revision",
            "revision": revision.model_dump(mode="json"),
        },
        schema_version=revision.revision_schema_version,
    )


def revision_from_raw_record(record: RawRecord) -> SecDocumentRevision:
    """Decode strict metadata and reject body-bearing or inconsistent RawRecords."""
    if record.record_id is None or record.source.source_id != SEC_DOCUMENT_SOURCE_ID:
        raise SecDocumentRepositoryError("document RawRecord source is invalid")
    if record.schema_version not in {SEC_DOCUMENT_SCHEMA_VERSION, SEC_DOCUMENT_SCHEMA_VERSION_V2}:
        raise SecDocumentRepositoryError("document RawRecord schema is invalid")
    if not isinstance(record.payload, dict) or set(record.payload) != {"kind", "revision"}:
        raise SecDocumentRepositoryError("document RawRecord payload is malformed")
    if record.payload["kind"] != "sec_document_revision":
        raise SecDocumentRepositoryError("document RawRecord payload kind is invalid")
    try:
        revision = SecDocumentRevision.model_validate_json(
            json.dumps(record.payload["revision"], separators=(",", ":"), sort_keys=True)
        )
    except (TypeError, ValidationError) as error:
        raise SecDocumentRepositoryError("document RawRecord revision is malformed") from error
    if record.record_id != revision.raw_record_id:
        raise SecDocumentRepositoryError("document RawRecord identifier does not match revision")
    if record.schema_version != revision.revision_schema_version:
        raise SecDocumentRepositoryError("document RawRecord schema conflicts with revision")
    if record.asset_id != revision.asset_id:
        raise SecDocumentRepositoryError("document RawRecord asset does not match revision")
    if (
        record.available_at != revision.available_at
        or record.received_at != revision.retrieved_at
        or record.source.retrieved_at != revision.retrieved_at
        or record.source.raw_uri != revision.source_url
        or record.source.checksum_sha256 != revision.content_sha256
    ):
        raise SecDocumentRepositoryError("document RawRecord metadata does not match revision")
    if record.event_time != revision.document.filing.accepted_at:
        raise SecDocumentRepositoryError("document RawRecord event_time does not match filing")
    return revision


class SecDocumentRepository:
    """Select document revisions with SQL filters before metadata materialization."""

    def __init__(
        self,
        raw_records: JsonRawRecordRepository,
        content: DocumentContentStore,
    ) -> None:
        self._raw_records = raw_records
        self._content = content

    def get_revision(self, revision_id: UUID) -> SecDocumentRevision | None:
        """Locate one known revision without reading its blob."""
        try:
            record = self._raw_records.get(SecDocumentRevision.expected_raw_record_id(revision_id))
        except RecordNotFoundError:
            return None
        return revision_from_raw_record(record)

    def list_revisions(
        self,
        *,
        asset_id: str,
        known_at: datetime,
        form: str | None = None,
        accession: str | None = None,
        revision_id: UUID | None = None,
    ) -> list[SecDocumentRevision]:
        """Load only RawRecords eligible at a point in time, then filter metadata."""
        records = self._raw_records.list(
            asset_id=asset_id,
            source_id=SEC_DOCUMENT_SOURCE_ID,
            schema_version=SEC_DOCUMENT_SCHEMA_VERSION_V2,
            available_to=known_at,
        )
        revisions: list[SecDocumentRevision] = []
        for record in records:
            revision = revision_from_raw_record(record)
            if revision.asset_id != asset_id:
                raise SecDocumentRepositoryError("document RawRecord asset does not match revision")
            if form is not None and revision.document.filing.form != form:
                continue
            if accession is not None and revision.document.filing.accession != accession:
                continue
            if revision_id is not None and revision.revision_id != revision_id:
                continue
            revisions.append(revision)
        return sorted(
            revisions,
            key=lambda item: (item.available_at, str(item.revision_id)),
        )

    def replay(
        self,
        *,
        asset_id: str,
        known_at: datetime,
        form: str | None = None,
        accession: str | None = None,
        revision_id: UUID | None = None,
        include_content: bool = False,
    ) -> SecDocumentReplay:
        """Return one latest eligible revision or an explicit missing state."""
        candidates = self.list_revisions(
            asset_id=asset_id,
            known_at=known_at,
            form=form,
            accession=accession,
            revision_id=revision_id,
        )
        legacy_excluded = self._raw_records.count(
            asset_id=asset_id,
            source_id=SEC_DOCUMENT_SOURCE_ID,
            schema_version=SEC_DOCUMENT_SCHEMA_VERSION,
        )
        if not candidates:
            return SecDocumentReplay(state="missing", legacy_records_excluded=legacy_excluded)
        latest_at = candidates[-1].available_at
        latest = [item for item in candidates if item.available_at == latest_at]
        if len({item.revision_id for item in latest}) != 1:
            raise SecDocumentRepositoryError("equally available document revisions are ambiguous")
        selected = latest[0]
        self._verify_lineage(selected)
        content = self._content.read(selected.content_sha256) if include_content else None
        if content is not None and len(content) != selected.content_size_bytes:
            raise SecDocumentRepositoryError("document content size does not match revision")
        return SecDocumentReplay(
            state="found",
            revision=selected,
            content=content,
            legacy_records_excluded=legacy_excluded,
        )

    def verify_revision(self, revision: SecDocumentRevision) -> None:
        """Verify blob and discovery lineage without materializing the blob."""
        self._verify_lineage(revision)
        self._content.verify(revision.content_sha256, size_bytes=revision.content_size_bytes)

    def _verify_lineage(self, revision: SecDocumentRevision) -> None:
        try:
            discovery = self._raw_records.get(revision.discovery_raw_record_id)
        except RecordNotFoundError as error:
            raise SecDocumentRepositoryError(
                "document revision has no submissions lineage"
            ) from error
        if revision.revision_schema_version == SEC_DOCUMENT_SCHEMA_VERSION:
            if discovery.available_at > revision.available_at:
                raise SecDocumentRepositoryError(
                    "document lineage became available after its revision"
                )
        elif discovery.received_at > revision.retrieved_at:
            # EDGAR acceptance (v2 available_at) can predate the Submissions capture by years
            # without invalidating the filing; only acquisition causality is checked here:
            # the submissions listing must have been received before the document itself.
            raise SecDocumentRepositoryError(
                "document lineage was received after the revision was retrieved"
            )
        if discovery.asset_id != revision.asset_id:
            raise SecDocumentRepositoryError("document lineage asset does not match the revision")
        if not discovery.source.source_id.endswith(":submissions"):
            raise SecDocumentRepositoryError("document lineage is not a submissions RawRecord")
        if not isinstance(discovery.payload, dict):
            raise SecDocumentRepositoryError("document submissions lineage payload is malformed")
        document = discovery.payload.get("document")
        discovered_cik = (
            str(document.get("cik", "")).zfill(10) if isinstance(document, dict) else ""
        )
        if discovered_cik != revision.document.filing.filer_cik:
            raise SecDocumentRepositoryError("document lineage CIK does not match the filing")


def verify_document_records(
    records: Iterable[RawRecord],
    repository: SecDocumentRepository,
) -> None:
    """Verify documentary records encountered in an existing paginated scan."""
    for record in records:
        if record.schema_version in {SEC_DOCUMENT_SCHEMA_VERSION, SEC_DOCUMENT_SCHEMA_VERSION_V2}:
            repository.verify_revision(revision_from_raw_record(record))
