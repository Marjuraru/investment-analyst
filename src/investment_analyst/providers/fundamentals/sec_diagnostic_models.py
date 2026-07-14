"""Strict models for auditable Apple SEC fundamental diagnostics."""

from datetime import date
from decimal import Decimal
from typing import Annotated
from uuid import UUID

from pydantic import BeforeValidator, ConfigDict, Field, field_validator, model_validator

from investment_analyst.core.models import (
    DataFrequency,
    DataQuality,
    DiagnosticResult,
    DiagnosticVerdict,
)
from investment_analyst.core.models.base import ContractModel, NonEmptyStr, UTCDateTime
from investment_analyst.providers.fundamentals.sec_fact_models import ASSET_ID

_ALLOWED_FREQUENCIES = frozenset({DataFrequency.ANNUAL, DataFrequency.QUARTERLY})


def _reject_decimal_float(value: object) -> object:
    if isinstance(value, (bool, float)):
        raise ValueError("diagnostic numeric values must use Decimal")
    return value


DiagnosticDecimal = Annotated[Decimal, BeforeValidator(_reject_decimal_float)]


class SecFundamentalDiagnosticRequest(ContractModel):
    """Point-in-time request for one Apple fundamental diagnostic."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    asset_id: NonEmptyStr = ASSET_ID
    known_at: UTCDateTime
    frequency: DataFrequency
    as_of_period_end: date | None = None

    @model_validator(mode="after")
    def validate_scope(self) -> "SecFundamentalDiagnosticRequest":
        """Validate the fixed Apple scope and requested reporting period."""
        if self.asset_id != ASSET_ID:
            raise ValueError("asset_id must identify Apple")
        if self.frequency not in _ALLOWED_FREQUENCIES:
            raise ValueError("frequency must be annual or quarterly")
        if self.as_of_period_end is not None and self.as_of_period_end > self.known_at.date():
            raise ValueError("as_of_period_end must not be later than known_at")
        return self


class SecFundamentalDiagnosticInput(ContractModel):
    """One observation identity assigned to a deterministic metric role."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    role: NonEmptyStr
    observation_id: UUID


