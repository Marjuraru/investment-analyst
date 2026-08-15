"""Strict, read-only contracts for materialized corporate valuation history."""

from datetime import date
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import ConfigDict, Field, field_validator, model_validator

from investment_analyst.core.models.base import ContractModel, NonEmptyStr, UTCDateTime


class CorporateValuationHistoryRequest(ContractModel):
    """One bounded point-in-time query over persisted valuation results."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    schema_version: Literal["corporate-valuation-history-request-v1"] = (
        "corporate-valuation-history-request-v1"
    )
    asset_id: NonEmptyStr
    known_at: UTCDateTime
    start_date: date
    end_date: date
    basis: Literal["latest_annual"] = "latest_annual"
    limit: int = Field(default=250, ge=1, le=500)

    @field_validator("start_date", "end_date", mode="before")
    @classmethod
    def require_date(cls, value: object) -> object:
        return date.fromisoformat(value) if isinstance(value, str) else value

    @field_validator("limit", mode="before")
    @classmethod
    def reject_boolean_limit(cls, value: object) -> object:
        if isinstance(value, bool):
            raise ValueError("limit must be an integer")
        return value

    @model_validator(mode="after")
    def validate_range(self) -> "CorporateValuationHistoryRequest":
        if self.end_date < self.start_date:
            raise ValueError("end_date must not precede start_date")
        return self


class CorporateValuationHistoryPoint(ContractModel):
    """One selected, materialized valuation result with exact traceability."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    metric_key: NonEmptyStr
    algorithm_version: NonEmptyStr
    unit: NonEmptyStr
    basis: Literal["latest_annual"]
    security_basis_version: NonEmptyStr
    valuation_date: date
    price_as_of: UTCDateTime
    annual_period_end: UTCDateTime
    source_known_at: UTCDateTime
    available_at: UTCDateTime
    result_id: UUID
    value: Decimal
    input_observation_ids: tuple[UUID, ...]


class CorporateValuationHistoryStatistics(ContractModel):
    """Transparent Decimal-only descriptive statistics for one sparse series."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    count: int = Field(ge=1)
    first_value: Decimal
    last_value: Decimal
    minimum: Decimal
    maximum: Decimal
    arithmetic_mean: Decimal
    value_range: Decimal
    previous_change: Decimal | None = None
    horizon_change: Decimal | None = None


class CorporateValuationHistorySeries(ContractModel):
    """A homogeneous metric/unit/version series, ordered by valuation date."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    metric_key: NonEmptyStr
    algorithm_version: NonEmptyStr
    unit: NonEmptyStr
    basis: Literal["latest_annual"]
    security_basis_version: NonEmptyStr
    points: tuple[CorporateValuationHistoryPoint, ...]
    statistics: CorporateValuationHistoryStatistics

    @model_validator(mode="after")
    def validate_points(self) -> "CorporateValuationHistorySeries":
        ordering = tuple((point.valuation_date, str(point.result_id)) for point in self.points)
        if not self.points or ordering != tuple(sorted(ordering)):
            raise ValueError("history points must be nonempty and deterministically ordered")
        if any(
            point.metric_key != self.metric_key
            or point.algorithm_version != self.algorithm_version
            or point.unit != self.unit
            or point.basis != self.basis
            or point.security_basis_version != self.security_basis_version
            for point in self.points
        ):
            raise ValueError("history series must not mix metric semantics")
        if self.statistics.count != len(self.points):
            raise ValueError("history statistics count must match points")
        return self


class CorporateValuationHistoryCoverage(ContractModel):
    """Bounded coverage without inventing unavailable historical dates."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    candidate_results: int = Field(ge=0)
    superseded_revisions: int = Field(ge=0)
    returned_points: int = Field(ge=0)
    returned_series: int = Field(ge=0)
    truncated: bool


class CorporateValuationHistory(ContractModel):
    """Versioned read-only history response without backfill claims."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["corporate-valuation-history-v1"] = "corporate-valuation-history-v1"
    request: CorporateValuationHistoryRequest
    series: tuple[CorporateValuationHistorySeries, ...]
    coverage: CorporateValuationHistoryCoverage
