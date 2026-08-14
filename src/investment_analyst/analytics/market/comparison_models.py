"""Strict contracts for deterministic multi-asset daily market comparisons."""

from datetime import UTC, date, datetime, time, timedelta
from typing import Literal
from uuid import UUID

from pydantic import ConfigDict, Field, field_validator, model_validator

from investment_analyst.analytics.market.bar_models import FinancialDecimal
from investment_analyst.core.models.base import ContractModel, NonEmptyStr, UTCDateTime

_MIN_COMMON_DATES = 21
_MAX_COMMON_DATES = 1_300
_MAX_ASSETS = 5


class MarketComparisonRequest(ContractModel):
    """One bounded point-in-time request for daily assets in the local universe."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    asset_ids: tuple[NonEmptyStr, ...]
    benchmark_id: NonEmptyStr
    start_date: date
    end_date: date
    known_at: UTCDateTime

    @model_validator(mode="after")
    def validate_request(self) -> "MarketComparisonRequest":
        if not 2 <= len(self.asset_ids) <= _MAX_ASSETS:
            raise ValueError("asset_ids must contain between 2 and 5 assets")
        if len(set(self.asset_ids)) != len(self.asset_ids):
            raise ValueError("asset_ids must not contain duplicates")
        if self.benchmark_id not in self.asset_ids:
            raise ValueError("benchmark_id must be included in asset_ids")
        if self.start_date > self.end_date:
            raise ValueError("start_date must not be later than end_date")
        return self

    @property
    def end_exclusive(self) -> datetime:
        """Convert the inclusive public date range to a UTC half-open interval."""
        return datetime.combine(self.end_date + timedelta(days=1), time.min, tzinfo=UTC)

    @property
    def start_instant(self) -> datetime:
        """Return the beginning of the public range in UTC."""
        return datetime.combine(self.start_date, time.min, tzinfo=UTC)

    @property
    def canonical_asset_ids(self) -> tuple[str, ...]:
        """Return benchmark first and peers in stable identity order."""
        return (self.benchmark_id,) + tuple(
            sorted(asset_id for asset_id in self.asset_ids if asset_id != self.benchmark_id)
        )


class MarketComparisonAsset(ContractModel):
    """One catalog-resolved daily source used by a comparison query."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    asset_id: NonEmptyStr
    source_id: NonEmptyStr
    quote_currency: NonEmptyStr


class MarketComparisonMetric(ContractModel):
    """Exact descriptive values for one asset over the shared sample."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    total_return: FinancialDecimal
    maximum_drawdown: FinancialDecimal
    daily_volatility: FinancialDecimal
    correlation_to_benchmark: FinancialDecimal | None
    beta_to_benchmark: FinancialDecimal | None
    correlation_status: Literal["available", "unavailable", "not_applicable"]
    beta_status: Literal["available", "unavailable", "not_applicable"]
    missing_requirements: tuple[NonEmptyStr, ...] = ()

    @model_validator(mode="after")
    def validate_statuses(self) -> "MarketComparisonMetric":
        if self.correlation_status == "available" and self.correlation_to_benchmark is None:
            raise ValueError("available correlation requires a value")
        if self.beta_status == "available" and self.beta_to_benchmark is None:
            raise ValueError("available beta requires a value")
        if self.correlation_status != "available" and self.correlation_to_benchmark is not None:
            raise ValueError("unavailable correlation must not expose a value")
        if self.beta_status != "available" and self.beta_to_benchmark is not None:
            raise ValueError("unavailable beta must not expose a value")
        return self


class MarketComparisonPoint(ContractModel):
    """One close and its source evidence in the shared sample."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    date: date
    close: FinancialDecimal
    normalized_close: FinancialDecimal
    close_observation_id: UUID
    available_at: UTCDateTime


class MarketComparisonSeries(ContractModel):
    """A canonical asset series restricted to the common daily calendar."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    asset_id: NonEmptyStr
    source_id: NonEmptyStr
    points: tuple[MarketComparisonPoint, ...]
    metrics: MarketComparisonMetric


class MarketComparisonCoverage(ContractModel):
    """Per-asset source coverage and calendar exclusions."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    asset_id: NonEmptyStr
    source_id: NonEmptyStr
    selected_bar_count: int = Field(ge=0)
    discarded_revisions: int = Field(ge=0)
    discarded_dates: tuple[date, ...]


class MarketMultiAssetComparisonResult(ContractModel):
    """Read-only comparison artifact; it is deliberately not a persisted MetricResult."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["market-multi-asset-comparison-v1"] = "market-multi-asset-comparison-v1"
    comparison_id: UUID
    algorithm_version: Literal["market-multi-asset-comparison-v1-decimal34"] = (
        "market-multi-asset-comparison-v1-decimal34"
    )
    benchmark_id: NonEmptyStr
    asset_ids: tuple[NonEmptyStr, ...]
    known_at: UTCDateTime
    start_date: date
    end_date: date
    quote_currency: NonEmptyStr
    common_dates: tuple[date, ...]
    series: tuple[MarketComparisonSeries, ...]
    coverage: tuple[MarketComparisonCoverage, ...]
    formulas: dict[NonEmptyStr, NonEmptyStr]
    limits: dict[NonEmptyStr, int]
    limitations: tuple[NonEmptyStr, ...]
    traceability_verified: Literal[True] = True

    @field_validator("common_dates")
    @classmethod
    def validate_common_dates(cls, value: tuple[date, ...]) -> tuple[date, ...]:
        if not _MIN_COMMON_DATES <= len(value) <= _MAX_COMMON_DATES:
            raise ValueError("common_dates must contain between 21 and 1300 dates")
        if tuple(sorted(value)) != value or len(set(value)) != len(value):
            raise ValueError("common_dates must be ordered and unique")
        return value

    @model_validator(mode="after")
    def validate_result(self) -> "MarketMultiAssetComparisonResult":
        if self.asset_ids != (self.benchmark_id,) + tuple(sorted(self.asset_ids[1:])):
            raise ValueError("asset_ids must be benchmark first and peers sorted")
        if tuple(item.asset_id for item in self.series) != self.asset_ids:
            raise ValueError("series must follow canonical asset_ids")
        if tuple(item.asset_id for item in self.coverage) != self.asset_ids:
            raise ValueError("coverage must follow canonical asset_ids")
        for item in self.series:
            if tuple(point.date for point in item.points) != self.common_dates:
                raise ValueError("each series must use the exact common_dates sample")
        return self

    def to_json_dict(self) -> dict[str, object]:
        """Return the versioned JSON contract with Decimal values encoded exactly."""
        return self.model_dump(mode="json")
