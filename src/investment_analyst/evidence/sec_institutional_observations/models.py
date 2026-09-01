"""Typed requests and accounting for the institutional-observation service."""

from uuid import UUID

from pydantic import ConfigDict, Field, field_validator, model_validator

from investment_analyst.core.models.base import ContractModel, NonEmptyStr, UTCDateTime
from investment_analyst.core.models.observation import NormalizedObservation
from investment_analyst.evidence.sec_documents.models import normalize_cik


class _Strict(ContractModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, str_strip_whitespace=True)


class InstitutionalObservationRequest(_Strict):
    asset_id: NonEmptyStr
    manager_cik: NonEmptyStr
    report_ids: tuple[UUID, ...]
    known_at: UTCDateTime

    @field_validator("manager_cik")
    @classmethod
    def _normalize_cik(cls, value: str) -> str:
        return normalize_cik(value)

    @model_validator(mode="after")
    def ids(self) -> "InstitutionalObservationRequest":
        if (
            not self.report_ids
            or len(self.report_ids) > 20
            or len(set(self.report_ids)) != len(self.report_ids)
        ):
            raise ValueError("report_ids must contain one to twenty unique values")
        return self


class InstitutionalObservationSummary(_Strict):
    asset_id: NonEmptyStr
    known_at: UTCDateTime
    normalized_at: UTCDateTime
    reports_examined: int = Field(ge=0)
    rows_examined: int = Field(ge=0)
    observations_generated: int = Field(ge=0)
    observations_created: int = Field(ge=0)
    observations_reused: int = Field(ge=0)
    skipped_by_reason: dict[NonEmptyStr, int] = Field(default_factory=dict)

    @model_validator(mode="after")
    def counts(self) -> "InstitutionalObservationSummary":
        if self.observations_created + self.observations_reused != self.observations_generated:
            raise ValueError("observation counts are inconsistent")
        return self


class InstitutionalObservationQuery(_Strict):
    asset_id: NonEmptyStr
    known_at: UTCDateTime
    manager_cik: NonEmptyStr | None = None
    report_id: UUID | None = None
    cusip: NonEmptyStr | None = None
    field_name: NonEmptyStr | None = None
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=1000, ge=1, le=10_000)

    @field_validator("manager_cik")
    @classmethod
    def query_cik(cls, value: str | None) -> str | None:
        return normalize_cik(value) if value is not None else None


class InstitutionalObservationQueryResult(_Strict):
    observations: tuple[NormalizedObservation, ...]
    total_matching: int = Field(ge=0)
    truncated: bool

    @model_validator(mode="after")
    def page(self) -> "InstitutionalObservationQueryResult":
        if self.total_matching < len(self.observations):
            raise ValueError("query counts are invalid")
        return self
