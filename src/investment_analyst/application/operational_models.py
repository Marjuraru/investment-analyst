"""Strict contracts for one operational Apple refresh execution."""

from datetime import date, datetime
from enum import StrEnum
from pathlib import Path
from typing import Literal
from uuid import UUID

from pydantic import ConfigDict, Field, ValidationInfo, field_validator, model_validator

from investment_analyst.analytics.consolidated_diagnostic_models import (
    ConsolidatedDiagnosticStatus,
)
from investment_analyst.application.aapl_bootstrap_models import (
    AaplMarketRefreshMode,
    AaplRefreshMode,
    AaplWorkspaceBootstrapRequest,
)
from investment_analyst.core.models import DataFrequency
from investment_analyst.core.models.base import ContractModel, NonEmptyStr, UTCDateTime
from investment_analyst.providers.fundamentals.sec_fact_models import ASSET_ID
from investment_analyst.workspace.models import WorkspaceInspection


class AaplDailyRunStatus(StrEnum):
    """Durable lifecycle status of one operational run."""

    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class AaplOperationalHealthStatus(StrEnum):
    """Compact operational health exposed to adapters and interfaces."""

    READY = "ready"
    RUNNING = "running"
    DEGRADED = "degraded"


class AaplDailyRunCounts(ContractModel):
    """Persisted aggregate counters without provider documents or observations."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    raw_records_created: int = Field(ge=0)
    raw_records_reused: int = Field(ge=0)
    observations_created: int = Field(ge=0)
    observations_reused: int = Field(ge=0)
    metric_results_created: int = Field(ge=0)
    metric_results_reused: int = Field(ge=0)
    diagnostics_created: int = Field(ge=0)
    diagnostics_reused: int = Field(ge=0)

    @field_validator("*", mode="before")
    @classmethod
    def reject_boolean_counts(cls, value: object) -> object:
        """Reject booleans accepted by Python's integer hierarchy."""
        if isinstance(value, bool):
            raise ValueError("operational counters must be integers")
        return value

    def to_json_dict(self) -> dict[str, object]:
        """Return explicit JSON-safe counters."""
        return self.model_dump(mode="json")


class AaplDailyRunFailure(ContractModel):
    """Bounded safe failure information without tracebacks or credentials."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    category: NonEmptyStr
    message: NonEmptyStr = Field(max_length=500)

    def to_json_dict(self) -> dict[str, object]:
        """Return the safe failure contract."""
        return {"category": self.category, "message": self.message}


class AaplDailyRunRequestSnapshot(ContractModel):
    """Round-trippable operational snapshot of the fixed Apple bootstrap request."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    asset_id: Literal["equity:us:aapl"] = ASSET_ID
    market_start: date
    market_end: date
    fundamental_frequency: DataFrequency
    refresh_mode: AaplRefreshMode
    requested_known_at: UTCDateTime | None = None
    require_complete: bool

    @field_validator("market_start", "market_end", mode="before")
    @classmethod
    def require_calendar_dates(cls, value: object, info: ValidationInfo) -> object:
        """Accept ISO JSON dates while rejecting datetimes and ambiguous values."""
        if isinstance(value, datetime):
            raise ValueError(f"{info.field_name} must be a date")
        if isinstance(value, str):
            try:
                return date.fromisoformat(value)
            except ValueError as error:
                raise ValueError(f"{info.field_name} must use YYYY-MM-DD") from error
        if not isinstance(value, date):
            raise ValueError(f"{info.field_name} must be a date")
        return value

    @field_validator("require_complete", mode="before")
    @classmethod
    def require_boolean(cls, value: object) -> object:
        """Reject truthy integers and strings in persisted state."""
        if not isinstance(value, bool):
            raise ValueError("require_complete must be a bool")
        return value

    @classmethod
    def from_request(cls, request: AaplWorkspaceBootstrapRequest) -> "AaplDailyRunRequestSnapshot":
        """Copy one validated bootstrap request without introducing free-form fields."""
        return cls(
            market_start=request.market_start,
            market_end=request.market_end,
            fundamental_frequency=request.fundamental_frequency,
            refresh_mode=request.refresh_mode,
            requested_known_at=request.requested_known_at,
            require_complete=request.require_complete,
        )

    def to_json_dict(self) -> dict[str, object]:
        """Return the stable operational request representation."""
        return {
            "asset_id": self.asset_id,
            "market_start": self.market_start.isoformat(),
            "market_end": self.market_end.isoformat(),
            "fundamental_frequency": self.fundamental_frequency.value,
            "refresh_mode": self.refresh_mode.value,
            "requested_known_at": (
                self.requested_known_at.isoformat() if self.requested_known_at else None
            ),
            "require_complete": self.require_complete,
        }

    def to_request(self) -> AaplWorkspaceBootstrapRequest:
        """Rebuild the existing bootstrap request without changing its public contract."""
        return AaplWorkspaceBootstrapRequest(
            market_start=self.market_start,
            market_end=self.market_end,
            fundamental_frequency=self.fundamental_frequency,
            refresh_mode=self.refresh_mode,
            requested_known_at=self.requested_known_at,
            require_complete=self.require_complete,
        )


