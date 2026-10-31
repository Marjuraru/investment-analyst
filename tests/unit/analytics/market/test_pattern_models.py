"""Contract tests for the ephemeral daily market pattern response."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from investment_analyst.analytics.market.pattern_models import (
    PATTERN_ALGORITHM_VERSION,
    PATTERN_SCHEMA_VERSION,
    DailyMarketMomentumSegment,
    DailyMarketPatternEvent,
    DailyMarketPatternMetricEvidence,
    DailyMarketPatternPoint,
    DailyMarketPatternsCoverage,
    DailyMarketPatternsRequest,
    DailyMarketPatternsResult,
    DailyMarketPatternType,
    MacdMomentumState,
    daily_market_momentum_segment_id,
    daily_market_pattern_event_id,
    daily_market_patterns_result_id,
)
from investment_analyst.analytics.market.statistics_definitions import (
    MACD_HISTOGRAM_KEY,
    RSI_KEY,
)
from investment_analyst.core.models import DataQuality

_KNOWN_AT = datetime(2026, 2, 1, tzinfo=UTC)
_SOURCE_ID = "alpaca-market-data:iex:test:daily-bars:adjustment-all"
_RSI_ALGORITHM = "market-rsi-incremental-v2-decimal34"
_MACD_ALGORITHM = "market-macd-incremental-v2-decimal34"


def _request(
    *,
    start: date = date(2026, 1, 1),
    end: date = date(2026, 1, 31),
    known_at: datetime = _KNOWN_AT,
) -> DailyMarketPatternsRequest:
    return DailyMarketPatternsRequest(
        asset_id="equity:us:test",
        start_date=start,
        end_date=end,
        known_at=known_at,
    )


def _available(
    metric_key: str,
    value: str,
    *,
    result_id: UUID | None = None,
    available_at: datetime = _KNOWN_AT,
    algorithm_version: str | None = None,
    unit: str | None = None,
) -> DailyMarketPatternMetricEvidence:
    return DailyMarketPatternMetricEvidence(
        metric_key=metric_key,
        status="available",
        value=Decimal(value),
        result_id=result_id or uuid4(),
        algorithm_version=(
            algorithm_version or (_RSI_ALGORITHM if metric_key == RSI_KEY else _MACD_ALGORITHM)
        ),
        unit=unit or ("index" if metric_key == RSI_KEY else "USD"),
        available_at=available_at,
        quality=DataQuality.VALID,
    )


def _missing(metric_key: str, reason: str = "metric_not_persisted"):
    return DailyMarketPatternMetricEvidence(
        metric_key=metric_key,
        status="missing",
        reason=reason,
    )


def _point(
    as_of: datetime,
    *,
    histogram: str | None,
    rsi: str | None,
    close: str = "100",
    available_at: datetime = _KNOWN_AT,
) -> DailyMarketPatternPoint:
    histogram_evidence = (
        _available(MACD_HISTOGRAM_KEY, histogram, available_at=available_at)
        if histogram is not None
        else _missing(MACD_HISTOGRAM_KEY)
    )
    rsi_evidence = (
        _available(RSI_KEY, rsi, available_at=available_at)
        if rsi is not None
        else _missing(RSI_KEY)
    )
    if histogram is None:
        state = MacdMomentumState.UNAVAILABLE
    elif Decimal(histogram) > 0:
        state = MacdMomentumState.POSITIVE
    elif Decimal(histogram) < 0:
        state = MacdMomentumState.NEGATIVE
    else:
        state = MacdMomentumState.NEUTRAL
    evidence_availability = [available_at]
    for evidence in (histogram_evidence, rsi_evidence):
        if evidence.available_at is not None:
            evidence_availability.append(evidence.available_at)
    return DailyMarketPatternPoint(
        as_of=as_of,
        close_observation_id=uuid4(),
        close=Decimal(close),
        close_available_at=available_at,
        evidence_available_at=max(evidence_availability),
        rsi=rsi_evidence,
        macd_histogram=histogram_evidence,
        macd_state=state,
    )


def _segment(
    points: tuple[DailyMarketPatternPoint, ...],
    *,
    state: MacdMomentumState,
    left_censored: bool,
    right_censored: bool,
) -> DailyMarketMomentumSegment:
    identifiers = tuple(
        point.macd_histogram.result_id
        for point in points
        if point.macd_histogram.result_id is not None
    )
    return DailyMarketMomentumSegment(
        segment_id=daily_market_momentum_segment_id(
            asset_id="equity:us:test",
            source_id=_SOURCE_ID,
            state=state,
            histogram_result_ids=identifiers,
            left_censored=left_censored,
            right_censored=right_censored,
        ),
        state=state,
        start_as_of=points[0].as_of,
        end_as_of=points[-1].as_of,
        observed_periods=len(points),
        duration_seconds=int((points[-1].as_of - points[0].as_of).total_seconds()),
        left_censored=left_censored,
        right_censored=right_censored,
        histogram_result_ids=identifiers,
    )


def _event(
    pattern_type: DailyMarketPatternType,
    previous: DailyMarketPatternPoint,
    current: DailyMarketPatternPoint,
) -> DailyMarketPatternEvent:
    if pattern_type is DailyMarketPatternType.RSI_CROSS_ABOVE_30:
        previous_evidence = previous.rsi
        current_evidence = current.rsi
        comparison = "previous_rsi <= 30 < current_rsi"
    elif pattern_type is DailyMarketPatternType.MACD_CROSS_POSITIVE:
        previous_evidence = previous.macd_histogram
        current_evidence = current.macd_histogram
        comparison = "previous_histogram <= 0 < current_histogram"
    else:
        previous_evidence = previous.macd_histogram
        current_evidence = current.macd_histogram
        comparison = "previous_histogram >= 0 > current_histogram"
    return DailyMarketPatternEvent(
        event_id=daily_market_pattern_event_id(
            asset_id="equity:us:test",
            source_id=_SOURCE_ID,
            pattern_type=pattern_type,
            previous=previous,
            current=current,
        ),
        pattern_type=pattern_type,
        previous_as_of=previous.as_of,
        current_as_of=current.as_of,
        previous_close_observation_id=previous.close_observation_id,
        current_close_observation_id=current.close_observation_id,
        previous_metric_result_id=previous_evidence.result_id,
        current_metric_result_id=current_evidence.result_id,
        previous_value=previous_evidence.value,
        current_value=current_evidence.value,
        comparison=comparison,
        previous_evidence_available_at=previous.evidence_available_at,
        current_evidence_available_at=current.evidence_available_at,
        evidence_available_at=max(
            previous.evidence_available_at,
            current.evidence_available_at,
        ),
    )


def _scenario() -> tuple[
    DailyMarketPatternsRequest,
    tuple[DailyMarketPatternPoint, ...],
    tuple[DailyMarketMomentumSegment, ...],
    tuple[DailyMarketPatternEvent, ...],
]:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    points = (
        _point(start, histogram="-1", rsi="25"),
        _point(start + timedelta(days=1), histogram="1", rsi="35"),
        _point(start + timedelta(days=2), histogram="2", rsi="40"),
    )
    segments = (
        _segment(
            points[:1], state=MacdMomentumState.NEGATIVE, left_censored=True, right_censored=False
        ),
        _segment(
            points[1:], state=MacdMomentumState.POSITIVE, left_censored=False, right_censored=True
        ),
    )
    events = (
        _event(DailyMarketPatternType.MACD_CROSS_POSITIVE, points[0], points[1]),
        _event(DailyMarketPatternType.RSI_CROSS_ABOVE_30, points[0], points[1]),
    )
    return _request(), points, segments, events


def _coverage(
    points: tuple[DailyMarketPatternPoint, ...],
    segments: tuple[DailyMarketMomentumSegment, ...],
    events: tuple[DailyMarketPatternEvent, ...],
) -> DailyMarketPatternsCoverage:
    return DailyMarketPatternsCoverage(
        close_observations_read=len(points),
        close_periods=len(points),
        discarded_close_revisions=0,
        point_count=len(points),
        rsi_available_points=sum(item.rsi.status == "available" for item in points),
        rsi_missing_points=sum(item.rsi.status == "missing" for item in points),
        macd_available_points=sum(item.macd_histogram.status == "available" for item in points),
        macd_missing_points=sum(item.macd_histogram.status == "missing" for item in points),
        metric_periods_without_close=0,
        segment_count=len(segments),
        event_count=len(events),
        earliest_as_of=points[0].as_of if points else None,
        latest_as_of=points[-1].as_of if points else None,
        maximum_evidence_available_at=(
            max(item.evidence_available_at for item in points) if points else None
        ),
    )


def _result(
    *,
    request: DailyMarketPatternsRequest | None = None,
    points: tuple[DailyMarketPatternPoint, ...] | None = None,
    segments: tuple[DailyMarketMomentumSegment, ...] | None = None,
    events: tuple[DailyMarketPatternEvent, ...] | None = None,
    coverage: DailyMarketPatternsCoverage | None = None,
    limitations: tuple[str, ...] = ("Descriptive reconstruction at the requested cut.",),
) -> DailyMarketPatternsResult:
    default_request, default_points, default_segments, default_events = _scenario()
    request = request or default_request
    points = default_points if points is None else points
    segments = default_segments if segments is None else segments
    events = default_events if events is None else events
    coverage = coverage or _coverage(points, segments, events)
    return DailyMarketPatternsResult(
        result_id=daily_market_patterns_result_id(
            request=request,
            asset_id=request.asset_id,
            source_id=_SOURCE_ID,
            currency="USD",
            points=points,
            segments=segments,
            events=events,
        ),
        request=request,
        asset_id=request.asset_id,
        source_id=_SOURCE_ID,
        currency="USD",
        points=points,
        segments=segments,
        events=events,
        coverage=coverage,
        limitations=limitations,
    )


def test_request_keeps_inclusive_180_day_range_and_converts_it_to_half_open_utc() -> None:
    request = _request(
        start=date(2026, 1, 1),
        end=date(2026, 6, 29),
        known_at=datetime(2026, 6, 30, tzinfo=UTC),
    )

    assert request.end_before_utc == datetime(2026, 6, 30, tzinfo=UTC)
    with pytest.raises(ValidationError, match="180"):
        _request(
            start=date(2026, 1, 1),
            end=date(2026, 6, 30),
            known_at=datetime(2026, 7, 1, tzinfo=UTC),
        )


def test_request_rejects_inverted_future_or_naive_cuts() -> None:
    with pytest.raises(ValidationError, match="end_date"):
        _request(start=date(2026, 1, 10), end=date(2026, 1, 9))
    with pytest.raises(ValidationError, match="known_at"):
        _request(end=date(2026, 1, 31), known_at=datetime(2026, 1, 30, tzinfo=UTC))
    with pytest.raises(ValidationError, match="timezone"):
        DailyMarketPatternsRequest(
            asset_id="equity:us:test",
            start_date=date(2026, 1, 1),
            end_date=date(2026, 1, 2),
            known_at=datetime(2026, 1, 3),
        )
    with pytest.raises(ValidationError):
        DailyMarketPatternsRequest.model_validate(
            {
                "asset_id": "equity:us:test",
                "start_date": "2026-01-01",
                "end_date": "2026-01-02",
                "known_at": "2026-01-03T00:00:00Z",
                "unknown": True,
            }
        )


def test_metric_evidence_requires_exactly_one_status_representation() -> None:
    with pytest.raises(ValidationError, match="absence reason"):
        DailyMarketPatternMetricEvidence(
            metric_key=RSI_KEY,
            status="available",
            value=Decimal("50"),
            result_id=uuid4(),
            algorithm_version=_RSI_ALGORITHM,
            unit="index",
            available_at=_KNOWN_AT,
            quality=DataQuality.VALID,
            reason="metric_not_eligible",
        )
    with pytest.raises(ValidationError, match="absence reason"):
        DailyMarketPatternMetricEvidence(
            metric_key=RSI_KEY,
            status="missing",
            value=Decimal("50"),
        )


def test_point_tracks_positive_negative_neutral_and_unavailable_states() -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    positive = _point(start, histogram="1", rsi="50")
    negative = _point(start, histogram="-1", rsi="50")
    neutral = _point(start, histogram="0", rsi="50")
    unavailable = _point(start, histogram=None, rsi="50")

    assert positive.macd_state is MacdMomentumState.POSITIVE
    assert negative.macd_state is MacdMomentumState.NEGATIVE
    assert neutral.macd_state is MacdMomentumState.NEUTRAL
    assert unavailable.macd_state is MacdMomentumState.UNAVAILABLE


def test_point_rejects_state_or_availability_contradictions_and_floats() -> None:
    point = _point(datetime(2026, 1, 1, tzinfo=UTC), histogram="1", rsi="50")
    with pytest.raises(ValidationError, match="macd_state"):
        DailyMarketPatternPoint(**{**point.model_dump(), "macd_state": MacdMomentumState.NEGATIVE})
    with pytest.raises(ValidationError, match="evidence_available_at"):
        DailyMarketPatternPoint(
            **{
                **point.model_dump(),
                "evidence_available_at": point.evidence_available_at - timedelta(days=1),
            }
        )
    with pytest.raises(ValidationError, match="Decimal"):
        DailyMarketPatternPoint(
            **{
                **point.model_dump(),
                "close": 1.5,
            }
        )
    with pytest.raises(ValidationError, match="positive"):
        DailyMarketPatternPoint(
            **{
                **point.model_dump(),
                "close": Decimal("0"),
            }
        )


def test_segment_rejects_unavailable_state_and_inconsistent_counts() -> None:
    points = (_point(datetime(2026, 1, 1, tzinfo=UTC), histogram="1", rsi="50"),)
    segment = _segment(
        points,
        state=MacdMomentumState.POSITIVE,
        left_censored=True,
        right_censored=True,
    )

    with pytest.raises(ValidationError, match="missing MACD"):
        DailyMarketMomentumSegment(
            **{**segment.model_dump(), "state": MacdMomentumState.UNAVAILABLE}
        )
    with pytest.raises(ValidationError, match="observed_periods"):
        DailyMarketMomentumSegment(**{**segment.model_dump(), "observed_periods": 2})
    with pytest.raises(ValidationError, match="duration_seconds"):
        DailyMarketMomentumSegment(**{**segment.model_dump(), "duration_seconds": 60})


def test_event_rejects_inconsistent_comparators_or_values() -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    previous = _point(start, histogram="-1", rsi="25")
    current = _point(start + timedelta(days=1), histogram="1", rsi="35")
    event = _event(DailyMarketPatternType.MACD_CROSS_POSITIVE, previous, current)

    with pytest.raises(ValidationError, match="comparison"):
        DailyMarketPatternEvent(
            **{**event.model_dump(), "comparison": "previous_histogram < 0 < current_histogram"}
        )
    with pytest.raises(ValidationError, match="macd_cross_positive"):
        DailyMarketPatternEvent(
            **{
                **event.model_dump(),
                "previous_value": Decimal("1"),
                "current_value": Decimal("2"),
            }
        )
    with pytest.raises(ValidationError, match="evidence_available_at"):
        DailyMarketPatternEvent(
            **{
                **event.model_dump(),
                "evidence_available_at": event.evidence_available_at - timedelta(days=1),
            }
        )
    negative = _event(
        DailyMarketPatternType.MACD_CROSS_NEGATIVE,
        current,
        _point(start + timedelta(days=2), histogram="-1", rsi="35"),
    )
    assert negative.pattern_type is DailyMarketPatternType.MACD_CROSS_NEGATIVE


def test_result_validates_point_order_coverage_segments_and_events() -> None:
    _, points, segments, events = _scenario()
    result = _result(points=points, segments=segments, events=events)

    assert result.coverage.point_count == 3
    assert result.coverage.segment_count == 2
    assert result.coverage.event_count == 2

    with pytest.raises(ValidationError, match="ordered"):
        _result(
            points=tuple(reversed(points)),
            segments=segments,
            events=events,
            coverage=_coverage(points, segments, events),
        )
    with pytest.raises(ValidationError, match="left range edge"):
        _result(
            points=points,
            segments=(
                segments[0].model_copy(update={"left_censored": False}),
                segments[1],
            ),
            events=events,
        )
    with pytest.raises(ValidationError, match="consecutive"):
        _result(
            points=points,
            segments=segments,
            events=(_event(DailyMarketPatternType.RSI_CROSS_ABOVE_30, points[0], points[2]),),
            coverage=_coverage(
                points,
                segments,
                (_event(DailyMarketPatternType.RSI_CROSS_ABOVE_30, points[0], points[2]),),
            ),
        )
    with pytest.raises(ValidationError, match="point_count"):
        _result(
            points=points,
            segments=segments,
            events=events,
            coverage=_coverage(points, segments, events).model_copy(
                update={
                    "point_count": 4,
                    "rsi_available_points": 4,
                    "macd_available_points": 4,
                }
            ),
        )


def test_result_identity_is_deterministic_and_changes_with_semantic_evidence() -> None:
    _, points, segments, events = _scenario()
    first = _result(points=points, segments=segments, events=events)
    second = _result(points=points, segments=segments, events=events)

    assert first.result_id == second.result_id
    changed_histogram = _point(
        datetime(2026, 1, 2, tzinfo=UTC),
        histogram="3",
        rsi="35",
    )
    changed_points = (first.points[0], changed_histogram, first.points[2])
    changed_segments = (
        _segment(
            changed_points[:1],
            state=MacdMomentumState.NEGATIVE,
            left_censored=True,
            right_censored=False,
        ),
        _segment(
            changed_points[1:],
            state=MacdMomentumState.POSITIVE,
            left_censored=False,
            right_censored=True,
        ),
    )
    changed_events = (
        _event(
            DailyMarketPatternType.MACD_CROSS_POSITIVE,
            changed_points[0],
            changed_points[1],
        ),
        _event(
            DailyMarketPatternType.RSI_CROSS_ABOVE_30,
            changed_points[0],
            changed_points[1],
        ),
    )
    revised = _result(
        points=changed_points,
        segments=changed_segments,
        events=changed_events,
        coverage=_coverage(changed_points, changed_segments, changed_events),
    )
    different_cut = _result(
        request=_request(known_at=datetime(2026, 2, 2, tzinfo=UTC)),
        points=first.points,
        segments=first.segments,
        events=first.events,
        coverage=first.coverage,
    )

    assert revised.result_id != first.result_id
    assert different_cut.result_id != first.result_id


def test_segment_and_event_identity_are_deterministic_and_revision_sensitive() -> None:
    _, points, segments, events = _scenario()

    assert (
        segments[0].segment_id
        == _segment(
            points[:1],
            state=MacdMomentumState.NEGATIVE,
            left_censored=True,
            right_censored=False,
        ).segment_id
    )
    changed_segment = _segment(
        points[:1],
        state=MacdMomentumState.NEGATIVE,
        left_censored=True,
        right_censored=True,
    )
    assert changed_segment.segment_id != segments[0].segment_id
    assert (
        events[0].event_id
        == _event(DailyMarketPatternType.MACD_CROSS_POSITIVE, points[0], points[1]).event_id
    )

    revised_second = _point(
        datetime(2026, 1, 2, tzinfo=UTC),
        histogram="1",
        rsi="35",
    )
    assert (
        _event(DailyMarketPatternType.MACD_CROSS_POSITIVE, points[0], revised_second).event_id
        != events[0].event_id
    )


def test_result_serializes_exact_decimals_without_float_coercion() -> None:
    payload = _result().to_json_dict()

    assert payload["schema_version"] == PATTERN_SCHEMA_VERSION
    assert payload["algorithm_version"] == PATTERN_ALGORITHM_VERSION
    assert payload["points"][0]["close"] == "100"
    assert payload["events"][0]["previous_value"] == "-1"
    encoded = json.dumps(payload, separators=(",", ":"))
    assert '"close":"100"' in encoded


def test_result_rejects_empty_limitations() -> None:
    with pytest.raises(ValidationError, match="limitations"):
        _result(limitations=())