class SecFundamentalDiagnosticMetric(ContractModel):
    """Validated persisted fundamental metric selected for a diagnostic."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    result_id: UUID
    metric_name: NonEmptyStr
    value: DiagnosticDecimal
    unit: NonEmptyStr
    frequency: DataFrequency
    period_start: UTCDateTime | None = None
    period_end: UTCDateTime
    available_at: UTCDateTime
    computed_at: UTCDateTime
    formula: NonEmptyStr
    algorithm_version: NonEmptyStr
    input_observation_ids: tuple[UUID, ...]
    input_roles: tuple[SecFundamentalDiagnosticInput, ...]
    quality: DataQuality

    @model_validator(mode="after")
    def validate_metric(self) -> "SecFundamentalDiagnosticMetric":
        """Require finite ratio data and deterministic, complete input roles."""
        if not self.value.is_finite():
            raise ValueError("metric value must be finite")
        if self.unit != "ratio":
            raise ValueError("fundamental diagnostic metrics must use ratio units")
        if self.frequency not in _ALLOWED_FREQUENCIES:
            raise ValueError("metric frequency must be annual or quarterly")
        if self.period_start is not None and self.period_start > self.period_end:
            raise ValueError("period_start must not be later than period_end")
        if self.available_at > self.computed_at:
            raise ValueError("metric available_at must not exceed computed_at")
        roles = tuple(item.role for item in self.input_roles)
        role_ids = tuple(item.observation_id for item in self.input_roles)
        if roles != tuple(sorted(roles)):
            raise ValueError("input roles must be ordered by role")
        if len(set(roles)) != len(roles):
            raise ValueError("input roles must be unique")
        if len(set(role_ids)) != len(role_ids):
            raise ValueError("input role observation IDs must be unique")
        if role_ids != self.input_observation_ids:
            raise ValueError("input_observation_ids must match deterministic input role order")
        if self.quality is not DataQuality.VALID:
            raise ValueError("fundamental diagnostic metrics must have VALID quality")
        return self

    def to_json_dict(self) -> dict[str, object]:
        """Return a compact explicit JSON-compatible metric representation."""
        return {
            "result_id": str(self.result_id),
            "metric_name": self.metric_name,
            "value": str(self.value),
            "unit": self.unit,
            "frequency": self.frequency.value,
            "period_start": self.period_start.isoformat() if self.period_start else None,
            "period_end": self.period_end.isoformat(),
            "available_at": self.available_at.isoformat(),
            "computed_at": self.computed_at.isoformat(),
            "formula": self.formula,
            "algorithm_version": self.algorithm_version,
            "input_observation_ids": [str(item) for item in self.input_observation_ids],
            "input_roles": [item.model_dump(mode="json") for item in self.input_roles],
            "quality": self.quality.value,
        }


class SecFundamentalDiagnosticSelection(ContractModel):
    """Point-in-time selection of one reporting period and its current metric revisions."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    request: SecFundamentalDiagnosticRequest
    target_period_end: UTCDateTime | None = None
    selected_metrics: tuple[SecFundamentalDiagnosticMetric, ...]
    missing_metric_names: tuple[NonEmptyStr, ...]
    metrics_examined: int = Field(ge=0)
    metrics_eligible: int = Field(ge=0)
    revisions_superseded: int = Field(ge=0)
    traceability_verified: bool

    @model_validator(mode="after")
    def validate_selection(self) -> "SecFundamentalDiagnosticSelection":
        """Keep period, ordering, counters, and missing metric names consistent."""
        names = tuple(item.metric_name for item in self.selected_metrics)
        if names != tuple(sorted(names)):
            raise ValueError("selected metrics must be ordered by metric name")
        if len(set(names)) != len(names):
            raise ValueError("selected metric names must be unique")
        if self.target_period_end is None and self.selected_metrics:
            raise ValueError("selected metrics require a target period")
        if self.target_period_end is not None and any(
            item.period_end != self.target_period_end for item in self.selected_metrics
        ):
            raise ValueError("selected metrics must belong to the target period")
        if any(item.frequency is not self.request.frequency for item in self.selected_metrics):
            raise ValueError("selected metrics must match request frequency")
        if self.metrics_eligible > self.metrics_examined:
            raise ValueError("eligible metric count cannot exceed examined count")
        if len(set(self.missing_metric_names)) != len(self.missing_metric_names):
            raise ValueError("missing metric names must be unique")
        if not self.traceability_verified:
            raise ValueError("traceability_verified must be true")
        return self

    def metric_result_ids(self) -> tuple[UUID, ...]:
        """Return selected metric result IDs in metric-name order."""
        return tuple(item.result_id for item in self.selected_metrics)

    def to_json_dict(self) -> dict[str, object]:
        """Return an explicit deterministic JSON-compatible selection."""
        return {
            "request": self.request.model_dump(mode="json"),
            "target_period_end": (
                self.target_period_end.isoformat() if self.target_period_end else None
            ),
            "selected_metrics": [item.to_json_dict() for item in self.selected_metrics],
            "missing_metric_names": list(self.missing_metric_names),
            "metrics_examined": self.metrics_examined,
            "metrics_eligible": self.metrics_eligible,
            "revisions_superseded": self.revisions_superseded,
            "traceability_verified": self.traceability_verified,
        }


