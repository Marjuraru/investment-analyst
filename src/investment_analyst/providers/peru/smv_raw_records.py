"""Immutable SMV registry snapshots stored with exact HTTPS response evidence."""

import base64
import binascii
import json
from datetime import datetime
from hashlib import sha256
from uuid import UUID, uuid5

from pydantic import ConfigDict

from investment_analyst.core.models import RawRecord, SourceDefinition, SourceReference, SourceType
from investment_analyst.core.models.base import ContractModel, NonEmptyStr
from investment_analyst.core.models.source import Sha256Checksum
from investment_analyst.providers.peru.smv_open_data import (
    SMV_COMPANIES_URL,
    SMV_PORTAL_SCHEMA_VERSION,
    SMV_SECURITIES_URL,
    SmvOpenDataDataset,
    SmvOpenDataError,
    SmvOpenDataFetch,
    SmvOpenDataSnapshot,
    parse_smv_portal_snapshot,
)
from investment_analyst.storage import StorageError

SMV_RAW_RECORD_SCHEMA = "smv-open-data-registry-raw-v1"
SMV_COMPANIES_SOURCE_ID = "smv-open-data:registered-companies"
SMV_SECURITIES_SOURCE_ID = "smv-open-data:registered-securities"
_RAW_RECORD_NAMESPACE = UUID("112ac3af-fb59-5980-b9f9-c28d41ec6e33")


