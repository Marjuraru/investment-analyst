"""Metadata-only RawRecord lineage and point-in-time selection for SEC documents."""

from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import UTC, datetime
from uuid import UUID

from pydantic import ValidationError

from investment_analyst.core.models import RawRecord, SourceReference
from investment_analyst.evidence.sec_documents.models import (
    SEC_DOCUMENT_SCHEMA_VERSION,
    SEC_DOCUMENT_SCHEMA_VERSION_V2,
    SEC_DOCUMENT_SCHEMA_VERSION_V3,
    SEC_DOCUMENT_SCHEMA_VERSION_V4,
    SEC_DOCUMENT_SOURCE_ID,
    SecAssetDocumentRevision,
    SecDocumentAcquisitionRevision,
    SecDocumentMetadataRevision,
    SecDocumentReplay,
    SecDocumentRevision,
    same_document_metadata_except_accepted_at,
    verify_sec_terminal_script_difference,
)
from investment_analyst.storage import DocumentContentStore, RecordNotFoundError, StorageError
from investment_analyst.storage.raw_records import JsonRawRecordRepository


class SecDocumentRepositoryError(StorageError):
    """A primary-document record or its lineage is malformed."""


def revision_to_raw_record(revision: SecAssetDocumentRevision) -> RawRecord:
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
            "kind": (
                "sec_document_metadata_revision"
                if isinstance(revision, SecDocumentMetadataRevision)
                else "sec_document_acquisition_revision"
                if isinstance(revision, SecDocumentAcquisitionRevision)
                else "sec_document_revision"
            ),
            "revision": revision.model_dump(mode="json"),
        },
        schema_version=revision.revision_schema_version,
    )


