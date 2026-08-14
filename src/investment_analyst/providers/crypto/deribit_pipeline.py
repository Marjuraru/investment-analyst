"""Append-only Deribit raw/observation pipelines and complete fetch receipts."""

import hashlib
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Literal
from urllib.parse import parse_qs, urlsplit
from uuid import UUID

from pydantic import ConfigDict, JsonValue, model_validator

from investment_analyst.core.models import NormalizedObservation, RawRecord, SourceReference
from investment_analyst.core.models.base import ContractModel, NonEmptyStr, UTCDateTime
from investment_analyst.providers.asset_config import DeribitAssetConfiguration
from investment_analyst.providers.crypto.deribit import DeribitClient
from investment_analyst.providers.crypto.deribit_normalizer import (
    canonical_identity,
    canonical_json_text,
    create_deribit_asset,
    create_deribit_sources,
    dvol_to_observations,
    dvol_to_raw_record,
    funding_to_observations,
    funding_to_raw_record,
    summary_to_observations,
    summary_to_raw_record,
)
from investment_analyst.storage import LocalStorage
from investment_analyst.storage.errors import RecordNotFoundError, StorageError

RECEIPT_SCHEMA_VERSION = "deribit-fetch-receipt-v1"
DeribitDataset = Literal["funding_history", "dvol_daily"]


