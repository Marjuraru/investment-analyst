"""Strict application contracts for bounded BTC-USD intraday workflows."""

from datetime import datetime, timedelta
from typing import Literal

from pydantic import ConfigDict, Field, field_validator, model_validator

from investment_analyst.analytics.market.intraday_models import (
    AggregatedIntradayBar,
    IntradayAggregationSeries,
    IntradayInterval,
)
from investment_analyst.core.models.base import ContractModel, UTCDateTime
from investment_analyst.providers.crypto.coinbase_intraday_normalizer import (
    SOURCE_ID as INTRADAY_SOURCE_ID,
)
from investment_analyst.providers.crypto.coinbase_normalizer import ASSET_ID
from investment_analyst.providers.crypto.coinbase_pipeline import CoinbaseImportSummary

BTC_INTRADAY_LOOKBACK_HOURS = 24
MAX_BTC_INTRADAY_CHART_BARS = 1_440


class BtcIntradayChartRequest(ContractModel):
    """Request the latest bounded local intraday window at one explicit cut."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    known_at: UTCDateTime
    interval: IntradayInterval
    lookback_hours: Literal[24] = BTC_INTRADAY_LOOKBACK_HOURS

    @model_validator(mode="after")
    def validate_query_horizon(self) -> "BtcIntradayChartRequest":
        """Require enough post-epoch history for the fixed lookback."""
        if self.query_end.timestamp() < self.lookback_hours * 3_600:
            raise ValueError("known_at is too early for the intraday lookback")
        return self

    @property
    def query_end(self) -> datetime:
        """Exclude the current, potentially incomplete source minute."""
        return self.known_at.replace(second=0, microsecond=0)

    @property
    def query_start(self) -> datetime:
        """Return the inclusive start of the fixed local lookback."""
        return self.query_end - timedelta(hours=self.lookback_hours)


class BtcIntradayChart(ContractModel):
    """Versioned web projection of locally aggregated one-minute evidence."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["btc-intraday-chart-v1"] = "btc-intraday-chart-v1"
    asset_id: Literal["crypto:btc-usd"] = ASSET_ID
    source_id: Literal["coinbase-exchange:btc-usd:minute-1-candles"] = INTRADAY_SOURCE_ID
    known_at: UTCDateTime
    start: UTCDateTime
    end: UTCDateTime
    lookback_hours: Literal[24] = BTC_INTRADAY_LOOKBACK_HOURS
    interval: IntradayInterval
    bars: tuple[AggregatedIntradayBar, ...]
    source_bar_count: int = Field(ge=0, le=MAX_BTC_INTRADAY_CHART_BARS)
    complete_interval_count: int = Field(ge=0, le=MAX_BTC_INTRADAY_CHART_BARS)
    incomplete_interval_count: int = Field(ge=0, le=MAX_BTC_INTRADAY_CHART_BARS)
    traceability_verified: Literal[True] = True

    @classmethod
    def from_series(
        cls,
        request: BtcIntradayChartRequest,
        series: IntradayAggregationSeries,
    ) -> "BtcIntradayChart":
        """Build the stable projection without dropping aggregation evidence."""
        return cls(
            known_at=request.known_at,
            start=request.query_start,
            end=request.query_end,
            interval=request.interval,
            bars=series.bars,
            source_bar_count=series.source_bar_count,
            complete_interval_count=series.complete_interval_count,
            incomplete_interval_count=series.incomplete_interval_count,
        )

    @model_validator(mode="after")
    def validate_projection(self) -> "BtcIntradayChart":
        """Keep range, counts, scope, and evidence internally aligned."""
        if self.end - self.start != timedelta(hours=self.lookback_hours):
            raise ValueError("intraday chart range does not match its lookback")
        if self.end > self.known_at:
            raise ValueError("intraday chart end must not be later than known_at")
        if len(self.bars) != self.complete_interval_count + self.incomplete_interval_count:
            raise ValueError("intraday chart interval counts do not match bars")
        if sum(bar.source_bar_count for bar in self.bars) != self.source_bar_count:
            raise ValueError("intraday chart source count does not match bars")
        for bar in self.bars:
            if (
                bar.asset_id != self.asset_id
                or bar.source_id != self.source_id
                or bar.interval is not self.interval
                or bar.period_end <= self.start
                or bar.period_start >= self.end
                or bar.available_at > self.known_at
            ):
                raise ValueError("intraday chart bar is outside the requested scope")
        return self

    def to_json_dict(self) -> dict[str, object]:
        """Return exact JSON-compatible values and evidence identifiers."""
        return self.model_dump(mode="json")


