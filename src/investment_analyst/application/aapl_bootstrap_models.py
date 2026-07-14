"""Strict contracts for the resumable Apple workspace bootstrap."""

from datetime import date, datetime
from enum import StrEnum
from uuid import UUID

from pydantic import ConfigDict, Field, field_validator, model_validator

from investment_analyst.analytics.consolidated_diagnostic_models import (
    ConsolidatedDiagnosticStatus,
    ConsolidatedDiagnosticView,
    ConsolidatedSectionStatus,
)
from investment_analyst.core.models import DataFrequency
from investment_analyst.core.models.base import ContractModel, NonEmptyStr, UTCDateTime
from investment_analyst.providers.fundamentals.sec_fact_models import ASSET_ID

_ALLOWED_FREQUENCIES = frozenset({DataFrequency.ANNUAL, DataFrequency.QUARTERLY})


class AaplBootstrapStageStatus(StrEnum):
    """Execution state of one bootstrap stage."""

    COMPLETED = "completed"
    REUSED = "reused"
    SKIPPED = "skipped"
    FAILED = "failed"


class AaplBootstrapStage(StrEnum):
    """Fixed ordered stages of the Apple workspace bootstrap."""

    SEC_FETCH = "sec_fetch"
    SEC_NORMALIZATION = "sec_normalization"
    MARKET_FETCH = "market_fetch"
    KNOWN_AT_RESOLUTION = "known_at_resolution"
    FUNDAMENTAL_METRICS = "fundamental_metrics"
    FUNDAMENTAL_DIAGNOSTIC = "fundamental_diagnostic"
    MARKET_STATISTICS = "market_statistics"
    MARKET_DIAGNOSTIC = "market_diagnostic"
    CONSOLIDATED_QUERY = "consolidated_query"


class AaplWorkspaceBootstrapRequest(ContractModel):
    """Fixed-scope request for one complete Apple workspace bootstrap."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    market_start: date
    market_end: date
    fundamental_frequency: DataFrequency
    requested_known_at: UTCDateTime | None = None
    require_complete: bool = False

    @field_validator("market_start", "market_end", mode="before")
    @classmethod
    def require_dates(cls, value: object, info) -> object:
        """Reject datetimes where calendar dates are required."""
        if isinstance(value, datetime) or not isinstance(value, date):
            raise ValueError(f"{info.field_name} must be a date")
        return value

    @field_validator("require_complete", mode="before")
    @classmethod
    def require_boolean(cls, value: object) -> object:
        """Reject truthy integers and strings as flags."""
        if not isinstance(value, bool):
            raise ValueError("require_complete must be a bool")
        return value

    @model_validator(mode="after")
    def validate_scope(self) -> "AaplWorkspaceBootstrapRequest":
        """Validate the exclusive market range and fixed fundamental frequency."""
        if self.market_start >= self.market_end:
            raise ValueError("market_start must be earlier than market_end")
        if self.fundamental_frequency not in _ALLOWED_FREQUENCIES:
            raise ValueError("fundamental_frequency must be annual or quarterly")
        return self

    def to_json_dict(self) -> dict[str, object]:
        """Return an explicit compact JSON-compatible request."""
        return {
            "asset_id": ASSET_ID,
            "market_start": self.market_start.isoformat(),
            "market_end": self.market_end.isoformat(),
            "fundamental_frequency": self.fundamental_frequency.value,
            "requested_known_at": (
                self.requested_known_at.isoformat() if self.requested_known_at is not None else None
            ),
            "require_complete": self.require_complete,
        }


class AaplBootstrapStageDetails(ContractModel):
    """Compact typed metadata shared by all bootstrap stages."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    asset_id: NonEmptyStr = ASSET_ID
    source_id: NonEmptyStr | None = None
    feed: NonEmptyStr | None = None
    minimum_known_at: UTCDateTime | None = None
    effective_known_at: UTCDateTime | None = None
    consolidated_status: ConsolidatedDiagnosticStatus | None = None
    market_status: ConsolidatedSectionStatus | None = None
    fundamental_status: ConsolidatedSectionStatus | None = None
    message: NonEmptyStr | None = None

    def to_json_dict(self) -> dict[str, object]:
        """Return explicit stage metadata without source documents or secrets."""
        return {
            "asset_id": self.asset_id,
            "source_id": self.source_id,
            "feed": self.feed,
            "minimum_known_at": (
                self.minimum_known_at.isoformat() if self.minimum_known_at else None
            ),
            "effective_known_at": (
                self.effective_known_at.isoformat() if self.effective_known_at else None
            ),
            "consolidated_status": (
                self.consolidated_status.value if self.consolidated_status else None
            ),
            "market_status": self.market_status.value if self.market_status else None,
            "fundamental_status": (
                self.fundamental_status.value if self.fundamental_status else None
            ),
            "message": self.message,
        }