class SecFundamentalDiagnosticComputation(ContractModel):
    """Complete in-memory result of one fundamental diagnostic computation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    request: SecFundamentalDiagnosticRequest
    selection: SecFundamentalDiagnosticSelection
    diagnostic: DiagnosticResult
    missing_requirements: tuple[NonEmptyStr, ...]
    coverage: DiagnosticDecimal = Field(ge=Decimal("0"), le=Decimal("1"))
    recency_factor: DiagnosticDecimal = Field(ge=Decimal("0"), le=Decimal("1"))
    traceability_verified: bool

    @model_validator(mode="after")
    def validate_computation(self) -> "SecFundamentalDiagnosticComputation":
        """Keep request, verdict, coverage, and traceability consistent."""
        if self.selection.request != self.request:
            raise ValueError("selection request must match computation request")
        if self.diagnostic.asset_id != self.request.asset_id:
            raise ValueError("diagnostic asset must match request")
        if self.diagnostic.verdict is DiagnosticVerdict.INSUFFICIENT_DATA:
            if self.diagnostic.components or self.diagnostic.evidence:
                raise ValueError("insufficient diagnostic must not contain components or evidence")
            if self.diagnostic.final_score != 0 or self.diagnostic.confidence != 0:
                raise ValueError("insufficient diagnostic score and confidence must be zero")
            if not self.missing_requirements:
                raise ValueError("insufficient diagnostic requires missing requirements")
        if not self.traceability_verified:
            raise ValueError("traceability_verified must be true")
        return self

    def to_json_dict(self) -> dict[str, object]:
        """Return an explicit JSON-compatible computation."""
        return {
            "request": self.request.model_dump(mode="json"),
            "selection": self.selection.to_json_dict(),
            "diagnostic": self.diagnostic.model_dump(mode="json"),
            "missing_requirements": list(self.missing_requirements),
            "coverage": str(self.coverage),
            "recency_factor": str(self.recency_factor),
            "traceability_verified": self.traceability_verified,
        }


class SecFundamentalDiagnosticRunSummary(ContractModel):
    """Compact auditable summary of one fundamental diagnostic persistence run."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    asset_id: NonEmptyStr
    known_at: UTCDateTime
    frequency: DataFrequency
    target_period_end: UTCDateTime | None = None
    computed_at: UTCDateTime
    diagnostic_id: UUID
    selected_metric_result_ids: tuple[UUID, ...]
    missing_requirements: tuple[NonEmptyStr, ...]
    final_score: DiagnosticDecimal
    verdict: DiagnosticVerdict
    quality: DataQuality
    confidence: DiagnosticDecimal
    coverage: DiagnosticDecimal
    recency_factor: DiagnosticDecimal
    diagnostics_generated: int = Field(ge=0)
    diagnostics_created: int = Field(ge=0)
    diagnostics_reused: int = Field(ge=0)
    raw_records_created: int = Field(ge=0)
    observations_created: int = Field(ge=0)
    metric_results_created: int = Field(ge=0)
    traceability_verified: bool

    @field_validator("diagnostics_generated", "diagnostics_created", "diagnostics_reused")
    @classmethod
    def validate_integer_counts(cls, value: int) -> int:
        """Reject booleans accepted by Python's integer hierarchy."""
        if isinstance(value, bool):
            raise ValueError("diagnostic counts must be integers")
        return value

    @model_validator(mode="after")
    def validate_summary(self) -> "SecFundamentalDiagnosticRunSummary":
        """Keep diagnostic and protected-record counts internally consistent."""
        if self.diagnostics_generated != self.diagnostics_created + self.diagnostics_reused:
            raise ValueError("generated diagnostics must equal created plus reused diagnostics")
        if any(
            value != 0
            for value in (
                self.raw_records_created,
                self.observations_created,
                self.metric_results_created,
            )
        ):
            raise ValueError("fundamental diagnostics must not create input data")
        if not Decimal("0") <= self.coverage <= Decimal("1"):
            raise ValueError("coverage must be between zero and one")
        if not Decimal("0") <= self.recency_factor <= Decimal("1"):
            raise ValueError("recency_factor must be between zero and one")
        if not self.traceability_verified:
            raise ValueError("traceability_verified must be true")
        return self

    def to_json_dict(self) -> dict[str, object]:
        """Return an explicit compact JSON-compatible summary."""
        return {
            "asset_id": self.asset_id,
            "known_at": self.known_at.isoformat(),
            "frequency": self.frequency.value,
            "target_period_end": (
                self.target_period_end.isoformat() if self.target_period_end else None
            ),
            "computed_at": self.computed_at.isoformat(),
            "selected_metric_result_ids": [str(item) for item in self.selected_metric_result_ids],
            "missing_requirements": list(self.missing_requirements),
            "final_score": str(self.final_score),
            "verdict": self.verdict.value,
            "quality": self.quality.value,
            "confidence": str(self.confidence),
            "coverage": str(self.coverage),
            "recency_factor": str(self.recency_factor),
            "diagnostics_generated": self.diagnostics_generated,
            "diagnostics_created": self.diagnostics_created,
            "diagnostics_reused": self.diagnostics_reused,
            "raw_records_created": self.raw_records_created,
            "observations_created": self.observations_created,
            "metric_results_created": self.metric_results_created,
            "traceability_verified": self.traceability_verified,
        }
