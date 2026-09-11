"""Strict operational contracts for the two-close institutional 13F history window.

The window is the ordered pair of the two most recent adjacent official Form 13F Data Sets. The
contracts admit only an operational cut and a bounded accession budget: dataset periods, URLs,
hashes, snapshots, managers, assets and report periods are never free payload, they always come
from the live catalog and from persisted evidence.
"""

from __future__ import annotations

from datetime import date
from typing import Literal
from uuid import UUID

from pydantic import ConfigDict, Field, model_validator

from investment_analyst.core.models.base import ContractModel, NonEmptyStr, UTCDateTime
from investment_analyst.evidence.sec_documents.models import normalize_cik

SEC_INSTITUTIONAL_HISTORY_CYCLE_SCHEMA_VERSION = "sec-institutional-history-cycle-v1"
SEC_INSTITUTIONAL_HISTORY_POLICY_VERSION = "sec-institutional-history-policy-v1"
SEC_INSTITUTIONAL_HISTORY_WINDOW_SIZE = 2
MAX_HISTORY_ACCESSIONS_PER_PERIOD = 2


class _Strict(ContractModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, str_strip_whitespace=True)


class SecInstitutionalHistoryRequest(_Strict):
    """Operational bounds for one step of the two-close 13F history window."""

    known_at: UTCDateTime
    accessions_per_period: int = Field(
        default=MAX_HISTORY_ACCESSIONS_PER_PERIOD,
        ge=1,
        le=MAX_HISTORY_ACCESSIONS_PER_PERIOD,
    )


class SecInstitutionalHistoryPeriodSummary(_Strict):
    """One side of the window: its official dataset and its persisted universe snapshot."""

    role: Literal["older", "newer"]
    period_start: date
    period_end: date
    dataset_url: NonEmptyStr
    dataset_sha256: NonEmptyStr
    snapshot_id: UUID
    zip_downloaded: bool = False
    snapshot_reused: bool = False

    @model_validator(mode="after")
    def validate_period(self) -> SecInstitutionalHistoryPeriodSummary:
        if self.period_start > self.period_end:
            raise ValueError("dataset period start must precede period end")
        return self


class SecInstitutionalHistoryTargetSummary(_Strict):
    """Auditable outcome of one common manager processed inside the window."""

    asset_id: NonEmptyStr
    manager_cik: NonEmptyStr
    manager_name: NonEmptyStr
    older_report_period: date
    newer_report_period: date
    older_candidate_ids: tuple[UUID, ...] = ()
    newer_candidate_ids: tuple[UUID, ...] = ()
    state: Literal["processed", "failed"]
    reason_code: NonEmptyStr | None = None
    submissions_calls: int = Field(default=0, ge=0)
    archives_calls: int = Field(default=0, ge=0)
    created_accessions: tuple[NonEmptyStr, ...] = ()
    reused_accessions: tuple[NonEmptyStr, ...] = ()
    rejected_accessions: tuple[NonEmptyStr, ...] = ()
    failed_accessions: tuple[NonEmptyStr, ...] = ()
    older_observations_created: int = Field(default=0, ge=0)
    older_observations_reused: int = Field(default=0, ge=0)
    newer_observations_created: int = Field(default=0, ge=0)
    newer_observations_reused: int = Field(default=0, ge=0)
    metrics_created: int = Field(default=0, ge=0)
    metrics_reused: int = Field(default=0, ge=0)
    weights_created: int = Field(default=0, ge=0)
    weights_reused: int = Field(default=0, ge=0)
    events_created: int = Field(default=0, ge=0)
    event_candidates: int = Field(default=0, ge=0)
    non_evaluable: dict[NonEmptyStr, int] = Field(default_factory=dict)
    traceability_verified: bool = False

    @model_validator(mode="after")
    def validate_target(self) -> SecInstitutionalHistoryTargetSummary:
        if (self.state == "failed") != (self.reason_code is not None):
            raise ValueError("only a failed target declares a reason code")
        if self.older_report_period >= self.newer_report_period:
            raise ValueError("older report period must precede the newer report period")
        for name in (
            "created_accessions",
            "reused_accessions",
            "rejected_accessions",
            "failed_accessions",
        ):
            values = getattr(self, name)
            if len(set(values)) != len(values):
                raise ValueError(f"{name} must contain unique accessions")
        for name in ("older_candidate_ids", "newer_candidate_ids"):
            values = getattr(self, name)
            if len(set(values)) != len(values):
                raise ValueError(f"{name} must contain unique identifiers")
        if any(count < 0 for count in self.non_evaluable.values()):
            raise ValueError("non evaluable counters must not be negative")
        return self


