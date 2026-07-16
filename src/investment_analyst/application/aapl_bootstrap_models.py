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


class AaplRefreshMode(StrEnum):
    """Requested market refresh behavior for one bootstrap run."""

    AUTO = "auto"
    FULL = "full"


class AaplMarketRefreshMode(StrEnum):
    """Resolved market refresh plan mode."""

    INITIAL = "initial"
    INCREMENTAL = "incremental"
    ALREADY_CURRENT = "already_current"
    BACKFILL = "backfill"
    FULL = "full"


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
    MARKET_REFRESH_PLANNING = "market_refresh_planning"
    MARKET_FETCH = "market_fetch"
    KNOWN_AT_RESOLUTION = "known_at_resolution"
    FUNDAMENTAL_METRICS = "fundamental_metrics"
    FUNDAMENTAL_DIAGNOSTIC = "fundamental_diagnostic"
    MARKET_STATISTICS = "market_statistics"
    MARKET_DIAGNOSTIC = "market_diagnostic"
    CONSOLIDATED_QUERY = "consolidated_query"


class AaplMarketDateInterval(ContractModel):
    """One user-facing inclusive calendar-date interval for Alpaca ingestion."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    start: date
    end: date

    @field_validator("start", "end", mode="before")
    @classmethod
    def require_dates(cls, value: object, info) -> object:
        """Reject datetimes where inclusive calendar dates are required."""
        if isinstance(value, datetime) or not isinstance(value, date):
            raise ValueError(f"{info.field_name} must be a date")
        return value

    @model_validator(mode="after")
    def validate_interval(self) -> "AaplMarketDateInterval":
        """Require a non-empty inclusive interval."""
        if self.start > self.end:
            raise ValueError("interval start must not be later than end")
        return self

    def to_json_dict(self) -> dict[str, object]:
        """Return explicit inclusive calendar bounds."""
        return {"start": self.start.isoformat(), "end": self.end.isoformat()}


class AaplMarketRefreshPlan(ContractModel):
    """Strict read-only plan for missing Apple IEX coverage at the range edges."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    requested_start: date
    requested_end: date
    persisted_earliest: UTCDateTime | None = None
    persisted_latest: UTCDateTime | None = None
    fetch_intervals: tuple[AaplMarketDateInterval, ...]
    mode: AaplMarketRefreshMode
    market_fetch_required: bool
    reason: NonEmptyStr
    traceability_verified: bool

    @field_validator("requested_start", "requested_end", mode="before")
    @classmethod
    def require_dates(cls, value: object, info) -> object:
        """Reject datetimes where requested calendar dates are required."""
        if isinstance(value, datetime) or not isinstance(value, date):
            raise ValueError(f"{info.field_name} must be a date")
        return value

    @field_validator("mode", mode="before")
    @classmethod
    def require_mode_enum(cls, value: object) -> object:
        """Reject untyped mode strings."""
        if not isinstance(value, AaplMarketRefreshMode):
            raise ValueError("mode must be an AaplMarketRefreshMode")
        return value

    @field_validator("market_fetch_required", "traceability_verified", mode="before")
    @classmethod
    def require_booleans(cls, value: object, info) -> object:
        """Reject truthy integers and strings as plan flags."""
        if not isinstance(value, bool):
            raise ValueError(f"{info.field_name} must be a bool")
        return value

    @model_validator(mode="after")
    def validate_plan(self) -> "AaplMarketRefreshPlan":
        """Validate coverage, interval ordering, mode coherence, and traceability."""
        if self.requested_start > self.requested_end:
            raise ValueError("requested_start must not be later than requested_end")
        if (self.persisted_earliest is None) != (self.persisted_latest is None):
            raise ValueError("persisted coverage bounds must be both present or both absent")
        if (
            self.persisted_earliest is not None
            and self.persisted_latest is not None
            and self.persisted_earliest > self.persisted_latest
        ):
            raise ValueError("persisted_earliest must not exceed persisted_latest")
        if len(self.fetch_intervals) > 2:
            raise ValueError("at most two market fetch intervals are supported")
        for interval in self.fetch_intervals:
            if interval.start < self.requested_start or interval.end > self.requested_end:
                raise ValueError("fetch intervals must remain inside the requested range")
        for previous, current in zip(self.fetch_intervals, self.fetch_intervals[1:], strict=False):
            if previous.end >= current.start:
                raise ValueError("fetch intervals must be ordered and non-overlapping")
        if self.market_fetch_required != bool(self.fetch_intervals):
            raise ValueError("market_fetch_required must match fetch_intervals")
        if not self.traceability_verified:
            raise ValueError("traceability_verified must be true")
        full_interval = AaplMarketDateInterval(
            start=self.requested_start,
            end=self.requested_end,
        )
        if self.mode is AaplMarketRefreshMode.ALREADY_CURRENT and self.fetch_intervals:
            raise ValueError("already_current must not contain fetch intervals")
        if self.mode is AaplMarketRefreshMode.INITIAL:
            if self.fetch_intervals != (full_interval,):
                raise ValueError("initial must fetch the complete requested interval")
            if self.persisted_earliest is not None:
                raise ValueError("initial must not report persisted coverage")
        if (
            self.mode in {AaplMarketRefreshMode.INCREMENTAL, AaplMarketRefreshMode.FULL}
            and len(self.fetch_intervals) != 1
        ):
            raise ValueError(f"{self.mode.value} must contain one fetch interval")
        if self.mode is AaplMarketRefreshMode.FULL and self.fetch_intervals != (full_interval,):
            raise ValueError("full must fetch the complete requested interval")
        if self.mode is AaplMarketRefreshMode.BACKFILL and (
            not self.fetch_intervals or self.fetch_intervals[0].start != self.requested_start
        ):
            raise ValueError("backfill must begin at requested_start")
        return self

    def to_json_dict(self) -> dict[str, object]:
        """Return compact JSON without persisted records or provider requests."""
        return {
            "mode": self.mode.value,
            "requested_start": self.requested_start.isoformat(),
            "requested_end": self.requested_end.isoformat(),
            "persisted_earliest": (
                self.persisted_earliest.isoformat() if self.persisted_earliest else None
            ),
            "persisted_latest": (
                self.persisted_latest.isoformat() if self.persisted_latest else None
            ),
            "fetch_intervals": [item.to_json_dict() for item in self.fetch_intervals],
            "market_fetch_required": self.market_fetch_required,
            "reason": self.reason,
            "traceability_verified": self.traceability_verified,
        }


