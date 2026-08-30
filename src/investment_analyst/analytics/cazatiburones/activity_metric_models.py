"""Ephemeral, non-persisted contracts for cazatiburones activity metric computation."""

from __future__ import annotations

from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import ConfigDict, Field, JsonValue, model_validator

from investment_analyst.core.models.base import ContractModel, NonEmptyStr, UTCDateTime
from investment_analyst.core.models.enums import DataQuality

ActivityMetricSkipReason = Literal[
    "not_evaluable_no_precedent",
    "missing_input_observation",
    "not_evaluable_zero_prior",
]


class _Strict(ContractModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, str_strip_whitespace=True)


class ActivityMetricCandidate(_Strict):
    """Pure engine output: everything needed to persist one MetricResult, pre-identity.

    ``strict=True`` on this contract is what rejects a ``float`` value or a non-finite
    ``Decimal`` before it ever reaches the persisted, non-strict ``MetricResult`` contract.
    """

    asset_id: NonEmptyStr
    metric_key: NonEmptyStr
    value: Decimal
    unit: NonEmptyStr
    as_of: UTCDateTime
    available_at: UTCDateTime
    known_at: UTCDateTime
    parameters: dict[NonEmptyStr, JsonValue]
    input_observation_ids: tuple[UUID, UUID]
    algorithm_version: NonEmptyStr
    quality: DataQuality

    @model_validator(mode="after")
    def validate_shape(self) -> ActivityMetricCandidate:
        if self.input_observation_ids[0] == self.input_observation_ids[1]:
            raise ValueError("input_observation_ids must be unique")
        if self.available_at > self.known_at:
            raise ValueError("available_at must not be later than known_at")
        return self


class ActivityMetricSkip(_Strict):
    """One (participant, metric) pair that could not become a MetricResult, with its reason."""

    metric_key: NonEmptyStr
    reason: ActivityMetricSkipReason


class ActivityMetricRunSummary(_Strict):
    """Compact, typed result of one computation run. Never persisted."""

    asset_id: NonEmptyStr
    known_at: UTCDateTime
    computed_at: UTCDateTime
    values_examined: int = Field(ge=0)
    metrics_generated: int = Field(ge=0)
    metrics_created: int = Field(ge=0)
    metrics_reused: int = Field(ge=0)
    skipped_total: int = Field(ge=0)
    skipped_by_reason: dict[NonEmptyStr, int] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_counts(self) -> ActivityMetricRunSummary:
        if self.metrics_created + self.metrics_reused != self.metrics_generated:
            raise ValueError("metrics_created plus metrics_reused must equal metrics_generated")
        if any(count < 0 for count in self.skipped_by_reason.values()):
            raise ValueError("skipped_by_reason counts must not be negative")
        if sum(self.skipped_by_reason.values()) != self.skipped_total:
            raise ValueError("skipped_by_reason must sum to skipped_total")
        if self.values_examined != self.metrics_generated + self.skipped_total:
            raise ValueError("values_examined must equal metrics_generated plus skipped_total")
        return self
