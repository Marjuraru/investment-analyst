"""Provider-independent analytical models for stored daily market bars."""

from decimal import Decimal
from typing import Annotated
from uuid import UUID

from pydantic import BeforeValidator, ConfigDict, Field, field_validator, model_validator

from investment_analyst.core.models.base import ContractModel, NonEmptyStr, UTCDateTime
from investment_analyst.core.models.enums import DataFrequency, DataQuality

_BAR_FIELDS = frozenset({"open", "high", "low", "close", "volume", "trade_count", "vwap"})
_BASE_FIELDS = frozenset({"open", "high", "low", "close", "volume"})


def _reject_float(value: object) -> object:
    if isinstance(value, (bool, float)):
        raise ValueError("financial values must not use float or bool")
    return value


FinancialDecimal = Annotated[Decimal, BeforeValidator(_reject_float)]


class HistoricalBarQuery(ContractModel):
    """Point-in-time request for one asset and one explicit market source."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
    )

    asset_id: NonEmptyStr
    source_id: NonEmptyStr
    start: UTCDateTime
    end: UTCDateTime
    known_at: UTCDateTime

    @model_validator(mode="after")
    def validate_range(self) -> "HistoricalBarQuery":
        """Require a non-empty half-open timestamp range."""
        if self.start >= self.end:
            raise ValueError("start must be earlier than end")
        return self


class MarketBar(ContractModel):
    """One complete provider-specific bar exposed through a common analytical shape."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
    )

    asset_id: NonEmptyStr
    source_id: NonEmptyStr
    raw_record_id: UUID
    frequency: DataFrequency
    timestamp: UTCDateTime
    available_at: UTCDateTime
    open: FinancialDecimal
    high: FinancialDecimal
    low: FinancialDecimal
    close: FinancialDecimal
    volume: FinancialDecimal
    trade_count: FinancialDecimal | None = None
    vwap: FinancialDecimal | None = None
    quality: DataQuality
    observation_ids: dict[NonEmptyStr, UUID]

    @field_validator("open", "high", "low", "close", "volume", "trade_count", "vwap")
    @classmethod
    def validate_finite_decimal(cls, value: Decimal | None) -> Decimal | None:
        """Reject non-finite decimal values."""
        if value is not None and not value.is_finite():
            raise ValueError("market bar numeric values must be finite")
        return value

    @model_validator(mode="after")
    def validate_bar(self) -> "MarketBar":
        """Validate daily OHLCV consistency and exact observation traceability."""
        if self.frequency is not DataFrequency.DAY_1:
            raise ValueError("MarketBar frequency must be DAY_1")
        if min(self.open, self.high, self.low, self.close) <= 0:
            raise ValueError("market bar prices must be greater than zero")
        if self.volume < 0:
            raise ValueError("volume must be non-negative")
        if self.low > self.high:
            raise ValueError("low must not be greater than high")
        if not self.low <= self.open <= self.high:
            raise ValueError("open must be within low and high")
        if not self.low <= self.close <= self.high:
            raise ValueError("close must be within low and high")
        if self.trade_count is not None and (
            self.trade_count < 0 or self.trade_count != self.trade_count.to_integral_value()
        ):
            raise ValueError("trade_count must be a non-negative integer")
        if self.vwap is not None and self.vwap <= 0:
            raise ValueError("vwap must be greater than zero")

        supplied_fields = set(self.observation_ids)
        if not supplied_fields <= _BAR_FIELDS:
            raise ValueError("observation_ids contains an unknown market bar field")
        expected_fields = set(_BASE_FIELDS)
        if self.trade_count is not None:
            expected_fields.add("trade_count")
        if self.vwap is not None:
            expected_fields.add("vwap")
        if supplied_fields != expected_fields:
            raise ValueError("observation_ids must match exactly the numeric fields in the bar")
        return self


class MarketBarCoverage(ContractModel):
    """Counts describing candidate revisions and selected point-in-time bars."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    candidate_versions: int = Field(ge=0)
    selected_versions: int = Field(ge=0)
    discarded_revisions: int = Field(ge=0)
    bar_count: int = Field(ge=0)
    earliest_timestamp: UTCDateTime | None = None
    latest_timestamp: UTCDateTime | None = None

    @model_validator(mode="after")
    def validate_counts(self) -> "MarketBarCoverage":
        """Keep revision and selected-bar counts internally consistent."""
        if self.candidate_versions != self.selected_versions + self.discarded_revisions:
            raise ValueError("candidate_versions must equal selected plus discarded versions")
        if self.bar_count != self.selected_versions:
            raise ValueError("bar_count must equal selected_versions")
        if self.bar_count == 0:
            if self.earliest_timestamp is not None or self.latest_timestamp is not None:
                raise ValueError("empty coverage must not define timestamps")
        elif self.earliest_timestamp is None or self.latest_timestamp is None:
            raise ValueError("non-empty coverage requires earliest and latest timestamps")
        elif self.earliest_timestamp > self.latest_timestamp:
            raise ValueError("earliest_timestamp must not be later than latest_timestamp")
        return self


class MarketBarSeries(ContractModel):
    """Immutable, ordered, point-in-time series with verified traceability."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    query: HistoricalBarQuery
    bars: tuple[MarketBar, ...]
    coverage: MarketBarCoverage
    traceability_verified: bool

    @model_validator(mode="after")
    def validate_series(self) -> "MarketBarSeries":
        """Validate ordering, scope, availability, and coverage."""
        if not self.traceability_verified:
            raise ValueError("traceability_verified must be true")
        timestamps = [bar.timestamp for bar in self.bars]
        if timestamps != sorted(timestamps):
            raise ValueError("bars must be ordered by timestamp")
        if len(timestamps) != len(set(timestamps)):
            raise ValueError("bars must not contain duplicate timestamps")
        for bar in self.bars:
            if bar.asset_id != self.query.asset_id or bar.source_id != self.query.source_id:
                raise ValueError("all bars must match the query asset and source")
            if not self.query.start <= bar.timestamp < self.query.end:
                raise ValueError("bar timestamp is outside the query range")
            if bar.available_at > self.query.known_at:
                raise ValueError("bar was not available at known_at")
        if self.coverage.bar_count != len(self.bars):
            raise ValueError("coverage bar_count must match the number of bars")
        if self.bars:
            if self.coverage.earliest_timestamp != self.bars[0].timestamp:
                raise ValueError("coverage earliest_timestamp does not match bars")
            if self.coverage.latest_timestamp != self.bars[-1].timestamp:
                raise ValueError("coverage latest_timestamp does not match bars")
        return self

    def to_json_dict(self) -> dict[str, object]:
        """Return a stable JSON-compatible representation without opaque dataclass conversion."""
        return {
            "query": self.query.model_dump(mode="json"),
            "bars": [bar.model_dump(mode="json") for bar in self.bars],
            "coverage": self.coverage.model_dump(mode="json"),
            "traceability_verified": self.traceability_verified,
        }
