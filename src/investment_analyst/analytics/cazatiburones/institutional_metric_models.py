"""Strict ephemeral contracts for institutional layer-3 metric computation."""

from datetime import date
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import ConfigDict, Field, JsonValue, model_validator

from investment_analyst.core.models.base import ContractModel, NonEmptyStr, UTCDateTime
from investment_analyst.core.models.enums import DataQuality
from investment_analyst.core.models.observation import NormalizedObservation

InstitutionalMetricSkipReason = Literal[
    "unresolved_close", "duplicate_position", "missing_position", "missing_field", "zero_prior"
]


class _Strict(ContractModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, str_strip_whitespace=True)


class InstitutionalMetricCandidate(_Strict):
    asset_id: NonEmptyStr
    metric_key: NonEmptyStr
    value: Decimal
    unit: NonEmptyStr
    as_of: UTCDateTime
    available_at: UTCDateTime
    known_at: UTCDateTime
    parameters: dict[NonEmptyStr, JsonValue]
    input_observation_ids: tuple[UUID, UUID]
    quality: DataQuality

    @model_validator(mode="after")
    def valid(self) -> "InstitutionalMetricCandidate":
        if (
            self.input_observation_ids[0] == self.input_observation_ids[1]
            or self.available_at > self.known_at
        ):
            raise ValueError("institutional metric candidate is invalid")
        return self


class InstitutionalMetricSkip(_Strict):
    metric_key: NonEmptyStr
    reason: InstitutionalMetricSkipReason


class InstitutionalMetricRunSummary(_Strict):
    asset_id: NonEmptyStr
    manager_cik: NonEmptyStr
    known_at: UTCDateTime
    computed_at: UTCDateTime
    values_examined: int = Field(ge=0)
    metrics_generated: int = Field(ge=0)
    metrics_created: int = Field(ge=0)
    metrics_reused: int = Field(ge=0)
    skipped_total: int = Field(ge=0)
    skipped_by_reason: dict[NonEmptyStr, int] = Field(default_factory=dict)

    @model_validator(mode="after")
    def counts(self) -> "InstitutionalMetricRunSummary":
        if (
            self.metrics_created + self.metrics_reused != self.metrics_generated
            or self.values_examined != self.metrics_generated + self.skipped_total
            or sum(self.skipped_by_reason.values()) != self.skipped_total
        ):
            raise ValueError("institutional metric run counts are inconsistent")
        return self


class InstitutionalMetricClose(_Strict):
    report_period: date
    artifact_id: UUID | None
    status: Literal["original_complete", "amended", "insufficient", "not_evaluable", "ambiguous"]
    observations: tuple[NormalizedObservation, ...] = ()
