"""Point-in-time reconstruction of provider-independent stored market bars."""

from collections import defaultdict
from datetime import datetime
from uuid import UUID

from investment_analyst.analytics.market.bar_models import (
    HistoricalBarQuery,
    MarketBar,
    MarketBarCoverage,
    MarketBarSeries,
)
from investment_analyst.analytics.market.bar_schemas import (
    MarketBarSchema,
    get_market_bar_schema,
)
from investment_analyst.core.models import DataFrequency, NormalizedObservation, RawRecord
from investment_analyst.storage import LocalStorage
from investment_analyst.storage.errors import RecordNotFoundError, StorageError


class MarketHistoryError(Exception):
    """Base error for unified historical market-bar reconstruction."""


class UnsupportedMarketSourceError(MarketHistoryError):
    """Raised when no explicit bar schema exists for a source."""


class IncompleteMarketBarError(MarketHistoryError):
    """Raised when one raw-record version lacks required observations."""


class ConflictingObservationError(MarketHistoryError):
    """Raised for duplicate or unsupported observations within one bar version."""


class AmbiguousRevisionError(MarketHistoryError):
    """Raised when two revisions have the same point-in-time availability."""


class TraceabilityError(MarketHistoryError):
    """Raised when observations, raw records, assets, sources, or timestamps disagree."""


