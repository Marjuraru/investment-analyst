"""FRED/ALFRED source identity and immutable vintage snapshot storage."""

import json
from datetime import UTC, date, datetime, time, timedelta
from hashlib import sha256
from typing import Literal
from uuid import UUID, uuid5

from pydantic import ConfigDict

from investment_analyst.core.models import RawRecord, SourceDefinition, SourceReference, SourceType
from investment_analyst.core.models.base import ContractModel, NonEmptyStr
from investment_analyst.core.models.source import Sha256Checksum
from investment_analyst.providers.macro.fred_alfred import (
    OFFICIAL_BASE_URL,
    FredObservationsResponse,
    FredVintageFetch,
    parse_stored_fred_response,
    validate_fred_series_id,
)
from investment_analyst.storage import StorageError

FRED_VINTAGE_SCHEMA = "fred-alfred-vintage-observations-v1"
_RAW_RECORD_NAMESPACE = UUID("5698bdff-5b47-5bc3-bd32-0ee45a08c651")


class FredRawRecordMetadata(ContractModel):
    """Canonical request identity encoded in a RawRecord record key."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    series_id: NonEmptyStr
    vintage_date: date
    observation_start: date
    observation_end: date
    units: Literal["lin"]
    output_type: Literal[1]
    body_sha256: Sha256Checksum
    canonical_payload_sha256: Sha256Checksum


class StoredFredVintage:
    """Verified view reconstructed from one immutable raw record."""

    __slots__ = ("metadata", "record", "response")

    def __init__(
        self,
        *,
        metadata: FredRawRecordMetadata,
        record: RawRecord,
        response: FredObservationsResponse,
    ) -> None:
        self.metadata = metadata
        self.record = record
        self.response = response


def fred_source_id(series_id: str) -> str:
    """Return the deterministic source identity for one FRED series."""
    canonical = validate_fred_series_id(series_id)
    return f"fred-alfred:series:{canonical.casefold()}:vintage-observations:lin"


def create_fred_source(series_id: str) -> SourceDefinition:
    """Create the official macro source definition for one independent series."""
    canonical = validate_fred_series_id(series_id)
    return SourceDefinition(
        source_id=fred_source_id(canonical),
        provider_name="Federal Reserve Bank of St. Louis",
        dataset_name=f"FRED/ALFRED {canonical} vintage observations",
        source_type=SourceType.MACRO,
        base_url=OFFICIAL_BASE_URL,
        is_official=True,
        coverage_notes=(
            "Official FRED series retrieved with ALFRED vintage-date semantics and no "
            "provider-side transformation (units=lin). Historical availability has daily, not "
            "intraday, precision; old vintages become conservatively usable at the next UTC day."
        ),
    )


def fred_vintage_to_raw_record(fetch: FredVintageFetch) -> RawRecord:
    """Convert one validated response into a deterministic macro RawRecord."""
    metadata = FredRawRecordMetadata(
        series_id=fetch.series_id,
        vintage_date=fetch.vintage_date,
        observation_start=fetch.requested_observation_start,
        observation_end=fetch.requested_observation_end,
        units="lin",
        output_type=1,
        body_sha256=fetch.body_sha256,
        canonical_payload_sha256=_canonical_payload_sha256(fetch.response_payload),
    )
    record_key = _metadata_json(metadata)
    source_id = fred_source_id(fetch.series_id)
    record_id = _record_id(source_id, metadata)
    available_at = _conservative_availability(fetch.vintage_date, fetch.retrieved_at)
    record = RawRecord(
        record_id=record_id,
        asset_id=None,
        source=SourceReference(
            source_id=source_id,
            record_key=record_key,
            retrieved_at=fetch.retrieved_at,
            raw_uri=fetch.public_request_url,
            checksum_sha256=fetch.body_sha256,
        ),
        event_time=None,
        available_at=available_at,
        received_at=fetch.retrieved_at,
        payload=fetch.response_payload,
        schema_version=FRED_VINTAGE_SCHEMA,
    )
    verified = stored_fred_vintage_from_raw_record(record)
    if verified.response != fetch.response:
        raise StorageError("FRED/ALFRED parsed response differs from its raw payload")
    return record


def stored_fred_vintage_from_raw_record(record: RawRecord) -> StoredFredVintage:
    """Decode and verify one FRED vintage RawRecord without trusting its index metadata."""
    if record.schema_version != FRED_VINTAGE_SCHEMA:
        raise StorageError("raw record is not a supported FRED/ALFRED vintage snapshot")
    if record.asset_id is not None:
        raise StorageError("FRED/ALFRED macro records must not invent an asset_id")
    if record.event_time is not None:
        raise StorageError("a full FRED/ALFRED snapshot must not invent one event_time")
    if record.source.record_key is None:
        raise StorageError("FRED/ALFRED record key is missing")
    try:
        metadata = FredRawRecordMetadata.model_validate_json(record.source.record_key)
    except ValueError as error:
        raise StorageError("FRED/ALFRED record key is malformed") from error
    validate_fred_series_id(metadata.series_id)
    if metadata.units != "lin" or metadata.output_type != 1:
        raise StorageError("FRED/ALFRED record transformation contract is inconsistent")
    if metadata.observation_start > metadata.observation_end:
        raise StorageError("FRED/ALFRED record observation bounds are inconsistent")
    source_id = fred_source_id(metadata.series_id)
    if record.source.source_id != source_id:
        raise StorageError("FRED/ALFRED record source identity is inconsistent")
    if record.source.checksum_sha256 != metadata.body_sha256:
        raise StorageError("FRED/ALFRED response checksum metadata is inconsistent")
    if _canonical_payload_sha256(record.payload) != metadata.canonical_payload_sha256:
        raise StorageError("FRED/ALFRED canonical payload checksum is inconsistent")
    if record.record_id != _record_id(source_id, metadata):
        raise StorageError("FRED/ALFRED raw record identity is inconsistent")
    if record.source.retrieved_at != record.received_at:
        raise StorageError("FRED/ALFRED receipt timestamps are inconsistent")
    if record.available_at != _conservative_availability(metadata.vintage_date, record.received_at):
        raise StorageError("FRED/ALFRED point-in-time availability is inconsistent")
    if record.source.raw_uri is None or "api_key" in record.source.raw_uri.casefold():
        raise StorageError("FRED/ALFRED public request URI is missing or unsafe")
    response = parse_stored_fred_response(
        record.payload,
        vintage_date=metadata.vintage_date,
        observation_start=metadata.observation_start,
        observation_end=metadata.observation_end,
    )
    return StoredFredVintage(metadata=metadata, record=record, response=response)


def _record_id(source_id: str, metadata: FredRawRecordMetadata) -> UUID:
    identity = json.dumps(
        {
            "schema_version": FRED_VINTAGE_SCHEMA,
            "source_id": source_id,
            **metadata.model_dump(mode="json"),
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return uuid5(_RAW_RECORD_NAMESPACE, identity)


def _metadata_json(metadata: FredRawRecordMetadata) -> str:
    return json.dumps(
        metadata.model_dump(mode="json"),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _canonical_payload_sha256(payload: object) -> str:
    try:
        encoded = json.dumps(
            payload,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise StorageError("FRED/ALFRED payload is not canonical JSON") from error
    return sha256(encoded).hexdigest()


def _conservative_availability(vintage_date: date, retrieved_at: datetime) -> datetime:
    next_utc_day = datetime.combine(vintage_date + timedelta(days=1), time.min, tzinfo=UTC)
    return min(retrieved_at, next_utc_day)
