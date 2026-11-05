"""Strict, explainable projection of one point-in-time research readiness query.

The view reuses the preregistered experiment and the deterministic dataset
summary. It adds only per-column admission aggregates derived from the
persisted evidence: no sample rows, prices, feature values, per-row labels or
validation/holdout targets are exposed.
"""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import ConfigDict, Field, model_validator

from investment_analyst.core.models.base import ContractModel, NonEmptyStr
from investment_analyst.research.predictive_models import (
    FEATURE_COLUMNS,
    PREREGISTERED_EXPERIMENT,
    FeatureState,
    PredictiveDatasetSummary,
    ReasonCode,
)

READINESS_VIEW_SCHEMA_VERSION = "research-pit-readiness-view-v1"
MAX_READINESS_VIEW_BYTES = 262_144

_FEATURE_STATE_ORDER: tuple[FeatureState, ...] = (
    FeatureState.AVAILABLE,
    FeatureState.MISSING,
    FeatureState.NOT_YET_AVAILABLE,
    FeatureState.INCOMPATIBLE,
    FeatureState.AMBIGUOUS,
)


class ReadinessColumnState(ContractModel):
    """Requested decisions of one feature column that share one state."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    state: FeatureState
    samples: int = Field(ge=0)


class ReadinessReasonCount(ContractModel):
    """Occurrences of one finite evidence reason inside a single column.

    A sample can emit several reasons for the same column, so occurrences are
    reason entries and may exceed the number of requested decisions.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    code: ReasonCode
    occurrences: int = Field(ge=1)


class ReadinessFeatureColumn(ContractModel):
    """Admission partition and reason incidence for one preregistered column."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    column_name: NonEmptyStr
    algorithm_version: NonEmptyStr | None = None
    unit: NonEmptyStr | None = None
    requested: int = Field(ge=0)
    states: tuple[ReadinessColumnState, ...]
    reasons: tuple[ReadinessReasonCount, ...] = ()

    @model_validator(mode="after")
    def validate_partition(self) -> ReadinessFeatureColumn:
        """Require the five declared states in order and an exact partition."""
        if tuple(item.state for item in self.states) != _FEATURE_STATE_ORDER:
            raise ValueError("column states must declare the five states in order")
        if sum(item.samples for item in self.states) != self.requested:
            raise ValueError("column state counts must add up to requested")
        if any(item.samples > self.requested for item in self.states):
            raise ValueError("a column state count must not exceed requested")
        codes = tuple(item.code for item in self.reasons)
        if len(set(codes)) != len(codes):
            raise ValueError("column reason codes must be unique")
        if tuple(sorted(codes, key=lambda code: code.value)) != codes:
            raise ValueError("column reason codes must be sorted")
        unavailable = sum(
            item.samples for item in self.states if item.state is not FeatureState.AVAILABLE
        )
        if unavailable == 0 and self.reasons:
            raise ValueError("a fully available column must not report failure reasons")
        if unavailable > 0 and not self.reasons:
            raise ValueError("an unavailable column must report at least one reason")
        return self


class ResearchPitReadinessView(ContractModel):
    """Explainable admission view over one deterministic research dataset."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["research-pit-readiness-view-v1"] = READINESS_VIEW_SCHEMA_VERSION
    workspace_uuid: UUID
    summary: PredictiveDatasetSummary
    feature_columns: tuple[ReadinessFeatureColumn, ...]
    reason_scope: Literal["column_reason_occurrences"] = "column_reason_occurrences"
    limitations: tuple[NonEmptyStr, ...] = ()

    @model_validator(mode="after")
    def validate_view(self) -> ResearchPitReadinessView:
        """Bind every column aggregate to the same preregistered summary."""
        if tuple(item.column_name for item in self.feature_columns) != FEATURE_COLUMNS:
            raise ValueError("readiness columns must preserve the preregistered order")
        for column in self.feature_columns:
            if column.requested != self.summary.counts.requested:
                raise ValueError("column requested count must match the summary")
        if self.summary.experiment_spec_id != PREREGISTERED_EXPERIMENT.experiment_spec_id:
            raise ValueError("readiness summary belongs to another experiment")
        if not self.limitations:
            raise ValueError("readiness view must publish its interpretation limits")
        return self

    def to_json_dict(self) -> dict[str, object]:
        """Return the deterministic JSON projection served by the API."""
        return self.model_dump(mode="json")
