"""Append-only ownership RawRecord codec."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal
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


OWNERSHIP_TERMINAL_REJECTION_REASONS = frozenset(
    {"forbidden_declaration", "not_xml", "incompatible_root"}
)
"""Versioned resource rejections that end resolution for one accession."""


@dataclass(frozen=True, slots=True)
class OwnershipAccessionState:
    """Typed persisted resolution of one Section 16 accession.

    ``accepted`` means a statement was verified; ``rejected`` means the evaluated resource was
    terminally rejected by a versioned reason code; ``partial`` means evidence exists without a
    statement or a terminal rejection, so the accession must be resumed instead of skipped.
    """

    accession: str
    form: str
    accepted_at: datetime
    resolution: Literal["accepted", "rejected", "partial"]

    @property
    def terminal(self) -> bool:
        """Return whether the accession no longer requires any SEC request."""
        return self.resolution != "partial"


@dataclass(slots=True)
class _OwnershipAccumulator:
    form: str
    accepted_at: datetime
    has_statement: bool = False
    has_accepted_outcome: bool = False
    rejected_reasons: set[str] = field(default_factory=set)


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
        schema_version=outcome.schema_version,
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
    if record.schema_version != outcome.schema_version:
        raise OwnershipRepositoryError("ownership outcome RawRecord schema conflicts")
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
    if record.schema_version != statement.schema_version:
        raise OwnershipRepositoryError("ownership RawRecord schema conflicts with statement")
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

    def list_accession_states(
        self, *, asset_id: str, known_at: datetime
    ) -> tuple[OwnershipAccessionState, ...]:
        """List typed per-accession resolution without downloading anything.

        Statements and outcomes of both integrated schema generations are read at the same
        point-in-time cut. An accession with an accepted resource but no statement stays
        ``partial`` so an interrupted parser or storage step is resumed instead of being
        mistaken for a completed one.
        """
        accumulated: dict[str, _OwnershipAccumulator] = {}
        for schema_version in (OWNERSHIP_SCHEMA_VERSION, OWNERSHIP_SCHEMA_VERSION_V2):
            for record in self._raw_records.list(
                asset_id=asset_id,
                source_id=OWNERSHIP_SOURCE_ID,
                schema_version=schema_version,
                available_to=known_at,
            ):
                filing = statement_from_raw_record(record).document_revision.document.filing
                entry = accumulated.get(filing.accession)
                if entry is None:
                    entry = _OwnershipAccumulator(
                        form=filing.form,
                        accepted_at=filing.accepted_at,
                    )
                    accumulated[filing.accession] = entry
                entry.has_statement = True
        for schema_version in (
            OWNERSHIP_OUTCOME_SCHEMA_VERSION,
            OWNERSHIP_OUTCOME_SCHEMA_VERSION_V2,
        ):
            for record in self._raw_records.list(
                asset_id=asset_id,
                source_id=OWNERSHIP_SOURCE_ID,
                schema_version=schema_version,
                available_to=known_at,
            ):
                outcome = outcome_from_raw_record(record)
                filing = outcome.filing
                entry = accumulated.get(filing.accession)
                if entry is None:
                    entry = _OwnershipAccumulator(
                        form=filing.form,
                        accepted_at=filing.accepted_at,
                    )
                    accumulated[filing.accession] = entry
                if outcome.status == "accepted":
                    entry.has_accepted_outcome = True
                else:
                    entry.rejected_reasons.add(outcome.reason_code)
        states = (
            OwnershipAccessionState(
                accession=accession,
                form=entry.form,
                accepted_at=entry.accepted_at,
                resolution=_resolution(entry),
            )
            for accession, entry in accumulated.items()
        )
        return tuple(sorted(states, key=lambda item: (item.accepted_at, item.accession)))


def _resolution(entry: _OwnershipAccumulator) -> Literal["accepted", "rejected", "partial"]:
    if entry.has_statement:
        return "accepted"
    if (
        not entry.has_accepted_outcome
        and entry.rejected_reasons & OWNERSHIP_TERMINAL_REJECTION_REASONS
    ):
        return "rejected"
    return "partial"
