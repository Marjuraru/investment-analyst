"""Paged point-in-time market history reconstruction over raw v2 projections."""

from __future__ import annotations

from collections.abc import Collection, Iterator
from datetime import datetime
from decimal import Decimal
from uuid import UUID

from pydantic import ConfigDict

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
from investment_analyst.analytics.market.daily_evidence import (
    DailyEvidenceFieldGroup,
    DailyEvidencePrefix,
    make_daily_evidence_prefix,
    observation_rows_digest,
)
from investment_analyst.analytics.market.history_service import (
    ConflictingObservationError,
    IncompleteMarketBarError,
    LatestRevisionState,
    TraceabilityError,
    consider_revision,
    require_unambiguous_revision,
)
from investment_analyst.core.models import DataFrequency, DataQuality
from investment_analyst.core.models.base import ContractModel
from investment_analyst.storage.raw_v2 import (
    MarketObservationGroupProjection,
    MarketObservationProjection,
    RawV2Staging,
)
from investment_analyst.storage.serialization import canonical_json_bytes, sha256_hex


class DailyMarketBarProjection(ContractModel):
    """Complete verified bar assembled from typed SQL projections."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    asset_id: str
    source_id: str
    raw_record_id: UUID
    frequency: DataFrequency
    timestamp: datetime
    available_at: datetime
    values: dict[str, Decimal]
    observation_ids: dict[str, UUID]
    quality: DataQuality
    raw_checksum_sha256: str
    observation_rows: dict[str, tuple[str | None, ...]]

    def to_market_bar(self) -> MarketBar:
        """Hydrate a strict analytical bar only when calculation needs it."""
        return MarketBar(
            asset_id=self.asset_id,
            source_id=self.source_id,
            raw_record_id=self.raw_record_id,
            frequency=self.frequency,
            timestamp=self.timestamp,
            available_at=self.available_at,
            open=self.values["open"],
            high=self.values["high"],
            low=self.values["low"],
            close=self.values["close"],
            volume=self.values["volume"],
            trade_count=self.values.get("trade_count"),
            vwap=self.values.get("vwap"),
            quality=self.quality,
            observation_ids=self.observation_ids,
        )

    def prefix(
        self,
        field_group: DailyEvidenceFieldGroup,
        parent: DailyEvidencePrefix | None,
    ) -> DailyEvidencePrefix:
        """Create one chained close or high/low/close evidence node."""
        fields = (
            ("close",) if field_group is DailyEvidenceFieldGroup.CLOSE else ("high", "low", "close")
        )
        observation_ids = tuple(self.observation_ids[field] for field in fields)
        rows = tuple(self.observation_rows[field] for field in fields)
        return make_daily_evidence_prefix(
            asset_id=self.asset_id,
            source_id=self.source_id,
            field_group=field_group,
            timestamp=self.timestamp,
            observation_ids=observation_ids,
            observation_digest=observation_rows_digest(rows),
            current_available_at=self.available_at,
            quality=self.quality,
            parent=parent,
        )


class MarketHistoryProjection(ContractModel):
    """PIT selected daily projections and their source coverage."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    query: HistoricalBarQuery
    bars: tuple[DailyMarketBarProjection, ...]
    candidate_versions: int
    discarded_revisions: int
    traceability_verified: bool

    def series(self, bars: Collection[MarketBar]) -> MarketBarSeries:
        """Return the standard complete series for a materialized selected subset."""
        ordered = tuple(sorted(bars, key=lambda item: item.timestamp))
        coverage = MarketBarCoverage(
            candidate_versions=self.candidate_versions,
            selected_versions=len(ordered),
            discarded_revisions=self.discarded_revisions,
            bar_count=len(ordered),
            earliest_timestamp=ordered[0].timestamp if ordered else None,
            latest_timestamp=ordered[-1].timestamp if ordered else None,
        )
        return MarketBarSeries(
            query=self.query,
            bars=ordered,
            coverage=coverage,
            traceability_verified=self.traceability_verified,
        )


