"""Bounded read-path tests for the daily pattern reconstruction."""

from __future__ import annotations

from collections.abc import Collection
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from investment_analyst.analytics.analytical_access_models import (
    MetricIndexEntry,
    MetricSeriesQuery,
)
from investment_analyst.analytics.market.incremental_recursive import (
    MACD_ALGORITHM_VERSION,
    RSI_ALGORITHM_VERSION,
)
from investment_analyst.analytics.market.pattern_models import (
    DailyMarketPatternsRequest,
)
from investment_analyst.analytics.market.pattern_service import (
    DailyMarketPatternsService,
)
from investment_analyst.analytics.market.statistics_definitions import (
    MACD_HISTOGRAM_KEY,
    RSI_KEY,
)
from investment_analyst.core.models import (
    DataFrequency,
    DataQuality,
    MetricResult,
    NormalizedObservation,
    SourceReference,
)

_ASSET_ID = "equity:us:scope"
_SOURCE_ID = "alpaca-market-data:iex:scope:daily-bars:adjustment-all"
_START = datetime(2026, 1, 1, tzinfo=UTC)
_KNOWN_AT = datetime(2026, 6, 1, tzinfo=UTC)
_PERIODS = 140


def _uuid8(name: str) -> UUID:
    raw = bytearray(uuid5(NAMESPACE_URL, name).bytes)
    raw[6] = (raw[6] & 0x0F) | 0x80
    raw[8] = (raw[8] & 0x3F) | 0x80
    return UUID(bytes=bytes(raw))


def _close(index: int, *, asset_id: str = _ASSET_ID, source_id: str = _SOURCE_ID):
    as_of = _START + timedelta(days=index)
    return NormalizedObservation(
        observation_id=uuid5(NAMESPACE_URL, f"close:{asset_id}:{source_id}:{index}"),
        raw_record_id=uuid5(NAMESPACE_URL, f"raw-close:{asset_id}:{source_id}:{index}"),
        asset_id=asset_id,
        field_name="close",
        value=Decimal("100") + Decimal(index),
        unit="USD",
        frequency=DataFrequency.DAY_1,
        observed_at=as_of,
        available_at=_KNOWN_AT,
        normalized_at=_KNOWN_AT + timedelta(minutes=1),
        source=SourceReference(
            source_id=source_id,
            record_key=f"close:{index}",
            retrieved_at=_KNOWN_AT,
        ),
        quality=DataQuality.VALID,
        transformation_version="scope-normalizer-v1",
    )


def _metric(
    metric_key: str,
    value: Decimal,
    as_of: datetime,
    *,
    asset_id: str = _ASSET_ID,
    source_id: str = _SOURCE_ID,
    available_at: datetime = _KNOWN_AT,
    computed_at: datetime | None = None,
) -> MetricResult:
    if metric_key == RSI_KEY:
        parameters: dict[str, object] = {
            "window": 14,
            "seed_method": "wilder_first_n_changes",
            "seed_start": _START.isoformat(),
            "price_field": "close",
            "source_id": source_id,
        }
        algorithm_version = RSI_ALGORITHM_VERSION
        unit = "index"
    else:
        parameters = {
            "fast_window": 12,
            "slow_window": 26,
            "signal_window": 9,
            "seed_method": "sma_first_signal_lines",
            "seed_start": _START.isoformat(),
            "source_id": source_id,
        }
        algorithm_version = MACD_ALGORITHM_VERSION
        unit = "USD"
    return MetricResult(
        result_id=_uuid8(f"{asset_id}:{source_id}:{metric_key}:{as_of.isoformat()}:{value}"),
        asset_id=asset_id,
        metric_key=metric_key,
        value=value,
        unit=unit,
        as_of=as_of,
        available_at=available_at,
        computed_at=computed_at or available_at,
        parameters=parameters,
        input_observation_ids=[uuid4()],
        algorithm_version=algorithm_version,
        quality=DataQuality.VALID,
    )


class _Observations:
    def __init__(self, rows: tuple[NormalizedObservation, ...]) -> None:
        self.rows = rows
        self.queries = 0

    def list(self, **kwargs: object) -> list[NormalizedObservation]:
        self.queries += 1
        return list(self.rows)


class _MetricCorpus:
    """Repository-like filtering, stable keyset pagination, and batch hydration."""

    def __init__(self, metrics: tuple[MetricResult, ...]) -> None:
        self.metrics = metrics
        self.page_calls: list[int] = []
        self.cursors: list[tuple[datetime, UUID] | None] = []
        self.hydration_batches: list[tuple[UUID, ...]] = []

    def list_metric_index_page(self, query: MetricSeriesQuery) -> tuple[MetricIndexEntry, ...]:
        self.page_calls.append(query.limit)
        self.cursors.append(
            (query.after.available_at, query.after.result_id) if query.after else None
        )
        entries = [
            entry
            for entry in (self._entry(metric) for metric in self.metrics)
            if entry.asset_id == query.asset_id
            and entry.metric_key in query.metric_keys
            and entry.source_id == query.source_id
            and query.as_of_from <= entry.as_of < query.as_of_before
            and entry.available_at <= query.known_at
        ]
        entries.sort(key=lambda item: (item.available_at, str(item.result_id)))
        if query.after is not None:
            cursor = (query.after.available_at, str(query.after.result_id))
            entries = [
                entry for entry in entries if (entry.available_at, str(entry.result_id)) > cursor
            ]
        return tuple(entries[: query.limit])

    def get_metric_results(self, result_ids: Collection[UUID]) -> dict[UUID, MetricResult]:
        identifiers = tuple(sorted(result_ids, key=str))
        self.hydration_batches.append(identifiers)
        selected = {metric.result_id: metric for metric in self.metrics}
        return {
            identifier: selected[identifier] for identifier in identifiers if identifier in selected
        }

    @staticmethod
    def _entry(metric: MetricResult) -> MetricIndexEntry:
        return MetricIndexEntry(
            result_id=metric.result_id,
            asset_id=metric.asset_id,
            metric_key=metric.metric_key,
            as_of=metric.as_of,
            available_at=metric.available_at,
            source_id=metric.parameters.get("source_id"),
            frequency=metric.parameters.get("frequency"),
            legacy_known_at=None,
        )


