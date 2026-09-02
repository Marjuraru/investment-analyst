"""Strict ephemeral candidates and skips for declared 13F weights."""

from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import ConfigDict, Field, JsonValue, model_validator

from investment_analyst.core.models.base import ContractModel, NonEmptyStr, UTCDateTime
from investment_analyst.core.models.enums import DataQuality


class _Strict(ContractModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class InstitutionalWeightCandidate(_Strict):
    asset_id: NonEmptyStr
    metric_key: NonEmptyStr
    value: Decimal
    available_at: UTCDateTime
    known_at: UTCDateTime
    input_observation_id: UUID
    parameters: dict[NonEmptyStr, JsonValue]
    quality: DataQuality

    @model_validator(mode="after")
    def valid(self) -> "InstitutionalWeightCandidate":
        if self.available_at > self.known_at:
            raise ValueError("institutional weight candidate is not point-in-time valid")
        return self


class InstitutionalWeightSkip(_Strict):
    metric_key: NonEmptyStr
    reason: Literal[
        "unresolved_close",
        "missing_total",
        "zero_total",
        "missing_position",
        "duplicate_position",
        "missing_field",
    ]


class InstitutionalWeightRunSummary(_Strict):
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
    def counts(self) -> "InstitutionalWeightRunSummary":
        if (
            self.metrics_created + self.metrics_reused != self.metrics_generated
            or self.values_examined != self.metrics_generated + self.skipped_total
            or sum(self.skipped_by_reason.values()) != self.skipped_total
        ):
            raise ValueError("institutional weight run counts are inconsistent")
        return self