class MarketHistoryPage(ContractModel):
    """One bounded page of selected daily bars and finalized revision counts."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    bars: tuple[DailyMarketBarProjection, ...]
    candidate_versions: int
    discarded_revisions: int


class HistoricalMarketDataV2Service:
    """Reconstruct PIT revisions without hydrating unchanged raw model objects."""

    def __init__(self, staging: RawV2Staging) -> None:
        if not staging.is_open:
            raise TraceabilityError("raw v2 staging must be open")
        self._staging = staging

    def query(self, query: HistoricalBarQuery) -> MarketHistoryProjection:
        """Collect paged output for compatibility with bounded callers."""
        pages = tuple(self.iter_pages(query))
        bars = tuple(item for page in pages for item in page.bars)
        candidate_versions = sum(page.candidate_versions for page in pages)
        discarded_revisions = sum(page.discarded_revisions for page in pages)
        return MarketHistoryProjection(
            query=query,
            bars=bars,
            candidate_versions=candidate_versions,
            discarded_revisions=discarded_revisions,
            traceability_verified=True,
        )

    def iter_pages(self, query: HistoricalBarQuery) -> Iterator[MarketHistoryPage]:
        """Select visible revisions with O(1) revision state and <=256 bars per page."""
        schema = self._schema(query.source_id)
        after_observed_at: datetime | None = None
        after_raw_record_id: UUID | None = None
        selected: list[DailyMarketBarProjection] = []
        page_candidates = 0
        page_discarded = 0
        active_timestamp: datetime | None = None
        active_revision: LatestRevisionState[DailyMarketBarProjection] = LatestRevisionState()
        while True:
            page = self._staging.list_market_observation_page(
                asset_id=query.asset_id,
                source_id=query.source_id,
                start=query.start,
                end=query.end,
                known_at=query.known_at,
                limit=256,
                after_observed_at=after_observed_at,
                after_raw_record_id=after_raw_record_id,
            )
            for group in page.groups:
                candidate = self._build_candidate(group, schema, query)
                if active_timestamp is not None and candidate.timestamp != active_timestamp:
                    selected.append(
                        require_unambiguous_revision(
                            active_revision,
                            timestamp=active_timestamp,
                        )
                    )
                    page_candidates += active_revision.candidates_seen
                    page_discarded += active_revision.candidates_seen - 1
                    if len(selected) == 256:
                        yield MarketHistoryPage(
                            bars=tuple(selected),
                            candidate_versions=page_candidates,
                            discarded_revisions=page_discarded,
                        )
                        selected.clear()
                        page_candidates = 0
                        page_discarded = 0
                if candidate.timestamp != active_timestamp:
                    active_timestamp = candidate.timestamp
                    active_revision = LatestRevisionState()
                active_revision = consider_revision(
                    active_revision,
                    candidate,
                    available_at=candidate.available_at,
                )
            if len(page.groups) < 256:
                break
            after_observed_at = page.next_observed_at
            after_raw_record_id = page.next_raw_record_id
            if after_observed_at is None or after_raw_record_id is None:
                raise TraceabilityError("market observation page did not return its next cursor")
        if active_timestamp is not None:
            selected.append(
                require_unambiguous_revision(active_revision, timestamp=active_timestamp)
            )
            page_candidates += active_revision.candidates_seen
            page_discarded += active_revision.candidates_seen - 1
        if selected or page_candidates:
            yield MarketHistoryPage(
                bars=tuple(selected),
                candidate_versions=page_candidates,
                discarded_revisions=page_discarded,
            )

    def materialize(
        self,
        projection: DailyMarketBarProjection,
        query: HistoricalBarQuery,
    ) -> MarketBar:
        """Hydrate and re-verify one changed bar and its complete raw record."""
        raw_records = self._staging.get_many([projection.raw_record_id])
        raw_record = raw_records.get(projection.raw_record_id)
        if raw_record is None:
            raise TraceabilityError("changed market bar raw record is missing")
        if sha256_hex(canonical_json_bytes(raw_record)) != projection.raw_checksum_sha256:
            raise TraceabilityError("changed market bar raw checksum diverged from its index")
        observations = self._staging.get_observations(tuple(projection.observation_ids.values()))
        if set(observations) != set(projection.observation_ids.values()):
            raise TraceabilityError("changed market bar observation is missing")
        selected = HistoricalMarketDataV2Service._history_builder()
        schema = self._schema(query.source_id)
        try:
            return selected._build_candidate(  # noqa: SLF001 - reuse canonical v1 validation
                projection.raw_record_id,
                list(observations.values()),
                raw_record,
                schema,
                query,
            )
        except Exception as error:
            if isinstance(error, (TraceabilityError, ConflictingObservationError)):
                raise
            raise TraceabilityError("changed market bar failed canonical hydration") from error

    def materialize_many(
        self,
        projections: Collection[DailyMarketBarProjection],
        query: HistoricalBarQuery,
    ) -> dict[UUID, MarketBar]:
        """Hydrate a bounded set of changed bars and verify every raw blob once."""
        typed = tuple(projections)
        if len(typed) > 256:
            raise ValueError("market bar materialization batch must not exceed 256 bars")
        if not typed:
            return {}
        raw_records = self._staging.get_many([item.raw_record_id for item in typed])
        observation_ids = tuple(
            dict.fromkeys(
                identifier for item in typed for identifier in item.observation_ids.values()
            )
        )
        observations = self._staging.get_observations(observation_ids)
        schema = self._schema(query.source_id)
        builder = HistoricalMarketDataV2Service._history_builder()
        output: dict[UUID, MarketBar] = {}
        for projection in typed:
            raw_record = raw_records.get(projection.raw_record_id)
            if raw_record is None:
                raise TraceabilityError("changed market bar raw record is missing")
            if sha256_hex(canonical_json_bytes(raw_record)) != projection.raw_checksum_sha256:
                raise TraceabilityError("changed market bar raw checksum diverged from its index")
            selected_observations = [
                observations[identifier]
                for identifier in projection.observation_ids.values()
                if identifier in observations
            ]
            if len(selected_observations) != len(projection.observation_ids):
                raise TraceabilityError("changed market bar observation is missing")
            try:
                output[projection.raw_record_id] = builder._build_candidate(  # noqa: SLF001
                    projection.raw_record_id,
                    selected_observations,
                    raw_record,
                    schema,
                    query,
                )
            except Exception as error:
                if isinstance(error, (TraceabilityError, ConflictingObservationError)):
                    raise
                raise TraceabilityError("changed market bar failed canonical hydration") from error
        return output

    @staticmethod
    def _history_builder():
        """Return the existing verifier implementation without a v1 storage adapter."""
        from investment_analyst.analytics.market.history_service import HistoricalMarketDataService

        return HistoricalMarketDataService.__new__(HistoricalMarketDataService)

    @staticmethod
    def _schema(source_id: str) -> MarketBarSchema:
        try:
            schema = get_market_bar_schema(source_id)
        except ValueError as error:
            raise TraceabilityError(str(error)) from error
        if schema.frequency is not DataFrequency.DAY_1:
            raise TraceabilityError("incremental market history requires DAY_1 source")
        return schema

    @staticmethod
    def _build_candidate(
        group: MarketObservationGroupProjection,
        schema: MarketBarSchema,
        query: HistoricalBarQuery,
    ) -> DailyMarketBarProjection:
        by_field: dict[str, MarketObservationProjection] = {}
        for observation in group.observations:
            if observation.field_name in by_field:
                raise ConflictingObservationError(
                    f"raw record {group.raw_record_id} has duplicate field "
                    f"{observation.field_name!r}"
                )
            by_field[observation.field_name] = observation
        allowed_fields = set(schema.required_fields) | set(schema.optional_fields)
        unexpected = sorted(set(by_field) - allowed_fields)
        if unexpected:
            raise ConflictingObservationError(
                f"source {query.source_id!r} contains unsupported field {unexpected[0]!r}"
            )
        missing = sorted(set(schema.required_fields) - set(by_field))
        if missing:
            raise IncompleteMarketBarError(
                f"raw record {group.raw_record_id} is missing required observations: "
                f"{', '.join(missing)}"
            )
        timestamps = {item.observed_at for item in by_field.values()}
        availability = {item.available_at for item in by_field.values()}
        if len(timestamps) != 1 or len(availability) != 1:
            raise TraceabilityError("daily observation group has inconsistent timestamps")
        timestamp = next(iter(timestamps))
        available_at = next(iter(availability))
        if timestamp is None or timestamp != group.raw_event_time:
            raise TraceabilityError("raw record event_time does not match observations")
        if group.raw_asset_id != query.asset_id or group.raw_source_id != query.source_id:
            raise TraceabilityError("raw record asset or source does not match the query")
        if group.raw_available_at != available_at:
            raise TraceabilityError("raw record available_at does not match observations")
        if available_at > query.known_at:
            raise TraceabilityError("raw record was not available at known_at")
        for field_name, observation in by_field.items():
            if (
                observation.asset_id != query.asset_id
                or observation.raw_asset_id != query.asset_id
                or observation.source_id != query.source_id
                or observation.raw_source_id != query.source_id
                or observation.frequency is not schema.frequency
            ):
                raise TraceabilityError("observation and raw record scope do not match")
            if observation.unit != schema.units[field_name]:
                raise ConflictingObservationError(
                    f"field {field_name!r} has unit {observation.unit!r}; "
                    f"expected {schema.units[field_name]!r}"
                )
            if observation.quality is not schema.expected_quality:
                raise ConflictingObservationError(
                    f"field {field_name!r} has quality {observation.quality.value!r}; "
                    f"expected {schema.expected_quality.value!r}"
                )
        values = {key: value.value for key, value in by_field.items()}
        if min(values["open"], values["high"], values["low"], values["close"]) <= 0:
            raise ConflictingObservationError("market bar prices must be positive")
        if values["low"] > values["high"]:
            raise ConflictingObservationError("market bar low exceeds high")
        if not values["low"] <= values["open"] <= values["high"]:
            raise ConflictingObservationError("market bar open is outside low/high")
        if not values["low"] <= values["close"] <= values["high"]:
            raise ConflictingObservationError("market bar close is outside low/high")
        if values["volume"] < 0:
            raise ConflictingObservationError("market bar volume must be non-negative")
        identifiers = {key: item.observation_id for key, item in by_field.items()}
        rows = {key: item.canonical_row() for key, item in by_field.items()}
        return DailyMarketBarProjection(
            asset_id=query.asset_id,
            source_id=query.source_id,
            raw_record_id=group.raw_record_id,
            frequency=schema.frequency,
            timestamp=timestamp,
            available_at=available_at,
            values=values,
            observation_ids=identifiers,
            quality=schema.expected_quality,
            raw_checksum_sha256=group.raw_checksum_sha256,
            observation_rows=rows,
        )


__all__ = [
    "DailyMarketBarProjection",
    "HistoricalMarketDataV2Service",
    "MarketHistoryProjection",
]
