"""Append-only RawRecord codec for row-scoped institutional correspondence claims."""

from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import datetime
from uuid import UUID

from investment_analyst.core.models import RawRecord, SourceReference
from investment_analyst.evidence.sec_institutional_correspondence.models import (
    ROW_CORRESPONDENCE_SCHEMA_VERSION,
    ROW_CORRESPONDENCE_SOURCE_ID,
    SecInstitutionalRowCorrespondence,
    same_evidence,
)
from investment_analyst.storage import RecordNotFoundError, StorageError


class SecInstitutionalRowCorrespondenceRepositoryError(StorageError):
    """A persisted row correspondence claim cannot be trusted."""


def _raw_uri(item: SecInstitutionalRowCorrespondence) -> str:
    return f"sec-universe-snapshot:{item.universe_snapshot_id}"


def row_correspondence_to_raw_record(item: SecInstitutionalRowCorrespondence) -> RawRecord:
    return RawRecord(
        record_id=item.raw_record_id,
        asset_id=item.asset_id,
        source=SourceReference(
            source_id=ROW_CORRESPONDENCE_SOURCE_ID,
            record_key=json.dumps(
                {"correspondence_id": str(item.correspondence_id)}, sort_keys=True
            ),
            retrieved_at=item.recorded_at,
            raw_uri=_raw_uri(item),
        ),
        event_time=item.event_time,
        available_at=item.available_at,
        received_at=item.recorded_at,
        payload={
            "kind": "sec_institutional_row_correspondence",
            "correspondence": item.model_dump(mode="json"),
        },
        schema_version=item.schema_version,
    )


def row_correspondence_from_raw_record(record: RawRecord) -> SecInstitutionalRowCorrespondence:
    if (
        record.source.source_id != ROW_CORRESPONDENCE_SOURCE_ID
        or record.schema_version != ROW_CORRESPONDENCE_SCHEMA_VERSION
        or not isinstance(record.payload, dict)
        or set(record.payload) != {"kind", "correspondence"}
        or record.payload.get("kind") != "sec_institutional_row_correspondence"
    ):
        raise SecInstitutionalRowCorrespondenceRepositoryError(
            "row correspondence RawRecord is malformed"
        )
    try:
        item = SecInstitutionalRowCorrespondence.model_validate_json(
            json.dumps(record.payload["correspondence"])
        )
    except (KeyError, TypeError, ValueError) as error:
        raise SecInstitutionalRowCorrespondenceRepositoryError(
            "row correspondence payload is malformed"
        ) from error
    expected_key = json.dumps({"correspondence_id": str(item.correspondence_id)}, sort_keys=True)
    if (
        record.record_id != item.raw_record_id
        or record.asset_id != item.asset_id
        or record.event_time != item.event_time
        or record.available_at != item.available_at
        or record.received_at != item.recorded_at
        or record.source.record_key != expected_key
        or record.source.retrieved_at != item.recorded_at
        or record.source.raw_uri != _raw_uri(item)
    ):
        raise SecInstitutionalRowCorrespondenceRepositoryError(
            "row correspondence RawRecord conflicts"
        )
    return item


class SecInstitutionalRowCorrespondenceRepository:
    """Append-only storage of row-scoped claims with point-in-time reads."""

    def __init__(self, raw_records) -> None:
        self._raw_records = raw_records

    def get(self, correspondence_id: UUID) -> SecInstitutionalRowCorrespondence | None:
        try:
            record = self._raw_records.get(
                SecInstitutionalRowCorrespondence.expected_raw_record_id(correspondence_id)
            )
        except RecordNotFoundError:
            return None
        return row_correspondence_from_raw_record(record)

    def save(self, item: SecInstitutionalRowCorrespondence) -> SecInstitutionalRowCorrespondence:
        existing = self.get(item.correspondence_id)
        if existing is not None:
            if not same_evidence(existing, item):
                raise SecInstitutionalRowCorrespondenceRepositoryError(
                    "row correspondence identity conflicts"
                )
            return existing
        self._raw_records.save(row_correspondence_to_raw_record(item))
        return item

    def list(
        self,
        *,
        known_at: datetime,
        asset_id: str | None = None,
        artifact_id: UUID | None = None,
        row_id: UUID | None = None,
    ) -> list[SecInstitutionalRowCorrespondence]:
        """Load claims available at one cut, then filter and order them deterministically."""
        claims = (
            row_correspondence_from_raw_record(record)
            for record in self._raw_records.list(
                asset_id=asset_id,
                source_id=ROW_CORRESPONDENCE_SOURCE_ID,
                schema_version=ROW_CORRESPONDENCE_SCHEMA_VERSION,
                available_to=known_at,
            )
        )
        return sorted(
            (
                item
                for item in claims
                if (artifact_id is None or item.artifact_id == artifact_id)
                and (row_id is None or item.row_id == row_id)
            ),
            key=lambda item: (item.available_at, str(item.correspondence_id)),
        )


def verify_sec_institutional_row_correspondence_records(
    records: Iterable[RawRecord],
    *,
    service,
) -> None:
    """Verify each claim against the complete persisted lineage in one bounded pass."""
    for record in records:
        if record.source.source_id != ROW_CORRESPONDENCE_SOURCE_ID:
            continue
        if record.schema_version != ROW_CORRESPONDENCE_SCHEMA_VERSION:
            raise SecInstitutionalRowCorrespondenceRepositoryError(
                "row correspondence schema version is invalid"
            )
        service.verify_lineage(row_correspondence_from_raw_record(record))
