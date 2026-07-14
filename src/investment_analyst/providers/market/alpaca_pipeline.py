"""Import AAPL IEX daily bars into local auditable storage."""

from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from investment_analyst.core.models import DataQuality, NormalizedObservation, RawRecord
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
        )

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
