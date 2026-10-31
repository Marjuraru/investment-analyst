"""Read-only service tests for daily MACD states and authorized crossings."""

from __future__ import annotations

from collections.abc import Collection
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import pytest

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
    DailyMarketPatternType,
    MacdMomentumState,
)
from investment_analyst.analytics.market.pattern_service import (
    AmbiguousDailyCloseRevisionError,
    AmbiguousDailyPatternEvidenceError,
    DailyMarketPatternsError,
    DailyMarketPatternsService,
    DuplicateDailyCloseDateError,
    UnsupportedDailyPatternsCurrencyError,
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

_ASSET_ID = "equity:us:test"
_SOURCE_ID = "alpaca-market-data:iex:test:daily-bars:adjustment-all"
_KNOWN_AT = datetime(2026, 2, 1, tzinfo=UTC)
_START = datetime(2026, 1, 1, tzinfo=UTC)


def _uuid8(name: str) -> UUID:
    raw = bytearray(uuid5(NAMESPACE_URL, name).bytes)
    raw[6] = (raw[6] & 0x0F) | 0x80
    raw[8] = (raw[8] & 0x3F) | 0x80
    return UUID(bytes=bytes(raw))


def _request(
    *,
    asset_id: str = _ASSET_ID,
    start: date = date(2026, 1, 1),
    end: date = date(2026, 1, 31),
    known_at: datetime = _KNOWN_AT,
) -> DailyMarketPatternsRequest:
    return DailyMarketPatternsRequest(
        asset_id=asset_id,
        start_date=start,
        end_date=end,
        known_at=known_at,
    )


def _close(
    as_of: datetime,
    value: str,
    *,
    available_at: datetime = _KNOWN_AT,
    asset_id: str = _ASSET_ID,
    source_id: str = _SOURCE_ID,
    unit: str = "USD",
    observation_id: UUID | None = None,
) -> NormalizedObservation:
    return NormalizedObservation(
        observation_id=observation_id or uuid4(),
        raw_record_id=uuid4(),
        asset_id=asset_id,
        field_name="close",
        value=Decimal(value),
        unit=unit,
        frequency=DataFrequency.DAY_1,
        observed_at=as_of,
        available_at=available_at,
        normalized_at=available_at + timedelta(minutes=1),
        source=SourceReference(
            source_id=source_id,
            record_key=f"{as_of.isoformat()}:{value}",
            retrieved_at=available_at,
        ),
        quality=DataQuality.VALID,
        transformation_version="test-normalizer-v1",
    )


def _metric(
    metric_key: str,
    value: str,
    as_of: datetime,
    *,
    available_at: datetime = _KNOWN_AT,
    computed_at: datetime | None = None,
    result_id: UUID | None = None,
    asset_id: str = _ASSET_ID,
    source_id: str = _SOURCE_ID,
    algorithm_version: str | None = None,
    unit: str | None = None,
    quality: DataQuality = DataQuality.VALID,
    parameter_overrides: dict[str, object] | None = None,
) -> MetricResult:
    if metric_key == RSI_KEY:
        parameters: dict[str, object] = {
            "window": 14,
            "seed_method": "wilder_first_n_changes",
            "seed_start": _START.isoformat(),
            "price_field": "close",
            "source_id": source_id,
        }
        algorithm_version = algorithm_version or RSI_ALGORITHM_VERSION
        unit = unit or "index"
    else:
        parameters = {
            "fast_window": 12,
            "slow_window": 26,
            "signal_window": 9,
            "seed_method": "sma_first_signal_lines",
            "seed_start": _START.isoformat(),
            "source_id": source_id,
        }
        algorithm_version = algorithm_version or MACD_ALGORITHM_VERSION
        unit = unit or "USD"
    if parameter_overrides:
        parameters.update(parameter_overrides)
    return MetricResult(
        result_id=result_id
        or _uuid8(f"{asset_id}:{source_id}:{metric_key}:{as_of.isoformat()}:{value}"),
        asset_id=asset_id,
        metric_key=metric_key,
        value=Decimal(value),
        unit=unit,
        as_of=as_of,
        available_at=available_at,
        computed_at=computed_at or available_at,
        parameters=parameters,
        input_observation_ids=[uuid4()],
        algorithm_version=algorithm_version,
        quality=quality,
    )


class _Observations:
    def __init__(self, rows: tuple[NormalizedObservation, ...]) -> None:
        self.rows = rows
        self.queries: list[dict[str, object]] = []
        self.writes = 0

    def list(self, **kwargs: object) -> list[NormalizedObservation]:
        self.queries.append(kwargs)
        return list(self.rows)

    def save(self, observation: NormalizedObservation) -> NormalizedObservation:
        self.writes += 1
        return observation


class _MetricStore:
    def __init__(self, metrics: tuple[MetricResult, ...]) -> None:
        self.metrics = metrics
        self.page_calls: list[MetricSeriesQuery] = []
        self.hydration_calls: list[tuple[UUID, ...]] = []

    def list_metric_index_page(self, query: MetricSeriesQuery) -> tuple[MetricIndexEntry, ...]:
        self.page_calls.append(query)
        rows = [
            entry
            for entry in (self._entry(metric) for metric in self.metrics)
            if entry.asset_id == query.asset_id
            and entry.metric_key in query.metric_keys
            and entry.source_id == query.source_id
            and entry.as_of >= query.as_of_from
            and entry.as_of < query.as_of_before
            and entry.available_at <= query.known_at
        ]
        rows.sort(key=lambda item: (item.available_at, str(item.result_id)))
        if query.after is not None:
            cursor = (query.after.available_at, str(query.after.result_id))
            rows = [item for item in rows if (item.available_at, str(item.result_id)) > cursor]
        return tuple(rows[: query.limit])

    def get_metric_results(self, result_ids: Collection[UUID]) -> dict[UUID, MetricResult]:
        identifiers = tuple(sorted(result_ids, key=str))
        self.hydration_calls.append(identifiers)
        selected = {metric.result_id: metric for metric in self.metrics}
        return {
            identifier: selected[identifier] for identifier in identifiers if identifier in selected
        }

    @staticmethod
    def _entry(metric: MetricResult) -> MetricIndexEntry:
        legacy = metric.parameters.get("known_at")
        return MetricIndexEntry(
            result_id=metric.result_id,
            asset_id=metric.asset_id,
            metric_key=metric.metric_key,
            as_of=metric.as_of,
            available_at=metric.available_at,
            source_id=metric.parameters.get("source_id"),
            frequency=metric.parameters.get("frequency"),
            legacy_known_at=legacy if isinstance(legacy, str) else None,
        )


def _periods(
    closes: tuple[NormalizedObservation, ...],
    metrics: tuple[MetricResult, ...],
) -> tuple[_Observations, _MetricStore]:
    return _Observations(closes), _MetricStore(metrics)


def _service(
    closes: tuple[NormalizedObservation, ...],
    metrics: tuple[MetricResult, ...],
) -> tuple[DailyMarketPatternsService, _Observations, _MetricStore]:
    observations, store = _periods(closes, metrics)
    return DailyMarketPatternsService(observations, store), observations, store


def _query(
    service: DailyMarketPatternsService,
    request: DailyMarketPatternsRequest | None = None,
    *,
    asset_id: str = _ASSET_ID,
    source_id: str = _SOURCE_ID,
    currency: str = "USD",
):
    return service.query(
        request or _request(),
        asset_id=asset_id,
        source_id=source_id,
        currency=currency,
    )


def _crossing_series() -> tuple[tuple[NormalizedObservation, ...], tuple[MetricResult, ...]]:
    closes: list[NormalizedObservation] = []
    metrics: list[MetricResult] = []
    histograms = ("-2", "-1", "1", "-1", "1")
    rsis = ("25", "28", "35", "32", "31")
    for index, (histogram, rsi) in enumerate(zip(histograms, rsis, strict=True)):
        as_of = _START + timedelta(days=index)
        closes.append(_close(as_of, str(100 + index)))
        metrics.append(_metric(MACD_HISTOGRAM_KEY, histogram, as_of))
        metrics.append(_metric(RSI_KEY, rsi, as_of))
    return tuple(closes), tuple(metrics)


def test_service_builds_states_segments_and_three_crossings() -> None:
    service, observations, store = _service(*_crossing_series())

    result = _query(service)

    assert [point.as_of for point in result.points] == [
        _START + timedelta(days=index) for index in range(5)
    ]
    assert [point.macd_state for point in result.points] == [
        MacdMomentumState.NEGATIVE,
        MacdMomentumState.NEGATIVE,
        MacdMomentumState.POSITIVE,
        MacdMomentumState.NEGATIVE,
        MacdMomentumState.POSITIVE,
    ]
    assert [segment.state for segment in result.segments] == [
        MacdMomentumState.NEGATIVE,
        MacdMomentumState.POSITIVE,
        MacdMomentumState.NEGATIVE,
        MacdMomentumState.POSITIVE,
    ]
    assert result.segments[0].left_censored is True
    assert result.segments[0].right_censored is False
    assert result.segments[-1].right_censored is True
    assert [event.pattern_type for event in result.events] == [
        DailyMarketPatternType.MACD_CROSS_POSITIVE,
        DailyMarketPatternType.RSI_CROSS_ABOVE_30,
        DailyMarketPatternType.MACD_CROSS_NEGATIVE,
        DailyMarketPatternType.MACD_CROSS_POSITIVE,
    ]
    assert result.coverage.close_observations_read == 5
    assert result.coverage.close_periods == 5
    assert result.coverage.point_count == 5
    assert result.coverage.segment_count == 4
    assert result.coverage.event_count == 4
    assert result.coverage.maximum_evidence_available_at == _KNOWN_AT
    assert observations.writes == 0
    assert len(store.page_calls) == 1
    assert all(len(batch) <= 256 for batch in store.hydration_calls)


def test_service_breaks_continuity_when_an_intermediate_metric_is_missing() -> None:
    closes = tuple(_close(_START + timedelta(days=index), "100") for index in range(4))
    metrics = (
        _metric(MACD_HISTOGRAM_KEY, "-1", _START),
        _metric(MACD_HISTOGRAM_KEY, "1", _START + timedelta(days=1)),
        _metric(MACD_HISTOGRAM_KEY, "1", _START + timedelta(days=3)),
        _metric(RSI_KEY, "25", _START),
        _metric(RSI_KEY, "35", _START + timedelta(days=1)),
    )
    service, _, _ = _service(closes, metrics)

    result = _query(service)

    assert result.points[2].macd_state is MacdMomentumState.UNAVAILABLE
    assert result.points[2].macd_histogram.reason == "metric_not_persisted"
    assert [
        (event.pattern_type, event.previous_as_of, event.current_as_of) for event in result.events
    ] == [
        (
            DailyMarketPatternType.MACD_CROSS_POSITIVE,
            _START,
            _START + timedelta(days=1),
        ),
        (
            DailyMarketPatternType.RSI_CROSS_ABOVE_30,
            _START,
            _START + timedelta(days=1),
        ),
    ]
    assert len(result.segments) == 3


def test_service_distinguishes_ineligible_from_unpersisted_evidence() -> None:
    closes = (_close(_START, "100"),)
    metrics = (
        _metric(
            MACD_HISTOGRAM_KEY,
            "1",
            _START,
            algorithm_version="market-macd-v1-decimal34",
        ),
        _metric(RSI_KEY, "50", _START, quality=DataQuality.SUSPECT),
    )
    service, _, _ = _service(closes, metrics)

    result = _query(service)

    assert result.points[0].macd_histogram.status == "missing"
    assert result.points[0].macd_histogram.reason == "metric_not_eligible"
    assert result.points[0].rsi.reason == "metric_not_eligible"


def test_service_selects_latest_available_revision_and_ignores_future_rows() -> None:
    closes = (
        _close(_START, "100"),
        _close(_START + timedelta(days=1), "101"),
    )
    metrics = (
        _metric(MACD_HISTOGRAM_KEY, "-1", _START),
        _metric(RSI_KEY, "25", _START),
        _metric(MACD_HISTOGRAM_KEY, "1", _START + timedelta(days=1)),
        _metric(RSI_KEY, "35", _START + timedelta(days=1)),
        _metric(
            MACD_HISTOGRAM_KEY,
            "-5",
            _START + timedelta(days=1),
            available_at=_KNOWN_AT + timedelta(seconds=1),
        ),
        _metric(
            RSI_KEY,
            "20",
            _START + timedelta(days=1),
            available_at=_KNOWN_AT + timedelta(seconds=1),
        ),
    )
    service, _, _ = _service(closes, metrics)

    result = _query(service)

    assert result.points[1].macd_histogram.value == Decimal("1")
    assert result.points[1].rsi.value == Decimal("35")
    assert [event.pattern_type for event in result.events] == [
        DailyMarketPatternType.MACD_CROSS_POSITIVE,
        DailyMarketPatternType.RSI_CROSS_ABOVE_30,
    ]


def test_service_uses_latest_available_and_computed_revision_without_ambiguity() -> None:
    as_of = _START
    closes = (_close(as_of, "100"),)
    metrics = (
        _metric(
            MACD_HISTOGRAM_KEY,
            "-1",
            as_of,
            available_at=_KNOWN_AT - timedelta(days=2),
            computed_at=_KNOWN_AT - timedelta(days=2),
        ),
        _metric(
            MACD_HISTOGRAM_KEY,
            "2",
            as_of,
            available_at=_KNOWN_AT - timedelta(days=1),
            computed_at=_KNOWN_AT - timedelta(days=1),
        ),
        _metric(
            MACD_HISTOGRAM_KEY,
            "3",
            as_of,
            available_at=_KNOWN_AT - timedelta(hours=12),
            computed_at=_KNOWN_AT - timedelta(hours=12),
        ),
        _metric(
            MACD_HISTOGRAM_KEY,
            "4",
            as_of,
            available_at=_KNOWN_AT - timedelta(hours=12),
            computed_at=_KNOWN_AT,
        ),
        _metric(RSI_KEY, "50", as_of),
    )
    service, _, _ = _service(closes, metrics)

    result = _query(service)

    assert result.points[0].macd_histogram.value == Decimal("4")
    assert result.points[0].macd_state is MacdMomentumState.POSITIVE


def test_service_fails_closed_on_ambiguous_metric_revisions() -> None:
    closes = (_close(_START, "100"),)
    metrics = (
        _metric(
            MACD_HISTOGRAM_KEY,
            "1",
            _START,
            available_at=_KNOWN_AT,
            computed_at=_KNOWN_AT,
        ),
        _metric(
            MACD_HISTOGRAM_KEY,
            "2",
            _START,
            available_at=_KNOWN_AT,
            computed_at=_KNOWN_AT,
        ),
        _metric(RSI_KEY, "50", _START),
    )
    service, _, _ = _service(closes, metrics)

    with pytest.raises(AmbiguousDailyPatternEvidenceError):
        _query(service)


def test_service_fails_closed_on_tied_close_revisions_and_duplicate_utc_dates() -> None:
    first = _close(_START, "100")
    second = _close(_START, "101")
    service, _, _ = _service((first, second), ())
    with pytest.raises(AmbiguousDailyCloseRevisionError):
        _query(service)

    duplicate_date = _close(_START + timedelta(hours=12), "100")
    service, _, _ = _service((first, duplicate_date), ())
    with pytest.raises(DuplicateDailyCloseDateError):
        _query(service)


def test_service_returns_explicit_empty_coverage_and_stable_identity() -> None:
    service, observations, store = _service((), ())

    first = _query(service)
    second = _query(service)

    assert first.points == ()
    assert first.segments == ()
    assert first.events == ()
    assert first.coverage.close_observations_read == 0
    assert first.coverage.point_count == 0
    assert first.coverage.earliest_as_of is None
    assert first.coverage.latest_as_of is None
    assert first.coverage.maximum_evidence_available_at is None
    assert first.limitations
    assert first.result_id == second.result_id
    assert observations.writes == 0
    assert store.page_calls and all(call.limit <= 256 for call in store.page_calls)


def test_service_change_of_cut_changes_identity_even_with_identical_evidence() -> None:
    service, _, _ = _service(*_crossing_series())

    first = _query(service)
    repeated = _query(service)
    later = _query(service, _request(known_at=_KNOWN_AT + timedelta(days=1)))

    assert first.result_id == repeated.result_id
    assert later.result_id != first.result_id


def test_service_rejects_foreign_evidence_instead_of_mixing_assets_or_units() -> None:
    foreign_close = _close(_START, "100", asset_id="equity:us:other")
    service, _, _ = _service((foreign_close,), ())
    with pytest.raises(DailyMarketPatternsError, match="scope"):
        _query(service)

    eur_close = _close(_START, "100", unit="EUR")
    service, _, _ = _service((eur_close,), ())
    with pytest.raises(UnsupportedDailyPatternsCurrencyError):
        _query(service)

    service, _, _ = _service((_close(_START, "100"),), ())
    with pytest.raises(UnsupportedDailyPatternsCurrencyError):
        _query(service, currency="EUR")


def test_service_rejects_asset_mismatch_before_reading_storage() -> None:
    service, observations, store = _service((), ())

    with pytest.raises(DailyMarketPatternsError, match="asset_id"):
        _query(service, asset_id="equity:us:other")

    assert observations.queries == []
    assert store.page_calls == []


def test_service_handles_neutral_histogram_as_its_own_state() -> None:
    closes = (
        _close(_START, "100"),
        _close(_START + timedelta(days=1), "101"),
    )
    metrics = (
        _metric(MACD_HISTOGRAM_KEY, "0", _START),
        _metric(MACD_HISTOGRAM_KEY, "0", _START + timedelta(days=1)),
        _metric(RSI_KEY, "50", _START),
        _metric(RSI_KEY, "50", _START + timedelta(days=1)),
    )
    service, _, _ = _service(closes, metrics)

    result = _query(service)

    assert [point.macd_state for point in result.points] == [
        MacdMomentumState.NEUTRAL,
        MacdMomentumState.NEUTRAL,
    ]
    assert result.segments[0].state is MacdMomentumState.NEUTRAL
    assert result.events == ()


def test_service_is_identical_for_a_synthetic_profile_without_ticker_branches() -> None:
    closes, metrics = _crossing_series()
    service, _, _ = _service(closes, metrics)
    synthetic_asset = "equity:us:synthetic-profile"
    synthetic_source = "alpaca-market-data:iex:synthetic:daily-bars:adjustment-all"
    synthetic_closes = tuple(
        _close(
            row.observed_at,
            str(row.value),
            asset_id=synthetic_asset,
            source_id=synthetic_source,
        )
        for row in closes
    )
    synthetic_metrics = tuple(
        _metric(
            row.metric_key,
            str(row.value),
            row.as_of,
            asset_id=synthetic_asset,
            source_id=synthetic_source,
        )
        for row in metrics
    )
    synthetic_service, _, _ = _service(synthetic_closes, synthetic_metrics)

    baseline = _query(service)
    synthetic = synthetic_service.query(
        _request(asset_id=synthetic_asset),
        asset_id=synthetic_asset,
        source_id=synthetic_source,
        currency="USD",
    )

    assert [event.pattern_type for event in synthetic.events] == [
        event.pattern_type for event in baseline.events
    ]
    assert [segment.state for segment in synthetic.segments] == [
        segment.state for segment in baseline.segments
    ]
    assert synthetic.result_id != baseline.result_id
