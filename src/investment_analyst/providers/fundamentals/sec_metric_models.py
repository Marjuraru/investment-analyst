"""Strict models for auditable Apple SEC fundamental metrics."""

from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import Annotated
from uuid import UUID

from pydantic import BeforeValidator, ConfigDict, Field, field_validator, model_validator

from investment_analyst.core.models import DataFrequency, DataQuality
from investment_analyst.core.models.base import ContractModel, NonEmptyStr, UTCDateTime
from investment_analyst.providers.fundamentals.sec_fact_models import ASSET_ID
from investment_analyst.providers.fundamentals.sec_query_models import (
    SecFundamentalPointInTimeResult,
)

_MAX_LIMIT = 100
_ALLOWED_FREQUENCIES = frozenset({DataFrequency.ANNUAL, DataFrequency.QUARTERLY})


def _reject_financial_float(value: object) -> object:
    if isinstance(value, (bool, float)):
        raise ValueError("financial values must use Decimal, not float or bool")
    return value


FinancialDecimal = Annotated[Decimal, BeforeValidator(_reject_financial_float)]


class SecMetricComparison(StrEnum):
    """Relationship between the current inputs used by a metric."""

    SAME_PERIOD = "same_period"
    YEAR_OVER_YEAR = "year_over_year"


class SecFundamentalMetricRequest(ContractModel):
    """Point-in-time request for Apple fundamental metrics."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    asset_id: NonEmptyStr = ASSET_ID
    known_at: UTCDateTime
    frequency: DataFrequency
    start_period_end: date | None = None
    end_period_end: date | None = None
    limit: int | None = Field(default=None, ge=1, le=_MAX_LIMIT)

    @field_validator("limit", mode="before")
    @classmethod
    def reject_boolean_limit(cls, value: object) -> object:
        """Reject booleans masquerading as integer limits."""
        if isinstance(value, bool):
            raise ValueError("limit must be an integer between 1 and 100")
        return value

    @model_validator(mode="after")
    def validate_scope(self) -> "SecFundamentalMetricRequest":
        """Validate the fixed Apple scope and inclusive period range."""
        if self.asset_id != ASSET_ID:
            raise ValueError("asset_id must identify Apple")
        if self.frequency not in _ALLOWED_FREQUENCIES:
            raise ValueError("frequency must be annual or quarterly")
        if (
            self.start_period_end is not None
            and self.end_period_end is not None
            and self.start_period_end > self.end_period_end
        ):
            raise ValueError("start_period_end must not be later than end_period_end")
        return self


class SecFundamentalMetricDefinition(ContractModel):
    """Explicit contract for one deterministic fundamental metric."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    metric_name: NonEmptyStr
    formula: NonEmptyStr
    required_fields: tuple[NonEmptyStr, ...]
    comparison: SecMetricComparison
    unit: NonEmptyStr
    algorithm_version: NonEmptyStr

    @model_validator(mode="after")
    def validate_definition(self) -> "SecFundamentalMetricDefinition":
        """Require unique inputs and ratio output units."""
        if not self.required_fields:
            raise ValueError("required_fields must not be empty")
        if len(set(self.required_fields)) != len(self.required_fields):
            raise ValueError("required_fields must be unique")
        if self.unit != "ratio":
            raise ValueError("fundamental metric unit must be ratio")
        return self


SEC_FUNDAMENTAL_METRIC_DEFINITIONS = (
    SecFundamentalMetricDefinition(
        metric_name="fundamental.net_margin",
        formula="net_income / revenue",
        required_fields=("fundamental.net_income", "fundamental.revenue"),
        comparison=SecMetricComparison.SAME_PERIOD,
        unit="ratio",
        algorithm_version="sec-fundamental-net-margin-v1-decimal34",
    ),
    SecFundamentalMetricDefinition(
        metric_name="fundamental.liabilities_to_assets",
        formula="liabilities / assets",
        required_fields=("fundamental.liabilities", "fundamental.assets"),
        comparison=SecMetricComparison.SAME_PERIOD,
        unit="ratio",
        algorithm_version="sec-fundamental-liabilities-to-assets-v1-decimal34",
    ),
    SecFundamentalMetricDefinition(
        metric_name="fundamental.liabilities_to_equity",
        formula="liabilities / stockholders_equity",
        required_fields=(
            "fundamental.liabilities",
            "fundamental.stockholders_equity",
        ),
        comparison=SecMetricComparison.SAME_PERIOD,
        unit="ratio",
        algorithm_version="sec-fundamental-liabilities-to-equity-v1-decimal34",
    ),
    SecFundamentalMetricDefinition(
        metric_name="fundamental.revenue_yoy_growth",
        formula="current_revenue / previous_revenue - 1",
        required_fields=("fundamental.revenue",),
        comparison=SecMetricComparison.YEAR_OVER_YEAR,
        unit="ratio",
        algorithm_version="sec-fundamental-revenue-yoy-growth-v1-decimal34",
    ),
    SecFundamentalMetricDefinition(
        metric_name="fundamental.net_income_yoy_change_rate",
        formula="(current_net_income - previous_net_income) / abs(previous_net_income)",
        required_fields=("fundamental.net_income",),
        comparison=SecMetricComparison.YEAR_OVER_YEAR,
        unit="ratio",
        algorithm_version="sec-fundamental-net-income-yoy-change-rate-v1-decimal34",
    ),
)
_DEFINITION_BY_NAME = {
    definition.metric_name: definition for definition in SEC_FUNDAMENTAL_METRIC_DEFINITIONS
}


