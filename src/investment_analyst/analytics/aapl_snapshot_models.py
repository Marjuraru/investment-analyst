"""Strict models for the re-entrant Apple market snapshot orchestrator."""

from datetime import date, datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import ConfigDict, Field, field_validator, model_validator

from investment_analyst.analytics.consolidated_diagnostic_models import (
    ConsolidatedDiagnosticStatus,
    ConsolidatedDiagnosticView,
)
from investment_analyst.core.models import DataFrequency, DiagnosticVerdict
from investment_analyst.core.models.base import ContractModel, NonEmptyStr, UTCDateTime
from investment_analyst.providers.fundamentals.sec_fact_models import ASSET_ID

_ALLOWED_FUNDAMENTAL_FREQUENCIES = frozenset({DataFrequency.ANNUAL, DataFrequency.QUARTERLY})
_FUNDAMENTAL_REUSE_REASON = "existing local point-in-time fundamental diagnostic is reused"


class AaplSnapshotStageStatus(StrEnum):
    """Execution state of one orchestrated stage."""

    COMPLETED = "completed"
    REUSED = "reused"
    SKIPPED = "skipped"
    FAILED = "failed"


class AaplSnapshotStage(StrEnum):
    """Fixed ordered stages in one Apple snapshot run."""

    MARKET_FETCH = "market_fetch"
    MARKET_STATISTICS = "market_statistics"
    MARKET_DIAGNOSTIC = "market_diagnostic"
    FUNDAMENTAL_REFRESH = "fundamental_refresh"
    CONSOLIDATED_QUERY = "consolidated_query"


def _require_date(value: object, name: str) -> date:
    if isinstance(value, datetime) or not isinstance(value, date):
        raise ValueError(f"{name} must be a date")
    return value


def _require_frequency(value: object) -> DataFrequency:
    if not isinstance(value, DataFrequency):
        raise ValueError("fundamental_frequency must use DataFrequency")
    return value


class AaplCompleteSnapshotRequest(ContractModel):
    """Fixed-scope request for one resumable Apple market refresh and query."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    asset_id: NonEmptyStr = ASSET_ID
    known_at: UTCDateTime
    market_start: date
    market_end: date
    fundamental_frequency: DataFrequency
    market_as_of: date | None = None
    fundamental_as_of: date | None = None
    require_complete: bool = False

    @field_validator("known_at", mode="before")
    @classmethod
    def require_datetime_object(cls, value: object) -> object:
        """Reject strings and naive values before UTC normalization."""
        if not isinstance(value, datetime):
            raise ValueError("known_at must be a timezone-aware datetime")
        return value

    @field_validator(
        "market_start", "market_end", "market_as_of", "fundamental_as_of", mode="before"
    )
    @classmethod
    def validate_dates(cls, value: object, info) -> object:
        """Accept date objects only, preserving explicit CLI parsing."""
        if value is None:
            return None
        return _require_date(value, info.field_name)

    @field_validator("fundamental_frequency", mode="before")
    @classmethod
    def validate_frequency_type(cls, value: object) -> DataFrequency:
        """Reject arbitrary frequency strings inside the model."""
        return _require_frequency(value)

    @field_validator("require_complete", mode="before")
    @classmethod
    def validate_bool(cls, value: object) -> bool:
        """Reject integers and strings masquerading as booleans."""
        if type(value) is not bool:
            raise ValueError("require_complete must be a bool")
        return value

    @model_validator(mode="after")
    def validate_scope(self) -> "AaplCompleteSnapshotRequest":
        """Validate fixed Apple scope, dates, and fundamental frequency."""
        if self.asset_id != ASSET_ID:
            raise ValueError("asset_id must identify Apple")
        if self.market_start > self.market_end:
            raise ValueError("market_start must not be later than market_end")
        if self.market_end > self.known_at.date():
            raise ValueError("market_end must not be later than known_at")
        if self.fundamental_frequency not in _ALLOWED_FUNDAMENTAL_FREQUENCIES:
            raise ValueError("fundamental_frequency must be annual or quarterly")
        if self.market_as_of is not None and self.market_as_of > self.known_at.date():
            raise ValueError("market_as_of must not be later than known_at")
        if self.fundamental_as_of is not None and self.fundamental_as_of > self.known_at.date():
            raise ValueError("fundamental_as_of must not be later than known_at")
        return self


class MarketFetchStageDetails(ContractModel):
    """Compact Alpaca IEX import details."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["market_fetch"] = "market_fetch"
    asset_id: NonEmptyStr
    source_id: NonEmptyStr
    feed: NonEmptyStr
    adjustment: NonEmptyStr
    request_count: int = Field(ge=0)
    bars_received: int = Field(ge=0)
    raw_records_created: int = Field(ge=0)
    raw_records_reused: int = Field(ge=0)
    observations_created: int = Field(ge=0)
    observations_reused: int = Field(ge=0)
    earliest_bar: UTCDateTime | None = None
    latest_bar: UTCDateTime | None = None


