"""Append-only RawRecord codec for beneficial-ownership evidence."""

from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import datetime
from uuid import UUID

from investment_analyst.core.models import RawRecord, SourceReference
from investment_analyst.evidence.sec_beneficial_ownership.models import (
    BENEFICIAL_OWNERSHIP_OUTCOME_SCHEMA_VERSION,
    BENEFICIAL_OWNERSHIP_SCHEMA_VERSION,
    BENEFICIAL_OWNERSHIP_SOURCE_ID,
    BeneficialOwnershipResolutionOutcome,
    BeneficialOwnershipStatement,
)
from investment_analyst.storage import RecordNotFoundError, StorageError


class BeneficialOwnershipRepositoryError(StorageError):
    """A persisted beneficial-ownership record cannot be trusted."""


def outcome_to_raw_record(outcome: BeneficialOwnershipResolutionOutcome) -> RawRecord:
    return RawRecord(
        record_id=outcome.raw_record_id,
        asset_id=outcome.asset_id,
        source=SourceReference(
            source_id=BENEFICIAL_OWNERSHIP_SOURCE_ID,
            record_key=json.dumps({"outcome_id": str(outcome.outcome_id)}, sort_keys=True),
            retrieved_at=outcome.retrieved_at,
            raw_uri=outcome.resource_url,
            checksum_sha256=outcome.content_sha256,
        ),
        event_time=outcome.filing.accepted_at,
        available_at=outcome.available_at,
        received_at=outcome.retrieved_at,
        payload={
            "kind": "sec_beneficial_ownership_outcome",
            "outcome": outcome.model_dump(mode="json"),
        },
        schema_version=outcome.schema_version,
    )


def outcome_from_raw_record(record: RawRecord) -> BeneficialOwnershipResolutionOutcome:
    if (
        record.source.source_id != BENEFICIAL_OWNERSHIP_SOURCE_ID
        or record.schema_version != BENEFICIAL_OWNERSHIP_OUTCOME_SCHEMA_VERSION
        or not isinstance(record.payload, dict)
        or record.payload.get("kind") != "sec_beneficial_ownership_outcome"
    ):
        raise BeneficialOwnershipRepositoryError(
            "beneficial ownership outcome RawRecord is malformed"
        )
    try:
        outcome = BeneficialOwnershipResolutionOutcome.model_validate_json(
            json.dumps(record.payload["outcome"])
        )
    except (KeyError, ValueError) as error:
        raise BeneficialOwnershipRepositoryError(
            "beneficial ownership outcome is malformed"
        ) from error
    if (
        record.record_id != outcome.raw_record_id
        or record.asset_id != outcome.asset_id
        or record.event_time != outcome.filing.accepted_at
        or record.available_at != outcome.available_at
        or record.received_at != outcome.retrieved_at
        or record.source.record_key
        != json.dumps({"outcome_id": str(outcome.outcome_id)}, sort_keys=True)
        or record.source.retrieved_at != outcome.retrieved_at
        or record.source.raw_uri != outcome.resource_url
        or record.source.checksum_sha256 != outcome.content_sha256
    ):
        raise BeneficialOwnershipRepositoryError("beneficial ownership outcome RawRecord conflicts")
    return outcome


def statement_to_raw_record(statement: BeneficialOwnershipStatement) -> RawRecord:
    return RawRecord(
        record_id=statement.raw_record_id,
        asset_id=statement.asset_id,
        source=SourceReference(
            source_id=BENEFICIAL_OWNERSHIP_SOURCE_ID,
            record_key=json.dumps({"statement_id": str(statement.statement_id)}, sort_keys=True),
            retrieved_at=statement.parsed_at,
            raw_uri=statement.document_revision.source_url,
            checksum_sha256=statement.document_revision.content_sha256,
        ),
        event_time=statement.document_revision.document.filing.accepted_at,
        available_at=statement.available_at,
        received_at=statement.parsed_at,
        payload={
            "kind": "sec_beneficial_ownership_statement",
            "statement": statement.model_dump(mode="json"),
        },
        schema_version=statement.schema_version,
    )


