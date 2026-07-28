"""Strict contracts for one catalog-backed SEC issuer fundamental refresh."""

from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BeforeValidator, ConfigDict, Field, model_validator

from investment_analyst.core.models import (
    DataFrequency,
    DiagnosticVerdict,
)
from investment_analyst.core.models.base import ContractModel, NonEmptyStr, UTCDateTime

_ALLOWED_FREQUENCIES = frozenset({DataFrequency.ANNUAL, DataFrequency.QUARTERLY})


def _reject_decimal_float(value: object) -> object:
    if isinstance(value, (bool, float)):
        raise ValueError("refresh decimal values must not use float or bool")
    return value


RefreshDecimal = Annotated[Decimal, BeforeValidator(_reject_decimal_float)]


class SecIssuerFundamentalRefreshStage(StrEnum):
    """Fixed ordered stages of one issuer-only SEC refresh."""

    SEC_FETCH = "sec_fetch"
    SEC_NORMALIZATION = "sec_normalization"
    KNOWN_AT_RESOLUTION = "known_at_resolution"
    FUNDAMENTAL_METRICS = "fundamental_metrics"
    FUNDAMENTAL_DIAGNOSTIC = "fundamental_diagnostic"


class SecIssuerFundamentalRefreshRequest(ContractModel):
    """Bounded request for one independent SEC issuer update."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    asset_id: NonEmptyStr
    frequency: DataFrequency
    requested_known_at: UTCDateTime | None = None

    @model_validator(mode="after")
    def validate_scope(self) -> "SecIssuerFundamentalRefreshRequest":
        """Restrict the issuer refresh to supported reporting frequencies."""
        if self.frequency not in _ALLOWED_FREQUENCIES:
            raise ValueError("frequency must be annual or quarterly")
        return self

    def to_json_dict(self) -> dict[str, object]:
        """Return a compact request without provider identity or secrets."""
        return self.model_dump(mode="json")


class SecIssuerFundamentalRefreshSummary(ContractModel):
    """Auditable outcome of SEC ingestion and independent fundamental analytics."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["sec-issuer-fundamental-refresh-v1"] = (
        "sec-issuer-fundamental-refresh-v1"
    )
    asset_id: NonEmptyStr
    source_id: NonEmptyStr
    request: SecIssuerFundamentalRefreshRequest
    effective_known_at: UTCDateTime
    fetched_at: UTCDateTime
    normalized_at: UTCDateTime
    documents_received: Literal[2]
    raw_records_created: int = Field(ge=0)
    raw_records_reused: int = Field(ge=0)
    facts_examined: int = Field(ge=0)
    facts_selected: int = Field(ge=0)
    observations_generated: int = Field(ge=0)
    observations_created: int = Field(ge=0)
    observations_reused: int = Field(ge=0)
    annual_observations: int = Field(ge=0)
    quarterly_observations: int = Field(ge=0)
    observation_field_counts: dict[NonEmptyStr, int]
    observation_skipped_counts: dict[NonEmptyStr, int]
    target_periods: int = Field(ge=0)
    metric_results_created: int = Field(ge=0)
    metric_results_reused: int = Field(ge=0)
    metric_counts: dict[NonEmptyStr, int]
    metric_skipped_counts: dict[NonEmptyStr, int]
    diagnostic_target_period_end: UTCDateTime | None = None
    diagnostic_verdict: DiagnosticVerdict
    diagnostic_coverage: RefreshDecimal = Field(ge=Decimal("0"), le=Decimal("1"))
    diagnostic_missing_requirements: tuple[NonEmptyStr, ...]
    diagnostics_created: int = Field(ge=0)
    diagnostics_reused: int = Field(ge=0)
    traceability_verified: bool

    @model_validator(mode="after")
    def validate_summary(self) -> "SecIssuerFundamentalRefreshSummary":
        """Keep identities, exact stage counts, and point-in-time cut coherent."""
        if self.asset_id != self.request.asset_id:
            raise ValueError("summary asset_id must match the refresh request")
        if (
            self.request.requested_known_at is not None
            and self.effective_known_at != self.request.requested_known_at
        ):
            raise ValueError("an explicit known_at must be preserved exactly")
        if self.raw_records_created + self.raw_records_reused != self.documents_received:
            raise ValueError("raw-record counts must match the two SEC documents")
        if self.observations_created + self.observations_reused != self.observations_generated:
            raise ValueError("observation counts must match generated observations")
        if sum(self.observation_field_counts.values()) != self.observations_generated:
            raise ValueError("observation field counts must match generated observations")
        if self.annual_observations + self.quarterly_observations != self.observations_generated:
            raise ValueError("observation frequencies must partition generated observations")
        generated_metrics = sum(self.metric_counts.values())
        if self.metric_results_created + self.metric_results_reused != generated_metrics:
            raise ValueError("metric counts must match created plus reused results")
        if self.diagnostics_created + self.diagnostics_reused != 1:
            raise ValueError("one diagnostic must be created or reused")
        if (
            self.diagnostic_target_period_end is not None
            and self.diagnostic_target_period_end > self.effective_known_at
        ):
            raise ValueError("diagnostic period must not exceed effective_known_at")
        if any(value < 0 for value in self.observation_skipped_counts.values()):
            raise ValueError("observation skipped counts must be non-negative")
        if any(value < 0 for value in self.metric_skipped_counts.values()):
            raise ValueError("metric skipped counts must be non-negative")
        if not self.traceability_verified:
            raise ValueError("traceability_verified must be true")
        return self

    def to_json_dict(self) -> dict[str, object]:
        """Return exact JSON primitives without raw SEC payloads or credentials."""
        return self.model_dump(mode="json")


__all__ = [
    "RefreshDecimal",
    "SecIssuerFundamentalRefreshRequest",
    "SecIssuerFundamentalRefreshStage",
    "SecIssuerFundamentalRefreshSummary",
]
