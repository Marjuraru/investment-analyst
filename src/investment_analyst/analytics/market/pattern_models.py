"""Versioned, deterministic daily market state and crossing-pattern contracts."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Literal
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import BeforeValidator, ConfigDict, Field, model_validator

from investment_analyst.analytics.analytical_access_models import analytical_content_hash
from investment_analyst.analytics.market.statistics_definitions import (
    MACD_HISTOGRAM_KEY,
    RSI_KEY,
)
from investment_analyst.core.models import DataQuality
from investment_analyst.core.models.base import ContractModel, NonEmptyStr, UTCDateTime

PATTERN_REQUEST_SCHEMA_VERSION = "daily-market-patterns-request-v1"
PATTERN_SCHEMA_VERSION = "daily-market-patterns-v1"
PATTERN_ALGORITHM_VERSION = "daily-market-patterns-v1-decimal34"
PATTERN_NAMESPACE = uuid5(NAMESPACE_URL, "investment-analyst/daily-market-patterns-v1")
MAX_PATTERN_RANGE_DAYS = 180


def _reject_float(value: object) -> object:
    if isinstance(value, (bool, float)):
        raise ValueError("pattern financial values must use Decimal")
    return value


FinancialDecimal = Annotated[Decimal, BeforeValidator(_reject_float)]


class MacdMomentumState(StrEnum):
    """Descriptive sign of the MACD histogram for one observed period."""

    POSITIVE = "positive"
    NEGATIVE = "negative"
    NEUTRAL = "neutral"
    UNAVAILABLE = "unavailable"


class DailyMarketPatternType(StrEnum):
    """The three crossing patterns authorized by this contract."""

    MACD_CROSS_POSITIVE = "macd_cross_positive"
    MACD_CROSS_NEGATIVE = "macd_cross_negative"
    RSI_CROSS_ABOVE_30 = "rsi_cross_above_30"


class DailyMarketPatternsRequest(ContractModel):
    """Inclusive public range and explicit point-in-time cut for one asset."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    schema_version: Literal["daily-market-patterns-request-v1"] = PATTERN_REQUEST_SCHEMA_VERSION
    asset_id: NonEmptyStr
    start_date: date
    end_date: date
    known_at: UTCDateTime

    @model_validator(mode="after")
    def validate_range(self) -> DailyMarketPatternsRequest:
        if self.end_date < self.start_date:
            raise ValueError("end_date must not precede start_date")
        if (self.end_date - self.start_date).days + 1 > MAX_PATTERN_RANGE_DAYS:
            raise ValueError("date range must not exceed 180 inclusive calendar days")
        if self.end_date > self.known_at.date():
            raise ValueError("end_date must not follow the known_at UTC date")
        return self

    @property
    def start_utc(self) -> datetime:
        """Inclusive public start as one UTC instant."""
        return datetime.combine(self.start_date, time.min, tzinfo=UTC)

    @property
    def end_before_utc(self) -> datetime:
        """Exclusive internal bound that keeps the inclusive final date."""
        return datetime.combine(self.end_date + timedelta(days=1), time.min, tzinfo=UTC)


