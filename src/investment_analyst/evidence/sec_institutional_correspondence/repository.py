"""Append-only RawRecord codec for row-scoped institutional correspondence claims."""

from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import datetime
from uuid import UUID

from investment_analyst.core.models import RawRecord, SourceReference
from investment_analyst.evidence.sec_documents.models import normalize_cik
from investment_analyst.evidence.sec_institutional_correspondence.models import (
    ROW_CORRESPONDENCE_SCHEMA_VERSION,
    ROW_CORRESPONDENCE_SOURCE_ID,
    SecInstitutionalRowCorrespondence,
    same_evidence,
)
from investment_analyst.storage import RecordNotFoundError, StorageError

_ROW_CLAIM_SELECTION_BATCH_SIZE = 512


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
        manager_cik: str | None = None,
        row_id: UUID | None = None,
    ) -> list[SecInstitutionalRowCorrespondence]:
        """Load claims available at one cut, then filter and order them deterministically."""
        if artifact_id is not None:
            manager = normalize_cik(manager_cik) if manager_cik is not None else None
            selected_ids = self._raw_records.select_record_ids_by_json_field(
                field="correspondence_artifact",
                values=(str(artifact_id),),
                source_id=ROW_CORRESPONDENCE_SOURCE_ID,
                schema_version=ROW_CORRESPONDENCE_SCHEMA_VERSION,
                available_to=known_at,
            )
            ordered = self._hydrate_selected_in_order(selected_ids)
            return sorted(
                (
                    claim
                    for claim in (row_correspondence_from_raw_record(record) for record in ordered)
                    if (asset_id is None or claim.asset_id == asset_id)
                    and (manager is None or claim.manager_cik == manager)
                    and (row_id is None or claim.row_id == row_id)
                ),
                key=lambda item: (item.available_at, str(item.correspondence_id)),
            )
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
                if (manager_cik is None or item.manager_cik == normalize_cik(manager_cik))
                and (row_id is None or item.row_id == row_id)
            ),
            key=lambda item: (item.available_at, str(item.correspondence_id)),
        )

    def selected_claim_ids_for_candidate(
        self, *, known_at: datetime, claim_ids: tuple[UUID, ...]
    ) -> set[UUID]:
        """Confirm exactly the declared claim IDs remain visible at one cut.

        Selection by closed-set identity precedes hydration: only the declared
        candidates are read and validated, never the asset history.
        """
        ordered_ids = tuple(dict.fromkeys(claim_ids))
        if not ordered_ids:
            return set()
        expected_raw_ids = tuple(
            SecInstitutionalRowCorrespondence.expected_raw_record_id(correspondence_id)
            for correspondence_id in ordered_ids
        )
        try:
            records_by_id = self._raw_records.get_many(expected_raw_ids)
        except RecordNotFoundError as error:
            raise SecInstitutionalRowCorrespondenceRepositoryError(
                "selected row correspondence claim is absent"
            ) from error
        visible: set[UUID] = set()
        for correspondence_id, raw_record_id in zip(ordered_ids, expected_raw_ids, strict=True):
            try:
                record = records_by_id[raw_record_id]
            except KeyError as error:
                raise SecInstitutionalRowCorrespondenceRepositoryError(
                    "selected row correspondence claim is absent"
                ) from error
            if record.available_at > known_at:
                continue
            claim = row_correspondence_from_raw_record(record)
            if claim.correspondence_id != correspondence_id:
                raise SecInstitutionalRowCorrespondenceRepositoryError(
                    "selected row correspondence claim conflicts"
                )
            visible.add(claim.correspondence_id)
        return visible

    def _hydrate_selected_in_order(self, record_ids: list[UUID]) -> list[RawRecord]:
        ordered: list[RawRecord] = []
        for offset in range(0, len(record_ids), _ROW_CLAIM_SELECTION_BATCH_SIZE):
            batch_ids = record_ids[offset : offset + _ROW_CLAIM_SELECTION_BATCH_SIZE]
            try:
                records_by_id = self._raw_records.get_many(batch_ids)
            except RecordNotFoundError as error:
                raise SecInstitutionalRowCorrespondenceRepositoryError(
                    "selected row correspondence claim is absent"
                ) from error
            for record_id in batch_ids:
                try:
                    ordered.append(records_by_id[record_id])
                except KeyError as error:
                    raise SecInstitutionalRowCorrespondenceRepositoryError(
                        "selected row correspondence claim is absent"
                    ) from error
        return ordered


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
