"""Strict point-in-time contracts for bounded Apple and Bitcoin market charts."""

from datetime import UTC, datetime
from decimal import Context, Decimal, localcontext
from enum import StrEnum
from typing import Literal
from uuid import UUID

from pydantic import ConfigDict, Field, field_validator, model_validator

from investment_analyst.analytics.market.statistics_models import (
    FinancialDecimal,
    MetricCalculation,
)
from investment_analyst.core.models import DataQuality
from investment_analyst.core.models.base import ContractModel, NonEmptyStr, UTCDateTime

_HISTORY_START = datetime(1970, 1, 1, tzinfo=UTC)
_DAYS_PER_YEAR = Decimal("365.2425")
MAX_MARKET_CHART_SESSIONS = 20_000


class AaplMarketChartPeriod(StrEnum):
    """Supported display ranges expressed as target trading-session counts."""

    ONE_MONTH = "1m"
    THREE_MONTHS = "3m"
    SIX_MONTHS = "6m"
    ONE_YEAR = "1y"
    TWO_YEARS = "2y"
    FIVE_YEARS = "5y"
    MAXIMUM = "max"


class AaplMarketChartInterval(StrEnum):
    """Supported source-faithful intervals for one displayed OHLCV point."""

    AUTOMATIC = "auto"
    ONE_DAY = "1d"
    ONE_WEEK = "1w"
    ONE_MONTH = "1mo"


class AaplMarketChartResolution(StrEnum):
    """Display resolution selected deterministically from the requested range."""

    DAILY = "daily"
    WEEKLY = "weekly"
    MONTHLY = "monthly"


_SESSION_LIMITS = {
    AaplMarketChartPeriod.ONE_MONTH: 22,
    AaplMarketChartPeriod.THREE_MONTHS: 66,
    AaplMarketChartPeriod.SIX_MONTHS: 132,
    AaplMarketChartPeriod.ONE_YEAR: 260,
    AaplMarketChartPeriod.TWO_YEARS: 520,
    AaplMarketChartPeriod.FIVE_YEARS: 1_300,
    AaplMarketChartPeriod.MAXIMUM: MAX_MARKET_CHART_SESSIONS,
}

_RESOLUTIONS = {
    AaplMarketChartPeriod.ONE_MONTH: AaplMarketChartResolution.DAILY,
    AaplMarketChartPeriod.THREE_MONTHS: AaplMarketChartResolution.DAILY,
    AaplMarketChartPeriod.SIX_MONTHS: AaplMarketChartResolution.DAILY,
    AaplMarketChartPeriod.ONE_YEAR: AaplMarketChartResolution.DAILY,
    AaplMarketChartPeriod.TWO_YEARS: AaplMarketChartResolution.DAILY,
    AaplMarketChartPeriod.FIVE_YEARS: AaplMarketChartResolution.WEEKLY,
    AaplMarketChartPeriod.MAXIMUM: AaplMarketChartResolution.MONTHLY,
}

_INTERVAL_RESOLUTIONS = {
    AaplMarketChartInterval.ONE_DAY: AaplMarketChartResolution.DAILY,
    AaplMarketChartInterval.ONE_WEEK: AaplMarketChartResolution.WEEKLY,
    AaplMarketChartInterval.ONE_MONTH: AaplMarketChartResolution.MONTHLY,
}


def _chart_resolution(
    period: AaplMarketChartPeriod,
    interval: AaplMarketChartInterval,
) -> AaplMarketChartResolution:
    if interval is AaplMarketChartInterval.AUTOMATIC:
        return _RESOLUTIONS[period]
    return _INTERVAL_RESOLUTIONS[interval]