class DailyMarketPatternMetricEvidence(ContractModel):
    """Exact evidence or an explicit absence reason for one period and metric."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    metric_key: NonEmptyStr
    status: Literal["available", "missing"]
    value: FinancialDecimal | None = None
    result_id: UUID | None = None
    algorithm_version: NonEmptyStr | None = None
    unit: NonEmptyStr | None = None
    available_at: UTCDateTime | None = None
    quality: DataQuality | None = None
    reason: NonEmptyStr | None = None

    @model_validator(mode="after")
    def validate_status(self) -> DailyMarketPatternMetricEvidence:
        evidence = (
            self.value,
            self.result_id,
            self.algorithm_version,
            self.unit,
            self.available_at,
            self.quality,
        )
        if self.status == "available":
            if any(item is None for item in evidence) or self.reason is not None:
                raise ValueError("available evidence requires values and no absence reason")
        elif any(item is not None for item in evidence) or self.reason is None:
            raise ValueError("missing evidence requires an absence reason and no values")
        return self


class DailyMarketPatternPoint(ContractModel):
    """One observed close period with its exact eligible analytical evidence."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    as_of: UTCDateTime
    close_observation_id: UUID
    close: FinancialDecimal
    close_available_at: UTCDateTime
    evidence_available_at: UTCDateTime
    rsi: DailyMarketPatternMetricEvidence
    macd_histogram: DailyMarketPatternMetricEvidence
    macd_state: MacdMomentumState

    @model_validator(mode="after")
    def validate_point(self) -> DailyMarketPatternPoint:
        if not self.close.is_finite() or self.close <= 0:
            raise ValueError("close must be a finite positive decimal")
        if self.rsi.metric_key != RSI_KEY:
            raise ValueError("the RSI evidence must use the authorized metric key")
        if self.macd_histogram.metric_key != MACD_HISTOGRAM_KEY:
            raise ValueError("the MACD evidence must use the authorized metric key")
        available = [self.close_available_at]
        for evidence in (self.rsi, self.macd_histogram):
            if evidence.status == "available" and evidence.available_at is not None:
                available.append(evidence.available_at)
        if self.evidence_available_at != max(available):
            raise ValueError("evidence_available_at must bind every selected evidence row")
        expected_state = _momentum_state(self.macd_histogram)
        if self.macd_state is not expected_state:
            raise ValueError("macd_state must match the selected histogram evidence")
        return self


class DailyMarketMomentumSegment(ContractModel):
    """Contiguous observed periods sharing one descriptive MACD histogram state."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    segment_id: UUID
    state: MacdMomentumState
    start_as_of: UTCDateTime
    end_as_of: UTCDateTime
    observed_periods: int = Field(ge=1)
    duration_seconds: int = Field(ge=0)
    left_censored: bool
    right_censored: bool
    histogram_result_ids: tuple[UUID, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_segment(self) -> DailyMarketMomentumSegment:
        if self.state is MacdMomentumState.UNAVAILABLE:
            raise ValueError("segments never represent missing MACD evidence")
        if self.start_as_of > self.end_as_of:
            raise ValueError("segment start_as_of must not follow end_as_of")
        if self.observed_periods != len(self.histogram_result_ids):
            raise ValueError("observed_periods must match the histogram evidence sequence")
        if self.duration_seconds != int((self.end_as_of - self.start_as_of).total_seconds()):
            raise ValueError("duration_seconds must be the UTC elapsed time between extremes")
        if len(set(self.histogram_result_ids)) != len(self.histogram_result_ids):
            raise ValueError("segment histogram evidence must not repeat an identity")
        return self


class DailyMarketPatternEvent(ContractModel):
    """One crossing event backed by two consecutive observed periods."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    event_id: UUID
    pattern_type: DailyMarketPatternType
    previous_as_of: UTCDateTime
    current_as_of: UTCDateTime
    previous_close_observation_id: UUID
    current_close_observation_id: UUID
    previous_metric_result_id: UUID
    current_metric_result_id: UUID
    previous_value: FinancialDecimal
    current_value: FinancialDecimal
    comparison: NonEmptyStr
    previous_evidence_available_at: UTCDateTime
    current_evidence_available_at: UTCDateTime
    evidence_available_at: UTCDateTime

    @model_validator(mode="after")
    def validate_event(self) -> DailyMarketPatternEvent:
        if self.previous_as_of >= self.current_as_of:
            raise ValueError("event periods must be strictly ordered")
        if self.evidence_available_at != max(
            self.previous_evidence_available_at,
            self.current_evidence_available_at,
        ):
            raise ValueError("event evidence_available_at must bind both observed periods")
        if not self.previous_value.is_finite() or not self.current_value.is_finite():
            raise ValueError("event values must be finite decimals")
        if self.pattern_type is DailyMarketPatternType.RSI_CROSS_ABOVE_30:
            if not (self.previous_value <= Decimal("30") and self.current_value > Decimal("30")):
                raise ValueError("rsi_cross_above_30 requires previous <= 30 and current > 30")
            expected = "previous_rsi <= 30 < current_rsi"
        elif self.pattern_type is DailyMarketPatternType.MACD_CROSS_POSITIVE:
            if not (self.previous_value <= 0 and self.current_value > 0):
                raise ValueError("macd_cross_positive requires previous <= 0 and current > 0")
            expected = "previous_histogram <= 0 < current_histogram"
        else:
            if not (self.previous_value >= 0 and self.current_value < 0):
                raise ValueError("macd_cross_negative requires previous >= 0 and current < 0")
            expected = "previous_histogram >= 0 > current_histogram"
        if self.comparison != expected:
            raise ValueError("comparison must declare the exact authorized comparators")
        return self


