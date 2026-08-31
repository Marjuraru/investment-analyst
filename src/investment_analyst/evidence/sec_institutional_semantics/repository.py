"""Append-only RawRecord codec for complete 13F semantic bundles."""

from __future__ import annotations

import json
from collections.abc import Iterable
from uuid import UUID

from investment_analyst.core.models import RawRecord, SourceReference
from investment_analyst.evidence.sec_institutional_semantics.models import (
    SEC_INSTITUTIONAL_SEMANTICS_SCHEMA_VERSION,
    SEC_INSTITUTIONAL_SEMANTICS_SOURCE_ID,
    InstitutionalHoldingsSemantics,
)
from investment_analyst.storage import RecordNotFoundError, StorageError


class InstitutionalSemanticsRepositoryError(StorageError):
    """A semantic bundle or its parent evidence cannot be trusted."""


def semantics_to_raw_record(item: InstitutionalHoldingsSemantics) -> RawRecord:
    return RawRecord(
        record_id=item.raw_record_id,
        asset_id=None,
        source=SourceReference(
            source_id=SEC_INSTITUTIONAL_SEMANTICS_SOURCE_ID,
            record_key=json.dumps({"artifact_id": str(item.artifact_id)}, sort_keys=True),
            retrieved_at=item.parsed_at,
            raw_uri=item.cover_revision.source_url,
            checksum_sha256=item.cover_revision.content_sha256,
        ),
        event_time=item.available_at,
        available_at=item.available_at,
        received_at=item.parsed_at,
        payload={
            "kind": "sec_institutional_holdings_semantics",
            "artifact": item.model_dump(mode="json"),
        },
        schema_version=SEC_INSTITUTIONAL_SEMANTICS_SCHEMA_VERSION,
    )


def semantics_from_raw_record(record: RawRecord) -> InstitutionalHoldingsSemantics:
    if (
        record.asset_id is not None
        or record.source.source_id != SEC_INSTITUTIONAL_SEMANTICS_SOURCE_ID
        or record.schema_version != SEC_INSTITUTIONAL_SEMANTICS_SCHEMA_VERSION
        or not isinstance(record.payload, dict)
        or set(record.payload) != {"kind", "artifact"}
        or record.payload.get("kind") != "sec_institutional_holdings_semantics"
    ):
        raise InstitutionalSemanticsRepositoryError(
            "institutional semantics RawRecord is malformed"
        )
    try:
        item = InstitutionalHoldingsSemantics.model_validate_json(
            json.dumps(record.payload["artifact"])
        )
    except (KeyError, TypeError, ValueError) as error:
        raise InstitutionalSemanticsRepositoryError(
            "institutional semantics payload is malformed"
        ) from error
    expected_key = json.dumps({"artifact_id": str(item.artifact_id)}, sort_keys=True)
    if (
        record.record_id != item.raw_record_id
        or record.event_time != item.available_at
        or record.available_at != item.available_at
        or record.received_at != item.parsed_at
        or record.source.record_key != expected_key
        or record.source.retrieved_at != item.parsed_at
        or record.source.raw_uri != item.cover_revision.source_url
        or record.source.checksum_sha256 != item.cover_revision.content_sha256
    ):
        raise InstitutionalSemanticsRepositoryError("institutional semantics RawRecord conflicts")
    return item


class InstitutionalSemanticsRepository:
    def __init__(self, raw_records) -> None:
        self._raw_records = raw_records

    def get(self, artifact_id: UUID) -> InstitutionalHoldingsSemantics | None:
        try:
            return semantics_from_raw_record(
                self._raw_records.get(
                    InstitutionalHoldingsSemantics.expected_raw_record_id(artifact_id)
                )
            )
        except RecordNotFoundError:
            return None

    def get_for_parent(self, parent) -> InstitutionalHoldingsSemantics | None:
        """Resolve through deterministic v1 lineage; never scan unrelated semantic bundles."""
        artifact_id = InstitutionalHoldingsSemantics.expected_id(
            parent.report_id,
            parent.cover_revision.revision_id,
            parent.information_table_revision.revision_id,
        )
        return self.get(artifact_id)

    def save(self, item: InstitutionalHoldingsSemantics) -> InstitutionalHoldingsSemantics:
        existing = self.get(item.artifact_id)
        if existing is not None:
            if existing.semantic_document() != item.semantic_document():
                raise InstitutionalSemanticsRepositoryError(
                    "institutional semantics identity conflicts"
                )
            return existing
        self._raw_records.save(semantics_to_raw_record(item))
        return item


def verify_institutional_semantics_records(
    records: Iterable[RawRecord],
    *,
    holdings_repository,
    filer_documents,
    content_store,
    parser,
) -> None:
    """Verify each bundle against the persisted v1 report and immutable XML bytes."""
    for record in records:
        if record.schema_version != SEC_INSTITUTIONAL_SEMANTICS_SCHEMA_VERSION:
            continue
        item = semantics_from_raw_record(record)
        parent = holdings_repository.get_report(item.parent_report_id)
        if (
            parent is None
            or parent.cover_revision != item.cover_revision
            or parent.information_table_revision != item.information_table_revision
            or parent.manager_cik != item.manager_cik
        ):
            raise InstitutionalSemanticsRepositoryError("semantic bundle parent lineage is invalid")
        filer_documents.verify_revision(item.cover_revision)
        filer_documents.verify_revision(item.information_table_revision)
        cover = content_store.read(item.cover_revision.content_sha256)
        table = content_store.read(item.information_table_revision.content_sha256)
        replay = parser(
            cover,
            table,
            parent_report_id=item.parent_report_id,
            cover_revision=item.cover_revision,
            information_table_revision=item.information_table_revision,
            parsed_at=item.parsed_at,
        )
        if replay.semantic_document() != item.semantic_document():
            raise InstitutionalSemanticsRepositoryError(
                "semantic bundle does not match persisted XML"
            )