class AaplWorkspaceBootstrapRequest(ContractModel):
    """Fixed-scope request for one complete Apple workspace bootstrap."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    market_start: date
    market_end: date
    fundamental_frequency: DataFrequency
    refresh_mode: AaplRefreshMode = AaplRefreshMode.AUTO
    requested_known_at: UTCDateTime | None = None
    require_complete: bool = False

    @field_validator("market_start", "market_end", mode="before")
    @classmethod
    def require_dates(cls, value: object, info) -> object:
        """Reject datetimes where calendar dates are required."""
        if isinstance(value, datetime) or not isinstance(value, date):
            raise ValueError(f"{info.field_name} must be a date")
        return value

    @field_validator("refresh_mode", mode="before")
    @classmethod
    def require_refresh_mode(cls, value: object) -> object:
        """Reject untyped refresh-mode strings."""
        if not isinstance(value, AaplRefreshMode):
            raise ValueError("refresh_mode must be an AaplRefreshMode")
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
            "refresh_mode": self.refresh_mode.value,
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
    intervals_executed: int | None = Field(default=None, ge=0)
    bars_processed: int | None = Field(default=None, ge=0)
    coverage_receipts_created: int | None = Field(default=None, ge=0)
    coverage_receipts_reused: int | None = Field(default=None, ge=0)
    empty_intervals_completed: int | None = Field(default=None, ge=0)
    message: NonEmptyStr | None = None

    @field_validator(
        "intervals_executed",
        "bars_processed",
        "coverage_receipts_created",
        "coverage_receipts_reused",
        "empty_intervals_completed",
        mode="before",
    )
    @classmethod
    def reject_boolean_detail_counts(cls, value: object) -> object:
        """Reject booleans where optional integer details are expected."""
        if isinstance(value, bool):
            raise ValueError("stage detail counts must be integers")
        return value

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
            "intervals_executed": self.intervals_executed,
            "bars_processed": self.bars_processed,
            "coverage_receipts_created": self.coverage_receipts_created,
            "coverage_receipts_reused": self.coverage_receipts_reused,
            "empty_intervals_completed": self.empty_intervals_completed,
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
    refresh_plan: AaplMarketRefreshPlan
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
        if (
            self.refresh_plan.requested_start != self.request.market_start
            or self.refresh_plan.requested_end != self.request.market_end
        ):
            raise ValueError("refresh plan range must match the bootstrap request")
        if (
            self.request.refresh_mode is AaplRefreshMode.FULL
            and self.refresh_plan.mode is not AaplMarketRefreshMode.FULL
        ):
            raise ValueError("full refresh requests require a full refresh plan")
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
            "refresh_plan": self.refresh_plan.to_json_dict(),
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