class SecFundamentalMetricInput(ContractModel):
    """One observation identity assigned to a deterministic analytical role."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    role: NonEmptyStr
    observation_id: UUID


class SecFundamentalMetricCandidate(ContractModel):
    """Validated fundamental calculation before persistence."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    asset_id: NonEmptyStr
    metric_name: NonEmptyStr
    value: FinancialDecimal
    unit: NonEmptyStr
    frequency: DataFrequency
    period_end: UTCDateTime
    available_at: UTCDateTime
    input_roles: tuple[SecFundamentalMetricInput, ...]
    formula: NonEmptyStr
    algorithm_version: NonEmptyStr
    comparison: SecMetricComparison
    fiscal_year: NonEmptyStr | None = None
    fiscal_period: NonEmptyStr | None = None
    quality: DataQuality

    @model_validator(mode="after")
    def validate_candidate(self) -> "SecFundamentalMetricCandidate":
        """Validate metric contract, finite value, and deterministic input ordering."""
        definition = _DEFINITION_BY_NAME.get(self.metric_name)
        if definition is None:
            raise ValueError("metric_name is not supported")
        if self.asset_id != ASSET_ID:
            raise ValueError("asset_id must identify Apple")
        if self.frequency not in _ALLOWED_FREQUENCIES:
            raise ValueError("frequency must be annual or quarterly")
        if self.unit != definition.unit or self.formula != definition.formula:
            raise ValueError("candidate does not match its metric definition")
        if self.algorithm_version != definition.algorithm_version:
            raise ValueError("candidate algorithm version is incorrect")
        if self.comparison is not definition.comparison:
            raise ValueError("candidate comparison is incorrect")
        if not self.value.is_finite():
            raise ValueError("metric value must be finite")
        if self.quality is not DataQuality.VALID:
            raise ValueError("SEC fundamental metrics must have VALID quality")
        roles = tuple(item.role for item in self.input_roles)
        identifiers = tuple(item.observation_id for item in self.input_roles)
        if not roles:
            raise ValueError("at least one input role is required")
        if roles != tuple(sorted(roles)):
            raise ValueError("input roles must be ordered by role")
        if len(set(roles)) != len(roles):
            raise ValueError("input roles must be unique")
        if len(set(identifiers)) != len(identifiers):
            raise ValueError("input observation IDs must be unique")
        return self

    def input_observation_ids(self) -> tuple[UUID, ...]:
        """Return observation IDs in deterministic role order."""
        return tuple(item.observation_id for item in self.input_roles)

    def to_json_dict(self) -> dict[str, object]:
        """Return a compact JSON-compatible candidate representation."""
        return {
            "asset_id": self.asset_id,
            "metric_name": self.metric_name,
            "value": str(self.value),
            "unit": self.unit,
            "frequency": self.frequency.value,
            "period_end": self.period_end.isoformat(),
            "available_at": self.available_at.isoformat(),
            "input_roles": [item.model_dump(mode="json") for item in self.input_roles],
            "formula": self.formula,
            "algorithm_version": self.algorithm_version,
            "comparison": self.comparison.value,
            "fiscal_year": self.fiscal_year,
            "fiscal_period": self.fiscal_period,
            "quality": self.quality.value,
        }


