"""Point-in-time reconstruction of provider-independent stored market bars."""

from collections import defaultdict
from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING
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
from investment_analyst.core.models import NormalizedObservation, RawRecord
from investment_analyst.storage import LocalStorage
from investment_analyst.storage.errors import RecordNotFoundError, StorageError

if TYPE_CHECKING:
    from investment_analyst.analytics.market.history_v2 import DailyMarketBarProjection
    from investment_analyst.storage.raw_v2 import (
        MarketObservationGroupProjection,
        RawV2Staging,
    )


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


@dataclass(frozen=True, slots=True)
class LatestRevisionState[Candidate]:
    """Bounded accumulator for the latest PIT revision of one timestamp."""

    candidate: Candidate | None = None
    available_at: datetime | None = None
    latest_ties: int = 0
    candidates_seen: int = 0


def consider_revision[Candidate](
    state: LatestRevisionState[Candidate],
    candidate: Candidate,
    *,
    available_at: datetime,
) -> LatestRevisionState[Candidate]:
    """Update a one-timestamp revision accumulator without storing its history."""
    if state.candidate is None or state.available_at is None:
        return LatestRevisionState(candidate, available_at, 1, state.candidates_seen + 1)
    if available_at > state.available_at:
        return LatestRevisionState(candidate, available_at, 1, state.candidates_seen + 1)
    if available_at == state.available_at:
        return LatestRevisionState(
            state.candidate,
            state.available_at,
            state.latest_ties + 1,
            state.candidates_seen + 1,
        )
    return LatestRevisionState(
        state.candidate,
        state.available_at,
        state.latest_ties,
        state.candidates_seen + 1,
    )