class SmvRawRecordMetadata(ContractModel):
    """Canonical query and response identity encoded in a registry RawRecord."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    dataset: SmvOpenDataDataset
    query_legal_name: NonEmptyStr
    semantic_sha256: Sha256Checksum
    body_sha256: Sha256Checksum


class StoredSmvRegistrySnapshot:
    """Verified snapshot reconstructed from one exact stored portal body."""

    __slots__ = ("metadata", "record", "snapshot")

    def __init__(
        self,
        *,
        metadata: SmvRawRecordMetadata,
        record: RawRecord,
        snapshot: SmvOpenDataSnapshot,
    ) -> None:
        self.metadata = metadata
        self.record = record
        self.snapshot = snapshot


def smv_source_id(dataset: SmvOpenDataDataset) -> str:
    """Return the stable source ID for one explicit SMV dataset."""
    if dataset is SmvOpenDataDataset.REGISTERED_COMPANIES:
        return SMV_COMPANIES_SOURCE_ID
    return SMV_SECURITIES_SOURCE_ID


def smv_source_url(dataset: SmvOpenDataDataset) -> str:
    """Return the official HTTPS portal page used for one dataset."""
    if dataset is SmvOpenDataDataset.REGISTERED_COMPANIES:
        return SMV_COMPANIES_URL
    return SMV_SECURITIES_URL


def create_smv_source(dataset: SmvOpenDataDataset) -> SourceDefinition:
    """Create one provider-independent definition for the ODbL registry dataset."""
    label = (
        "registered companies"
        if dataset is SmvOpenDataDataset.REGISTERED_COMPANIES
        else "registered securities"
    )
    return SourceDefinition(
        source_id=smv_source_id(dataset),
        provider_name="Superintendencia del Mercado de Valores del Perú",
        dataset_name=f"SMV Open Data {label}",
        source_type=SourceType.REGISTRY,
        base_url=smv_source_url(dataset),
        is_official=True,
        coverage_notes=(
            "Official ODbL registry queried by exact legal name through the HTTPS portal. "
            "The registered-securities field labelled CodigoISIN is retained as an abbreviated "
            "provider code and is never promoted to a complete ISIN without corroboration."
        ),
    )


def smv_fetch_to_raw_record(fetch: SmvOpenDataFetch) -> RawRecord:
    """Convert one verified portal response into an exact immutable RawRecord."""
    try:
        fetch.response_body.decode("utf-8")
    except UnicodeDecodeError as error:
        raise StorageError("SMV response body is not valid UTF-8") from error
    if fetch.snapshot.schema_version != SMV_PORTAL_SCHEMA_VERSION:
        raise StorageError("SMV parsed snapshot schema is unsupported")
    if fetch.requested_url != smv_source_url(fetch.snapshot.dataset):
        raise StorageError("SMV fetch requested URL does not match its dataset")
    if fetch.final_url != fetch.requested_url:
        raise StorageError("SMV fetch final URL does not match its official request URL")
    if fetch.content_type not in {"text/html", "application/xhtml+xml"}:
        raise StorageError("SMV fetch content type is inconsistent")
    body_sha256 = sha256(fetch.response_body).hexdigest()
    if body_sha256 != fetch.body_sha256:
        raise StorageError("SMV response body checksum is inconsistent")
    semantic_sha256 = smv_snapshot_sha256(fetch.snapshot)
    metadata = SmvRawRecordMetadata(
        dataset=fetch.snapshot.dataset,
        query_legal_name=fetch.snapshot.query_legal_name,
        semantic_sha256=semantic_sha256,
        body_sha256=body_sha256,
    )
    source_id = smv_source_id(metadata.dataset)
    record = RawRecord(
        record_id=_record_id(source_id, metadata, fetch.retrieved_at),
        asset_id=None,
        source=SourceReference(
            source_id=source_id,
            record_key=_metadata_json(metadata),
            retrieved_at=fetch.retrieved_at,
            raw_uri=fetch.final_url,
            checksum_sha256=body_sha256,
        ),
        event_time=None,
        available_at=fetch.retrieved_at,
        received_at=fetch.retrieved_at,
        payload={
            "encoding": "base64",
            "body_base64": base64.b64encode(fetch.response_body).decode("ascii"),
        },
        schema_version=SMV_RAW_RECORD_SCHEMA,
    )
    verified = stored_smv_snapshot_from_raw_record(record)
    if verified.snapshot != fetch.snapshot:
        raise StorageError("SMV stored response differs from its parsed fetch snapshot")
    return record


def stored_smv_snapshot_from_raw_record(record: RawRecord) -> StoredSmvRegistrySnapshot:
    """Decode and verify one SMV snapshot without trusting index metadata."""
    if record.schema_version != SMV_RAW_RECORD_SCHEMA:
        raise StorageError("raw record is not a supported SMV registry snapshot")
    if record.asset_id is not None or record.event_time is not None:
        raise StorageError("SMV registry snapshots must not invent one asset or event time")
    if record.source.record_key is None:
        raise StorageError("SMV registry record key is missing")
    body = _payload_body(record.payload)
    try:
        metadata = SmvRawRecordMetadata.model_validate_json(record.source.record_key)
    except ValueError as error:
        raise StorageError("SMV registry record key is malformed") from error
    source_id = smv_source_id(metadata.dataset)
    if record.source.source_id != source_id:
        raise StorageError("SMV registry source identity is inconsistent")
    if record.source.raw_uri != smv_source_url(metadata.dataset):
        raise StorageError("SMV registry raw URI is inconsistent")
    body_sha256 = sha256(body).hexdigest()
    if body_sha256 != metadata.body_sha256:
        raise StorageError("SMV registry response body checksum is inconsistent")
    if record.source.checksum_sha256 != body_sha256:
        raise StorageError("SMV registry source checksum is inconsistent")
    if record.source.retrieved_at != record.received_at:
        raise StorageError("SMV registry retrieval timestamps are inconsistent")
    if record.available_at != record.received_at:
        raise StorageError("SMV registry availability must equal first local receipt")
    if record.record_id != _record_id(source_id, metadata, record.received_at):
        raise StorageError("SMV registry raw record identity is inconsistent")
    try:
        snapshot = parse_smv_portal_snapshot(
            body.decode("utf-8"),
            dataset=metadata.dataset,
            query_legal_name=metadata.query_legal_name,
        )
    except (SmvOpenDataError, ValueError) as error:
        raise StorageError("SMV registry raw body no longer validates") from error
    if smv_snapshot_sha256(snapshot) != metadata.semantic_sha256:
        raise StorageError("SMV registry semantic checksum is inconsistent")
    return StoredSmvRegistrySnapshot(
        metadata=metadata,
        record=record,
        snapshot=snapshot,
    )


def smv_snapshot_sha256(snapshot: SmvOpenDataSnapshot) -> str:
    """Hash one canonical parsed snapshot independently of portal form state."""
    encoded = json.dumps(
        snapshot.to_json_dict(),
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


def _payload_body(payload: object) -> bytes:
    if not isinstance(payload, dict) or set(payload) != {"encoding", "body_base64"}:
        raise StorageError("SMV registry raw payload has an invalid envelope")
    encoding = payload.get("encoding")
    body_base64 = payload.get("body_base64")
    if encoding != "base64" or not isinstance(body_base64, str):
        raise StorageError("SMV registry raw payload must contain one Base64 body")
    try:
        body = base64.b64decode(body_base64, validate=True)
        body.decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, ValueError) as error:
        raise StorageError("SMV registry raw payload body is invalid") from error
    return body


def _record_id(
    source_id: str,
    metadata: SmvRawRecordMetadata,
    retrieved_at: datetime,
) -> UUID:
    identity = json.dumps(
        {
            "schema_version": SMV_RAW_RECORD_SCHEMA,
            "source_id": source_id,
            "retrieved_at": retrieved_at.isoformat(),
            **metadata.model_dump(mode="json"),
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return uuid5(_RAW_RECORD_NAMESPACE, identity)


def _metadata_json(metadata: SmvRawRecordMetadata) -> str:
    return json.dumps(
        metadata.model_dump(mode="json"),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


__all__ = [
    "SMV_COMPANIES_SOURCE_ID",
    "SMV_RAW_RECORD_SCHEMA",
    "SMV_SECURITIES_SOURCE_ID",
    "SmvRawRecordMetadata",
    "StoredSmvRegistrySnapshot",
    "create_smv_source",
    "smv_fetch_to_raw_record",
    "smv_snapshot_sha256",
    "smv_source_id",
    "stored_smv_snapshot_from_raw_record",
]
