from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import datetime
from uuid import UUID

from investment_analyst.core.interfaces.repositories import RawRecordRepository
from investment_analyst.core.models import RawRecord, SourceReference
from investment_analyst.evidence.instrument_correspondence.models import (
    INSTRUMENT_CORRESPONDENCE_SCHEMA_VERSION,
    INSTRUMENT_CORRESPONDENCE_SOURCE_ID,
    InstrumentCorrespondence,
)
from investment_analyst.storage import RecordNotFoundError, StorageError


class InstrumentCorrespondenceRepositoryError(StorageError):
    """A declared correspondence record cannot be trusted."""


def _provenance(catalog_version: int, declared_by: str) -> dict[str, int | str]:
    if isinstance(catalog_version, bool) or catalog_version < 1:
        raise InstrumentCorrespondenceRepositoryError(
            "instrument correspondence catalog version is invalid"
        )
    if not declared_by.strip():
        raise InstrumentCorrespondenceRepositoryError(
            "instrument correspondence declarer is invalid"
        )
    return {"catalog_version": catalog_version, "declared_by": declared_by}


def correspondence_to_raw_record(
    item: InstrumentCorrespondence, *, catalog_version: int, declared_by: str
) -> RawRecord:
    return RawRecord(
        record_id=item.raw_record_id,
        asset_id=item.asset_id,
        source=SourceReference(
            source_id=INSTRUMENT_CORRESPONDENCE_SOURCE_ID,
            record_key=json.dumps(
                {"correspondence_id": str(item.correspondence_id)}, sort_keys=True
            ),
            retrieved_at=item.recorded_at,
            raw_uri="catalog:default_assets.v1.json",
        ),
        event_time=item.event_time,
        available_at=item.available_at,
        received_at=item.recorded_at,
        payload={
            "kind": "instrument_correspondence",
            "correspondence": item.model_dump(mode="json"),
            "provenance": _provenance(catalog_version, declared_by),
        },
        schema_version=item.schema_version,
    )


def correspondence_from_raw_record(record: RawRecord) -> InstrumentCorrespondence:
    if (
        record.source.source_id != INSTRUMENT_CORRESPONDENCE_SOURCE_ID
        or record.schema_version != INSTRUMENT_CORRESPONDENCE_SCHEMA_VERSION
        or not isinstance(record.payload, dict)
        or record.payload.get("kind") != "instrument_correspondence"
    ):
        raise InstrumentCorrespondenceRepositoryError(
            "instrument correspondence RawRecord is malformed"
        )
    try:
        item = InstrumentCorrespondence.model_validate_json(
            json.dumps(record.payload["correspondence"])
        )
        provenance = record.payload["provenance"]
        if not isinstance(provenance, dict):
            raise TypeError("provenance must be an object")
        _provenance(
            provenance["catalog_version"],
            provenance["declared_by"],
        )
    except (KeyError, TypeError, ValueError) as error:
        raise InstrumentCorrespondenceRepositoryError(
            "instrument correspondence payload is malformed"
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
        or record.source.raw_uri != "catalog:default_assets.v1.json"
    ):
        raise InstrumentCorrespondenceRepositoryError(
            "instrument correspondence RawRecord conflicts"
        )
    return item


class InstrumentCorrespondenceRepository:
    def __init__(self, raw_records: RawRecordRepository) -> None:
        self._raw_records = raw_records

    def get(self, correspondence_id: UUID) -> InstrumentCorrespondence | None:
        try:
            return correspondence_from_raw_record(
                self._raw_records.get(
                    InstrumentCorrespondence.expected_raw_record_id(correspondence_id)
                )
            )
        except RecordNotFoundError:
            return None

    def save(
        self, item: InstrumentCorrespondence, *, catalog_version: int, declared_by: str
    ) -> InstrumentCorrespondence:
        _provenance(catalog_version, declared_by)
        existing = self.get(item.correspondence_id)
        if existing is not None and existing != item:
            raise InstrumentCorrespondenceRepositoryError(
                "instrument correspondence identity conflicts"
            )
        if existing is not None:
            return existing
        self._raw_records.save(
            correspondence_to_raw_record(
                item, catalog_version=catalog_version, declared_by=declared_by
            )
        )
        return item

    def list(
        self, *, known_at: datetime, asset_id: str | None = None
    ) -> list[InstrumentCorrespondence]:
        return sorted(
            (
                correspondence_from_raw_record(r)
                for r in self._raw_records.list(
                    asset_id=asset_id,
                    source_id=INSTRUMENT_CORRESPONDENCE_SOURCE_ID,
                    schema_version=INSTRUMENT_CORRESPONDENCE_SCHEMA_VERSION,
                    available_to=known_at,
                )
            ),
            key=lambda item: (item.available_at, item.effective_from, str(item.correspondence_id)),
        )


def verify_instrument_correspondence_records(records: Iterable[RawRecord]) -> None:
    for record in records:
        if record.source.source_id == INSTRUMENT_CORRESPONDENCE_SOURCE_ID:
            if record.schema_version != INSTRUMENT_CORRESPONDENCE_SCHEMA_VERSION:
                raise InstrumentCorrespondenceRepositoryError(
                    "instrument correspondence schema version is invalid"
                )
            correspondence_from_raw_record(record)
