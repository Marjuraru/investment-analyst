"""Auditable local import pipeline for Coinbase BTC-USD daily candles."""

from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

from investment_analyst.core.models import NormalizedObservation, RawRecord
from investment_analyst.providers.asset_config import CoinbaseAssetConfiguration
from investment_analyst.providers.crypto.coinbase_exchange import (
    DAILY_GRANULARITY_SECONDS,
    CoinbaseCandle,
    CoinbaseExchangeClient,
)
from investment_analyst.providers.crypto.coinbase_normalizer import (
    ASSET_ID,
    PRODUCT_ID,
    SOURCE_ID,
    candle_to_observations,
    candle_to_raw_record,
    create_coinbase_asset,
    create_coinbase_source,
)
from investment_analyst.storage.errors import RecordNotFoundError, StorageError
from investment_analyst.storage.local import LocalStorage


@dataclass(frozen=True, slots=True)
class CoinbaseImportSummary:
    """Compact auditable outcome of one historical import run."""

    asset_id: str
    source_id: str
    requested_start: datetime
    requested_end: datetime
    retrieved_at: datetime
    request_count: int
    candles_received: int
    raw_records_created: int
    raw_records_reused: int
    observations_created: int
    observations_reused: int
    missing_intervals: tuple[datetime, ...]
    earliest_candle: datetime | None
    latest_candle: datetime | None
    traceability_verified: bool

    def to_json_dict(self) -> dict[str, object]:
        """Return an explicit JSON-compatible representation."""
        return {
            "asset_id": self.asset_id,
            "source_id": self.source_id,
            "requested_start": self.requested_start.isoformat(),
            "requested_end": self.requested_end.isoformat(),
            "retrieved_at": self.retrieved_at.isoformat(),
            "request_count": self.request_count,
            "candles_received": self.candles_received,
            "raw_records_created": self.raw_records_created,
            "raw_records_reused": self.raw_records_reused,
            "observations_created": self.observations_created,
            "observations_reused": self.observations_reused,
            "missing_intervals": [value.isoformat() for value in self.missing_intervals],
            "earliest_candle": (
                self.earliest_candle.isoformat() if self.earliest_candle is not None else None
            ),
            "latest_candle": (
                self.latest_candle.isoformat() if self.latest_candle is not None else None
            ),
            "traceability_verified": self.traceability_verified,
        }


