"""Strict contracts for deterministic aggregation of verified one-minute bars."""

from datetime import timedelta
from enum import StrEnum
from typing import Literal
from uuid import UUID

from pydantic import ConfigDict, Field, field_validator, model_validator

from investment_analyst.analytics.market.bar_models import (
    FinancialDecimal,
    HistoricalBarQuery,
)
from investment_analyst.analytics.market.bar_schemas import get_market_bar_schema
from investment_analyst.core.models import DataFrequency, DataQuality
from investment_analyst.core.models.base import ContractModel, NonEmptyStr, UTCDateTime

INTRADAY_AGGREGATION_ALGORITHM = "market-intraday-ohlcv-v1-decimal34"


class IntradayInterval(StrEnum):
    """Supported fixed UTC intervals derived from one-minute evidence."""

    MINUTE_1 = "1m"
    MINUTE_5 = "5m"
    MINUTE_15 = "15m"
    MINUTE_30 = "30m"
    MINUTE_45 = "45m"
    HOUR_1 = "1h"
    HOUR_2 = "2h"
    HOUR_4 = "4h"
    HOUR_5 = "5h"

    @property
    def seconds(self) -> int:
        """Return the exact fixed-width bucket duration."""
        return _INTERVAL_SECONDS[self]

    @property
    def expected_source_bars(self) -> int:
        """Return the number of one-minute bars in one complete bucket."""
        return self.seconds // 60


_INTERVAL_SECONDS = {
    IntradayInterval.MINUTE_1: 60,
    IntradayInterval.MINUTE_5: 300,
    IntradayInterval.MINUTE_15: 900,
    IntradayInterval.MINUTE_30: 1_800,
    IntradayInterval.MINUTE_45: 2_700,
    IntradayInterval.HOUR_1: 3_600,
    IntradayInterval.HOUR_2: 7_200,
    IntradayInterval.HOUR_4: 14_400,
    IntradayInterval.HOUR_5: 18_000,
}


class IntradayAggregationRequest(ContractModel):
    """Request one fixed-width aggregation over verified minute history."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    query: HistoricalBarQuery
    interval: IntradayInterval

    @model_validator(mode="after")
    def validate_source_frequency(self) -> "IntradayAggregationRequest":
        """Require an explicitly registered one-minute source."""
        try:
            schema = get_market_bar_schema(self.query.source_id)
        except ValueError as error:
            raise ValueError("intraday source is not registered") from error
        if schema.frequency is not DataFrequency.MINUTE_1:
            raise ValueError("intraday aggregation requires a MINUTE_1 source")
        return self


class AggregatedIntradayBar(ContractModel):
    """One auditable fixed UTC bucket derived only from stored minute bars."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    asset_id: NonEmptyStr
    source_id: NonEmptyStr
    source_frequency: Literal[DataFrequency.MINUTE_1] = DataFrequency.MINUTE_1
    interval: IntradayInterval
    period_start: UTCDateTime
    period_end: UTCDateTime
    available_at: UTCDateTime
    source_bar_count: int = Field(ge=1, le=300)
    expected_source_bar_count: int = Field(ge=1, le=300)
    interval_complete: bool
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
    aggregation_algorithm_version: Literal["market-intraday-ohlcv-v1-decimal34"] = (
        INTRADAY_AGGREGATION_ALGORITHM
    )

    @field_validator("interval_complete", mode="before")
    @classmethod
    def require_complete_boolean(cls, value: object) -> object:
        """Reject truthy coercions for interval completeness."""
        if not isinstance(value, bool):
            raise ValueError("interval_complete must be a boolean")
        return value

    @model_validator(mode="after")
    def validate_bucket(self) -> "AggregatedIntradayBar":
        """Keep values, duration, completeness, and evidence counts aligned."""
        if self.period_end - self.period_start != timedelta(seconds=self.interval.seconds):
            raise ValueError("intraday period duration does not match interval")
        if self.expected_source_bar_count != self.interval.expected_source_bars:
            raise ValueError("expected source count does not match interval")
        if self.source_bar_count > self.expected_source_bar_count:
            raise ValueError("source bar count exceeds interval capacity")
        if self.interval_complete != (self.source_bar_count == self.expected_source_bar_count):
            raise ValueError("interval completeness does not match source count")
        if min(self.open, self.high, self.low, self.close) <= 0:
            raise ValueError("intraday prices must be greater than zero")
        if self.low > self.high or not self.low <= self.open <= self.high:
            raise ValueError("intraday open must remain within low and high")
        if not self.low <= self.close <= self.high:
            raise ValueError("intraday close must remain within low and high")
        if self.volume < 0:
            raise ValueError("intraday volume must be non-negative")
        if self.trade_count is not None and (
            self.trade_count < 0 or self.trade_count != self.trade_count.to_integral_value()
        ):
            raise ValueError("intraday trade_count must be a non-negative integer")
        if self.vwap is not None and self.vwap <= 0:
            raise ValueError("intraday vwap must be greater than zero")
        if len(self.raw_record_ids) != self.source_bar_count:
            raise ValueError("raw-record evidence must cover every source bar")
        if len(set(self.raw_record_ids)) != len(self.raw_record_ids):
            raise ValueError("raw-record evidence must be unique")
        if len(self.volume_input_observation_ids) != self.source_bar_count:
            raise ValueError("volume evidence must cover every source bar")
        if self.trade_count is None:
            if self.trade_count_input_observation_ids:
                raise ValueError("missing trade_count must not retain partial evidence")
        elif len(self.trade_count_input_observation_ids) != self.source_bar_count:
            raise ValueError("trade_count evidence must cover every source bar")
        if self.vwap is None:
            if self.vwap_input_observation_ids:
                raise ValueError("missing vwap must not retain partial evidence")
        elif len(self.vwap_input_observation_ids) != self.source_bar_count:
            raise ValueError("vwap evidence must cover every source bar")
        return self


class IntradayAggregationSeries(ContractModel):
    """Complete deterministic output for one intraday aggregation request."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    request: IntradayAggregationRequest
    bars: tuple[AggregatedIntradayBar, ...]
    source_bar_count: int = Field(ge=0)
    complete_interval_count: int = Field(ge=0)
    incomplete_interval_count: int = Field(ge=0)
    traceability_verified: Literal[True] = True

    @model_validator(mode="after")
    def validate_series(self) -> "IntradayAggregationSeries":
        """Keep ordering, scope, and coverage counts deterministic."""
        starts = [bar.period_start for bar in self.bars]
        if starts != sorted(starts) or len(starts) != len(set(starts)):
            raise ValueError("intraday bars must be ordered and unique")
        if sum(bar.source_bar_count for bar in self.bars) != self.source_bar_count:
            raise ValueError("source bar count does not match aggregated evidence")
        complete = sum(bar.interval_complete for bar in self.bars)
        if complete != self.complete_interval_count:
            raise ValueError("complete interval count does not match bars")
        if len(self.bars) - complete != self.incomplete_interval_count:
            raise ValueError("incomplete interval count does not match bars")
        for bar in self.bars:
            if (
                bar.asset_id != self.request.query.asset_id
                or bar.source_id != self.request.query.source_id
                or bar.interval is not self.request.interval
            ):
                raise ValueError("aggregated intraday bar is outside request scope")
        return self

    def to_json_dict(self) -> dict[str, object]:
        """Return exact JSON-compatible values and evidence identifiers."""
        return self.model_dump(mode="json")
