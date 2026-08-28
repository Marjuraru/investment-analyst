"""Append-only ownership RawRecord codec."""

from __future__ import annotations

import json
from datetime import datetime
from uuid import UUID

from investment_analyst.core.models import RawRecord, SourceReference
from investment_analyst.evidence.sec_ownership.models import (
    OWNERSHIP_OUTCOME_SCHEMA_VERSION,
    OWNERSHIP_OUTCOME_SCHEMA_VERSION_V2,
    OWNERSHIP_SCHEMA_VERSION,
    OWNERSHIP_SCHEMA_VERSION_V2,
    OWNERSHIP_SOURCE_ID,
    OwnershipResolutionOutcome,
    OwnershipStatement,
)
from investment_analyst.storage import RecordNotFoundError, StorageError


class OwnershipRepositoryError(StorageError):
    pass


def verify_ownership_records(records, document_repository, content_store) -> None:
    """Verify ownership outcomes and statements inside the existing paginated scan."""
    for record in records:
        if record.schema_version in {
            OWNERSHIP_OUTCOME_SCHEMA_VERSION,
            OWNERSHIP_OUTCOME_SCHEMA_VERSION_V2,
        }:
            outcome = outcome_from_raw_record(record)
            content_store.verify(outcome.content_sha256, size_bytes=outcome.content_size_bytes)
        elif record.schema_version in {OWNERSHIP_SCHEMA_VERSION, OWNERSHIP_SCHEMA_VERSION_V2}:
            statement = statement_from_raw_record(record)
            document_repository.verify_revision(statement.document_revision)


def outcome_to_raw_record(outcome: OwnershipResolutionOutcome) -> RawRecord:
    return RawRecord(
        record_id=outcome.raw_record_id,
        asset_id=outcome.asset_id,
        source=SourceReference(
            source_id=OWNERSHIP_SOURCE_ID,
            record_key=json.dumps({"outcome_id": str(outcome.outcome_id)}, sort_keys=True),
            retrieved_at=outcome.retrieved_at,
            raw_uri=outcome.resource_url,
            checksum_sha256=outcome.content_sha256,
        ),
        event_time=outcome.filing.accepted_at,
        available_at=outcome.available_at,
        received_at=outcome.retrieved_at,
        payload={"kind": "sec_ownership_outcome", "outcome": outcome.model_dump(mode="json")},
        schema_version=(
            OWNERSHIP_OUTCOME_SCHEMA_VERSION_V2
            if outcome.resolver_version == "sec-ownership-resolver-v2"
            else OWNERSHIP_OUTCOME_SCHEMA_VERSION
        ),
    )


def outcome_from_raw_record(record: RawRecord) -> OwnershipResolutionOutcome:
    if (
        record.source.source_id != OWNERSHIP_SOURCE_ID
        or record.schema_version
        not in {OWNERSHIP_OUTCOME_SCHEMA_VERSION, OWNERSHIP_OUTCOME_SCHEMA_VERSION_V2}
        or not isinstance(record.payload, dict)
        or record.payload.get("kind") != "sec_ownership_outcome"
    ):
        raise OwnershipRepositoryError("ownership outcome RawRecord is malformed")
    try:
        outcome = OwnershipResolutionOutcome.model_validate_json(
            json.dumps(record.payload["outcome"])
        )
    except (KeyError, ValueError) as error:
        raise OwnershipRepositoryError("ownership outcome is malformed") from error
    if record.record_id != outcome.raw_record_id or record.available_at != outcome.available_at:
        raise OwnershipRepositoryError("ownership outcome RawRecord conflicts")
    return outcome


def statement_to_raw_record(statement: OwnershipStatement) -> RawRecord:
    return RawRecord(
        record_id=statement.raw_record_id,
        asset_id=statement.asset_id,
        source=SourceReference(
            source_id=OWNERSHIP_SOURCE_ID,
            record_key=json.dumps({"statement_id": str(statement.statement_id)}, sort_keys=True),
            retrieved_at=statement.parsed_at,
            raw_uri=statement.document_revision.source_url,
            checksum_sha256=statement.document_revision.content_sha256,
        ),
        event_time=statement.document_revision.document.filing.accepted_at,
        available_at=statement.available_at,
        received_at=statement.parsed_at,
        payload={"kind": "sec_ownership_statement", "statement": statement.model_dump(mode="json")},
        schema_version=statement.schema_version,
    )


def statement_from_raw_record(record: RawRecord) -> OwnershipStatement:
    if (
        record.source.source_id != OWNERSHIP_SOURCE_ID
        or record.schema_version not in {OWNERSHIP_SCHEMA_VERSION, OWNERSHIP_SCHEMA_VERSION_V2}
        or not isinstance(record.payload, dict)
        or record.payload.get("kind") != "sec_ownership_statement"
    ):
        raise OwnershipRepositoryError("ownership RawRecord is malformed")
    try:
        statement = OwnershipStatement.model_validate_json(json.dumps(record.payload["statement"]))
    except (KeyError, ValueError) as error:
        raise OwnershipRepositoryError("ownership statement is malformed") from error
    if record.record_id != statement.raw_record_id or record.available_at != statement.available_at:
        raise OwnershipRepositoryError("ownership RawRecord conflicts with statement")
    return statement


class OwnershipRepository:
    def __init__(self, raw_records) -> None:
        self._raw_records = raw_records

    def get(self, statement_id: UUID) -> OwnershipStatement | None:
        try:
            return statement_from_raw_record(
                self._raw_records.get(OwnershipStatement.expected_raw_record_id(statement_id))
            )
        except RecordNotFoundError:
            return None

    def get_outcome(self, outcome_id: UUID) -> OwnershipResolutionOutcome | None:
        try:
            return outcome_from_raw_record(
                self._raw_records.get(OwnershipResolutionOutcome.expected_raw_record_id(outcome_id))
            )
        except RecordNotFoundError:
            return None

    def save_outcome(self, outcome: OwnershipResolutionOutcome) -> OwnershipResolutionOutcome:
        existing = self.get_outcome(outcome.outcome_id)
        if existing is not None and existing != outcome:
            raise OwnershipRepositoryError("ownership outcome identity conflicts")
        self._raw_records.save(outcome_to_raw_record(outcome))
        return outcome

    def save(self, statement: OwnershipStatement) -> OwnershipStatement:
        existing = self.get(statement.statement_id)
        if existing is not None and existing != statement:
            raise OwnershipRepositoryError("ownership identity conflicts")
        self._raw_records.save(statement_to_raw_record(statement))
        return statement

    def list(self, *, asset_id: str, known_at: datetime) -> list[OwnershipStatement]:
        return sorted(
            (
                statement_from_raw_record(record)
                for record in self._raw_records.list(
                    asset_id=asset_id,
                    source_id=OWNERSHIP_SOURCE_ID,
                    schema_version=OWNERSHIP_SCHEMA_VERSION_V2,
                    available_to=known_at,
                )
            ),
            key=lambda item: (
                item.available_at,
                item.document_revision.document.filing.accession,
                str(item.statement_id),
            ),
        )