class HistoricalMarketDataService:
    """Reconstruct complete daily bars from stored observations and raw records."""

    def __init__(self, storage: LocalStorage) -> None:
        storage.require_open()
        self._storage = storage

    def query(self, query: HistoricalBarQuery) -> MarketBarSeries:
        """Return the latest known complete revision for every timestamp in the query."""
        schema = self._schema(query.source_id)
        observations = self._storage.observations.list(
            asset_id=query.asset_id,
            available_to=query.known_at,
        )
        scoped = self._scope_observations(observations, query)
        allowed_fields = set(schema.required_fields) | set(schema.optional_fields)
        unexpected = sorted(
            observation.field_name
            for observation in scoped
            if observation.field_name not in allowed_fields
        )
        if unexpected:
            raise ConflictingObservationError(
                f"source {query.source_id!r} contains unsupported field {unexpected[0]!r}"
            )

        grouped: dict[UUID, list[NormalizedObservation]] = defaultdict(list)
        for observation in scoped:
            grouped[observation.raw_record_id].append(observation)

        candidates = [
            self._build_candidate(raw_record_id, group, schema, query)
            for raw_record_id, group in sorted(grouped.items(), key=lambda item: str(item[0]))
        ]
        selected, discarded = self._select_revisions(candidates)
        bars = tuple(sorted(selected, key=lambda bar: bar.timestamp))
        coverage = MarketBarCoverage(
            candidate_versions=len(candidates),
            selected_versions=len(bars),
            discarded_revisions=discarded,
            bar_count=len(bars),
            earliest_timestamp=bars[0].timestamp if bars else None,
            latest_timestamp=bars[-1].timestamp if bars else None,
        )
        return MarketBarSeries(
            query=query,
            bars=bars,
            coverage=coverage,
            traceability_verified=True,
        )

    @staticmethod
    def _schema(source_id: str) -> MarketBarSchema:
        try:
            return get_market_bar_schema(source_id)
        except ValueError as error:
            raise UnsupportedMarketSourceError(str(error)) from error

    @staticmethod
    def _scope_observations(
        observations: list[NormalizedObservation],
        query: HistoricalBarQuery,
    ) -> list[NormalizedObservation]:
        return [
            observation
            for observation in observations
            if observation.source.source_id == query.source_id
            and observation.frequency is DataFrequency.DAY_1
            and observation.observed_at is not None
            and query.start <= observation.observed_at < query.end
            and observation.available_at <= query.known_at
        ]

    def _build_candidate(
        self,
        raw_record_id: UUID,
        observations: list[NormalizedObservation],
        schema: MarketBarSchema,
        query: HistoricalBarQuery,
    ) -> MarketBar:
        by_field: dict[str, NormalizedObservation] = {}
        for observation in observations:
            if observation.field_name in by_field:
                raise ConflictingObservationError(
                    f"raw record {raw_record_id} has duplicate field {observation.field_name!r}"
                )
            by_field[observation.field_name] = observation

        missing = sorted(set(schema.required_fields) - set(by_field))
        if missing:
            raise IncompleteMarketBarError(
                f"raw record {raw_record_id} is missing required fields: {', '.join(missing)}"
            )
        self._verify_observations(raw_record_id, by_field, schema, query)
        raw_record = self._get_raw_record(raw_record_id)
        first = by_field[schema.required_fields[0]]
        self._verify_raw_record(raw_record, first, query)

        values = {field_name: observation.value for field_name, observation in by_field.items()}
        observation_ids = {
            field_name: observation.observation_id for field_name, observation in by_field.items()
        }
        return MarketBar(
            asset_id=query.asset_id,
            source_id=query.source_id,
            raw_record_id=raw_record_id,
            frequency=DataFrequency.DAY_1,
            timestamp=first.observed_at,
            available_at=first.available_at,
            open=values["open"],
            high=values["high"],
            low=values["low"],
            close=values["close"],
            volume=values["volume"],
            trade_count=values.get("trade_count"),
            vwap=values.get("vwap"),
            quality=schema.expected_quality,
            observation_ids=observation_ids,
        )

    @staticmethod
    def _verify_observations(
        raw_record_id: UUID,
        observations: dict[str, NormalizedObservation],
        schema: MarketBarSchema,
        query: HistoricalBarQuery,
    ) -> None:
        timestamps = {observation.observed_at for observation in observations.values()}
        available_times = {observation.available_at for observation in observations.values()}
        if len(timestamps) != 1 or None in timestamps:
            raise TraceabilityError(
                f"raw record {raw_record_id} observations do not share one observed_at"
            )
        if len(available_times) != 1:
            raise TraceabilityError(
                f"raw record {raw_record_id} observations do not share one available_at"
            )
        for field_name, observation in observations.items():
            if observation.asset_id != query.asset_id:
                raise TraceabilityError(f"raw record {raw_record_id} mixes asset identifiers")
            if observation.source.source_id != query.source_id:
                raise TraceabilityError(f"raw record {raw_record_id} mixes source identifiers")
            if observation.frequency is not DataFrequency.DAY_1:
                raise TraceabilityError(f"raw record {raw_record_id} contains non-daily data")
            if observation.unit != schema.units[field_name]:
                raise ConflictingObservationError(
                    f"field {field_name!r} for raw record {raw_record_id} has unit "
                    f"{observation.unit!r}; expected {schema.units[field_name]!r}"
                )
            if observation.quality is not schema.expected_quality:
                raise ConflictingObservationError(
                    f"field {field_name!r} for raw record {raw_record_id} has quality "
                    f"{observation.quality.value!r}; expected {schema.expected_quality.value!r}"
                )

    def _get_raw_record(self, raw_record_id: UUID) -> RawRecord:
        try:
            return self._storage.raw_records.get(raw_record_id)
        except (RecordNotFoundError, StorageError) as error:
            raise TraceabilityError(f"raw record {raw_record_id} cannot be verified") from error

    @staticmethod
    def _verify_raw_record(
        raw_record: RawRecord,
        observation: NormalizedObservation,
        query: HistoricalBarQuery,
    ) -> None:
        if raw_record.asset_id != query.asset_id:
            raise TraceabilityError("raw record asset_id does not match the query")
        if raw_record.source.source_id != query.source_id:
            raise TraceabilityError("raw record source_id does not match the query")
        if raw_record.event_time != observation.observed_at:
            raise TraceabilityError("raw record event_time does not match observed_at")
        if raw_record.available_at != observation.available_at:
            raise TraceabilityError("raw record available_at does not match observations")
        if raw_record.available_at > query.known_at:
            raise TraceabilityError("raw record was not available at known_at")

    @staticmethod
    def _select_revisions(candidates: list[MarketBar]) -> tuple[list[MarketBar], int]:
        by_timestamp: dict[datetime, list[MarketBar]] = defaultdict(list)
        for candidate in candidates:
            by_timestamp[candidate.timestamp].append(candidate)

        selected: list[MarketBar] = []
        for timestamp, versions in by_timestamp.items():
            latest_available = max(version.available_at for version in versions)
            latest = [version for version in versions if version.available_at == latest_available]
            if len(latest) != 1:
                raise AmbiguousRevisionError(
                    f"timestamp {timestamp} has multiple revisions with available_at "
                    f"{latest_available.isoformat()}"
                )
            selected.append(latest[0])
        return selected, len(candidates) - len(selected)