class AaplMarketChartRequest(ContractModel):
    """Request one bounded chart at an explicit point-in-time cut."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    known_at: UTCDateTime
    period: AaplMarketChartPeriod = AaplMarketChartPeriod.SIX_MONTHS
    interval: AaplMarketChartInterval = AaplMarketChartInterval.AUTOMATIC
    short_sma_window: int = Field(default=5, ge=2, le=200)
    long_sma_window: int = Field(default=20, ge=3, le=400)
    third_sma_window: int = Field(default=50, ge=4, le=400)

    @field_validator(
        "short_sma_window",
        "long_sma_window",
        "third_sma_window",
        mode="before",
    )
    @classmethod
    def validate_sma_window_type(cls, value: object) -> object:
        """Reject booleans and coercible strings before integer bounds are applied."""
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("chart SMA windows must be integers")
        return value

    @field_validator("known_at")
    @classmethod
    def validate_known_at(cls, value: datetime) -> datetime:
        """Reject cuts outside the supported Apple-history horizon."""
        if value <= _HISTORY_START:
            raise ValueError("known_at must be later than 1970-01-01T00:00:00Z")
        return value

    @model_validator(mode="after")
    def validate_sma_order(self) -> "AaplMarketChartRequest":
        """Keep the analytical roles ordered and computational work bounded."""
        if self.short_sma_window >= self.long_sma_window:
            raise ValueError("short_sma_window must be smaller than long_sma_window")
        if (
            "third_sma_window" in self.model_fields_set
            and self.long_sma_window >= self.third_sma_window
        ):
            raise ValueError("long_sma_window must be smaller than third_sma_window")
        return self

    @property
    def session_limit(self) -> int:
        """Return the target number of source trading sessions for the range."""
        return _SESSION_LIMITS[self.period]

    @property
    def resolution(self) -> AaplMarketChartResolution:
        """Return the deterministic resolution for the range and explicit interval."""
        return _chart_resolution(self.period, self.interval)


class BtcMarketChartRequest(AaplMarketChartRequest):
    """Request one bounded BTC-USD chart at an explicit point-in-time cut."""


class AaplMarketChartSma(ContractModel):
    """One exact SMA value with sufficient evidence to audit its calculation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    value: FinancialDecimal
    window: int = Field(ge=2, le=400)
    resolution: AaplMarketChartResolution
    available_at: UTCDateTime
    input_observation_ids: tuple[UUID, ...]
    algorithm_version: Literal["market-chart-sma-v2-decimal34"]

    @field_validator("window", mode="before")
    @classmethod
    def validate_window_type(cls, value: object) -> object:
        """Reject booleans and coercible values in exact calculation evidence."""
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("chart SMA window must be an integer")
        return value

    @field_validator("value")
    @classmethod
    def validate_value(cls, value: Decimal) -> Decimal:
        """Reject non-finite or non-positive plotted averages."""
        if not value.is_finite() or value <= 0:
            raise ValueError("chart SMA must be finite and greater than zero")
        return value

    @model_validator(mode="after")
    def validate_inputs(self) -> "AaplMarketChartSma":
        """Require one unique close observation for every session in the window."""
        if len(self.input_observation_ids) != self.window:
            raise ValueError("SMA input count must equal its window")
        if len(set(self.input_observation_ids)) != len(self.input_observation_ids):
            raise ValueError("SMA input observation IDs must be unique")
        return self