def revision_from_raw_record(record: RawRecord) -> SecAssetDocumentRevision:
    """Decode strict metadata and reject body-bearing or inconsistent RawRecords."""
    if record.record_id is None or record.source.source_id != SEC_DOCUMENT_SOURCE_ID:
        raise SecDocumentRepositoryError("document RawRecord source is invalid")
    if record.schema_version not in {
        SEC_DOCUMENT_SCHEMA_VERSION,
        SEC_DOCUMENT_SCHEMA_VERSION_V2,
        SEC_DOCUMENT_SCHEMA_VERSION_V3,
        SEC_DOCUMENT_SCHEMA_VERSION_V4,
    }:
        raise SecDocumentRepositoryError("document RawRecord schema is invalid")
    if not isinstance(record.payload, dict) or set(record.payload) != {"kind", "revision"}:
        raise SecDocumentRepositoryError("document RawRecord payload is malformed")
    try:
        encoded = json.dumps(record.payload["revision"], separators=(",", ":"), sort_keys=True)
        if record.schema_version == SEC_DOCUMENT_SCHEMA_VERSION_V3:
            if record.payload["kind"] != "sec_document_metadata_revision":
                raise SecDocumentRepositoryError("metadata RawRecord payload kind is invalid")
            revision: SecAssetDocumentRevision = SecDocumentMetadataRevision.model_validate_json(
                encoded
            )
        elif record.schema_version == SEC_DOCUMENT_SCHEMA_VERSION_V4:
            if record.payload["kind"] != "sec_document_acquisition_revision":
                raise SecDocumentRepositoryError("acquisition RawRecord payload kind is invalid")
            revision = SecDocumentAcquisitionRevision.model_validate_json(encoded)
        else:
            if record.payload["kind"] != "sec_document_revision":
                raise SecDocumentRepositoryError("document RawRecord payload kind is invalid")
            revision = SecDocumentRevision.model_validate_json(encoded)
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

    def get_revision(self, revision_id: UUID) -> SecAssetDocumentRevision | None:
        """Locate one known revision without reading its blob."""
        try:
            record = self._raw_records.get(
                SecDocumentAcquisitionRevision.expected_raw_record_id(revision_id)
            )
        except RecordNotFoundError:
            try:
                record = self._raw_records.get(
                    SecDocumentMetadataRevision.expected_raw_record_id(revision_id)
                )
            except RecordNotFoundError:
                try:
                    record = self._raw_records.get(
                        SecDocumentRevision.expected_raw_record_id(revision_id)
                    )
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
    ) -> list[SecAssetDocumentRevision]:
        """Load only RawRecords eligible at a point in time, then filter metadata."""
        records = self._raw_records.list(
            asset_id=asset_id,
            source_id=SEC_DOCUMENT_SOURCE_ID,
            schema_version=SEC_DOCUMENT_SCHEMA_VERSION_V2,
            available_to=known_at,
        )
        records.extend(
            self._raw_records.list(
                asset_id=asset_id,
                source_id=SEC_DOCUMENT_SOURCE_ID,
                schema_version=SEC_DOCUMENT_SCHEMA_VERSION_V3,
                available_to=known_at,
            )
        )
        records.extend(
            self._raw_records.list(
                asset_id=asset_id,
                source_id=SEC_DOCUMENT_SOURCE_ID,
                schema_version=SEC_DOCUMENT_SCHEMA_VERSION_V4,
                available_to=known_at,
            )
        )
        revisions: list[SecAssetDocumentRevision] = []
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
        history = candidates
        if revision_id is not None:
            selected_accession = candidates[0].document.filing.accession
            history = self.list_revisions(
                asset_id=asset_id,
                known_at=known_at,
                accession=selected_accession,
            )
        self.verify_revision_history(history)
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

    def verify_revision_history(self, revisions: Iterable[SecAssetDocumentRevision]) -> None:
        """Require one connected, non-forking v2-rooted history per accession."""
        grouped: dict[tuple[str, str], list[SecAssetDocumentRevision]] = {}
        for revision in revisions:
            if revision.revision_schema_version == SEC_DOCUMENT_SCHEMA_VERSION:
                continue
            key = (revision.asset_id, revision.document.filing.accession)
            grouped.setdefault(key, []).append(revision)

        for group in grouped.values():
            by_id = {revision.revision_id: revision for revision in group}
            if len(by_id) != len(group):
                raise SecDocumentRepositoryError("document history contains duplicate revisions")
            roots = [
                revision
                for revision in group
                if isinstance(revision, SecDocumentRevision)
                and revision.revision_schema_version == SEC_DOCUMENT_SCHEMA_VERSION_V2
            ]
            if len(roots) != 1:
                raise SecDocumentRepositoryError(
                    "document history must have exactly one v2 root revision"
                )
            reference = roots[0]
            for revision in group:
                if (
                    revision.asset_id != reference.asset_id
                    or revision.document.document_id != reference.document.document_id
                    or not same_document_metadata_except_accepted_at(
                        revision.document, reference.document
                    )
                    or revision.source_url != reference.source_url
                ):
                    raise SecDocumentRepositoryError(
                        "document history contains an unrelated identity or source"
                    )
                self._verify_lineage(revision)

            children: dict[UUID, list[UUID]] = {}
            for revision in group:
                if isinstance(
                    revision, (SecDocumentMetadataRevision, SecDocumentAcquisitionRevision)
                ):
                    if revision.prior_revision_id not in by_id:
                        raise SecDocumentRepositoryError(
                            "document history contains a disconnected prior revision"
                        )
                    children.setdefault(revision.prior_revision_id, []).append(revision.revision_id)
            if any(len(child_ids) != 1 for child_ids in children.values()):
                raise SecDocumentRepositoryError("document history contains divergent forks")
            reachable: set[UUID] = set()
            current_id = roots[0].revision_id
            while current_id not in reachable:
                reachable.add(current_id)
                child_ids = children.get(current_id, [])
                if not child_ids:
                    break
                current_id = child_ids[0]
            if reachable != set(by_id):
                raise SecDocumentRepositoryError("document history is cyclic or disconnected")

    def verify_revision(self, revision: SecAssetDocumentRevision) -> None:
        """Verify blob and discovery lineage without materializing the blob."""
        self._verify_lineage(revision)
        self._content.verify(revision.content_sha256, size_bytes=revision.content_size_bytes)

    def _verify_lineage(self, revision: SecAssetDocumentRevision) -> None:
        current: SecAssetDocumentRevision = revision
        visited: set[UUID] = set()
        while True:
            if current.revision_id in visited:
                raise SecDocumentRepositoryError("metadata revision prior chain contains a cycle")
            visited.add(current.revision_id)
            self._verify_discovery_lineage(current)
            if not isinstance(
                current, (SecDocumentMetadataRevision, SecDocumentAcquisitionRevision)
            ):
                return
            prior = self.get_revision(current.prior_revision_id)
            if prior is None:
                raise SecDocumentRepositoryError("document revision prior is missing")
            if prior.revision_schema_version == SEC_DOCUMENT_SCHEMA_VERSION:
                raise SecDocumentRepositoryError("document revision prior cannot be legacy v1")
            if (
                prior.asset_id != current.asset_id
                or prior.document.document_id != current.document.document_id
                or not same_document_metadata_except_accepted_at(prior.document, current.document)
                or prior.source_url != current.source_url
                or prior.available_at > current.available_at
            ):
                raise SecDocumentRepositoryError(
                    "document revision prior conflicts with identity, metadata, source or time"
                )
            if isinstance(current, SecDocumentMetadataRevision):
                if (
                    prior.document.filing.accepted_at == current.document.filing.accepted_at
                    or prior.content_sha256 != current.content_sha256
                    or prior.content_size_bytes != current.content_size_bytes
                ):
                    raise SecDocumentRepositoryError(
                        "metadata revision prior conflicts with accepted_at or content"
                    )
            else:
                self._content.verify(
                    prior.content_sha256,
                    size_bytes=prior.content_size_bytes,
                )
                prior_content = self._content.read(prior.content_sha256)
                current_content = self._content.read(current.content_sha256)
                try:
                    verify_sec_terminal_script_difference(
                        prior_content,
                        current_content,
                        document_name=current.document.name,
                        proof=current.terminal_script_difference,
                    )
                except ValueError as error:
                    raise SecDocumentRepositoryError(
                        "acquisition revision terminal script proof is invalid"
                    ) from error
                if current.prior_content_sha256 != prior.content_sha256:
                    raise SecDocumentRepositoryError(
                        "acquisition revision prior content digest does not match its lineage"
                    )
                expected_available_at = max(
                    current.document.filing.accepted_at,
                    current.metadata_observed_at,
                    current.retrieved_at,
                    prior.available_at,
                )
                if current.available_at != expected_available_at:
                    raise SecDocumentRepositoryError(
                        "acquisition availability does not include its verified prior"
                    )
            if prior.available_at > current.available_at:
                raise SecDocumentRepositoryError(
                    "document revision availability precedes its prior"
                )
            current = prior

    def _verify_discovery_lineage(self, revision: SecAssetDocumentRevision) -> None:
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
        if (
            isinstance(revision, (SecDocumentMetadataRevision, SecDocumentAcquisitionRevision))
            and discovery.received_at != revision.metadata_observed_at
        ):
            raise SecDocumentRepositoryError(
                "metadata observation time does not match its Submissions lineage"
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
    histories: set[tuple[str, str]] = set()
    for record in records:
        if record.schema_version in {
            SEC_DOCUMENT_SCHEMA_VERSION,
            SEC_DOCUMENT_SCHEMA_VERSION_V2,
            SEC_DOCUMENT_SCHEMA_VERSION_V3,
            SEC_DOCUMENT_SCHEMA_VERSION_V4,
        }:
            revision = revision_from_raw_record(record)
            repository.verify_revision(revision)
            if revision.revision_schema_version != SEC_DOCUMENT_SCHEMA_VERSION:
                histories.add((revision.asset_id, revision.document.filing.accession))
    for asset_id, accession in histories:
        revisions = repository.list_revisions(
            asset_id=asset_id,
            known_at=datetime.max.replace(tzinfo=UTC),
            accession=accession,
        )
        repository.verify_revision_history(revisions)
