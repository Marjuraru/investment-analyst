"""Provider-independent contracts for crypto-derivatives metrics and diagnostics."""

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Literal
from uuid import UUID

from pydantic import ConfigDict, Field, model_validator

from investment_analyst.core.models import MetricResult, NormalizedObservation
from investment_analyst.core.models.base import ContractModel, NonEmptyStr, UTCDateTime


class CryptoDerivativesDiagnosticStatus(StrEnum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    INSUFFICIENT_DATA = "insufficient_data"


class FundingDirection(StrEnum):
    POSITIVE = "positive"
    ZERO = "zero"
    NEGATIVE = "negative"
    UNAVAILABLE = "unavailable"


class DvolDirection(StrEnum):
    RISING = "rising"
    UNCHANGED = "unchanged"
    FALLING = "falling"
    UNAVAILABLE = "unavailable"


class _DerivativeModel(ContractModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class CryptoDerivativesMetricComputation(_DerivativeModel):
    schema_version: Literal["crypto-derivatives-metric-computation-v1"] = (
        "crypto-derivatives-metric-computation-v1"
    )
    results: tuple[MetricResult, ...]
    missing_requirements: tuple[NonEmptyStr, ...]

    @model_validator(mode="after")
    def validate_output(self) -> "CryptoDerivativesMetricComputation":
        identities = tuple(result.result_id for result in self.results)
        if len(identities) != len(set(identities)):
            raise ValueError("derivatives metric results must have unique identities")
        ordering = tuple(
            (result.as_of, result.metric_key, str(result.result_id)) for result in self.results
        )
        if ordering != tuple(sorted(ordering)):
            raise ValueError("derivatives metric results must be deterministically ordered")
        if self.missing_requirements != tuple(sorted(set(self.missing_requirements))):
            raise ValueError("missing requirements must be unique and sorted")
        return self


class CryptoDerivativesMetricPersistenceSummary(_DerivativeModel):
    schema_version: Literal["crypto-derivatives-metric-persistence-summary-v1"] = (
        "crypto-derivatives-metric-persistence-summary-v1"
    )
    results: tuple[MetricResult, ...]
    results_created: int = Field(ge=0)
    results_reused: int = Field(ge=0)
    missing_requirements: tuple[NonEmptyStr, ...]
    traceability_verified: bool


class CryptoDerivativeObservationValue(_DerivativeModel):
    field_name: NonEmptyStr
    value: Decimal
    unit: NonEmptyStr
    observed_at: UTCDateTime
    available_at: UTCDateTime
    observation_id: UUID
    raw_record_id: UUID
    source_id: NonEmptyStr
    age_seconds: int = Field(ge=0)


class CryptoDerivativeMetricValue(_DerivativeModel):
    metric_key: NonEmptyStr
    value: Decimal
    unit: NonEmptyStr
    as_of: UTCDateTime
    available_at: UTCDateTime
    result_id: UUID
    input_observation_ids: tuple[UUID, ...]


class CryptoDerivativesCoverage(_DerivativeModel):
    requested_start: UTCDateTime
    requested_end: UTCDateTime
    known_at: UTCDateTime
    funding_observation_count: int = Field(ge=0)
    dvol_observation_count: int = Field(ge=0)
    summary_snapshot_count: int = Field(ge=0)
    metric_count: int = Field(ge=0)


class CryptoDerivativesDiagnostic(_DerivativeModel):
    schema_version: Literal["crypto-derivatives-diagnostic-v1"] = "crypto-derivatives-diagnostic-v1"
    diagnostic_id: UUID
    asset_id: NonEmptyStr
    source_ids: tuple[NonEmptyStr, ...]
    known_at: UTCDateTime
    status: CryptoDerivativesDiagnosticStatus
    funding_direction: FundingDirection
    dvol_direction: DvolDirection
    funding_sum_168h: CryptoDerivativeMetricValue | None = None
    dvol_change_7d: CryptoDerivativeMetricValue | None = None
    latest_open_interest: CryptoDerivativeObservationValue | None = None
    latest_current_funding: CryptoDerivativeObservationValue | None = None
    latest_funding_8h: CryptoDerivativeObservationValue | None = None
    latest_spread_bps: CryptoDerivativeMetricValue | None = None
    observation_ids: tuple[UUID, ...]
    metric_result_ids: tuple[UUID, ...]
    missing_requirements: tuple[NonEmptyStr, ...]
    limitations: tuple[NonEmptyStr, ...]

    @model_validator(mode="after")
    def validate_diagnostic(self) -> "CryptoDerivativesDiagnostic":
        if self.source_ids != tuple(sorted(set(self.source_ids))):
            raise ValueError("diagnostic source IDs must be unique and sorted")
        if self.observation_ids != tuple(sorted(set(self.observation_ids), key=str)):
            raise ValueError("diagnostic observation IDs must be unique and sorted")
        if self.metric_result_ids != tuple(sorted(set(self.metric_result_ids), key=str)):
            raise ValueError("diagnostic metric IDs must be unique and sorted")
        if self.missing_requirements != tuple(sorted(set(self.missing_requirements))):
            raise ValueError("diagnostic missing requirements must be unique and sorted")
        return self


class CryptoDerivativesQueryResult(_DerivativeModel):
    schema_version: Literal["crypto-derivatives-query-result-v1"] = (
        "crypto-derivatives-query-result-v1"
    )
    asset_id: NonEmptyStr
    source_ids: tuple[NonEmptyStr, ...]
    known_at: UTCDateTime
    metrics: tuple[MetricResult, ...]
    diagnostic: CryptoDerivativesDiagnostic
    coverage: CryptoDerivativesCoverage
    raw_record_ids: tuple[UUID, ...]
    traceability_verified: bool

    def to_json_dict(self) -> dict[str, object]:
        return self.model_dump(mode="json")


def observation_time(observation: NormalizedObservation) -> datetime:
    """Return the required event time for derivatives observations."""
    if observation.observed_at is None:
        raise ValueError("derivatives observations require observed_at")
    return observation.observed_at


__all__ = [
    "CryptoDerivativeMetricValue",
    "CryptoDerivativeObservationValue",
    "CryptoDerivativesCoverage",
    "CryptoDerivativesDiagnostic",
    "CryptoDerivativesDiagnosticStatus",
    "CryptoDerivativesMetricComputation",
    "CryptoDerivativesMetricPersistenceSummary",
    "CryptoDerivativesQueryResult",
    "DvolDirection",
    "FundingDirection",
    "observation_time",
]