class SecFundamentalMetricComputation(ContractModel):
    """Complete in-memory output of the fundamental metric engine."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    request: SecFundamentalMetricRequest
    source_result: SecFundamentalPointInTimeResult
    candidates: tuple[SecFundamentalMetricCandidate, ...]
    metric_counts: dict[NonEmptyStr, int]
    skipped_counts: dict[NonEmptyStr, int]
    target_periods: tuple[UTCDateTime, ...]
    traceability_verified: bool

    @model_validator(mode="after")
    def validate_computation(self) -> "SecFundamentalMetricComputation":
        """Validate deterministic ordering and count consistency."""
        ordering = tuple(
            (candidate.period_end, candidate.metric_name) for candidate in self.candidates
        )
        if ordering != tuple(sorted(ordering)):
            raise ValueError("candidates must be ordered by period and metric name")
        if self.target_periods != tuple(sorted(self.target_periods)):
            raise ValueError("target periods must be ordered chronologically")
        if len(set(self.target_periods)) != len(self.target_periods):
            raise ValueError("target periods must be unique")
        if sum(self.metric_counts.values()) != len(self.candidates):
            raise ValueError("metric_counts must match candidates")
        if any(value < 0 for value in self.skipped_counts.values()):
            raise ValueError("skipped counts must be non-negative")
        if not self.traceability_verified:
            raise ValueError("traceability_verified must be true")
        return self

    def to_json_dict(self) -> dict[str, object]:
        """Return an explicit JSON-compatible representation."""
        return {
            "request": self.request.model_dump(mode="json"),
            "source_result": self.source_result.to_json_dict(),
            "candidates": [item.to_json_dict() for item in self.candidates],
            "metric_counts": dict(self.metric_counts),
            "skipped_counts": dict(self.skipped_counts),
            "target_periods": [item.isoformat() for item in self.target_periods],
            "traceability_verified": self.traceability_verified,
        }


class SecFundamentalMetricImportSummary(ContractModel):
    """Auditable summary of one fundamental metric persistence run."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    asset_id: NonEmptyStr
    known_at: UTCDateTime
    frequency: DataFrequency
    computed_at: UTCDateTime
    periods_examined: int = Field(ge=0)
    target_periods: int = Field(ge=0)
    complete_periods: int = Field(ge=0)
    incomplete_periods: int = Field(ge=0)
    metrics_generated: int = Field(ge=0)
    metrics_created: int = Field(ge=0)
    metrics_reused: int = Field(ge=0)
    metric_counts: dict[NonEmptyStr, int]
    skipped_counts: dict[NonEmptyStr, int]
    earliest_period_end: UTCDateTime | None = None
    latest_period_end: UTCDateTime | None = None
    raw_records_created: int = Field(ge=0)
    observations_created: int = Field(ge=0)
    diagnostics_created: int = Field(ge=0)
    traceability_verified: bool

    @model_validator(mode="after")
    def validate_summary(self) -> "SecFundamentalMetricImportSummary":
        """Keep period, metric, and side-effect counts consistent."""
        if self.target_periods != self.complete_periods + self.incomplete_periods:
            raise ValueError("target periods must equal complete plus incomplete periods")
        if self.metrics_generated != self.metrics_created + self.metrics_reused:
            raise ValueError("generated metrics must equal created plus reused metrics")
        if sum(self.metric_counts.values()) != self.metrics_generated:
            raise ValueError("metric_counts must match generated metrics")
        if self.target_periods == 0:
            if self.earliest_period_end is not None or self.latest_period_end is not None:
                raise ValueError("empty target ranges must not define period bounds")
        elif self.earliest_period_end is None or self.latest_period_end is None:
            raise ValueError("non-empty target ranges require period bounds")
        elif self.earliest_period_end > self.latest_period_end:
            raise ValueError("earliest_period_end must not exceed latest_period_end")
        if any(
            value != 0
            for value in (
                self.raw_records_created,
                self.observations_created,
                self.diagnostics_created,
            )
        ):
            raise ValueError("fundamental metrics must not create source data or diagnostics")
        if not self.traceability_verified:
            raise ValueError("traceability_verified must be true")
        return self

    def to_json_dict(self) -> dict[str, object]:
        """Return an explicit compact JSON-compatible summary."""
        return {
            "asset_id": self.asset_id,
            "known_at": self.known_at.isoformat(),
            "frequency": self.frequency.value,
            "computed_at": self.computed_at.isoformat(),
            "periods_examined": self.periods_examined,
            "target_periods": self.target_periods,
            "complete_periods": self.complete_periods,
            "incomplete_periods": self.incomplete_periods,
            "metrics_generated": self.metrics_generated,
            "metrics_created": self.metrics_created,
            "metrics_reused": self.metrics_reused,
            "metric_counts": dict(self.metric_counts),
            "skipped_counts": dict(self.skipped_counts),
            "earliest_period_end": (
                self.earliest_period_end.isoformat() if self.earliest_period_end else None
            ),
            "latest_period_end": (
                self.latest_period_end.isoformat() if self.latest_period_end else None
            ),
            "raw_records_created": self.raw_records_created,
            "observations_created": self.observations_created,
            "diagnostics_created": self.diagnostics_created,
            "traceability_verified": self.traceability_verified,
        }


def get_sec_fundamental_metric_definition(
    metric_name: str,
) -> SecFundamentalMetricDefinition:
    """Return one exact fundamental metric definition."""
    try:
        return _DEFINITION_BY_NAME[metric_name]
    except KeyError as error:
        raise ValueError(f"unsupported SEC fundamental metric: {metric_name}") from error