class DailyMarketPatternsCoverage(ContractModel):
    """Counts and bounds describing what this reconstruction could evaluate."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    close_observations_read: int = Field(ge=0)
    close_periods: int = Field(ge=0)
    discarded_close_revisions: int = Field(ge=0)
    point_count: int = Field(ge=0)
    rsi_available_points: int = Field(ge=0)
    rsi_missing_points: int = Field(ge=0)
    macd_available_points: int = Field(ge=0)
    macd_missing_points: int = Field(ge=0)
    metric_periods_without_close: int = Field(ge=0)
    segment_count: int = Field(ge=0)
    event_count: int = Field(ge=0)
    earliest_as_of: UTCDateTime | None = None
    latest_as_of: UTCDateTime | None = None
    maximum_evidence_available_at: UTCDateTime | None = None

    @model_validator(mode="after")
    def validate_coverage(self) -> DailyMarketPatternsCoverage:
        if self.close_observations_read != self.close_periods + self.discarded_close_revisions:
            raise ValueError("close counts must reconcile read, selected, and discarded rows")
        if self.point_count != self.rsi_available_points + self.rsi_missing_points:
            raise ValueError("RSI counts must reconcile the described points")
        if self.point_count != self.macd_available_points + self.macd_missing_points:
            raise ValueError("MACD counts must reconcile the described points")
        if self.point_count == 0:
            if any(
                item is not None
                for item in (
                    self.earliest_as_of,
                    self.latest_as_of,
                    self.maximum_evidence_available_at,
                )
            ):
                raise ValueError("empty coverage must not define temporal bounds")
        elif (
            self.earliest_as_of is None
            or self.latest_as_of is None
            or self.maximum_evidence_available_at is None
        ):
            raise ValueError("non-empty coverage requires temporal bounds")
        elif self.earliest_as_of > self.latest_as_of:
            raise ValueError("earliest_as_of must not follow latest_as_of")
        return self


class DailyMarketPatternsResult(ContractModel):
    """Ephemeral descriptive reconstruction at one explicit cut; never persisted."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    schema_version: Literal["daily-market-patterns-v1"] = PATTERN_SCHEMA_VERSION
    algorithm_version: Literal["daily-market-patterns-v1-decimal34"] = PATTERN_ALGORITHM_VERSION
    result_id: UUID
    request: DailyMarketPatternsRequest
    asset_id: NonEmptyStr
    source_id: NonEmptyStr
    currency: NonEmptyStr
    points: tuple[DailyMarketPatternPoint, ...]
    segments: tuple[DailyMarketMomentumSegment, ...]
    events: tuple[DailyMarketPatternEvent, ...]
    coverage: DailyMarketPatternsCoverage
    limitations: tuple[NonEmptyStr, ...]

    @model_validator(mode="after")
    def validate_result(self) -> DailyMarketPatternsResult:
        if not self.limitations:
            raise ValueError("a daily market pattern result must declare its limitations")
        if self.request.asset_id != self.asset_id:
            raise ValueError("request asset_id must match the resolved asset")
        if self.coverage.point_count != len(self.points):
            raise ValueError("coverage point_count must match the described points")
        if self.coverage.segment_count != len(self.segments):
            raise ValueError("coverage segment_count must match the described segments")
        if self.coverage.event_count != len(self.events):
            raise ValueError("coverage event_count must match the described events")
        timestamps = tuple(point.as_of for point in self.points)
        if timestamps != tuple(sorted(timestamps)):
            raise ValueError("points must be ordered by as_of")
        if len(set(timestamps)) != len(timestamps):
            raise ValueError("points must not repeat one observed period")
        if self.points:
            if self.coverage.earliest_as_of != self.points[0].as_of:
                raise ValueError("coverage earliest_as_of must match the first point")
            if self.coverage.latest_as_of != self.points[-1].as_of:
                raise ValueError("coverage latest_as_of must match the last point")
            if self.coverage.maximum_evidence_available_at != max(
                point.evidence_available_at for point in self.points
            ):
                raise ValueError("coverage must bind the maximum point evidence availability")
        self._validate_segments()
        self._validate_events()
        return self

    def _validate_segments(self) -> None:
        positions = {point.as_of: index for index, point in enumerate(self.points)}
        previous_end = -1
        for segment in self.segments:
            if segment.start_as_of not in positions or segment.end_as_of not in positions:
                raise ValueError("segments must reference described points")
            start = positions[segment.start_as_of]
            end = positions[segment.end_as_of]
            if start > end or start <= previous_end:
                raise ValueError("segments must be ordered and non-overlapping")
            previous_end = end
            window = self.points[start : end + 1]
            if any(point.macd_state is not segment.state for point in window):
                raise ValueError("segment state must match every described point")
            evidence = tuple(point.macd_histogram.result_id for point in window)
            if evidence != segment.histogram_result_ids:
                raise ValueError("segment evidence must match its described points")
            if segment.left_censored != (start == 0):
                raise ValueError("only the first segment may touch the left range edge")
            if segment.right_censored != (end == len(self.points) - 1):
                raise ValueError("only the last segment may touch the right range edge")

    def _validate_events(self) -> None:
        positions = {point.as_of: index for index, point in enumerate(self.points)}
        seen: set[tuple[int, DailyMarketPatternType]] = set()
        for event in self.events:
            if event.previous_as_of not in positions or event.current_as_of not in positions:
                raise ValueError("events must reference described points")
            previous_index = positions[event.previous_as_of]
            current_index = positions[event.current_as_of]
            if current_index != previous_index + 1:
                raise ValueError("events require two consecutive observed periods")
            key = (previous_index, event.pattern_type)
            if key in seen:
                raise ValueError("one observed pair may emit at most one event per type")
            seen.add(key)
            previous_point = self.points[previous_index]
            current_point = self.points[current_index]
            previous_evidence = (
                previous_point.rsi
                if event.pattern_type is DailyMarketPatternType.RSI_CROSS_ABOVE_30
                else previous_point.macd_histogram
            )
            current_evidence = (
                current_point.rsi
                if event.pattern_type is DailyMarketPatternType.RSI_CROSS_ABOVE_30
                else current_point.macd_histogram
            )
            if (
                event.previous_close_observation_id != previous_point.close_observation_id
                or event.current_close_observation_id != current_point.close_observation_id
                or event.previous_metric_result_id != previous_evidence.result_id
                or event.current_metric_result_id != current_evidence.result_id
                or event.previous_value != previous_evidence.value
                or event.current_value != current_evidence.value
                or event.previous_evidence_available_at != previous_point.evidence_available_at
                or event.current_evidence_available_at != current_point.evidence_available_at
            ):
                raise ValueError("event evidence must match its described points")

    def to_json_dict(self) -> dict[str, object]:
        """Return the lossless JSON projection with exact Decimal strings."""
        return self.model_dump(mode="json")


