"""Import AAPL IEX daily bars into local auditable storage."""

import json
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from typing import Literal
from uuid import UUID, uuid5

from pydantic import ConfigDict, Field, field_validator, model_validator

from investment_analyst.core.models import (
    DataQuality,
    NormalizedObservation,
    RawRecord,
    SourceReference,
)
from investment_analyst.core.models.base import ContractModel, NonEmptyStr, UTCDateTime
from investment_analyst.providers.asset_config import AlpacaAssetConfiguration
from investment_analyst.providers.market.alpaca_normalizer import (
    ASSET_ID,
    SOURCE_ID,
    SYMBOL,
    bar_to_observations,
    bar_to_raw_record,
    create_alpaca_asset,
    create_alpaca_source,
)
from investment_analyst.providers.market.alpaca_stock import (
    ADJUSTMENT,
    FEED,
    AlpacaStockClient,
)
from investment_analyst.storage import LocalStorage
from investment_analyst.storage.errors import RecordNotFoundError, StorageError

ALPACA_FETCH_RECEIPT_SCHEMA = "alpaca-market-fetch-receipt-v1"
ALPACA_FETCH_RECEIPT_VERSION = 1
ALPACA_INTERVAL_SEMANTICS = "half-open-utc"
_RECEIPT_NAMESPACE = UUID("674ca499-5610-5cb5-89d2-6efed7b966cf")


class AlpacaMarketFetchReceipt(ContractModel):
    """Auditable evidence that one Alpaca interval completed successfully."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    asset_id: NonEmptyStr
    source_id: NonEmptyStr
    feed: NonEmptyStr
    adjustment: NonEmptyStr
    requested_start: UTCDateTime
    requested_end: UTCDateTime
    interval_semantics: Literal["half-open-utc"] = ALPACA_INTERVAL_SEMANTICS
    retrieved_at: UTCDateTime
    bar_count: int = Field(ge=0)
    page_count: int = Field(ge=1)
    schema_version: int = Field(default=ALPACA_FETCH_RECEIPT_VERSION, ge=1)
    traceability_verified: bool

    @field_validator("bar_count", "page_count", "schema_version", mode="before")
    @classmethod
    def reject_boolean_counts(cls, value: object) -> object:
        """Reject booleans accepted by Python's integer hierarchy."""
        if isinstance(value, bool):
            raise ValueError("receipt counts and version must be integers")
        return value

    @field_validator("traceability_verified", mode="before")
    @classmethod
    def require_traceability_boolean(cls, value: object) -> object:
        """Reject truthy integers and strings as traceability flags."""
        if not isinstance(value, bool):
            raise ValueError("traceability_verified must be a bool")
        return value

    @model_validator(mode="after")
    def validate_receipt(self) -> "AlpacaMarketFetchReceipt":
        """Validate the completed half-open interval and fixed schema version."""
        if self.requested_start >= self.requested_end:
            raise ValueError("requested_start must be earlier than requested_end")
        if self.schema_version != ALPACA_FETCH_RECEIPT_VERSION:
            raise ValueError("unsupported Alpaca fetch receipt schema version")
        if not self.traceability_verified:
            raise ValueError("traceability_verified must be true")
        return self


def alpaca_fetch_receipt_id(
    *,
    asset_id: str,
    source_id: str,
    feed: str,
    adjustment: str,
    requested_start: datetime,
    requested_end: datetime,
    schema_version: int = ALPACA_FETCH_RECEIPT_VERSION,
) -> UUID:
    """Return the stable identity of one completed provider interval."""
    identity = json.dumps(
        {
            "adjustment": adjustment,
            "asset_id": asset_id,
            "feed": feed,
            "requested_end": requested_end.astimezone(UTC).isoformat(),
            "requested_start": requested_start.astimezone(UTC).isoformat(),
            "schema_version": schema_version,
            "source_id": source_id,
        },
        separators=(",", ":"),
        sort_keys=True,
    )
    return uuid5(_RECEIPT_NAMESPACE, identity)


def _receipt_record_key(receipt: AlpacaMarketFetchReceipt) -> str:
    return "|".join(
        (
            "coverage",
            receipt.requested_start.isoformat(),
            receipt.requested_end.isoformat(),
            receipt.feed,
            receipt.adjustment,
            str(receipt.schema_version),
        )
    )