class SecInstitutionalHistorySummary(_Strict):
    """Auditable, safe operational telemetry for one two-close history window attempt."""

    schema_version: Literal["sec-institutional-history-cycle-v1"] = (
        SEC_INSTITUTIONAL_HISTORY_CYCLE_SCHEMA_VERSION
    )
    policy_version: Literal["sec-institutional-history-policy-v1"] = (
        SEC_INSTITUTIONAL_HISTORY_POLICY_VERSION
    )
    effective_known_at: UTCDateTime
    status: Literal["preparing", "processed", "completed", "failed"]
    phase: Literal["preparing", "ready", "completed"]
    reason_code: NonEmptyStr | None = None
    catalog_calls: int = Field(default=0, ge=0)
    zip_calls: int = Field(default=0, ge=0)
    submissions_calls: int = Field(default=0, ge=0)
    archives_calls: int = Field(default=0, ge=0)
    periods: tuple[SecInstitutionalHistoryPeriodSummary, ...] = ()
    common_manager_count: int = Field(default=0, ge=0)
    excluded_manager_count: int = Field(default=0, ge=0)
    non_comparable_manager_count: int = Field(default=0, ge=0)
    total_targets: int = Field(default=0, ge=0)
    target_cursor_before: int = Field(default=0, ge=0)
    target_cursor_after: int = Field(default=0, ge=0)
    coverage_complete: bool = False
    manager_cik: NonEmptyStr | None = None
    manager_name: NonEmptyStr | None = None
    target: SecInstitutionalHistoryTargetSummary | None = None
    notifications_created: int = Field(default=0, ge=0)
    notifications_reused: int = Field(default=0, ge=0)
    source_ids: tuple[NonEmptyStr, ...] = ()

    @model_validator(mode="after")
    def validate_lifecycle_and_counters(self) -> SecInstitutionalHistorySummary:
        if (self.status == "failed") != (self.reason_code is not None):
            raise ValueError("failed history steps require exactly one reason code")
        if self.target_cursor_after < self.target_cursor_before:
            raise ValueError("target cursor must never regress")
        if len(self.periods) > SEC_INSTITUTIONAL_HISTORY_WINDOW_SIZE:
            raise ValueError("a declared window carries at most two datasets")
        roles = [item.role for item in self.periods]
        if len({item.role for item in self.periods}) != len(roles):
            raise ValueError("window datasets must be declared once per role")
        if roles and roles != ["older", "newer"] and roles not in (["older"], ["newer"]):
            raise ValueError("window datasets must be declared as older then newer")
        if self.status == "processed" and self.phase != "ready":
            raise ValueError("a processed step requires a ready window")
        if self.status == "completed" and self.phase != "completed":
            raise ValueError("a completed step requires the completed phase")
        if self.target is not None:
            if self.status == "preparing":
                raise ValueError("a preparing step does not report a target")
            if self.status == "failed" and self.target.state != "failed":
                raise ValueError("a failed step requires a failed target")
            if self.status in {"processed", "completed"} and self.target.state != "processed":
                raise ValueError("a processed step requires a processed target")
        return self

    def to_json_dict(self) -> dict[str, object]:
        """Return JSON-safe metadata without credentials, document content, or secrets."""
        return self.model_dump(mode="json")


def normalize_manager_cik(value: str) -> str:
    """Return the canonical zero-padded CIK used by every institutional contract."""
    return normalize_cik(value)


__all__ = [
    "MAX_HISTORY_ACCESSIONS_PER_PERIOD",
    "SEC_INSTITUTIONAL_HISTORY_CYCLE_SCHEMA_VERSION",
    "SEC_INSTITUTIONAL_HISTORY_POLICY_VERSION",
    "SEC_INSTITUTIONAL_HISTORY_WINDOW_SIZE",
    "SecInstitutionalHistoryPeriodSummary",
    "SecInstitutionalHistoryRequest",
    "SecInstitutionalHistorySummary",
    "SecInstitutionalHistoryTargetSummary",
    "normalize_manager_cik",
]
