"""Small immutable operational snapshot for latency-sensitive local reads."""

from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from pydantic import ConfigDict, Field

from investment_analyst.application.manual_operations import ManualOperationStatus
from investment_analyst.core.models.base import ContractModel, NonEmptyStr, UTCDateTime


class OperationalOverviewSnapshot(ContractModel):
    """Versioned health summary that excludes retained histories and provider payloads."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["operational-overview-snapshot-v1"] = "operational-overview-snapshot-v1"
    generated_at: UTCDateTime
    operational_status: NonEmptyStr
    workspace_status: NonEmptyStr
    workspace_id: UUID | None = None
    latest_run_id: UUID | None = None
    latest_run_status: NonEmptyStr | None = None
    scheduler_enabled: bool
    scheduled_job_count: int = Field(default=0, ge=0)
    scheduled_running_count: int = Field(default=0, ge=0)
    scheduled_failed_count: int = Field(default=0, ge=0)
    scheduled_blocked_count: int = Field(default=0, ge=0)
    scheduled_retry_wait_count: int = Field(default=0, ge=0)
    scheduled_current_count: int = Field(default=0, ge=0)
    scheduled_stale_count: int = Field(default=0, ge=0)
    scheduled_incomplete_count: int = Field(default=0, ge=0)
    scheduled_next_run_at: UTCDateTime | None = None
    scheduled_next_retry_at: UTCDateTime | None = None
    queued_operation_count: int = Field(default=0, ge=0)
    running_operation_count: int = Field(default=0, ge=0)
    failed_operation_count: int = Field(default=0, ge=0)
    latest_operation_id: UUID | None = None
    latest_operation_status: ManualOperationStatus | None = None
    watchlist_asset_count: int = Field(default=0, ge=0)
    favorite_asset_count: int = Field(default=0, ge=0)
    scheduled_asset_count: int = Field(default=0, ge=0)
    unavailable_preference_count: int = Field(default=0, ge=0)

    @classmethod
    def now(cls, **values: object) -> "OperationalOverviewSnapshot":
        """Build one UTC snapshot at the adapter boundary."""
        return cls(generated_at=datetime.now(UTC), **values)

    def to_json_dict(self) -> dict[str, object]:
        """Return compact JSON primitives."""
        return self.model_dump(mode="json")


__all__ = ["OperationalOverviewSnapshot"]