def alpaca_fetch_receipt_to_raw_record(receipt: AlpacaMarketFetchReceipt) -> RawRecord:
    """Encode one typed coverage receipt through the existing RawRecord abstraction."""
    record_id = alpaca_fetch_receipt_id(
        asset_id=receipt.asset_id,
        source_id=receipt.source_id,
        feed=receipt.feed,
        adjustment=receipt.adjustment,
        requested_start=receipt.requested_start,
        requested_end=receipt.requested_end,
        schema_version=receipt.schema_version,
    )
    return RawRecord(
        record_id=record_id,
        asset_id=receipt.asset_id,
        source=SourceReference(
            source_id=receipt.source_id,
            record_key=_receipt_record_key(receipt),
            retrieved_at=receipt.retrieved_at,
        ),
        event_time=None,
        available_at=receipt.retrieved_at,
        received_at=receipt.retrieved_at,
        payload=receipt.model_dump(mode="json"),
        schema_version=ALPACA_FETCH_RECEIPT_SCHEMA,
    )


def alpaca_fetch_receipt_from_raw_record(
    record: RawRecord,
) -> AlpacaMarketFetchReceipt | None:
    """Decode and verify a receipt RawRecord, or ignore another raw schema."""
    if record.schema_version != ALPACA_FETCH_RECEIPT_SCHEMA:
        return None
    receipt = AlpacaMarketFetchReceipt.model_validate(record.payload)
    expected_id = alpaca_fetch_receipt_id(
        asset_id=receipt.asset_id,
        source_id=receipt.source_id,
        feed=receipt.feed,
        adjustment=receipt.adjustment,
        requested_start=receipt.requested_start,
        requested_end=receipt.requested_end,
        schema_version=receipt.schema_version,
    )
    if record.record_id != expected_id:
        raise StorageError("Alpaca coverage receipt identity is inconsistent")
    if record.asset_id != receipt.asset_id or record.source.source_id != receipt.source_id:
        raise StorageError("Alpaca coverage receipt scope is inconsistent")
    if record.source.record_key != _receipt_record_key(receipt):
        raise StorageError("Alpaca coverage receipt record key is inconsistent")
    if record.event_time is not None:
        raise StorageError("Alpaca coverage receipt must not invent an event time")
    if (
        record.available_at != receipt.retrieved_at
        or record.received_at != receipt.retrieved_at
        or record.source.retrieved_at != receipt.retrieved_at
    ):
        raise StorageError("Alpaca coverage receipt timestamps are inconsistent")
    return receipt


def alpaca_receipt_covers_calendar_days(receipt: AlpacaMarketFetchReceipt) -> bool:
    """Return whether receipt bounds align to complete UTC calendar days."""
    return (
        receipt.requested_start.time() == time.min
        and receipt.requested_end.time() == time.min
        and receipt.requested_start.utcoffset() == timedelta(0)
        and receipt.requested_end.utcoffset() == timedelta(0)
    )


@dataclass(frozen=True, slots=True)
class AlpacaImportSummary:
    """Compact JSON-ready outcome of one AAPL historical import."""

    asset_id: str
    source_id: str
    requested_start: datetime
    requested_end: datetime
    retrieved_at: datetime
    feed: str
    adjustment: str
    request_count: int
    bars_received: int
    raw_records_created: int
    raw_records_reused: int
    observations_created: int
    observations_reused: int
    earliest_bar: datetime | None
    latest_bar: datetime | None
    traceability_verified: bool
    coverage_receipts_created: int = 0
    coverage_receipts_reused: int = 0
    empty_intervals_completed: int = 0

    def to_json_dict(self) -> dict[str, object]:
        """Return an explicit JSON-compatible representation."""
        return {
            "asset_id": self.asset_id,
            "source_id": self.source_id,
            "requested_start": self.requested_start.isoformat(),
            "requested_end": self.requested_end.isoformat(),
            "retrieved_at": self.retrieved_at.isoformat(),
            "feed": self.feed,
            "adjustment": self.adjustment,
            "request_count": self.request_count,
            "bars_received": self.bars_received,
            "raw_records_created": self.raw_records_created,
            "raw_records_reused": self.raw_records_reused,
            "observations_created": self.observations_created,
            "observations_reused": self.observations_reused,
            "coverage_receipts_created": self.coverage_receipts_created,
            "coverage_receipts_reused": self.coverage_receipts_reused,
            "empty_intervals_completed": self.empty_intervals_completed,
            "earliest_bar": self.earliest_bar.isoformat() if self.earliest_bar else None,
            "latest_bar": self.latest_bar.isoformat() if self.latest_bar else None,
            "traceability_verified": self.traceability_verified,
        }