def statement_from_raw_record(record: RawRecord) -> BeneficialOwnershipStatement:
    if (
        record.source.source_id != BENEFICIAL_OWNERSHIP_SOURCE_ID
        or record.schema_version != BENEFICIAL_OWNERSHIP_SCHEMA_VERSION
        or not isinstance(record.payload, dict)
        or record.payload.get("kind") != "sec_beneficial_ownership_statement"
    ):
        raise BeneficialOwnershipRepositoryError("beneficial ownership RawRecord is malformed")
    try:
        statement = BeneficialOwnershipStatement.model_validate_json(
            json.dumps(record.payload["statement"])
        )
    except (KeyError, ValueError) as error:
        raise BeneficialOwnershipRepositoryError(
            "beneficial ownership statement is malformed"
        ) from error
    if (
        record.record_id != statement.raw_record_id
        or record.asset_id != statement.asset_id
        or record.event_time != statement.document_revision.document.filing.accepted_at
        or record.available_at != statement.available_at
        or record.received_at != statement.parsed_at
        or record.source.record_key
        != json.dumps({"statement_id": str(statement.statement_id)}, sort_keys=True)
        or record.source.retrieved_at != statement.parsed_at
        or record.source.raw_uri != statement.document_revision.source_url
        or record.source.checksum_sha256 != statement.document_revision.content_sha256
    ):
        raise BeneficialOwnershipRepositoryError("beneficial ownership RawRecord conflicts")
    return statement


class BeneficialOwnershipRepository:
    def __init__(self, raw_records) -> None:
        self._raw_records = raw_records

    def get(self, statement_id: UUID) -> BeneficialOwnershipStatement | None:
        try:
            return statement_from_raw_record(
                self._raw_records.get(
                    BeneficialOwnershipStatement.expected_raw_record_id(statement_id)
                )
            )
        except RecordNotFoundError:
            return None

    def get_outcome(self, outcome_id: UUID) -> BeneficialOwnershipResolutionOutcome | None:
        try:
            return outcome_from_raw_record(
                self._raw_records.get(
                    BeneficialOwnershipResolutionOutcome.expected_raw_record_id(outcome_id)
                )
            )
        except RecordNotFoundError:
            return None

    def save(self, statement: BeneficialOwnershipStatement) -> BeneficialOwnershipStatement:
        existing = self.get(statement.statement_id)
        if existing is not None and existing != statement:
            raise BeneficialOwnershipRepositoryError("beneficial ownership identity conflicts")
        self._raw_records.save(statement_to_raw_record(statement))
        return statement

    def save_outcome(
        self, outcome: BeneficialOwnershipResolutionOutcome
    ) -> BeneficialOwnershipResolutionOutcome:
        existing = self.get_outcome(outcome.outcome_id)
        if existing is not None and existing != outcome:
            raise BeneficialOwnershipRepositoryError(
                "beneficial ownership outcome identity conflicts"
            )
        self._raw_records.save(outcome_to_raw_record(outcome))
        return outcome

    def list(self, *, asset_id: str, known_at: datetime) -> list[BeneficialOwnershipStatement]:
        return sorted(
            (
                statement_from_raw_record(record)
                for record in self._raw_records.list(
                    asset_id=asset_id,
                    source_id=BENEFICIAL_OWNERSHIP_SOURCE_ID,
                    schema_version=BENEFICIAL_OWNERSHIP_SCHEMA_VERSION,
                    available_to=known_at,
                )
            ),
            key=lambda item: (
                item.available_at,
                item.document_revision.document.filing.accession,
                str(item.statement_id),
            ),
        )


def verify_beneficial_ownership_records(
    records: Iterable[RawRecord], document_repository, content_store
) -> None:
    """Verify new records while preserving prior verification signatures."""
    for record in records:
        if record.schema_version == BENEFICIAL_OWNERSHIP_OUTCOME_SCHEMA_VERSION:
            outcome = outcome_from_raw_record(record)
            content_store.verify(outcome.content_sha256, size_bytes=outcome.content_size_bytes)
        elif record.schema_version == BENEFICIAL_OWNERSHIP_SCHEMA_VERSION:
            statement = statement_from_raw_record(record)
            document_repository.verify_revision(statement.document_revision)
