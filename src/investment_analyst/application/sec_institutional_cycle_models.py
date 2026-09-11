"""Strict operational contracts for the scheduled Form 13F cycle."""

from __future__ import annotations

from datetime import date
from typing import Literal
from uuid import UUID

from pydantic import ConfigDict, Field, model_validator

from investment_analyst.core.models.base import ContractModel, NonEmptyStr, UTCDateTime

SEC_INSTITUTIONAL_SCHEDULED_CYCLE_SCHEMA_VERSION = "sec-institutional-scheduled-cycle-v1"
SEC_INSTITUTIONAL_CYCLE_POLICY = "sec-institutional-cycle-policy-v1"
MAX_CYCLE_ACCESSIONS_PER_MANAGER = 2


class _Strict(ContractModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, str_strip_whitespace=True)


class SecInstitutionalCycleRequest(_Strict):
    """Operational bounds for one step of the scheduled 13F cycle."""

    known_at: UTCDateTime
    force_dataset_refresh: bool = False
    accessions_per_manager: int = Field(
        default=MAX_CYCLE_ACCESSIONS_PER_MANAGER,
        ge=1,
        le=MAX_CYCLE_ACCESSIONS_PER_MANAGER,
    )


class SecInstitutionalCycleSummary(_Strict):
    """Auditable, safe operational telemetry for one scheduled 13F cycle attempt."""

    schema_version: Literal["sec-institutional-scheduled-cycle-v1"] = (
        SEC_INSTITUTIONAL_SCHEDULED_CYCLE_SCHEMA_VERSION
    )
    policy_version: Literal["sec-institutional-cycle-policy-v1"] = SEC_INSTITUTIONAL_CYCLE_POLICY
    effective_known_at: UTCDateTime
    status: Literal["processed", "completed", "skipped", "failed"]
    reason_code: NonEmptyStr | None = None
    catalog_calls: int = Field(default=0, ge=0)
    zip_calls: int = Field(default=0, ge=0)
    submissions_calls: int = Field(default=0, ge=0)
    archives_calls: int = Field(default=0, ge=0)
    dataset_period_start: date | None = None
    dataset_period_end: date | None = None
    dataset_url: NonEmptyStr | None = None
    dataset_sha256: NonEmptyStr | None = None
    snapshot_id: UUID | None = None
    manager_cursor_before: int = Field(default=0, ge=0)
    manager_cursor_after: int = Field(default=0, ge=0)
    total_managers: int = Field(default=0, ge=0)
    coverage_complete: bool = False
    manager_cik: NonEmptyStr | None = None
    manager_name: NonEmptyStr | None = None
    report_period: date | None = None
    created_accessions: tuple[NonEmptyStr, ...] = ()
    reused_accessions: tuple[NonEmptyStr, ...] = ()
    rejected_accessions: tuple[NonEmptyStr, ...] = ()
    failed_accessions: tuple[NonEmptyStr, ...] = ()
    backlog_after: int = Field(default=0, ge=0)
    observations_created: int = Field(default=0, ge=0)
    observations_reused: int = Field(default=0, ge=0)
    traceability_verified: bool = False
    source_ids: tuple[NonEmptyStr, ...] = ()

    @property
    def created_accessions_count(self) -> int:
        return len(self.created_accessions)

    @property
    def reused_accessions_count(self) -> int:
        return len(self.reused_accessions)

    @model_validator(mode="after")
    def validate_lifecycle_and_counters(self) -> SecInstitutionalCycleSummary:
        if (self.status == "failed") != (self.reason_code is not None):
            raise ValueError("failed cycle steps require exactly one reason code")
        if self.manager_cursor_after < self.manager_cursor_before:
            raise ValueError("manager cursor must never regress")
        for name in (
            "created_accessions",
            "reused_accessions",
            "rejected_accessions",
            "failed_accessions",
        ):
            values = getattr(self, name)
            if len(set(values)) != len(values):
                raise ValueError(f"{name} must contain unique accessions")
        if (self.dataset_period_start is None) != (self.dataset_period_end is None):
            raise ValueError("dataset period start and end must accompany each other")
        if (
            self.dataset_period_start is not None
            and self.dataset_period_end is not None
            and self.dataset_period_start > self.dataset_period_end
        ):
            raise ValueError("dataset period start must precede period end")
        return self