class AlpacaHistoricalPipeline:
    """Persist real AAPL IEX bars without calculating analytics."""

    def __init__(
        self,
        storage: LocalStorage,
        client: AlpacaStockClient,
        *,
        configuration: AlpacaAssetConfiguration | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._storage = storage
        self._client = client
        self._configuration = configuration or AlpacaAssetConfiguration(
            asset_id=ASSET_ID,
            symbol=SYMBOL,
            feed=FEED,
            adjustment=ADJUSTMENT,
            source_id=SOURCE_ID,
        )
        if self._configuration != AlpacaAssetConfiguration(
            asset_id=ASSET_ID,
            symbol=SYMBOL,
            feed=FEED,
            adjustment=ADJUSTMENT,
            source_id=SOURCE_ID,
        ):
            raise StorageError("Alpaca configuration does not match the current persisted identity")
        self._clock = clock

    def run(self, start: datetime, end: datetime) -> AlpacaImportSummary:
        """Fetch AAPL, persist raw and normalized data, and verify traceability."""
        self._storage.require_open()
        metric_ids_before = {result.result_id for result in self._storage.metric_results.list()}
        diagnostic_ids_before = {
            result.diagnostic_id for result in self._storage.diagnostics.list()
        }
        fetch = self._client.fetch_daily_bars(self._configuration.symbol, start, end)
        if (
            fetch.symbol != self._configuration.symbol
            or fetch.feed != self._configuration.feed
            or fetch.adjustment != self._configuration.adjustment
        ):
            raise StorageError("Alpaca fetch result does not match the resolved configuration")
        self._storage.assets.upsert(create_alpaca_asset())
        self._storage.sources.upsert(create_alpaca_source())

        raw_created = 0
        raw_reused = 0
        observations_created = 0
        observations_reused = 0
        stored_records: list[RawRecord] = []
        stored_observations: list[NormalizedObservation] = []
        normalized_at = max(_as_utc(self._clock()), fetch.retrieved_at)
        request_url = fetch.request_urls[0] if fetch.request_urls else ""

        for bar in fetch.bars:
            candidate = bar_to_raw_record(
                bar,
                retrieved_at=fetch.retrieved_at,
                request_url=request_url,
            )
            try:
                stored_record = self._storage.raw_records.get(candidate.record_id)
                raw_reused += 1
            except RecordNotFoundError:
                self._storage.raw_records.save(candidate)
                stored_record = self._storage.raw_records.get(candidate.record_id)
                raw_created += 1
            stored_records.append(stored_record)

            for observation in bar_to_observations(
                bar,
                stored_record,
                normalized_at=normalized_at,
            ):
                try:
                    stored_observation = self._storage.observations.get(observation.observation_id)
                    observations_reused += 1
                except RecordNotFoundError:
                    self._storage.observations.save(observation)
                    stored_observation = self._storage.observations.get(observation.observation_id)
                    observations_created += 1
                stored_observations.append(stored_observation)

        self._verify_traceability(stored_records, stored_observations)
        if {
            result.result_id for result in self._storage.metric_results.list()
        } != metric_ids_before:
            raise StorageError("Alpaca import unexpectedly changed metric results")
        if {
            result.diagnostic_id for result in self._storage.diagnostics.list()
        } != diagnostic_ids_before:
            raise StorageError("Alpaca import unexpectedly changed diagnostic results")

        receipt_created, receipt_reused = self._persist_fetch_receipt(fetch)
        bar_times = tuple(bar.timestamp for bar in fetch.bars)
        return AlpacaImportSummary(
            asset_id=self._configuration.asset_id,
            source_id=self._configuration.source_id,
            requested_start=fetch.requested_start,
            requested_end=fetch.requested_end,
            retrieved_at=fetch.retrieved_at,
            feed=fetch.feed,
            adjustment=fetch.adjustment,
            request_count=len(fetch.request_urls),
            bars_received=len(fetch.bars),
            raw_records_created=raw_created,
            raw_records_reused=raw_reused,
            observations_created=observations_created,
            observations_reused=observations_reused,
            earliest_bar=min(bar_times) if bar_times else None,
            latest_bar=max(bar_times) if bar_times else None,
            traceability_verified=True,
            coverage_receipts_created=receipt_created,
            coverage_receipts_reused=receipt_reused,
            empty_intervals_completed=int(not fetch.bars),
        )

    def _persist_fetch_receipt(self, fetch) -> tuple[int, int]:
        receipt = AlpacaMarketFetchReceipt(
            asset_id=self._configuration.asset_id,
            source_id=self._configuration.source_id,
            feed=fetch.feed,
            adjustment=fetch.adjustment,
            requested_start=fetch.requested_start,
            requested_end=fetch.requested_end,
            retrieved_at=fetch.retrieved_at,
            bar_count=len(fetch.bars),
            page_count=len(fetch.request_urls),
            traceability_verified=True,
        )
        candidate = alpaca_fetch_receipt_to_raw_record(receipt)
        try:
            stored = self._storage.raw_records.get(candidate.record_id)
            created, reused = 0, 1
        except RecordNotFoundError:
            self._storage.raw_records.save(candidate)
            stored = self._storage.raw_records.get(candidate.record_id)
            created, reused = 1, 0
        decoded = alpaca_fetch_receipt_from_raw_record(stored)
        if decoded is None:
            raise StorageError("persisted Alpaca coverage receipt could not be decoded")
        if (
            decoded.asset_id != receipt.asset_id
            or decoded.source_id != receipt.source_id
            or decoded.feed != receipt.feed
            or decoded.adjustment != receipt.adjustment
            or decoded.requested_start != receipt.requested_start
            or decoded.requested_end != receipt.requested_end
        ):
            raise StorageError("persisted Alpaca coverage receipt does not match the request")
        return created, reused

    def _verify_traceability(
        self,
        records: list[RawRecord],
        observations: list[NormalizedObservation],
    ) -> None:
        if self._storage.assets.get(ASSET_ID) != create_alpaca_asset():
            raise StorageError("Alpaca asset round-trip verification failed")
        if self._storage.sources.get(SOURCE_ID) != create_alpaca_source():
            raise StorageError("Alpaca source round-trip verification failed")
        record_by_id = {record.record_id: record for record in records}
        if len(record_by_id) != len(records):
            raise StorageError("duplicate raw record identifiers appeared in one Alpaca import")
        counts = Counter(observation.raw_record_id for observation in observations)

        for record in records:
            if self._storage.raw_records.get(record.record_id) != record:
                raise StorageError("Alpaca raw record round-trip verification failed")
            if (
                record.asset_id != self._configuration.asset_id
                or record.source.source_id != self._configuration.source_id
            ):
                raise StorageError("Alpaca raw record asset or source is inconsistent")
            if (
                not isinstance(record.payload, dict)
                or record.payload.get("symbol") != self._configuration.symbol
            ):
                raise StorageError("Alpaca raw record payload does not represent AAPL")
            if counts[record.record_id] != 7:
                raise StorageError("each raw Alpaca bar must have seven observations")
            _require_utc(record.event_time, "raw event_time")
            _require_utc(record.available_at, "raw available_at")
            _require_utc(record.received_at, "raw received_at")
            _require_utc(record.source.retrieved_at, "source retrieved_at")

        for observation in observations:
            record = record_by_id.get(observation.raw_record_id)
            if record is None:
                raise StorageError("Alpaca observation references a missing raw record")
            if self._storage.observations.get(observation.observation_id) != observation:
                raise StorageError("Alpaca observation round-trip verification failed")
            if (
                observation.asset_id != self._configuration.asset_id
                or record.asset_id != observation.asset_id
            ):
                raise StorageError("Alpaca observation asset does not match its raw record")
            if observation.source != record.source:
                raise StorageError("Alpaca observation source does not match its raw record")
            if observation.quality is not DataQuality.PARTIAL:
                raise StorageError("Alpaca IEX observations must have PARTIAL quality")
            if observation.period_start is not None or observation.period_end is not None:
                raise StorageError("Alpaca observations must not invent reporting periods")
            if observation.available_at > observation.normalized_at:
                raise StorageError("Alpaca observation uses information after normalized_at")
            _require_utc(observation.observed_at, "observation observed_at")
            _require_utc(observation.available_at, "observation available_at")
            _require_utc(observation.normalized_at, "observation normalized_at")


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise StorageError("pipeline clock must return a timezone-aware datetime")
    return value.astimezone(UTC)


def _require_utc(value: datetime | None, label: str) -> None:
    if value is None or value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise StorageError(f"{label} must be UTC")