class MarketStatisticsStageDetails(ContractModel):
    """Compact persisted market-statistics details."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["market_statistics"] = "market_statistics"
    asset_id: NonEmptyStr
    source_id: NonEmptyStr
    known_at: UTCDateTime
    bar_count: int = Field(ge=0)
    definitions_upserted: int = Field(ge=0)
    results_generated: int = Field(ge=0)
    results_created: int = Field(ge=0)
    results_reused: int = Field(ge=0)
    earliest_as_of: UTCDateTime | None = None
    latest_as_of: UTCDateTime | None = None


class MarketDiagnosticStageDetails(ContractModel):
    """Compact persisted market-diagnostic details."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["market_diagnostic"] = "market_diagnostic"
    asset_id: NonEmptyStr
    source_id: NonEmptyStr
    known_at: UTCDateTime
    as_of: UTCDateTime
    verdict: DiagnosticVerdict
    diagnostics_generated: int = Field(ge=0)
    diagnostics_created: int = Field(ge=0)
    diagnostics_reused: int = Field(ge=0)


class FundamentalRefreshStageDetails(ContractModel):
    """Explicit statement that no SEC or fundamental refresh ran."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["fundamental_refresh"] = "fundamental_refresh"
    reason: NonEmptyStr = _FUNDAMENTAL_REUSE_REASON

    @model_validator(mode="after")
    def validate_reason(self) -> "FundamentalRefreshStageDetails":
        """Keep the skip reason stable and audit-friendly."""
        if self.reason != _FUNDAMENTAL_REUSE_REASON:
            raise ValueError("fundamental refresh reason is fixed for this orchestrator")
        return self


class ConsolidatedQueryStageDetails(ContractModel):
    """Read-only consolidated query result for the final stage."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["consolidated_query"] = "consolidated_query"
    view: ConsolidatedDiagnosticView


StageDetails = Annotated[
    MarketFetchStageDetails
    | MarketStatisticsStageDetails
    | MarketDiagnosticStageDetails
    | FundamentalRefreshStageDetails
    | ConsolidatedQueryStageDetails,
    Field(discriminator="kind"),
]


