"""Ephemeral, non-persisted contracts for declared-activity observation runs."""

from __future__ import annotations

from typing import Literal

from pydantic import ConfigDict, Field, model_validator

from investment_analyst.core.models.base import ContractModel, NonEmptyStr, UTCDateTime

DeclaredActivitySkipReason = Literal["missing_value", "missing_date"]


class _Strict(ContractModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, str_strip_whitespace=True)


class DeclaredActivityObservationRunSummary(_Strict):
    """Compact, typed result of one normalization run. Never persisted."""

    asset_id: NonEmptyStr
    known_at: UTCDateTime
    normalized_at: UTCDateTime
    statements_examined: int = Field(ge=0)
    values_examined: int = Field(ge=0)
    observations_generated: int = Field(ge=0)
    observations_created: int = Field(ge=0)
    observations_reused: int = Field(ge=0)
    skipped_total: int = Field(ge=0)
    skipped_by_reason: dict[NonEmptyStr, int] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_counts(self) -> DeclaredActivityObservationRunSummary:
        if self.observations_created + self.observations_reused != self.observations_generated:
            raise ValueError(
                "observations_created plus observations_reused must equal observations_generated"
            )
        if any(count < 0 for count in self.skipped_by_reason.values()):
            raise ValueError("skipped_by_reason counts must not be negative")
        if sum(self.skipped_by_reason.values()) != self.skipped_total:
            raise ValueError("skipped_by_reason must sum to skipped_total")
        if self.values_examined != self.observations_generated + self.skipped_total:
            raise ValueError("values_examined must equal observations_generated plus skipped_total")
        return self