def _eligible_metrics() -> tuple[MetricResult, ...]:
    metrics: list[MetricResult] = []
    for index in range(_PERIODS):
        as_of = _START + timedelta(days=index)
        histogram = Decimal("-1") if index % 2 == 0 else Decimal("1")
        rsi = Decimal("25.1234567890123456789012345678901234")
        metrics.append(_metric(MACD_HISTOGRAM_KEY, histogram, as_of))
        metrics.append(_metric(RSI_KEY, rsi, as_of))
    for index in range(0, 10):
        as_of = _START + timedelta(days=index)
        metrics.append(
            _metric(
                MACD_HISTOGRAM_KEY,
                Decimal("2.9999999999999999999999999999999999"),
                as_of,
                available_at=_KNOWN_AT - timedelta(days=1),
                computed_at=_KNOWN_AT - timedelta(days=1),
            )
        )
    return tuple(metrics)


def _foreign_metrics(multiplier: int) -> tuple[MetricResult, ...]:
    foreign: list[MetricResult] = []
    for copy in range(multiplier):
        for index in range(0, 130):
            as_of = _START + timedelta(days=index)
            foreign.append(
                _metric(
                    RSI_KEY,
                    Decimal("45.0000000000000000000000000000000001"),
                    as_of,
                    asset_id=f"equity:us:foreign-{copy}",
                    source_id=f"foreign-source-{copy}",
                )
            )
            foreign.append(
                _metric(
                    MACD_HISTOGRAM_KEY,
                    Decimal("0.0000000000000000000000000000000001"),
                    as_of,
                    asset_id=_ASSET_ID,
                    source_id=f"foreign-source-{copy}",
                )
            )
    return tuple(foreign)


def _request() -> DailyMarketPatternsRequest:
    return DailyMarketPatternsRequest(
        asset_id=_ASSET_ID,
        start_date=date(2026, 1, 1),
        end_date=date(2026, 5, 20),
        known_at=_KNOWN_AT,
    )


def _run(foreign_multiplier: int):
    closes = tuple(_close(index) for index in range(_PERIODS))
    observations = _Observations(closes)
    corpus = _MetricCorpus(_eligible_metrics() + _foreign_metrics(foreign_multiplier))
    result = DailyMarketPatternsService(observations, corpus).query(
        _request(),
        asset_id=_ASSET_ID,
        source_id=_SOURCE_ID,
        currency="USD",
    )
    return result, observations, corpus


def test_corpus_above_256_forces_complete_pagination_and_keeps_every_revision() -> None:
    result, observations, corpus = _run(foreign_multiplier=1)

    hydrated = tuple(identifier for batch in corpus.hydration_batches for identifier in batch)
    assert len(corpus.page_calls) > 1
    assert all(limit <= 256 for limit in corpus.page_calls)
    assert all(len(batch) <= 256 for batch in corpus.hydration_batches)
    assert len(hydrated) == len(set(hydrated))
    assert len(hydrated) > 256
    assert observations.queries == 1
    assert len(result.points) == _PERIODS
    assert result.coverage.close_observations_read == _PERIODS
    assert result.coverage.point_count == _PERIODS
    assert result.coverage.rsi_available_points == _PERIODS
    assert result.coverage.macd_available_points == _PERIODS


def test_query_does_not_hydrate_foreign_or_out_of_scope_rows() -> None:
    _, _, corpus = _run(foreign_multiplier=1)
    hydrated = {identifier for batch in corpus.hydration_batches for identifier in batch}
    eligible = {
        metric.result_id
        for metric in corpus.metrics
        if metric.asset_id == _ASSET_ID and metric.parameters.get("source_id") == _SOURCE_ID
    }

    assert hydrated == eligible


def test_one_x_and_ten_x_foreign_corpora_produce_identical_results_and_decimals() -> None:
    baseline, _, baseline_corpus = _run(foreign_multiplier=1)
    amplified, _, amplified_corpus = _run(foreign_multiplier=10)

    assert baseline.result_id == amplified.result_id
    assert [point.close for point in baseline.points] == [point.close for point in amplified.points]
    assert [point.rsi.value for point in baseline.points] == [
        point.rsi.value for point in amplified.points
    ]
    assert [point.macd_state for point in baseline.points] == [
        point.macd_state for point in amplified.points
    ]
    assert [segment.segment_id for segment in baseline.segments] == [
        segment.segment_id for segment in amplified.segments
    ]
    assert [event.event_id for event in baseline.events] == [
        event.event_id for event in amplified.events
    ]
    assert len(baseline_corpus.hydration_batches) == len(amplified_corpus.hydration_batches)
    assert baseline.points[0].rsi.value == Decimal("25.1234567890123456789012345678901234")


def test_read_path_is_bounded_without_per_period_queries() -> None:
    _, observations, corpus = _run(foreign_multiplier=1)

    assert observations.queries == 1
    assert len(corpus.hydration_batches) <= len(corpus.page_calls)
    assert len(corpus.hydration_batches) < _PERIODS
    assert all(cursor is None or cursor[0] <= _KNOWN_AT for cursor in corpus.cursors)