def require_unambiguous_revision[Candidate](
    state: LatestRevisionState[Candidate],
    *,
    timestamp: datetime,
) -> Candidate:
    """Return the unique latest PIT revision or raise the canonical ambiguity error."""
    if state.candidate is None or state.available_at is None:
        raise ValueError("revision accumulator must contain a candidate")
    if state.latest_ties != 1:
        raise AmbiguousRevisionError(
            f"timestamp {timestamp} has multiple revisions with available_at "
            f"{state.available_at.isoformat()}"
        )
    return state.candidate


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
            source_id=query.source_id,
            frequency=schema.frequency,
            observed_from=query.start,
            observed_before=query.end,
            available_to=query.known_at,
        )
        scoped = self._scope_observations(observations, schema, query)
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

        raw_records = self._get_raw_records(grouped)
        candidates = [
            self._build_candidate(
                raw_record_id,
                group,
                raw_records[raw_record_id],
                schema,
                query,
            )
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
        schema: MarketBarSchema,
        query: HistoricalBarQuery,
    ) -> list[NormalizedObservation]:
        return [
            observation
            for observation in observations
            if observation.source.source_id == query.source_id
            and observation.frequency is schema.frequency
            and observation.observed_at is not None
            and query.start <= observation.observed_at < query.end
            and observation.available_at <= query.known_at
        ]

    def _build_candidate(
        self,
        raw_record_id: UUID,
        observations: list[NormalizedObservation],
        raw_record: RawRecord,
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
            frequency=schema.frequency,
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
            if observation.frequency is not schema.frequency:
                raise TraceabilityError(f"raw record {raw_record_id} contains the wrong frequency")
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

    def _get_raw_records(
        self,
        raw_record_ids: Collection[UUID],
    ) -> dict[UUID, RawRecord]:
        try:
            return self._storage.raw_records.get_many(raw_record_ids)
        except (RecordNotFoundError, StorageError) as error:
            raise TraceabilityError(f"raw records cannot be verified: {error}") from error

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
        return select_point_in_time_revisions(
            candidates,
            timestamp=lambda item: item.timestamp,
            available_at=lambda item: item.available_at,
        )


def select_point_in_time_revisions[RevisionCandidate](
    candidates: Sequence[RevisionCandidate],
    *,
    timestamp: Callable[[RevisionCandidate], datetime],
    available_at: Callable[[RevisionCandidate], datetime],
) -> tuple[list[RevisionCandidate], int]:
    """Select one latest available candidate per timestamp using the v1 rule."""
    by_timestamp: dict[datetime, LatestRevisionState[RevisionCandidate]] = {}
    for candidate in candidates:
        at = timestamp(candidate)
        by_timestamp[at] = consider_revision(
            by_timestamp.get(at, LatestRevisionState()),
            candidate,
            available_at=available_at(candidate),
        )

    selected: list[RevisionCandidate] = []
    for at, state in by_timestamp.items():
        selected.append(require_unambiguous_revision(state, timestamp=at))
    return selected, len(candidates) - len(selected)


@dataclass(frozen=True, slots=True)
class MarketHistorySummary:
    """PIT counts and only the latest fully verified v2 projection."""

    query: HistoricalBarQuery
    bar_count: int
    candidate_versions: int
    discarded_revisions: int
    earliest_timestamp: datetime | None
    latest_timestamp: datetime | None
    latest_projection: "DailyMarketBarProjection | None"


@dataclass(frozen=True, slots=True)
class _V2SummaryCandidate:
    group: "MarketObservationGroupProjection"
    timestamp: datetime
    available_at: datetime


def summarize_market_history_v2(
    staging: "RawV2Staging",
    query: HistoricalBarQuery,
) -> MarketHistorySummary:
    """Select every PIT revision but build only the latest complete projection.

    The query keeps the canonical timestamp ordering and ambiguity rule. Discarded
    revisions are counted without materializing their values; the latest selected
    group is still fully built and can be materialized by the caller to verify its
    raw record, observations, units, quality and values.
    """
    from investment_analyst.analytics.market.history_v2 import HistoricalMarketDataV2Service

    service = HistoricalMarketDataV2Service(staging)
    schema = service._schema(query.source_id)  # noqa: SLF001 - canonical schema resolution
    after_observed_at: datetime | None = None
    after_raw_record_id: UUID | None = None
    active_timestamp: datetime | None = None
    active_revision: LatestRevisionState[_V2SummaryCandidate] = LatestRevisionState()
    bar_count = 0
    candidate_versions = 0
    discarded_revisions = 0
    earliest_timestamp: datetime | None = None
    latest_candidate: _V2SummaryCandidate | None = None

    def finalize() -> None:
        nonlocal bar_count, candidate_versions, discarded_revisions
        nonlocal earliest_timestamp, latest_candidate
        if active_timestamp is None:
            return
        selected = require_unambiguous_revision(active_revision, timestamp=active_timestamp)
        bar_count += 1
        candidate_versions += active_revision.candidates_seen
        discarded_revisions += active_revision.candidates_seen - 1
        if earliest_timestamp is None:
            earliest_timestamp = active_timestamp
        latest_candidate = selected

    while True:
        page = staging.list_market_observation_page(
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
            candidate = _summary_candidate(group, query)
            if active_timestamp is not None and candidate.timestamp != active_timestamp:
                finalize()
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
    finalize()
    latest_projection = (
        HistoricalMarketDataV2Service._build_candidate(  # noqa: SLF001
            latest_candidate.group,
            schema,
            query,
        )
        if latest_candidate is not None
        else None
    )
    return MarketHistorySummary(
        query=query,
        bar_count=bar_count,
        candidate_versions=candidate_versions,
        discarded_revisions=discarded_revisions,
        earliest_timestamp=earliest_timestamp,
        latest_timestamp=latest_candidate.timestamp if latest_candidate is not None else None,
        latest_projection=latest_projection,
    )


def _summary_candidate(
    group: "MarketObservationGroupProjection",
    query: HistoricalBarQuery,
) -> _V2SummaryCandidate:
    if group.raw_asset_id != query.asset_id or group.raw_source_id != query.source_id:
        raise TraceabilityError("raw record asset or source does not match the query")
    observations = group.observations
    if not observations:
        raise TraceabilityError("daily observation group is empty")
    timestamps = {item.observed_at for item in observations}
    availability = {item.available_at for item in observations}
    if len(timestamps) != 1 or len(availability) != 1:
        raise TraceabilityError("daily observation group has inconsistent timestamps")
    timestamp = next(iter(timestamps))
    available_at = next(iter(availability))
    if timestamp is None or timestamp != group.raw_event_time:
        raise TraceabilityError("raw record event_time does not match observations")
    if group.raw_available_at != available_at:
        raise TraceabilityError("raw record available_at does not match observations")
    if available_at > query.known_at:
        raise TraceabilityError("raw record was not available at known_at")
    return _V2SummaryCandidate(
        group=group,
        timestamp=timestamp,
        available_at=available_at,
    )
