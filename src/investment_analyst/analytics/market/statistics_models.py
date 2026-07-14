"""Strict models for deterministic point-in-time market statistics."""

import json
from decimal import Decimal
from typing import Annotated
from uuid import UUID

from pydantic import BeforeValidator, ConfigDict, Field, JsonValue, field_validator, model_validator

from investment_analyst.analytics.market.bar_models import HistoricalBarQuery
from investment_analyst.core.models.base import ContractModel, NonEmptyStr, UTCDateTime
from investment_analyst.core.models.enums import DataQuality

_MAX_WINDOW = 10_000


def _reject_financial_float(value: object) -> object:
    if isinstance(value, (bool, float)):
        raise ValueError("financial values must use Decimal, not float or bool")
    return value


def _validate_window(value: object, *, minimum: int, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer")
    if value < minimum:
        raise ValueError(f"{name} must be at least {minimum}")
    if value > _MAX_WINDOW:
        raise ValueError(f"{name} must not exceed {_MAX_WINDOW}")
    return value


FinancialDecimal = Annotated[Decimal, BeforeValidator(_reject_financial_float)]


class MarketStatisticsRequest(ContractModel):
    """Parameters for statistics computed from one point-in-time bar query."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    query: HistoricalBarQuery
    sma_windows: tuple[int, ...] = (5, 20)
    volatility_window: int = 20
    relative_volume_window: int = 20

    @field_validator("sma_windows", mode="before")
    @classmethod
    def validate_sma_windows(cls, value: object) -> tuple[int, ...]:
        """Validate, deduplicate defensively, and sort requested SMA windows."""
        if not isinstance(value, (tuple, list)):
            raise ValueError("sma_windows must be a tuple or list of integers")
        windows = tuple(_validate_window(item, minimum=1, name="SMA window") for item in value)
        if not windows:
            raise ValueError("sma_windows must not be empty")
        if len(set(windows)) != len(windows):
            raise ValueError("sma_windows must be unique")
        return tuple(sorted(windows))

    @field_validator("volatility_window", mode="before")
    @classmethod
    def validate_volatility_window(cls, value: object) -> int:
        """Require a valid sample-volatility window."""
        return _validate_window(value, minimum=2, name="volatility_window")

    @field_validator("relative_volume_window", mode="before")
    @classmethod
    def validate_relative_volume_window(cls, value: object) -> int:
        """Require a valid historical-volume window."""
        return _validate_window(value, minimum=1, name="relative_volume_window")


class MetricCalculation(ContractModel):
    """Validated analytical output before persistence assigns identity and computation time."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    asset_id: NonEmptyStr
    source_id: NonEmptyStr
    metric_key: NonEmptyStr
    value: FinancialDecimal
    unit: NonEmptyStr
    as_of: UTCDateTime
    available_at: UTCDateTime
    parameters: dict[NonEmptyStr, JsonValue]
    input_observation_ids: tuple[UUID, ...]
    algorithm_version: NonEmptyStr
    quality: DataQuality

    @field_validator("value")
    @classmethod
    def validate_value(cls, value: Decimal) -> Decimal:
        """Reject NaN and infinite analytical values."""
        if not value.is_finite():
            raise ValueError("metric value must be finite")
        return value

    @model_validator(mode="after")
    def validate_traceability(self) -> "MetricCalculation":
        """Require unique input observations and JSON-compatible parameters."""
        if not self.input_observation_ids:
            raise ValueError("at least one input observation ID is required")
        if len(set(self.input_observation_ids)) != len(self.input_observation_ids):
            raise ValueError("input observation IDs must be unique")
        try:
            json.dumps(self.parameters, allow_nan=False, sort_keys=True)
        except (TypeError, ValueError) as error:
            raise ValueError("parameters must be JSON serializable") from error
        return self


class MarketStatisticsComputation(ContractModel):
    """Complete in-memory output of the deterministic statistics engine."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    request: MarketStatisticsRequest
    bar_count: int = Field(ge=0)
    calculations: tuple[MetricCalculation, ...]
    calculation_counts: dict[NonEmptyStr, int]
    warmup_counts: dict[NonEmptyStr, int]
    zero_denominator_skips: dict[NonEmptyStr, int]
    traceability_verified: bool

    @model_validator(mode="after")
    def validate_computation(self) -> "MarketStatisticsComputation":
        """Only expose computations whose traceability was verified."""
        if not self.traceability_verified:
            raise ValueError("traceability_verified must be true")
        for mapping in (
            self.calculation_counts,
            self.warmup_counts,
            self.zero_denominator_skips,
        ):
            if any(value < 0 for value in mapping.values()):
                raise ValueError("computation counts must be non-negative")
        return self

    def to_json_dict(self) -> dict[str, object]:
        """Return an explicit JSON-compatible representation."""
        return {
            "request": self.request.model_dump(mode="json"),
            "bar_count": self.bar_count,
            "calculations": [item.model_dump(mode="json") for item in self.calculations],
            "calculation_counts": dict(self.calculation_counts),
            "warmup_counts": dict(self.warmup_counts),
            "zero_denominator_skips": dict(self.zero_denominator_skips),
            "traceability_verified": self.traceability_verified,
        }


class MarketStatisticsRunSummary(ContractModel):
    """Auditable summary of one statistics persistence run."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    asset_id: NonEmptyStr
    source_id: NonEmptyStr
    requested_start: UTCDateTime
    requested_end: UTCDateTime
    known_at: UTCDateTime
    computed_at: UTCDateTime
    bar_count: int = Field(ge=0)
    definitions_upserted: int = Field(ge=0)
    results_generated: int = Field(ge=0)
    results_created: int = Field(ge=0)
    results_reused: int = Field(ge=0)
    result_counts: dict[NonEmptyStr, int]
    warmup_counts: dict[NonEmptyStr, int]
    zero_denominator_skips: dict[NonEmptyStr, int]
    earliest_as_of: UTCDateTime | None = None
    latest_as_of: UTCDateTime | None = None
    traceability_verified: bool

    @model_validator(mode="after")
    def validate_summary(self) -> "MarketStatisticsRunSummary":
        """Keep generated, created, reused, and timestamp counts consistent."""
        if self.results_generated != self.results_created + self.results_reused:
            raise ValueError("results_generated must equal created plus reused")
        if self.results_generated == 0:
            if self.earliest_as_of is not None or self.latest_as_of is not None:
                raise ValueError("empty runs must not define result timestamps")
        elif self.earliest_as_of is None or self.latest_as_of is None:
            raise ValueError("non-empty runs require earliest and latest timestamps")
        elif self.earliest_as_of > self.latest_as_of:
            raise ValueError("earliest_as_of must not be later than latest_as_of")
        if not self.traceability_verified:
            raise ValueError("traceability_verified must be true")
        return self

    def to_json_dict(self) -> dict[str, object]:
        """Return an explicit JSON-compatible representation."""
        return self.model_dump(mode="json")