class _PipelineModel(ContractModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class DeribitFetchReceipt(_PipelineModel):
    """Immutable proof that one logical historical interval completed successfully."""

    schema_version: Literal["deribit-fetch-receipt-v1"] = RECEIPT_SCHEMA_VERSION
    receipt_id: UUID
    asset_id: NonEmptyStr
    source_id: NonEmptyStr
    dataset: DeribitDataset
    requested_start: UTCDateTime
    requested_end: UTCDateTime
    completed_at: UTCDateTime
    request_count: int
    row_count: int

    @model_validator(mode="after")
    def validate_receipt(self) -> "DeribitFetchReceipt":
        if self.requested_start >= self.requested_end:
            raise ValueError("receipt start must be earlier than end")
        if self.completed_at < self.requested_end:
            raise ValueError("receipt completion cannot predate its completed interval")
        if self.request_count < 1 or self.row_count < 0:
            raise ValueError("receipt request and row counts are invalid")
        if self.receipt_id != receipt_id(
            asset_id=self.asset_id,
            source_id=self.source_id,
            dataset=self.dataset,
            requested_start=self.requested_start,
            requested_end=self.requested_end,
        ):
            raise ValueError("receipt_id does not match its canonical preimage")
        return self


class DeribitImportSummary(_PipelineModel):
    """Compact outcome for one completed historical or snapshot stage."""

    schema_version: Literal["deribit-import-summary-v1"] = "deribit-import-summary-v1"
    asset_id: NonEmptyStr
    source_id: NonEmptyStr
    dataset: Literal["funding_history", "dvol_daily", "perpetual_summary"]
    requested_start: UTCDateTime | None = None
    requested_end: UTCDateTime | None = None
    retrieved_at: UTCDateTime
    request_count: int
    rows_received: int
    raw_records_created: int
    raw_records_reused: int
    observations_created: int
    observations_reused: int
    receipt_id: UUID | None = None
    receipt_created: bool = False
    traceability_verified: bool

    @model_validator(mode="after")
    def validate_summary(self) -> "DeribitImportSummary":
        historical = self.dataset != "perpetual_summary"
        if historical != (self.requested_start is not None and self.requested_end is not None):
            raise ValueError("historical summaries require both requested bounds")
        if historical != (self.receipt_id is not None):
            raise ValueError("only completed historical stages require a receipt")
        if (
            min(
                self.request_count,
                self.rows_received,
                self.raw_records_created,
                self.raw_records_reused,
                self.observations_created,
                self.observations_reused,
            )
            < 0
        ):
            raise ValueError("import summary counts must not be negative")
        return self

    def to_json_dict(self) -> dict[str, object]:
        return self.model_dump(mode="json")


def receipt_id(
    *,
    asset_id: str,
    source_id: str,
    dataset: DeribitDataset,
    requested_start: datetime,
    requested_end: datetime,
) -> UUID:
    """Return the fixed receipt identity for one requested half-open interval."""
    preimage: dict[str, JsonValue] = {
        "asset_id": asset_id,
        "dataset": dataset,
        "requested_end": _utc(requested_end).isoformat(),
        "requested_start": _utc(requested_start).isoformat(),
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "source_id": source_id,
    }
    return canonical_identity(preimage)


def receipt_to_raw_record(receipt: DeribitFetchReceipt) -> RawRecord:
    """Encode complete interval evidence through the existing generic raw layer."""
    payload = receipt.model_dump(mode="json")
    payload_text = canonical_json_text(payload)
    checksum = hashlib.sha256(payload_text.encode("utf-8")).hexdigest()
    return RawRecord(
        record_id=receipt.receipt_id,
        asset_id=receipt.asset_id,
        source=SourceReference(
            source_id=receipt.source_id,
            record_key=canonical_json_text(
                {
                    "asset_id": receipt.asset_id,
                    "dataset": receipt.dataset,
                    "requested_end": receipt.requested_end.isoformat(),
                    "requested_start": receipt.requested_start.isoformat(),
                    "schema_version": RECEIPT_SCHEMA_VERSION,
                    "source_id": receipt.source_id,
                }
            ),
            retrieved_at=receipt.completed_at,
            checksum_sha256=checksum,
        ),
        event_time=None,
        available_at=receipt.completed_at,
        received_at=receipt.completed_at,
        payload=payload,
        schema_version=RECEIPT_SCHEMA_VERSION,
    )


def raw_record_to_receipt(record: RawRecord) -> DeribitFetchReceipt | None:
    """Decode and verify a Deribit receipt while ignoring ordinary provider rows."""
    if record.schema_version != RECEIPT_SCHEMA_VERSION:
        return None
    receipt = DeribitFetchReceipt.model_validate(record.payload)
    payload_text = canonical_json_text(receipt.model_dump(mode="json"))
    checksum = hashlib.sha256(payload_text.encode("utf-8")).hexdigest()
    if (
        record.record_id != receipt.receipt_id
        or record.asset_id != receipt.asset_id
        or record.source.source_id != receipt.source_id
        or record.received_at != receipt.completed_at
        or record.available_at != receipt.completed_at
        or record.source.checksum_sha256 != checksum
    ):
        raise StorageError("stored Deribit receipt does not match its raw evidence")
    return receipt


class DeribitEvidencePipeline:
    """Persist one complete Deribit dataset stage using the shared writer connection."""

    def __init__(
        self,
        storage: LocalStorage,
        client: DeribitClient,
        *,
        configuration: DeribitAssetConfiguration,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._storage = storage
        self._client = client
        self._configuration = configuration
        self._clock = clock

    def import_funding(self, start: datetime, end: datetime) -> DeribitImportSummary:
        """Fetch, validate, persist, and receipt one funding-history interval."""
        fetch = self._client.fetch_funding_history(
            self._configuration.instrument_name,
            start,
            end,
        )
        normalized_at = max(_utc(self._clock()), fetch.retrieved_at)
        records: list[RawRecord] = []
        observations: list[NormalizedObservation] = []
        counts = [0, 0, 0, 0]
        self._prepare(self._configuration.funding_source_id)
        for point in fetch.points:
            request_url = _request_url_for_event(point.timestamp, fetch.request_urls)
            candidate = funding_to_raw_record(
                point,
                configuration=self._configuration,
                received_at=fetch.retrieved_at,
                request_url=request_url,
            )
            stored, created = self._save_raw(candidate)
            counts[0 if created else 1] += 1
            records.append(stored)
            for observation in funding_to_observations(
                point,
                stored,
                configuration=self._configuration,
                normalized_at=normalized_at,
            ):
                persisted, observation_created = self._save_observation(observation)
                counts[2 if observation_created else 3] += 1
                observations.append(persisted)
        self._verify_traceability(records, observations, self._configuration.funding_source_id)
        receipt, receipt_created = self._save_receipt(
            dataset="funding_history",
            source_id=self._configuration.funding_source_id,
            start=fetch.requested_start,
            end=fetch.requested_end,
            completed_at=fetch.retrieved_at,
            request_count=len(fetch.request_urls),
            row_count=len(fetch.points),
        )
        return DeribitImportSummary(
            asset_id=self._configuration.asset_id,
            source_id=self._configuration.funding_source_id,
            dataset="funding_history",
            requested_start=fetch.requested_start,
            requested_end=fetch.requested_end,
            retrieved_at=fetch.retrieved_at,
            request_count=len(fetch.request_urls),
            rows_received=len(fetch.points),
            raw_records_created=counts[0],
            raw_records_reused=counts[1],
            observations_created=counts[2],
            observations_reused=counts[3],
            receipt_id=receipt.receipt_id,
            receipt_created=receipt_created,
            traceability_verified=True,
        )

    def import_dvol(self, start: datetime, end: datetime) -> DeribitImportSummary:
        """Fetch all DVOL pages before writing rows or a complete receipt."""
        fetch = self._client.fetch_dvol_daily(self._configuration.currency, start, end)
        normalized_at = max(_utc(self._clock()), fetch.retrieved_at)
        records: list[RawRecord] = []
        observations: list[NormalizedObservation] = []
        counts = [0, 0, 0, 0]
        self._prepare(self._configuration.dvol_source_id)
        for candle in fetch.candles:
            request_url = _request_url_for_event(
                candle.start,
                tuple(reversed(fetch.request_urls)),
            )
            candidate = dvol_to_raw_record(
                candle,
                configuration=self._configuration,
                received_at=fetch.retrieved_at,
                request_url=request_url,
            )
            stored, created = self._save_raw(candidate)
            counts[0 if created else 1] += 1
            records.append(stored)
            for observation in dvol_to_observations(
                candle,
                stored,
                configuration=self._configuration,
                normalized_at=normalized_at,
            ):
                persisted, observation_created = self._save_observation(observation)
                counts[2 if observation_created else 3] += 1
                observations.append(persisted)
        self._verify_traceability(records, observations, self._configuration.dvol_source_id)
        receipt, receipt_created = self._save_receipt(
            dataset="dvol_daily",
            source_id=self._configuration.dvol_source_id,
            start=fetch.requested_start,
            end=fetch.requested_end,
            completed_at=fetch.retrieved_at,
            request_count=len(fetch.request_urls),
            row_count=len(fetch.candles),
        )
        return DeribitImportSummary(
            asset_id=self._configuration.asset_id,
            source_id=self._configuration.dvol_source_id,
            dataset="dvol_daily",
            requested_start=fetch.requested_start,
            requested_end=fetch.requested_end,
            retrieved_at=fetch.retrieved_at,
            request_count=len(fetch.request_urls),
            rows_received=len(fetch.candles),
            raw_records_created=counts[0],
            raw_records_reused=counts[1],
            observations_created=counts[2],
            observations_reused=counts[3],
            receipt_id=receipt.receipt_id,
            receipt_created=receipt_created,
            traceability_verified=True,
        )

    def capture_summary(self) -> DeribitImportSummary:
        """Persist one current summary without manufacturing historical coverage."""
        fetch = self._client.fetch_perpetual_summary(self._configuration.instrument_name)
        normalized_at = max(_utc(self._clock()), fetch.retrieved_at)
        self._prepare(self._configuration.summary_source_id)
        candidate = summary_to_raw_record(
            fetch.summary,
            configuration=self._configuration,
            received_at=fetch.retrieved_at,
            request_url=fetch.request_url,
        )
        stored, raw_created = self._save_raw(candidate)
        observations: list[NormalizedObservation] = []
        observations_created = 0
        observations_reused = 0
        for observation in summary_to_observations(
            fetch.summary,
            stored,
            configuration=self._configuration,
            normalized_at=normalized_at,
        ):
            persisted, created = self._save_observation(observation)
            observations_created += int(created)
            observations_reused += int(not created)
            observations.append(persisted)
        self._verify_traceability(
            [stored],
            observations,
            self._configuration.summary_source_id,
        )
        return DeribitImportSummary(
            asset_id=self._configuration.asset_id,
            source_id=self._configuration.summary_source_id,
            dataset="perpetual_summary",
            retrieved_at=fetch.retrieved_at,
            request_count=1,
            rows_received=1,
            raw_records_created=int(raw_created),
            raw_records_reused=int(not raw_created),
            observations_created=observations_created,
            observations_reused=observations_reused,
            traceability_verified=True,
        )

    def _prepare(self, source_id: str) -> None:
        self._storage.require_open()
        self._storage.assets.upsert(create_deribit_asset(self._configuration))
        sources = {
            source.source_id: source for source in create_deribit_sources(self._configuration)
        }
        self._storage.sources.upsert(sources[source_id])

    def _save_raw(self, candidate: RawRecord) -> tuple[RawRecord, bool]:
        try:
            return self._storage.raw_records.get(candidate.record_id), False
        except RecordNotFoundError:
            self._storage.raw_records.save(candidate)
            return self._storage.raw_records.get(candidate.record_id), True

    def _save_observation(
        self,
        candidate: NormalizedObservation,
    ) -> tuple[NormalizedObservation, bool]:
        try:
            return self._storage.observations.get(candidate.observation_id), False
        except RecordNotFoundError:
            self._storage.observations.save(candidate)
            return self._storage.observations.get(candidate.observation_id), True

    def _save_receipt(
        self,
        *,
        dataset: DeribitDataset,
        source_id: str,
        start: datetime,
        end: datetime,
        completed_at: datetime,
        request_count: int,
        row_count: int,
    ) -> tuple[DeribitFetchReceipt, bool]:
        identifier = receipt_id(
            asset_id=self._configuration.asset_id,
            source_id=source_id,
            dataset=dataset,
            requested_start=start,
            requested_end=end,
        )
        try:
            stored = self._storage.raw_records.get(identifier)
            receipt = raw_record_to_receipt(stored)
            if receipt is None:
                raise StorageError("Deribit receipt identity points to a non-receipt raw record")
            return receipt, False
        except RecordNotFoundError:
            receipt = DeribitFetchReceipt(
                receipt_id=identifier,
                asset_id=self._configuration.asset_id,
                source_id=source_id,
                dataset=dataset,
                requested_start=start,
                requested_end=end,
                completed_at=completed_at,
                request_count=request_count,
                row_count=row_count,
            )
            self._storage.raw_records.save(receipt_to_raw_record(receipt))
            stored = self._storage.raw_records.get(identifier)
            verified = raw_record_to_receipt(stored)
            if verified != receipt:
                raise StorageError("Deribit receipt round-trip verification failed") from None
            return receipt, True

    def _verify_traceability(
        self,
        records: list[RawRecord],
        observations: list[NormalizedObservation],
        source_id: str,
    ) -> None:
        by_id = {record.record_id: record for record in records}
        if len(by_id) != len(records):
            raise StorageError("duplicate Deribit raw identities appeared in one stage")
        for record in records:
            if (
                record.asset_id != self._configuration.asset_id
                or record.source.source_id != source_id
                or record.available_at != record.received_at
                or self._storage.raw_records.get(record.record_id) != record
            ):
                raise StorageError("Deribit raw round-trip traceability failed")
        for observation in observations:
            raw = by_id.get(observation.raw_record_id)
            if (
                raw is None
                or observation.asset_id != self._configuration.asset_id
                or observation.source != raw.source
                or observation.available_at != raw.received_at
                or self._storage.observations.get(observation.observation_id) != observation
            ):
                raise StorageError("Deribit observation round-trip traceability failed")


def list_deribit_receipts(
    storage: LocalStorage,
    *,
    source_id: str,
    dataset: DeribitDataset,
) -> tuple[DeribitFetchReceipt, ...]:
    """Return only verified receipts for one source/dataset in deterministic order."""
    receipts: list[DeribitFetchReceipt] = []
    for record in storage.raw_records.list(source_id=source_id):
        receipt = raw_record_to_receipt(record)
        if receipt is not None and receipt.dataset == dataset:
            receipts.append(receipt)
    return tuple(
        sorted(
            receipts,
            key=lambda item: (item.requested_start, item.requested_end, item.receipt_id),
        )
    )


def _request_url_for_event(event_time: datetime, request_urls: tuple[str, ...]) -> str:
    event_ms = _milliseconds(event_time)
    for request_url in request_urls:
        query = parse_qs(urlsplit(request_url).query)
        try:
            start_ms = int(query["start_timestamp"][0])
            end_ms = int(query["end_timestamp"][0])
        except (KeyError, IndexError, ValueError) as error:
            raise StorageError("recorded Deribit URL has invalid range parameters") from error
        if start_ms <= event_ms < end_ms:
            return request_url
    raise StorageError("no recorded Deribit request URL covers a returned event")


def _milliseconds(value: datetime) -> int:
    utc_value = _utc(value)
    epoch = datetime(1970, 1, 1, tzinfo=UTC)
    delta = utc_value - epoch
    return delta.days * 86_400_000 + delta.seconds * 1_000 + delta.microseconds // 1_000


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime must include timezone information")
    return value.astimezone(UTC)


__all__ = [
    "DeribitDataset",
    "DeribitEvidencePipeline",
    "DeribitFetchReceipt",
    "DeribitImportSummary",
    "RECEIPT_SCHEMA_VERSION",
    "list_deribit_receipts",
    "raw_record_to_receipt",
    "receipt_id",
    "receipt_to_raw_record",
]