class CoinbaseHistoricalPipeline:
    """Import real public Coinbase candles without calculating analytics."""

    def __init__(
        self,
        storage: LocalStorage,
        client: CoinbaseExchangeClient,
        *,
        configuration: CoinbaseAssetConfiguration | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._storage = storage
        self._client = client
        self._configuration = configuration or CoinbaseAssetConfiguration(
            asset_id=ASSET_ID,
            product_id=PRODUCT_ID,
            source_id=SOURCE_ID,
            granularity_seconds=DAILY_GRANULARITY_SECONDS,
        )
        if self._configuration != CoinbaseAssetConfiguration(
            asset_id=ASSET_ID,
            product_id=PRODUCT_ID,
            source_id=SOURCE_ID,
            granularity_seconds=DAILY_GRANULARITY_SECONDS,
        ):
            raise StorageError(
                "Coinbase configuration does not match the current persisted identity"
            )
        self._clock = clock

    def run(self, start: datetime, end: datetime) -> CoinbaseImportSummary:
        """Fetch BTC-USD, persist raw and normalized data, and verify traceability."""
        self._storage.require_open()
        fetch = self._client.fetch_daily_candles(self._configuration.product_id, start, end)
        if fetch.product_id != self._configuration.product_id:
            raise StorageError("Coinbase fetch result does not match the resolved configuration")
        self._storage.assets.upsert(create_coinbase_asset())
        self._storage.sources.upsert(create_coinbase_source())

        raw_created = 0
        raw_reused = 0
        observations_created = 0
        observations_reused = 0
        stored_records: list[RawRecord] = []
        stored_observations: list[NormalizedObservation] = []
        normalized_at = max(_as_utc(self._clock()), fetch.retrieved_at)

        for candle in fetch.candles:
            request_url = _request_url_for_candle(candle, fetch.request_urls)
            candidate = candle_to_raw_record(
                candle,
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

            candidates = candle_to_observations(
                candle,
                stored_record,
                normalized_at=normalized_at,
            )
            for observation in candidates:
                try:
                    stored_observation = self._storage.observations.get(observation.observation_id)
                    observations_reused += 1
                except RecordNotFoundError:
                    self._storage.observations.save(observation)
                    stored_observation = self._storage.observations.get(observation.observation_id)
                    observations_created += 1
                stored_observations.append(stored_observation)

        self._verify_traceability(stored_records, stored_observations)
        missing = _missing_daily_intervals(
            fetch.requested_start,
            fetch.requested_end,
            fetch.candles,
        )
        candle_times = tuple(candle.start for candle in fetch.candles)
        return CoinbaseImportSummary(
            asset_id=self._configuration.asset_id,
            source_id=self._configuration.source_id,
            requested_start=fetch.requested_start,
            requested_end=fetch.requested_end,
            retrieved_at=fetch.retrieved_at,
            request_count=len(fetch.request_urls),
            candles_received=len(fetch.candles),
            raw_records_created=raw_created,
            raw_records_reused=raw_reused,
            observations_created=observations_created,
            observations_reused=observations_reused,
            missing_intervals=missing,
            earliest_candle=min(candle_times) if candle_times else None,
            latest_candle=max(candle_times) if candle_times else None,
            traceability_verified=True,
        )

    def _verify_traceability(
        self,
        records: list[RawRecord],
        observations: list[NormalizedObservation],
    ) -> None:
        if self._storage.assets.get(ASSET_ID) != create_coinbase_asset():
            raise StorageError("Coinbase asset round-trip verification failed")
        if self._storage.sources.get(SOURCE_ID) != create_coinbase_source():
            raise StorageError("Coinbase source round-trip verification failed")
        record_by_id = {record.record_id: record for record in records}
        if len(record_by_id) != len(records):
            raise StorageError("duplicate raw record identifiers appeared in one import")
        counts = Counter(observation.raw_record_id for observation in observations)

        for record in records:
            if self._storage.raw_records.get(record.record_id) != record:
                raise StorageError("raw record round-trip verification failed")
            if (
                record.asset_id != self._configuration.asset_id
                or record.source.source_id != self._configuration.source_id
            ):
                raise StorageError("raw record asset or source does not match BTC-USD")
            if not isinstance(record.payload, dict):
                raise StorageError("raw record payload is not an object")
            if record.payload.get("product_id") != self._configuration.product_id:
                raise StorageError("raw record payload does not represent BTC-USD")
            if counts[record.record_id] != 5:
                raise StorageError("each raw Coinbase candle must have five observations")
            _require_utc(record.event_time, "raw event_time")
            _require_utc(record.available_at, "raw available_at")
            _require_utc(record.received_at, "raw received_at")
            _require_utc(record.source.retrieved_at, "source retrieved_at")

        for observation in observations:
            record = record_by_id.get(observation.raw_record_id)
            if record is None:
                raise StorageError("observation references a missing raw record")
            if self._storage.observations.get(observation.observation_id) != observation:
                raise StorageError("observation round-trip verification failed")
            if (
                observation.asset_id != self._configuration.asset_id
                or record.asset_id != observation.asset_id
            ):
                raise StorageError("observation asset does not match its raw record")
            if observation.source != record.source:
                raise StorageError("observation source does not match its raw record")
            if observation.period_start is None or observation.period_end is None:
                raise StorageError("Coinbase observation must have an explicit daily period")
            if observation.period_end != observation.period_start + timedelta(days=1):
                raise StorageError("Coinbase observation period is not exactly one day")
            if record.available_at > observation.normalized_at:
                raise StorageError("observation uses information after normalized_at")
            for value, label in (
                (observation.observed_at, "observed_at"),
                (observation.period_start, "period_start"),
                (observation.period_end, "period_end"),
                (observation.available_at, "available_at"),
                (observation.normalized_at, "normalized_at"),
            ):
                _require_utc(value, f"observation {label}")


def _request_url_for_candle(candle: CoinbaseCandle, request_urls: tuple[str, ...]) -> str:
    for request_url in request_urls:
        query = parse_qs(urlsplit(request_url).query)
        try:
            start = _as_utc(datetime.fromisoformat(query["start"][0]))
            end = _as_utc(datetime.fromisoformat(query["end"][0]))
        except (KeyError, IndexError, ValueError) as error:
            raise StorageError(
                "recorded Coinbase request URL has invalid range parameters"
            ) from error
        if start <= candle.start < end:
            return request_url
    raise StorageError("no recorded Coinbase request URL covers a returned candle")


def _missing_daily_intervals(
    start: datetime,
    end: datetime,
    candles: tuple[CoinbaseCandle, ...],
) -> tuple[datetime, ...]:
    present = {candle.start for candle in candles}
    missing: list[datetime] = []
    cursor = start
    while cursor < end:
        if cursor not in present:
            missing.append(cursor)
        cursor += timedelta(days=1)
    return tuple(missing)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise StorageError("pipeline clock must return a timezone-aware datetime")
    return value.astimezone(UTC)


def _require_utc(value: datetime | None, label: str) -> None:
    if value is None or value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise StorageError(f"{label} must be UTC")