class AaplSnapshotStageSummary(ContractModel):
    """Compact, typed, and secret-free result of one orchestrated stage."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    stage: AaplSnapshotStage
    status: AaplSnapshotStageStatus
    records_generated: int = Field(ge=0)
    records_created: int = Field(ge=0)
    records_reused: int = Field(ge=0)
    started_at: UTCDateTime
    completed_at: UTCDateTime
    details: StageDetails
    traceability_verified: bool

    @field_validator("records_generated", "records_created", "records_reused", mode="before")
    @classmethod
    def reject_boolean_counts(cls, value: object) -> object:
        """Reject booleans masquerading as counters."""
        if isinstance(value, bool):
            raise ValueError("stage counts must be integers")
        return value

    @model_validator(mode="after")
    def validate_stage(self) -> "AaplSnapshotStageSummary":
        """Validate timing, counts, details, and stage status semantics."""
        if self.completed_at < self.started_at:
            raise ValueError("completed_at must not be earlier than started_at")
        if self.records_generated != self.records_created + self.records_reused:
            raise ValueError("records_generated must equal created plus reused")
        if self.details.kind != self.stage.value:
            raise ValueError("stage details must match the stage")
        if self.status is AaplSnapshotStageStatus.SKIPPED and any(
            (self.records_generated, self.records_created, self.records_reused)
        ):
            raise ValueError("skipped stages must not report generated records")
        if self.status is AaplSnapshotStageStatus.REUSED and (
            self.records_generated == 0 or self.records_created != 0
        ):
            raise ValueError("reused stages require only reused records")
        if self.status is AaplSnapshotStageStatus.FAILED and self.traceability_verified:
            raise ValueError("failed stage cannot claim verified traceability")
        if self.status is not AaplSnapshotStageStatus.FAILED and not self.traceability_verified:
            raise ValueError("successful and skipped stages require traceability verification")
        return self

    def to_json_dict(self) -> dict[str, object]:
        """Return an explicit compact JSON-compatible stage representation."""
        return {
            "stage": self.stage.value,
            "status": self.status.value,
            "records_generated": self.records_generated,
            "records_created": self.records_created,
            "records_reused": self.records_reused,
            "started_at": self.started_at.isoformat(),
            "completed_at": self.completed_at.isoformat(),
            "details": self.details.model_dump(mode="json"),
            "traceability_verified": self.traceability_verified,
        }


class AaplCompleteSnapshotSummary(ContractModel):
    """Final resumable Apple snapshot result with independent diagnostic modes."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    request: AaplCompleteSnapshotRequest
    source: NonEmptyStr
    feed: NonEmptyStr
    market_fetch: AaplSnapshotStageSummary
    market_statistics: AaplSnapshotStageSummary
    market_diagnostic: AaplSnapshotStageSummary
    fundamental_refresh: AaplSnapshotStageSummary
    consolidated: AaplSnapshotStageSummary
    overall_status: ConsolidatedDiagnosticStatus
    started_at: UTCDateTime
    completed_at: UTCDateTime
    stages_completed: int = Field(ge=0)
    stages_reused: int = Field(ge=0)
    stages_skipped: int = Field(ge=0)
    traceability_verified: bool

    @field_validator("stages_completed", "stages_reused", "stages_skipped", mode="before")
    @classmethod
    def reject_boolean_counts(cls, value: object) -> object:
        """Reject booleans masquerading as stage counters."""
        if isinstance(value, bool):
            raise ValueError("stage counters must be integers")
        return value

    @model_validator(mode="after")
    def validate_summary(self) -> "AaplCompleteSnapshotSummary":
        """Validate stage order, feed, final status, and aggregate counts."""
        stages = self.stages()
        expected = (
            AaplSnapshotStage.MARKET_FETCH,
            AaplSnapshotStage.MARKET_STATISTICS,
            AaplSnapshotStage.MARKET_DIAGNOSTIC,
            AaplSnapshotStage.FUNDAMENTAL_REFRESH,
            AaplSnapshotStage.CONSOLIDATED_QUERY,
        )
        if tuple(item.stage for item in stages) != expected:
            raise ValueError("snapshot stages are not in the required order")
        if self.source != "Alpaca Market Data" or self.feed != "iex":
            raise ValueError("snapshot source and feed must be Alpaca Market Data IEX")
        if self.completed_at < self.started_at:
            raise ValueError("completed_at must not be earlier than started_at")
        consolidated_details = self.consolidated.details
        if not isinstance(consolidated_details, ConsolidatedQueryStageDetails):
            raise ValueError("consolidated stage must contain the consolidated view")
        if self.overall_status is not consolidated_details.view.status:
            raise ValueError("overall_status must match the consolidated view")
        counts = {
            AaplSnapshotStageStatus.COMPLETED: 0,
            AaplSnapshotStageStatus.REUSED: 0,
            AaplSnapshotStageStatus.SKIPPED: 0,
        }
        for stage in stages:
            if stage.status in counts:
                counts[stage.status] += 1
        if self.stages_completed != counts[AaplSnapshotStageStatus.COMPLETED]:
            raise ValueError("stages_completed does not match stage summaries")
        if self.stages_reused != counts[AaplSnapshotStageStatus.REUSED]:
            raise ValueError("stages_reused does not match stage summaries")
        if self.stages_skipped != counts[AaplSnapshotStageStatus.SKIPPED]:
            raise ValueError("stages_skipped does not match stage summaries")
        if not self.traceability_verified:
            raise ValueError("traceability_verified must be true")
        return self

    def stages(self) -> tuple[AaplSnapshotStageSummary, ...]:
        """Return the five stages in deterministic execution order."""
        return (
            self.market_fetch,
            self.market_statistics,
            self.market_diagnostic,
            self.fundamental_refresh,
            self.consolidated,
        )

    def to_json_dict(self) -> dict[str, object]:
        """Return a compact explicit JSON-compatible representation."""
        return {
            "request": self.request.model_dump(mode="json"),
            "source": self.source,
            "feed": self.feed,
            "stages": {stage.stage.value: stage.to_json_dict() for stage in self.stages()},
            "consolidated": self.consolidated.details.view.to_json_dict(),
            "overall_status": self.overall_status.value,
            "started_at": self.started_at.isoformat(),
            "completed_at": self.completed_at.isoformat(),
            "stages_completed": self.stages_completed,
            "stages_reused": self.stages_reused,
            "stages_skipped": self.stages_skipped,
            "traceability_verified": self.traceability_verified,
        }


__all__ = [
    "AaplCompleteSnapshotRequest",
    "AaplCompleteSnapshotSummary",
    "AaplSnapshotStage",
    "AaplSnapshotStageStatus",
    "AaplSnapshotStageSummary",
    "ConsolidatedQueryStageDetails",
    "FundamentalRefreshStageDetails",
    "MarketDiagnosticStageDetails",
    "MarketFetchStageDetails",
    "MarketStatisticsStageDetails",
]