class BtcIntradayRefreshRequest(ContractModel):
    """Request one explicit, bounded refresh of recent one-minute evidence."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    asset_id: Literal["crypto:btc-usd"] = ASSET_ID
    hours: Literal[24] = BTC_INTRADAY_LOOKBACK_HOURS
    requested_end: UTCDateTime | None = None

    @field_validator("requested_end")
    @classmethod
    def validate_requested_end(cls, value: datetime | None) -> datetime | None:
        """Keep provider ranges aligned to complete UTC source minutes."""
        if value is not None and (value.second != 0 or value.microsecond != 0):
            raise ValueError("requested_end must align to a whole UTC minute")
        return value


class BtcIntradayRefreshSummary(ContractModel):
    """Compact, versioned outcome of one append-only intraday import."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["btc-intraday-refresh-v1"] = "btc-intraday-refresh-v1"
    asset_id: Literal["crypto:btc-usd"] = ASSET_ID
    source_id: Literal["coinbase-exchange:btc-usd:minute-1-candles"] = INTRADAY_SOURCE_ID
    requested_start: UTCDateTime
    requested_end: UTCDateTime
    retrieved_at: UTCDateTime
    request_count: int = Field(ge=1, le=5)
    candles_received: int = Field(ge=0, le=MAX_BTC_INTRADAY_CHART_BARS)
    raw_records_created: int = Field(ge=0, le=MAX_BTC_INTRADAY_CHART_BARS)
    raw_records_reused: int = Field(ge=0, le=MAX_BTC_INTRADAY_CHART_BARS)
    observations_created: int = Field(ge=0, le=MAX_BTC_INTRADAY_CHART_BARS * 5)
    observations_reused: int = Field(ge=0, le=MAX_BTC_INTRADAY_CHART_BARS * 5)
    missing_intervals: tuple[UTCDateTime, ...]
    earliest_candle: UTCDateTime | None = None
    latest_candle: UTCDateTime | None = None
    traceability_verified: Literal[True] = True

    @classmethod
    def from_import(cls, summary: CoinbaseImportSummary) -> "BtcIntradayRefreshSummary":
        """Convert the provider result into the stable application contract."""
        return cls(**summary.to_json_dict())

    @model_validator(mode="after")
    def validate_refresh(self) -> "BtcIntradayRefreshSummary":
        """Verify fixed duration, persistence counts, and returned coverage."""
        if self.requested_end - self.requested_start != timedelta(hours=24):
            raise ValueError("intraday refresh must cover exactly 24 hours")
        if any(
            value.second != 0 or value.microsecond != 0
            for value in (self.requested_start, self.requested_end)
        ):
            raise ValueError("intraday refresh range must align to whole UTC minutes")
        if self.retrieved_at < self.requested_end:
            raise ValueError("intraday retrieval must not predate the requested range")
        if self.raw_records_created + self.raw_records_reused != self.candles_received:
            raise ValueError("intraday raw-record counts do not match received candles")
        if self.observations_created + self.observations_reused != self.candles_received * 5:
            raise ValueError("intraday observation counts do not match received candles")
        if self.candles_received + len(self.missing_intervals) != MAX_BTC_INTRADAY_CHART_BARS:
            raise ValueError("intraday refresh coverage does not match the requested range")
        if (
            tuple(sorted(self.missing_intervals)) != self.missing_intervals
            or len(set(self.missing_intervals)) != len(self.missing_intervals)
            or any(
                value.second != 0
                or value.microsecond != 0
                or not self.requested_start <= value < self.requested_end
                for value in self.missing_intervals
            )
        ):
            raise ValueError("intraday missing intervals are invalid")
        if self.candles_received == 0:
            if self.earliest_candle is not None or self.latest_candle is not None:
                raise ValueError("empty intraday refresh must not define candle bounds")
        elif self.earliest_candle is None or self.latest_candle is None:
            raise ValueError("non-empty intraday refresh requires candle bounds")
        elif (
            self.earliest_candle > self.latest_candle
            or not self.requested_start <= self.earliest_candle < self.requested_end
            or not self.requested_start <= self.latest_candle < self.requested_end
        ):
            raise ValueError("intraday candle bounds are outside the requested range")
        return self

    def to_json_dict(self) -> dict[str, object]:
        """Return exact JSON-compatible values and idempotence counters."""
        return self.model_dump(mode="json")
