"""Strict application contracts for Deribit refresh and point-in-time query."""

from datetime import UTC, date, datetime, timedelta
from enum import StrEnum
from typing import Literal

from pydantic import ConfigDict, Field, field_validator, model_validator

from investment_analyst.core.models.base import ContractModel, NonEmptyStr, UTCDateTime
from investment_analyst.providers.crypto.deribit_pipeline import DeribitImportSummary


class CryptoDerivativesRefreshMode(StrEnum):
    AUTO = "auto"
    FULL = "full"


class CryptoDerivativesPlanMode(StrEnum):
    INITIAL = "initial"
    INCREMENTAL = "incremental"
    ALREADY_CURRENT = "already_current"
    BACKFILL = "backfill"
    FULL = "full"


class _ApplicationModel(ContractModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class CryptoDerivativesInterval(_ApplicationModel):
    start: UTCDateTime
    end: UTCDateTime

    @model_validator(mode="after")
    def validate_interval(self) -> "CryptoDerivativesInterval":
        if self.start >= self.end:
            raise ValueError("derivatives interval start must be earlier than end")
        return self


class CryptoDerivativesDatasetPlan(_ApplicationModel):
    mode: CryptoDerivativesPlanMode
    intervals: tuple[CryptoDerivativesInterval, ...]

    @model_validator(mode="after")
    def validate_plan(self) -> "CryptoDerivativesDatasetPlan":
        if self.mode is CryptoDerivativesPlanMode.ALREADY_CURRENT and self.intervals:
            raise ValueError("already_current plan cannot contain fetch intervals")
        if self.mode is not CryptoDerivativesPlanMode.ALREADY_CURRENT and not self.intervals:
            raise ValueError("fetching plans require at least one interval")
        ordering = tuple((item.start, item.end) for item in self.intervals)
        if ordering != tuple(sorted(ordering)):
            raise ValueError("derivatives plan intervals must be sorted")
        for previous, current in zip(self.intervals, self.intervals[1:], strict=False):
            if previous.end > current.start:
                raise ValueError("derivatives plan intervals must not overlap")
        return self


class CryptoDerivativesRefreshPlan(_ApplicationModel):
    schema_version: Literal["crypto-derivatives-refresh-plan-v1"] = (
        "crypto-derivatives-refresh-plan-v1"
    )
    requested_start: UTCDateTime
    requested_end: UTCDateTime
    funding: CryptoDerivativesDatasetPlan
    dvol: CryptoDerivativesDatasetPlan


class CryptoDerivativesRefreshRequest(_ApplicationModel):
    schema_version: Literal["crypto-derivatives-refresh-request-v1"] = (
        "crypto-derivatives-refresh-request-v1"
    )
    asset_id: NonEmptyStr
    start_date: date
    end_date: date
    refresh_mode: CryptoDerivativesRefreshMode = CryptoDerivativesRefreshMode.AUTO
    known_at: UTCDateTime | None = None

    @field_validator("start_date", "end_date", mode="before")
    @classmethod
    def require_date(cls, value: object) -> object:
        if isinstance(value, datetime):
            raise ValueError("derivatives public bounds must be dates")
        if isinstance(value, str):
            try:
                return date.fromisoformat(value)
            except ValueError as error:
                raise ValueError("derivatives public bounds must use YYYY-MM-DD") from error
        if not isinstance(value, date):
            raise ValueError("derivatives public bounds must be dates")
        return value

    @model_validator(mode="after")
    def validate_range(self) -> "CryptoDerivativesRefreshRequest":
        if self.start_date > self.end_date:
            raise ValueError("start_date must not be after end_date")
        return self


class CryptoDerivativesQueryRequest(_ApplicationModel):
    schema_version: Literal["crypto-derivatives-query-request-v1"] = (
        "crypto-derivatives-query-request-v1"
    )
    asset_id: NonEmptyStr
    start_date: date
    end_date: date
    known_at: UTCDateTime

    @field_validator("start_date", "end_date", mode="before")
    @classmethod
    def require_date(cls, value: object) -> object:
        return CryptoDerivativesRefreshRequest.require_date(value)

    @model_validator(mode="after")
    def validate_range(self) -> "CryptoDerivativesQueryRequest":
        if self.start_date > self.end_date:
            raise ValueError("start_date must not be after end_date")
        return self


class CryptoDerivativesMetricStageSummary(_ApplicationModel):
    schema_version: Literal["crypto-derivatives-metric-stage-summary-v1"] = (
        "crypto-derivatives-metric-stage-summary-v1"
    )
    results_created: int = Field(ge=0)
    results_reused: int = Field(ge=0)
    missing_requirements: tuple[NonEmptyStr, ...]
    traceability_verified: bool


class CryptoDerivativesRefreshSummary(_ApplicationModel):
    schema_version: Literal["crypto-derivatives-refresh-summary-v1"] = (
        "crypto-derivatives-refresh-summary-v1"
    )
    asset_id: NonEmptyStr
    source_ids: tuple[NonEmptyStr, ...]
    requested_start: UTCDateTime
    requested_end: UTCDateTime
    effective_known_at: UTCDateTime
    plan: CryptoDerivativesRefreshPlan
    funding_stages: tuple[DeribitImportSummary, ...]
    dvol_stages: tuple[DeribitImportSummary, ...]
    summary_stage: DeribitImportSummary
    metric_stage: CryptoDerivativesMetricStageSummary
    traceability_verified: bool

    @model_validator(mode="after")
    def validate_summary(self) -> "CryptoDerivativesRefreshSummary":
        if self.source_ids != tuple(sorted(set(self.source_ids))):
            raise ValueError("derivatives source IDs must be unique and sorted")
        if self.effective_known_at < self.summary_stage.retrieved_at:
            raise ValueError("effective_known_at must include the current summary retrieval")
        return self

    @property
    def created_count(self) -> int:
        stages = (*self.funding_stages, *self.dvol_stages, self.summary_stage)
        return (
            sum(stage.raw_records_created + stage.observations_created for stage in stages)
            + self.metric_stage.results_created
        )

    @property
    def reused_count(self) -> int:
        stages = (*self.funding_stages, *self.dvol_stages, self.summary_stage)
        return sum(stage.raw_records_reused + stage.observations_reused for stage in stages) + (
            self.metric_stage.results_reused
        )

    def to_json_dict(self) -> dict[str, object]:
        document = self.model_dump(mode="json")
        document["created_count"] = self.created_count
        document["reused_count"] = self.reused_count
        return document


def public_date_bounds(start_date: date, end_date: date) -> tuple[datetime, datetime]:
    """Convert inclusive public UTC dates to one half-open internal interval."""
    start = datetime.combine(start_date, datetime.min.time(), tzinfo=UTC)
    end = datetime.combine(end_date + timedelta(days=1), datetime.min.time(), tzinfo=UTC)
    return start, end


__all__ = [
    "CryptoDerivativesDatasetPlan",
    "CryptoDerivativesInterval",
    "CryptoDerivativesMetricStageSummary",
    "CryptoDerivativesPlanMode",
    "CryptoDerivativesQueryRequest",
    "CryptoDerivativesRefreshMode",
    "CryptoDerivativesRefreshPlan",
    "CryptoDerivativesRefreshRequest",
    "CryptoDerivativesRefreshSummary",
    "public_date_bounds",
]