class AaplMarketChartPoint(ContractModel):
    """One displayed OHLCV interval with exact source-session evidence."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    resolution: AaplMarketChartResolution
    period_start_timestamp: UTCDateTime
    timestamp: UTCDateTime
    bar_available_at: UTCDateTime
    source_session_count: int = Field(ge=1, le=31)
    calendar_interval_closed: bool
    open: FinancialDecimal
    high: FinancialDecimal
    low: FinancialDecimal
    close: FinancialDecimal
    volume: FinancialDecimal
    trade_count: FinancialDecimal | None = None
    vwap: FinancialDecimal | None = None
    quality: DataQuality
    raw_record_ids: tuple[UUID, ...]
    open_observation_id: UUID
    high_observation_id: UUID
    low_observation_id: UUID
    close_observation_id: UUID
    volume_input_observation_ids: tuple[UUID, ...]
    trade_count_input_observation_ids: tuple[UUID, ...] = ()
    vwap_input_observation_ids: tuple[UUID, ...] = ()
    short_sma: AaplMarketChartSma | None = None
    long_sma: AaplMarketChartSma | None = None
    third_sma: AaplMarketChartSma | None = None
    aggregation_algorithm_version: Literal["market-chart-ohlcv-v1-decimal34"] = (
        "market-chart-ohlcv-v1-decimal34"
    )

    @field_validator("calendar_interval_closed", mode="before")
    @classmethod
    def validate_calendar_interval_closed_type(cls, value: object) -> object:
        """Keep interval state explicit instead of accepting truthy coercions."""
        if not isinstance(value, bool):
            raise ValueError("calendar_interval_closed must be a boolean")
        return value

    @model_validator(mode="after")
    def validate_values(self) -> "AaplMarketChartPoint":
        """Keep interval values, evidence, and named SMA slots semantically valid."""
        if self.period_start_timestamp > self.timestamp:
            raise ValueError("chart interval timestamps are reversed")
        if self.resolution is AaplMarketChartResolution.DAILY:
            if (
                self.source_session_count != 1
                or self.period_start_timestamp != self.timestamp
                or not self.calendar_interval_closed
            ):
                raise ValueError("daily chart points must contain exactly one source session")
        elif self.resolution is AaplMarketChartResolution.WEEKLY:
            start_week = self.period_start_timestamp.date().isocalendar()[:2]
            end_week = self.timestamp.date().isocalendar()[:2]
            if start_week != end_week:
                raise ValueError("weekly chart points must remain within one ISO week")
        elif (
            self.period_start_timestamp.year,
            self.period_start_timestamp.month,
        ) != (self.timestamp.year, self.timestamp.month):
            raise ValueError("monthly chart points must remain within one UTC month")
        if min(self.open, self.high, self.low, self.close) <= 0:
            raise ValueError("chart prices must be greater than zero")
        if self.low > self.high or not self.low <= self.open <= self.high:
            raise ValueError("chart open must be within low and high")
        if not self.low <= self.close <= self.high:
            raise ValueError("chart close must be within low and high")
        if self.volume < 0:
            raise ValueError("chart volume must be non-negative")
        if self.trade_count is not None and (
            self.trade_count < 0 or self.trade_count != self.trade_count.to_integral_value()
        ):
            raise ValueError("chart trade_count must be a non-negative integer")
        if self.vwap is not None and self.vwap <= 0:
            raise ValueError("chart vwap must be greater than zero")
        required_evidence = (
            self.raw_record_ids,
            self.volume_input_observation_ids,
        )
        if any(len(items) != self.source_session_count for items in required_evidence):
            raise ValueError("chart interval evidence must cover every source session")
        if any(len(set(items)) != len(items) for items in required_evidence):
            raise ValueError("chart interval evidence IDs must be unique")
        if self.trade_count is None:
            if self.trade_count_input_observation_ids:
                raise ValueError("missing chart trade_count must not expose input evidence")
        elif len(self.trade_count_input_observation_ids) != self.source_session_count:
            raise ValueError("chart trade_count evidence must cover every source session")
        if self.vwap is None:
            if self.vwap_input_observation_ids:
                raise ValueError("missing chart VWAP must not expose input evidence")
        elif len(self.vwap_input_observation_ids) != self.source_session_count:
            raise ValueError("chart VWAP evidence must cover every source session")
        for items in (
            self.trade_count_input_observation_ids,
            self.vwap_input_observation_ids,
        ):
            if len(set(items)) != len(items):
                raise ValueError("chart optional evidence IDs must be unique")
        if (
            self.short_sma is not None
            and self.long_sma is not None
            and self.short_sma.window >= self.long_sma.window
        ):
            raise ValueError("short chart SMA window must be smaller than long SMA window")
        for sma in (self.short_sma, self.long_sma, self.third_sma):
            if sma is not None and sma.resolution is not self.resolution:
                raise ValueError("chart SMA resolution must match its point")
        return self


class AaplMarketChartRangeStatistics(ContractModel):
    """Exact descriptive statistics for only the displayed interval closes."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    resolution: AaplMarketChartResolution
    point_count: int = Field(ge=0, le=MAX_MARKET_CHART_SESSIONS)
    source_session_count: int = Field(ge=0, le=MAX_MARKET_CHART_SESSIONS)
    start_timestamp: UTCDateTime | None = None
    end_timestamp: UTCDateTime | None = None
    elapsed_days: int = Field(default=0, ge=0)
    high: FinancialDecimal | None = None
    low: FinancialDecimal | None = None
    return_rate: FinancialDecimal | None = None
    compound_annual_growth_rate: FinancialDecimal | None = None
    maximum_drawdown_rate: FinancialDecimal | None = None
    high_observation_id: UUID | None = None
    low_observation_id: UUID | None = None
    return_input_observation_ids: tuple[UUID, ...] = ()
    maximum_drawdown_peak_timestamp: UTCDateTime | None = None
    maximum_drawdown_trough_timestamp: UTCDateTime | None = None
    maximum_drawdown_input_observation_ids: tuple[UUID, ...] = ()
    algorithm_version: Literal["market-chart-range-statistics-v3-decimal34"] = (
        "market-chart-range-statistics-v3-decimal34"
    )

    @model_validator(mode="after")
    def validate_statistics(self) -> "AaplMarketChartRangeStatistics":
        """Keep empty, single-session, and multi-session summaries unambiguous."""
        bounded_values = (
            self.start_timestamp,
            self.end_timestamp,
            self.high,
            self.low,
            self.high_observation_id,
            self.low_observation_id,
        )
        if self.point_count == 0:
            if self.source_session_count != 0:
                raise ValueError("empty chart range statistics must not define source sessions")
            if any(value is not None for value in bounded_values):
                raise ValueError("empty chart range statistics must not define bounds")
            if (
                self.elapsed_days != 0
                or self.return_rate is not None
                or self.compound_annual_growth_rate is not None
                or self.maximum_drawdown_rate is not None
                or self.return_input_observation_ids
                or self.maximum_drawdown_peak_timestamp is not None
                or self.maximum_drawdown_trough_timestamp is not None
                or self.maximum_drawdown_input_observation_ids
            ):
                raise ValueError("empty chart range statistics must not define performance")
            return self
        if self.source_session_count < self.point_count:
            raise ValueError("chart range source sessions must cover every point")
        if any(value is None for value in bounded_values):
            raise ValueError("non-empty chart range statistics require complete bounds")
        if (
            self.start_timestamp is not None
            and self.end_timestamp is not None
            and self.start_timestamp > self.end_timestamp
        ):
            raise ValueError("chart range timestamps are reversed")
        if self.high is not None and self.low is not None:
            if not self.high.is_finite() or not self.low.is_finite() or self.low <= 0:
                raise ValueError("chart range prices must be finite and greater than zero")
            if self.low > self.high:
                raise ValueError("chart range low must not exceed high")
        if self.point_count == 1:
            if (
                self.elapsed_days != 0
                or self.return_rate is not None
                or self.compound_annual_growth_rate is not None
                or self.maximum_drawdown_rate is not None
                or self.return_input_observation_ids
                or self.maximum_drawdown_peak_timestamp is not None
                or self.maximum_drawdown_trough_timestamp is not None
                or self.maximum_drawdown_input_observation_ids
            ):
                raise ValueError("single-session chart range must not define performance")
            return self
        if self.elapsed_days <= 0:
            raise ValueError("multi-session chart range requires positive elapsed days")
        if self.return_rate is None or not self.return_rate.is_finite():
            raise ValueError("multi-session chart range requires a finite return")
        if self.return_rate <= -1:
            raise ValueError("chart range return must remain greater than -100 percent")
        if len(self.return_input_observation_ids) != 2:
            raise ValueError("chart range return requires its first and last close observations")
        if len(set(self.return_input_observation_ids)) != 2:
            raise ValueError("chart range return observations must be distinct")
        if self.elapsed_days >= 365:
            if (
                self.compound_annual_growth_rate is None
                or not self.compound_annual_growth_rate.is_finite()
                or self.compound_annual_growth_rate <= -1
            ):
                raise ValueError("long chart ranges require a finite CAGR")
        elif self.compound_annual_growth_rate is not None:
            raise ValueError("short chart ranges must not annualize their return")
        if (
            self.maximum_drawdown_rate is None
            or not self.maximum_drawdown_rate.is_finite()
            or not Decimal("-1") < self.maximum_drawdown_rate <= 0
        ):
            raise ValueError("multi-session chart range requires a valid maximum drawdown")
        if (
            self.maximum_drawdown_peak_timestamp is None
            or self.maximum_drawdown_trough_timestamp is None
            or self.maximum_drawdown_peak_timestamp > self.maximum_drawdown_trough_timestamp
        ):
            raise ValueError("maximum drawdown requires ordered peak and trough timestamps")
        if len(self.maximum_drawdown_input_observation_ids) != 2:
            raise ValueError("maximum drawdown requires peak and trough close observations")
        return self


