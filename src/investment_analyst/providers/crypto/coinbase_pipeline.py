"""Auditable local import pipelines for Coinbase BTC-USD candles."""

from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol
from urllib.parse import parse_qs, urlsplit

from investment_analyst.core.models import (
    Asset,
    DataFrequency,
    NormalizedObservation,
    RawRecord,
    SourceDefinition,
)
from investment_analyst.providers.asset_config import CoinbaseAssetConfiguration
from investment_analyst.providers.crypto.coinbase_exchange import (
    DAILY_GRANULARITY_SECONDS,
    MINUTE_GRANULARITY_SECONDS,
    CoinbaseCandle,
    CoinbaseExchangeClient,
)
from investment_analyst.providers.crypto.coinbase_intraday_normalizer import (
    SOURCE_ID as INTRADAY_SOURCE_ID,
)
from investment_analyst.providers.crypto.coinbase_intraday_normalizer import (
    candle_to_intraday_observations,
    candle_to_intraday_raw_record,
    create_coinbase_intraday_source,
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


class _RawRecordFactory(Protocol):
    def __call__(
        self,
        candle: CoinbaseCandle,
        *,
        retrieved_at: datetime,
        request_url: str,
    ) -> RawRecord: ...


class _ObservationFactory(Protocol):
    def __call__(
        self,
        candle: CoinbaseCandle,
        raw_record: RawRecord,
        *,
        normalized_at: datetime,
    ) -> tuple[NormalizedObservation, ...]: ...


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


class _CoinbaseCandlePipeline:
    """Shared append-only persistence for one explicit Coinbase candle contract."""

    def __init__(
        self,
        storage: LocalStorage,
        client: CoinbaseExchangeClient,
        *,
        configuration: CoinbaseAssetConfiguration,
        expected_configuration: CoinbaseAssetConfiguration,
        frequency: DataFrequency,
        period: timedelta,
        asset_factory: Callable[[], Asset],
        source_factory: Callable[[], SourceDefinition],
        raw_record_factory: _RawRecordFactory,
        observation_factory: _ObservationFactory,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._storage = storage
        self._client = client
        self._configuration = configuration
        if self._configuration != expected_configuration:
            raise StorageError(
                "Coinbase configuration does not match the current persisted identity"
            )
        self._frequency = frequency
        self._period = period
        self._asset_factory = asset_factory
        self._source_factory = source_factory
        self._raw_record_factory = raw_record_factory
        self._observation_factory = observation_factory
        self._clock = clock

    def run(self, start: datetime, end: datetime) -> CoinbaseImportSummary:
        """Fetch BTC-USD, persist raw and normalized data, and verify traceability."""
        self._storage.require_open()
        fetch = self._client.fetch_candles(
            self._configuration.product_id,
            start,
            end,
            granularity_seconds=self._configuration.granularity_seconds,
        )
        if (
            fetch.product_id != self._configuration.product_id
            or fetch.granularity_seconds != self._configuration.granularity_seconds
        ):
            raise StorageError("Coinbase fetch result does not match the resolved configuration")
        self._storage.assets.upsert(self._asset_factory())
        self._storage.sources.upsert(self._source_factory())

        raw_created = 0
        raw_reused = 0
        observations_created = 0
        observations_reused = 0
        stored_records: list[RawRecord] = []
        stored_observations: list[NormalizedObservation] = []
        normalized_at = max(_as_utc(self._clock()), fetch.retrieved_at)

        for candle in fetch.candles:
            request_url = _request_url_for_candle(candle, fetch.request_urls)
            candidate = self._raw_record_factory(
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

            candidates = self._observation_factory(
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
        missing = _missing_intervals(
            fetch.requested_start,
            fetch.requested_end,
            fetch.candles,
            self._period,
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
        if self._storage.assets.get(self._configuration.asset_id) != self._asset_factory():
            raise StorageError("Coinbase asset round-trip verification failed")
        if self._storage.sources.get(self._configuration.source_id) != self._source_factory():
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
            if record.payload.get("granularity_seconds") != self._configuration.granularity_seconds:
                raise StorageError("raw record payload has the wrong candle granularity")
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
                raise StorageError("Coinbase observation must have an explicit candle period")
            if observation.period_end != observation.period_start + self._period:
                raise StorageError("Coinbase observation period has the wrong duration")
            if observation.frequency is not self._frequency:
                raise StorageError("Coinbase observation has the wrong frequency")
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


class CoinbaseHistoricalPipeline(_CoinbaseCandlePipeline):
    """Import the existing Coinbase daily dataset without changing its identity."""

    def __init__(
        self,
        storage: LocalStorage,
        client: CoinbaseExchangeClient,
        *,
        configuration: CoinbaseAssetConfiguration | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        expected = CoinbaseAssetConfiguration(
            asset_id=ASSET_ID,
            product_id=PRODUCT_ID,
            source_id=SOURCE_ID,
            granularity_seconds=DAILY_GRANULARITY_SECONDS,
            base_unit="BTC",
            quote_unit="USD",
        )
        super().__init__(
            storage,
            client,
            configuration=configuration or expected,
            expected_configuration=expected,
            frequency=DataFrequency.DAY_1,
            period=timedelta(days=1),
            asset_factory=create_coinbase_asset,
            source_factory=create_coinbase_source,
            raw_record_factory=candle_to_raw_record,
            observation_factory=candle_to_observations,
            clock=clock,
        )


class CoinbaseIntradayPipeline(_CoinbaseCandlePipeline):
    """Import the separate Coinbase one-minute dataset without daily analytics."""

    def __init__(
        self,
        storage: LocalStorage,
        client: CoinbaseExchangeClient,
        *,
        configuration: CoinbaseAssetConfiguration | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        expected = CoinbaseAssetConfiguration(
            asset_id=ASSET_ID,
            product_id=PRODUCT_ID,
            source_id=INTRADAY_SOURCE_ID,
            granularity_seconds=MINUTE_GRANULARITY_SECONDS,
            base_unit="BTC",
            quote_unit="USD",
        )
        super().__init__(
            storage,
            client,
            configuration=configuration or expected,
            expected_configuration=expected,
            frequency=DataFrequency.MINUTE_1,
            period=timedelta(minutes=1),
            asset_factory=create_coinbase_asset,
            source_factory=create_coinbase_intraday_source,
            raw_record_factory=candle_to_intraday_raw_record,
            observation_factory=candle_to_intraday_observations,
            clock=clock,
        )


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


def _missing_intervals(
    start: datetime,
    end: datetime,
    candles: tuple[CoinbaseCandle, ...],
    period: timedelta,
) -> tuple[datetime, ...]:
    present = {candle.start for candle in candles}
    missing: list[datetime] = []
    cursor = start
    while cursor < end:
        if cursor not in present:
            missing.append(cursor)
        cursor += period
    return tuple(missing)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise StorageError("pipeline clock must return a timezone-aware datetime")
    return value.astimezone(UTC)


def _require_utc(value: datetime | None, label: str) -> None:
    if value is None or value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise StorageError(f"{label} must be UTC")