class AaplBootstrapStageSummary(ContractModel):
    """Auditable compact outcome of one bootstrap stage."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    stage: AaplBootstrapStage
    status: AaplBootstrapStageStatus
    generated: int = Field(ge=0)
    created: int = Field(ge=0)
    reused: int = Field(ge=0)
    started_at: UTCDateTime
    completed_at: UTCDateTime
    details: AaplBootstrapStageDetails
    traceability_verified: bool

    @field_validator("generated", "created", "reused", mode="before")
    @classmethod
    def reject_boolean_counts(cls, value: object) -> object:
        """Reject booleans accepted by Python's integer hierarchy."""
        if isinstance(value, bool):
            raise ValueError("stage counters must be integers")
        return value

    @model_validator(mode="after")
    def validate_stage(self) -> "AaplBootstrapStageSummary":
        """Validate counters, ordering, and traceability."""
        if self.created + self.reused > self.generated:
            raise ValueError("created plus reused cannot exceed generated")
        if self.started_at > self.completed_at:
            raise ValueError("stage started_at must not exceed completed_at")
        if not self.traceability_verified:
            raise ValueError("traceability_verified must be true")
        return self

    def to_json_dict(self) -> dict[str, object]:
        """Return a compact deterministic JSON-compatible stage."""
        return {
            "stage": self.stage.value,
            "status": self.status.value,
            "generated": self.generated,
            "created": self.created,
            "reused": self.reused,
            "started_at": self.started_at.isoformat(),
            "completed_at": self.completed_at.isoformat(),
            "details": self.details.to_json_dict(),
            "traceability_verified": self.traceability_verified,
        }


class AaplWorkspaceBootstrapSummary(ContractModel):
    """Complete compact outcome of one resumable Apple workspace bootstrap."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    workspace_id: UUID
    asset_id: NonEmptyStr = ASSET_ID
    source: NonEmptyStr
    feed: NonEmptyStr
    request: AaplWorkspaceBootstrapRequest
    requested_known_at: UTCDateTime | None = None
    effective_known_at: UTCDateTime
    stages: tuple[AaplBootstrapStageSummary, ...]
    consolidated: ConsolidatedDiagnosticView
    overall_status: ConsolidatedDiagnosticStatus
    raw_records_created: int = Field(ge=0)
    raw_records_reused: int = Field(ge=0)
    observations_created: int = Field(ge=0)
    observations_reused: int = Field(ge=0)
    metric_results_created: int = Field(ge=0)
    metric_results_reused: int = Field(ge=0)
    diagnostics_created: int = Field(ge=0)
    diagnostics_reused: int = Field(ge=0)
    traceability_verified: bool

    @field_validator(
        "raw_records_created",
        "raw_records_reused",
        "observations_created",
        "observations_reused",
        "metric_results_created",
        "metric_results_reused",
        "diagnostics_created",
        "diagnostics_reused",
        mode="before",
    )
    @classmethod
    def reject_boolean_counts(cls, value: object) -> object:
        """Reject booleans masquerading as aggregate counters."""
        if isinstance(value, bool):
            raise ValueError("bootstrap counters must be integers")
        return value

    @model_validator(mode="after")
    def validate_summary(self) -> "AaplWorkspaceBootstrapSummary":
        """Validate stage order, point-in-time context, and consolidated status."""
        expected_stages = tuple(AaplBootstrapStage)
        if tuple(item.stage for item in self.stages) != expected_stages:
            raise ValueError("bootstrap stages must use the fixed execution order")
        if self.asset_id != ASSET_ID:
            raise ValueError("bootstrap summary must identify Apple")
        if self.requested_known_at != self.request.requested_known_at:
            raise ValueError("summary requested_known_at must match request")
        if (
            self.requested_known_at is not None
            and self.effective_known_at != self.requested_known_at
        ):
            raise ValueError("explicit requested_known_at must remain unchanged")
        if self.consolidated.request.known_at != self.effective_known_at:
            raise ValueError("consolidated known_at must match effective_known_at")
        if self.overall_status is not self.consolidated.status:
            raise ValueError("overall status must match consolidated status")
        if not self.consolidated.traceability_verified or not self.traceability_verified:
            raise ValueError("bootstrap traceability must be verified")
        return self

    def to_json_dict(self) -> dict[str, object]:
        """Return compact JSON with the consolidated view embedded exactly once."""
        return {
            "workspace_id": str(self.workspace_id),
            "asset_id": self.asset_id,
            "source": self.source,
            "feed": self.feed,
            "request": self.request.to_json_dict(),
            "requested_known_at": (
                self.requested_known_at.isoformat() if self.requested_known_at is not None else None
            ),
            "effective_known_at": self.effective_known_at.isoformat(),
            "stages": [item.to_json_dict() for item in self.stages],
            "consolidated": self.consolidated.to_json_dict(),
            "overall_status": self.overall_status.value,
            "counts": {
                "raw_records_created": self.raw_records_created,
                "raw_records_reused": self.raw_records_reused,
                "observations_created": self.observations_created,
                "observations_reused": self.observations_reused,
                "metric_results_created": self.metric_results_created,
                "metric_results_reused": self.metric_results_reused,
                "diagnostics_created": self.diagnostics_created,
                "diagnostics_reused": self.diagnostics_reused,
            },
            "traceability_verified": self.traceability_verified,
        }