class AaplMarketChartCoverage(ContractModel):
    """Coverage counts for source sessions and resolution-dependent points."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    candidate_versions: int = Field(ge=0)
    total_available_sessions: int = Field(ge=0)
    discarded_revisions: int = Field(ge=0)
    selected_sessions: int = Field(ge=0, le=MAX_MARKET_CHART_SESSIONS)
    omitted_sessions: int = Field(ge=0)
    displayed_points: int = Field(ge=0, le=MAX_MARKET_CHART_SESSIONS)
    earliest_available_timestamp: UTCDateTime | None = None
    latest_available_timestamp: UTCDateTime | None = None
    earliest_selected_timestamp: UTCDateTime | None = None
    latest_selected_timestamp: UTCDateTime | None = None

    @model_validator(mode="after")
    def validate_coverage(self) -> "AaplMarketChartCoverage":
        """Keep full-history, revision, and bounded-display counts aligned."""
        if self.candidate_versions != self.total_available_sessions + self.discarded_revisions:
            raise ValueError("candidate_versions must equal available sessions plus revisions")
        if self.total_available_sessions != self.selected_sessions + self.omitted_sessions:
            raise ValueError("available sessions must equal selected plus omitted sessions")
        if self.displayed_points > self.selected_sessions:
            raise ValueError("displayed points must not exceed selected source sessions")
        available_bounds = (
            self.earliest_available_timestamp,
            self.latest_available_timestamp,
        )
        selected_bounds = (
            self.earliest_selected_timestamp,
            self.latest_selected_timestamp,
        )
        if self.total_available_sessions == 0 and available_bounds != (None, None):
            raise ValueError("empty available coverage must not define timestamps")
        if self.total_available_sessions > 0 and None in available_bounds:
            raise ValueError("non-empty available coverage requires timestamps")
        if self.selected_sessions == 0:
            if self.displayed_points != 0 or selected_bounds != (None, None):
                raise ValueError("empty selected coverage must not define points or timestamps")
        elif self.displayed_points == 0 or None in selected_bounds:
            raise ValueError("non-empty selected coverage requires points and timestamps")
        if (
            self.earliest_available_timestamp is not None
            and self.latest_available_timestamp is not None
            and self.earliest_available_timestamp > self.latest_available_timestamp
        ):
            raise ValueError("available coverage timestamps are reversed")
        if (
            self.earliest_selected_timestamp is not None
            and self.latest_selected_timestamp is not None
            and self.earliest_selected_timestamp > self.latest_selected_timestamp
        ):
            raise ValueError("selected coverage timestamps are reversed")
        return self


class AaplMarketChart(ContractModel):
    """Versioned exact-data contract consumed by the local market chart."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    schema_version: Literal["aapl-market-chart-v5"] = "aapl-market-chart-v5"
    asset_id: Literal["equity:us:aapl"] = "equity:us:aapl"
    source_id: Literal["alpaca-market-data:iex:aapl:daily-bars:adjustment-all"] = (
        "alpaca-market-data:iex:aapl:daily-bars:adjustment-all"
    )
    known_at: UTCDateTime
    period: AaplMarketChartPeriod
    interval: AaplMarketChartInterval
    session_limit: int = Field(ge=1, le=MAX_MARKET_CHART_SESSIONS)
    resolution: AaplMarketChartResolution
    resolution_policy_version: Literal["market-chart-resolution-policy-v2"] = (
        "market-chart-resolution-policy-v2"
    )
    price_field: Literal["close"] = "close"
    price_unit: Literal["USD"] = "USD"
    volume_unit: Literal["shares"] = "shares"
    sma_windows: tuple[int, int, int] = (5, 20, 50)
    points: tuple[AaplMarketChartPoint, ...]
    latest_session: AaplMarketChartPoint | None = None
    range_statistics: AaplMarketChartRangeStatistics
    latest_statistics: tuple[MetricCalculation, ...]
    coverage: AaplMarketChartCoverage
    traceability_verified: Literal[True] = True
    limitations: tuple[NonEmptyStr, ...]

    @field_validator("sma_windows", mode="before")
    @classmethod
    def validate_sma_window_types(cls, value: object) -> object:
        """Reject coercion in the public chart-window contract."""
        if not isinstance(value, (list, tuple)) or len(value) != 3:
            raise ValueError("chart SMA windows must contain exactly three integers")
        if any(isinstance(item, bool) or not isinstance(item, int) for item in value):
            raise ValueError("chart SMA windows must contain exactly three integers")
        return value

    @model_validator(mode="after")
    def validate_chart(self) -> "AaplMarketChart":
        """Validate order, scope, boundedness, availability, and coverage."""
        if self.session_limit != _SESSION_LIMITS[self.period]:
            raise ValueError("session_limit does not match the requested period")
        if self.resolution is not _chart_resolution(self.period, self.interval):
            raise ValueError("resolution does not match the requested period and interval")
        short_window, long_window, third_window = self.sma_windows
        if (
            not 2 <= short_window <= 200
            or not 3 <= long_window <= 400
            or not 4 <= third_window <= 400
        ):
            raise ValueError("chart SMA windows are outside supported bounds")
        if short_window >= long_window:
            raise ValueError("short chart SMA window must be smaller than long SMA window")
        if len(self.points) != self.coverage.displayed_points:
            raise ValueError("point count must match displayed-point coverage")
        if len(self.points) != self.range_statistics.point_count:
            raise ValueError("point count must match range statistics")
        if self.range_statistics.resolution is not self.resolution:
            raise ValueError("range-statistics resolution must match the chart")
        if self.range_statistics.source_session_count != self.coverage.selected_sessions:
            raise ValueError("range-statistics source sessions must match coverage")
        if len(self.points) > self.session_limit:
            raise ValueError("point count exceeds the bounded session limit")
        selected_source_sessions = sum(point.source_session_count for point in self.points)
        if selected_source_sessions != self.coverage.selected_sessions:
            raise ValueError("point source-session counts must match selected coverage")
        timestamps = [point.timestamp for point in self.points]
        if timestamps != sorted(timestamps) or len(timestamps) != len(set(timestamps)):
            raise ValueError("chart points must be ordered and unique")
        for point in self.points:
            if point.resolution is not self.resolution:
                raise ValueError("chart point resolution does not match the chart")
            if point.timestamp >= self.known_at or point.bar_available_at > self.known_at:
                raise ValueError("chart point is outside the requested point-in-time cut")
            if point.short_sma is not None and point.short_sma.window != short_window:
                raise ValueError("short chart SMA does not match the requested window")
            if point.long_sma is not None and point.long_sma.window != long_window:
                raise ValueError("long chart SMA does not match the requested window")
            if point.third_sma is not None and point.third_sma.window != third_window:
                raise ValueError("third chart SMA does not match the requested window")
            for sma in (point.short_sma, point.long_sma, point.third_sma):
                if sma is not None and sma.available_at > self.known_at:
                    raise ValueError("chart SMA was not available at known_at")
        if self.latest_session is None:
            if self.coverage.total_available_sessions != 0:
                raise ValueError("non-empty market coverage requires the latest daily session")
        else:
            if self.latest_session.resolution is not AaplMarketChartResolution.DAILY:
                raise ValueError("latest_session must preserve daily resolution")
            if (
                self.latest_session.timestamp >= self.known_at
                or self.latest_session.bar_available_at > self.known_at
            ):
                raise ValueError("latest_session is outside the requested point-in-time cut")
            if not self.points or self.latest_session.timestamp != self.points[-1].timestamp:
                raise ValueError("latest_session must match the latest displayed interval")
            if (
                self.latest_session.short_sma is not None
                and self.latest_session.short_sma.window != short_window
            ):
                raise ValueError("latest short SMA does not match the requested window")
            if (
                self.latest_session.long_sma is not None
                and self.latest_session.long_sma.window != long_window
            ):
                raise ValueError("latest long SMA does not match the requested window")
            if (
                self.latest_session.third_sma is not None
                and self.latest_session.third_sma.window != third_window
            ):
                raise ValueError("latest third SMA does not match the requested window")
            for sma in (
                self.latest_session.short_sma,
                self.latest_session.long_sma,
                self.latest_session.third_sma,
            ):
                if sma is not None and sma.available_at > self.known_at:
                    raise ValueError("latest chart SMA was not available at known_at")
        expected_statistics = {
            "market.history.simple_return_1d": (
                "market-simple-return-1d-v1-decimal34",
                None,
            ),
            "market.history.rolling_daily_volatility": (
                "market-rolling-daily-volatility-v1-decimal34",
                20,
            ),
            "market.history.relative_volume": (
                "market-relative-volume-v1-decimal34",
                20,
            ),
        }
        statistic_keys = [item.metric_key for item in self.latest_statistics]
        if len(statistic_keys) != len(set(statistic_keys)):
            raise ValueError("latest chart statistics must be unique by metric key")
        if not set(statistic_keys) <= set(expected_statistics):
            raise ValueError("latest chart statistics contain an unsupported metric")
        latest_timestamp = (
            self.latest_session.timestamp if self.latest_session is not None else None
        )
        for statistic in self.latest_statistics:
            algorithm, expected_window = expected_statistics[statistic.metric_key]
            if statistic.asset_id != self.asset_id or statistic.source_id != self.source_id:
                raise ValueError("latest chart statistic has incompatible scope")
            if statistic.as_of != latest_timestamp or statistic.available_at > self.known_at:
                raise ValueError("latest chart statistic is outside the requested cut")
            if statistic.algorithm_version != algorithm or statistic.unit != "ratio":
                raise ValueError("latest chart statistic has unsupported semantics")
            if (
                expected_window is not None
                and statistic.parameters.get("window") != expected_window
            ):
                raise ValueError("latest chart statistic has an unsupported window")
        if self.points:
            if self.coverage.earliest_selected_timestamp != self.points[0].period_start_timestamp:
                raise ValueError("earliest selected timestamp does not match points")
            if self.coverage.latest_selected_timestamp != self.points[-1].timestamp:
                raise ValueError("latest selected timestamp does not match points")
            if self.range_statistics.start_timestamp != self.points[0].timestamp:
                raise ValueError("range start timestamp does not match points")
            if self.range_statistics.end_timestamp != self.points[-1].timestamp:
                raise ValueError("range end timestamp does not match points")
            self._validate_range_statistics()
        elif self.latest_statistics or self.latest_session is not None:
            raise ValueError("empty chart must not expose latest market data")
        return self

    def _validate_range_statistics(self) -> None:
        """Recalculate exact range statistics from displayed point evidence."""
        statistics = self.range_statistics
        first = self.points[0]
        latest = self.points[-1]
        highest = max(self.points, key=lambda point: point.high)
        lowest = min(self.points, key=lambda point: point.low)
        elapsed_days = (latest.timestamp.date() - first.timestamp.date()).days
        expected_return: Decimal | None = None
        expected_cagr: Decimal | None = None
        expected_drawdown: Decimal | None = None
        drawdown_peak = first
        drawdown_trough = first
        if len(self.points) > 1:
            running_peak = first
            with localcontext(Context(prec=34)):
                expected_return = latest.close / first.close - Decimal("1")
                if elapsed_days >= 365:
                    expected_cagr = (latest.close / first.close) ** (
                        _DAYS_PER_YEAR / Decimal(elapsed_days)
                    ) - Decimal("1")
                expected_drawdown = Decimal("0")
                for point in self.points[1:]:
                    if point.close > running_peak.close:
                        running_peak = point
                    drawdown = point.close / running_peak.close - Decimal("1")
                    if drawdown < expected_drawdown:
                        expected_drawdown = drawdown
                        drawdown_peak = running_peak
                        drawdown_trough = point
        expected_return_inputs = (
            (first.close_observation_id, latest.close_observation_id)
            if len(self.points) > 1
            else ()
        )
        expected_drawdown_inputs = (
            (drawdown_peak.close_observation_id, drawdown_trough.close_observation_id)
            if len(self.points) > 1
            else ()
        )
        expected_drawdown_peak_timestamp = drawdown_peak.timestamp if len(self.points) > 1 else None
        expected_drawdown_trough_timestamp = (
            drawdown_trough.timestamp if len(self.points) > 1 else None
        )
        if (
            statistics.resolution is not self.resolution
            or statistics.point_count != len(self.points)
            or statistics.source_session_count
            != sum(point.source_session_count for point in self.points)
            or statistics.elapsed_days != elapsed_days
            or statistics.high != highest.high
            or statistics.low != lowest.low
            or statistics.high_observation_id != highest.high_observation_id
            or statistics.low_observation_id != lowest.low_observation_id
            or statistics.return_rate != expected_return
            or statistics.compound_annual_growth_rate != expected_cagr
            or statistics.return_input_observation_ids != expected_return_inputs
            or statistics.maximum_drawdown_rate != expected_drawdown
            or statistics.maximum_drawdown_peak_timestamp != expected_drawdown_peak_timestamp
            or statistics.maximum_drawdown_trough_timestamp != expected_drawdown_trough_timestamp
            or statistics.maximum_drawdown_input_observation_ids != expected_drawdown_inputs
        ):
            raise ValueError("chart range statistics do not match displayed evidence")

    def to_json_dict(self) -> dict[str, object]:
        """Return an explicit JSON-compatible contract with exact decimals as strings."""
        return self.model_dump(mode="json")


class BtcMarketChart(AaplMarketChart):
    """Versioned BTC-USD chart contract over Coinbase Exchange daily candles."""

    schema_version: Literal["btc-market-chart-v1"] = "btc-market-chart-v1"
    asset_id: Literal["crypto:btc-usd"] = "crypto:btc-usd"
    source_id: Literal["coinbase-exchange:btc-usd:daily-candles"] = (
        "coinbase-exchange:btc-usd:daily-candles"
    )
    volume_unit: Literal["BTC"] = "BTC"
