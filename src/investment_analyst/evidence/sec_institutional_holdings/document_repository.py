"""Codec and lineage checks for SEC documents linked to a filer."""

from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import datetime
from uuid import UUID

from pydantic import ValidationError

from investment_analyst.core.models import RawRecord, SourceReference
from investment_analyst.evidence.sec_documents.models import (
    FILER_REVISION_SCHEMA_VERSION,
    SecFilerDocumentRevision,
    normalize_cik,
)
from investment_analyst.storage import RecordNotFoundError, StorageError

SEC_FILER_DOCUMENT_SOURCE_ID = "sec-edgar:filer-documents"


class SecFilerDocumentRepositoryError(StorageError):
    """A filer-document record or its discovery lineage is invalid."""


def filer_revision_to_raw_record(revision: SecFilerDocumentRevision) -> RawRecord:
    return RawRecord(
        record_id=revision.raw_record_id,
        asset_id=None,
        source=SourceReference(
            source_id=SEC_FILER_DOCUMENT_SOURCE_ID,
            record_key=json.dumps({"revision_id": str(revision.revision_id)}, sort_keys=True),
            retrieved_at=revision.retrieved_at,
            raw_uri=revision.source_url,
            checksum_sha256=revision.content_sha256,
        ),
        event_time=revision.document.filing.accepted_at,
        available_at=revision.available_at,
        received_at=revision.retrieved_at,
        payload={
            "kind": "sec_filer_document_revision",
            "revision": revision.model_dump(mode="json"),
        },
        schema_version=revision.revision_schema_version,
    )


def filer_revision_from_raw_record(record: RawRecord) -> SecFilerDocumentRevision:
    if (
        record.asset_id is not None
        or record.source.source_id != SEC_FILER_DOCUMENT_SOURCE_ID
        or record.schema_version != FILER_REVISION_SCHEMA_VERSION
        or not isinstance(record.payload, dict)
        or set(record.payload) != {"kind", "revision"}
        or record.payload.get("kind") != "sec_filer_document_revision"
    ):
        raise SecFilerDocumentRepositoryError("filer document RawRecord is malformed")
    try:
        revision = SecFilerDocumentRevision.model_validate_json(
            json.dumps(record.payload["revision"], separators=(",", ":"), sort_keys=True)
        )
    except (KeyError, TypeError, ValidationError) as error:
        raise SecFilerDocumentRepositoryError("filer document revision is malformed") from error
    expected_key = json.dumps({"revision_id": str(revision.revision_id)}, sort_keys=True)
    if (
        record.record_id != revision.raw_record_id
        or record.source.record_key != expected_key
        or record.event_time != revision.document.filing.accepted_at
        or record.available_at != revision.available_at
        or record.received_at != revision.retrieved_at
        or record.source.retrieved_at != revision.retrieved_at
        or record.source.raw_uri != revision.source_url
        or record.source.checksum_sha256 != revision.content_sha256
    ):
        raise SecFilerDocumentRepositoryError("filer document RawRecord conflicts")
    return revision


class SecFilerDocumentRepository:
    def __init__(self, raw_records, content_store) -> None:
        self._raw_records = raw_records
        self._content_store = content_store

    def get_revision(self, revision_id: UUID) -> SecFilerDocumentRevision | None:
        try:
            record = self._raw_records.get(
                SecFilerDocumentRevision.expected_raw_record_id(revision_id)
            )
        except RecordNotFoundError:
            return None
        return filer_revision_from_raw_record(record)

    def save(self, revision: SecFilerDocumentRevision) -> SecFilerDocumentRevision:
        existing = self.get_revision(revision.revision_id)
        if existing is not None and existing != revision:
            raise SecFilerDocumentRepositoryError("filer document identity conflicts")
        self._raw_records.save(filer_revision_to_raw_record(revision))
        return revision

    def verify_revision(self, revision: SecFilerDocumentRevision) -> None:
        persisted = self.get_revision(revision.revision_id)
        if persisted != revision:
            raise SecFilerDocumentRepositoryError("filer document revision is not persisted")
        try:
            discovery = self._raw_records.get(revision.discovery_raw_record_id)
        except RecordNotFoundError as error:
            raise SecFilerDocumentRepositoryError(
                "filer document has no submissions lineage"
            ) from error
        if (
            discovery.asset_id is not None
            or not discovery.source.source_id.endswith(":submissions")
            or discovery.received_at > revision.retrieved_at
            or not isinstance(discovery.payload, dict)
        ):
            raise SecFilerDocumentRepositoryError("filer document submissions lineage is invalid")
        document = discovery.payload.get("document")
        discovered_cik = (
            str(document.get("cik", "")).zfill(10) if isinstance(document, dict) else ""
        )
        if discovered_cik != revision.filer_cik:
            raise SecFilerDocumentRepositoryError("filer document lineage CIK conflicts")
        self._content_store.verify(revision.content_sha256, size_bytes=revision.content_size_bytes)

    def list_revisions(
        self,
        *,
        available_to: datetime | None = None,
        filer_cik: str | None = None,
        form: str | None = None,
        accession: str | None = None,
        revision_id: UUID | None = None,
    ) -> list[SecFilerDocumentRevision]:
        """Load only RawRecords eligible at a point in time, then filter and order metadata."""
        records = self._raw_records.list(
            asset_id=None,
            source_id=SEC_FILER_DOCUMENT_SOURCE_ID,
            schema_version=FILER_REVISION_SCHEMA_VERSION,
            available_to=available_to,
        )
        normalized_cik = normalize_cik(filer_cik) if filer_cik is not None else None
        revisions: list[SecFilerDocumentRevision] = []
        for record in records:
            revision = filer_revision_from_raw_record(record)
            if normalized_cik is not None and revision.filer_cik != normalized_cik:
                continue
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


def verify_filer_document_records(
    records: Iterable[RawRecord], repository: SecFilerDocumentRepository
) -> None:
    for record in records:
        if record.schema_version == FILER_REVISION_SCHEMA_VERSION:
            repository.verify_revision(filer_revision_from_raw_record(record))