class AaplDailyRunState(ContractModel):
    """Atomic latest-run state consumed by scheduling and user interfaces."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    schema_version: Literal["aapl-daily-run-state-v1"] = "aapl-daily-run-state-v1"
    run_id: UUID
    status: AaplDailyRunStatus
    workspace_root: Path
    workspace_id: UUID | None = None
    request: AaplDailyRunRequestSnapshot
    started_at: UTCDateTime
    completed_at: UTCDateTime | None = None
    effective_known_at: UTCDateTime | None = None
    refresh_mode: AaplMarketRefreshMode | None = None
    overall_status: ConsolidatedDiagnosticStatus | None = None
    counts: AaplDailyRunCounts | None = None
    traceability_verified: bool | None = None
    failure: AaplDailyRunFailure | None = None

    @field_validator("workspace_root", mode="before")
    @classmethod
    def normalize_workspace_root(cls, value: object) -> object:
        """Normalize path input without creating or inspecting the workspace."""
        if isinstance(value, str):
            if not value.strip():
                raise ValueError("workspace_root must not be empty")
            return str(Path(value).expanduser().resolve(strict=False))
        if isinstance(value, Path):
            return value.expanduser().resolve(strict=False)
        return value

    @field_validator("request", mode="before")
    @classmethod
    def snapshot_request(cls, value: object) -> object:
        """Convert only the existing typed bootstrap request at the application boundary."""
        if isinstance(value, AaplWorkspaceBootstrapRequest):
            return AaplDailyRunRequestSnapshot.from_request(value)
        return value

    @field_validator("traceability_verified", mode="before")
    @classmethod
    def require_optional_boolean(cls, value: object) -> object:
        """Reject truthy integers and strings as traceability flags."""
        if value is not None and not isinstance(value, bool):
            raise ValueError("traceability_verified must be a bool")
        return value

    @model_validator(mode="after")
    def validate_lifecycle(self) -> "AaplDailyRunState":
        """Keep timestamps, outcome data, and failure data coherent with status."""
        if not self.workspace_root.is_absolute():
            raise ValueError("workspace_root must be absolute")
        if self.completed_at is not None and self.started_at > self.completed_at:
            raise ValueError("started_at must not be later than completed_at")

        outcome = (
            self.workspace_id,
            self.effective_known_at,
            self.refresh_mode,
            self.overall_status,
            self.counts,
            self.traceability_verified,
        )
        if self.status is AaplDailyRunStatus.RUNNING:
            if self.completed_at is not None or any(item is not None for item in outcome):
                raise ValueError("running state cannot contain a completed outcome")
            if self.failure is not None:
                raise ValueError("running state cannot contain a failure")
        elif self.status is AaplDailyRunStatus.SUCCEEDED:
            if self.completed_at is None or any(item is None for item in outcome):
                raise ValueError("succeeded state requires a complete outcome")
            if self.traceability_verified is not True:
                raise ValueError("succeeded state requires verified traceability")
            if self.failure is not None:
                raise ValueError("succeeded state cannot contain a failure")
        else:
            if self.completed_at is None or self.failure is None:
                raise ValueError("failed state requires completion time and safe failure")
            if any(item is not None for item in outcome):
                raise ValueError("failed state cannot claim a completed analytical outcome")
        return self

    def to_json_dict(self) -> dict[str, object]:
        """Return deterministic JSON primitives without credentials or provider payloads."""
        return {
            "schema_version": self.schema_version,
            "run_id": str(self.run_id),
            "status": self.status.value,
            "workspace_root": str(self.workspace_root),
            "workspace_id": str(self.workspace_id) if self.workspace_id else None,
            "request": self.request.to_json_dict(),
            "started_at": self.started_at.isoformat(),
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "effective_known_at": (
                self.effective_known_at.isoformat() if self.effective_known_at else None
            ),
            "refresh_mode": self.refresh_mode.value if self.refresh_mode else None,
            "overall_status": self.overall_status.value if self.overall_status else None,
            "counts": self.counts.to_json_dict() if self.counts else None,
            "traceability_verified": self.traceability_verified,
            "failure": self.failure.to_json_dict() if self.failure else None,
        }


class AaplOperationalHealth(ContractModel):
    """Read-only workspace and latest-run health for automation and interfaces."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: AaplOperationalHealthStatus
    workspace: WorkspaceInspection
    latest_run: AaplDailyRunState | None = None
    issues: tuple[NonEmptyStr, ...] = ()

    @model_validator(mode="after")
    def validate_health(self) -> "AaplOperationalHealth":
        """Require degraded health for incomplete workspaces and failed executions."""
        if (
            self.workspace.status != "ready"
            and self.status is not AaplOperationalHealthStatus.DEGRADED
        ):
            raise ValueError("incomplete workspace health must be degraded")
        if (
            self.latest_run is not None
            and self.latest_run.status is AaplDailyRunStatus.FAILED
            and self.status is not AaplOperationalHealthStatus.DEGRADED
        ):
            raise ValueError("failed latest run must produce degraded health")
        return self

    def to_json_dict(self) -> dict[str, object]:
        """Return one bounded JSON-safe operational snapshot."""
        return {
            "status": self.status.value,
            "workspace": self.workspace.to_json_dict(),
            "latest_run": self.latest_run.to_json_dict() if self.latest_run else None,
            "issues": list(self.issues),
        }