def pattern_request_identity_payload(request: DailyMarketPatternsRequest) -> dict[str, object]:
    """Return the stable semantic payload of one request."""
    return {
        "asset_id": request.asset_id,
        "start_date": request.start_date.isoformat(),
        "end_date": request.end_date.isoformat(),
        "known_at": request.known_at,
    }


def daily_market_momentum_segment_id(
    *,
    asset_id: str,
    source_id: str,
    state: MacdMomentumState,
    histogram_result_ids: tuple[UUID, ...],
    left_censored: bool,
    right_censored: bool,
) -> UUID:
    """Return the deterministic identity of one described momentum segment."""
    return _identity(
        "segment",
        {
            "asset_id": asset_id,
            "source_id": source_id,
            "state": state,
            "histogram_result_ids": histogram_result_ids,
            "left_censored": left_censored,
            "right_censored": right_censored,
        },
    )


def daily_market_pattern_event_id(
    *,
    asset_id: str,
    source_id: str,
    pattern_type: DailyMarketPatternType,
    previous: DailyMarketPatternPoint,
    current: DailyMarketPatternPoint,
) -> UUID:
    """Return the deterministic identity of one crossing event."""
    return _identity(
        "event",
        {
            "asset_id": asset_id,
            "source_id": source_id,
            "pattern_type": pattern_type,
            "previous": _event_point_payload(previous),
            "current": _event_point_payload(current),
        },
    )


def daily_market_patterns_result_id(
    *,
    request: DailyMarketPatternsRequest,
    asset_id: str,
    source_id: str,
    currency: str,
    points: tuple[DailyMarketPatternPoint, ...],
    segments: tuple[DailyMarketMomentumSegment, ...],
    events: tuple[DailyMarketPatternEvent, ...],
) -> UUID:
    """Return the deterministic identity of the complete ephemeral result."""
    return _identity(
        "result",
        {
            "schema_version": PATTERN_SCHEMA_VERSION,
            "request": pattern_request_identity_payload(request),
            "asset_id": asset_id,
            "source_id": source_id,
            "currency": currency,
            "points": points,
            "segment_ids": tuple(segment.segment_id for segment in segments),
            "event_ids": tuple(event.event_id for event in events),
        },
    )


def _identity(artifact: str, payload: dict[str, object]) -> UUID:
    return uuid5(
        PATTERN_NAMESPACE,
        analytical_content_hash(
            {
                "artifact": artifact,
                "algorithm_version": PATTERN_ALGORITHM_VERSION,
                **payload,
            }
        ),
    )


def _event_point_payload(point: DailyMarketPatternPoint) -> dict[str, object]:
    return {
        "as_of": point.as_of,
        "close_observation_id": point.close_observation_id,
        "rsi_result_id": point.rsi.result_id,
        "macd_histogram_result_id": point.macd_histogram.result_id,
    }


def _momentum_state(evidence: DailyMarketPatternMetricEvidence) -> MacdMomentumState:
    if evidence.status == "missing" or evidence.value is None:
        return MacdMomentumState.UNAVAILABLE
    if evidence.value > 0:
        return MacdMomentumState.POSITIVE
    if evidence.value < 0:
        return MacdMomentumState.NEGATIVE
    return MacdMomentumState.NEUTRAL


__all__ = [
    "DailyMarketMomentumSegment",
    "DailyMarketPatternEvent",
    "DailyMarketPatternMetricEvidence",
    "DailyMarketPatternPoint",
    "DailyMarketPatternType",
    "DailyMarketPatternsCoverage",
    "DailyMarketPatternsRequest",
    "DailyMarketPatternsResult",
    "MAX_PATTERN_RANGE_DAYS",
    "MacdMomentumState",
    "PATTERN_ALGORITHM_VERSION",
    "PATTERN_NAMESPACE",
    "PATTERN_REQUEST_SCHEMA_VERSION",
    "PATTERN_SCHEMA_VERSION",
    "daily_market_momentum_segment_id",
    "daily_market_pattern_event_id",
    "daily_market_patterns_result_id",
    "pattern_request_identity_payload",
]
