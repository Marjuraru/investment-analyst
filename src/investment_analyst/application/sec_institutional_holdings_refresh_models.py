"""Strict contracts for the directed, resumable Form 13F acquisition from the manager universe.

The request admits only operational bounds. Manager CIK, asset, CUSIP, accession, form, URL, and
report period are never free payload: the persisted universe snapshot and the fresh official
Submissions remain the only authorities over what may be imported.
"""

from __future__ import annotations

from datetime import date
from typing import Literal
from uuid import UUID

from pydantic import ConfigDict, Field, field_validator, model_validator

from investment_analyst.core.models.base import ContractModel, NonEmptyStr, UTCDateTime
from investment_analyst.evidence.sec_documents.models import normalize_cik

SEC_INSTITUTIONAL_HOLDINGS_DIRECTED_REFRESH_SCHEMA_VERSION = (
    "sec-institutional-holdings-directed-refresh-v1"
)
SEC_INSTITUTIONAL_HOLDINGS_DIRECTED_REFRESH_POLICY = "sec-institutional-holdings-directed-page-v1"
MAX_DIRECTED_MANAGERS_PER_PAGE = 25
MAX_DIRECTED_ACCESSIONS_PER_MANAGER = 10


class _Strict(ContractModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, str_strip_whitespace=True)


class SecInstitutionalHoldingsDirectedRefreshRequest(_Strict):
    """Operational bounds of one bounded 13F acquisition over the persisted manager universe."""

    known_at: UTCDateTime
    manager_offset: int = Field(default=0, ge=0)
    manager_limit: int = Field(default=1, ge=1, le=MAX_DIRECTED_MANAGERS_PER_PAGE)
    accessions_per_manager: int = Field(default=1, ge=1, le=MAX_DIRECTED_ACCESSIONS_PER_MANAGER)


class SecInstitutionalHoldingsDirectedManagerSummary(_Strict):
    """Per-manager outcome of the directed acquisition with reconcilable counters."""

    manager_cik: NonEmptyStr
    manager_name: NonEmptyStr
    report_period: date
    state: Literal["processed", "failed"]
    reason_code: NonEmptyStr | None = None
    candidate_ids: tuple[UUID, ...]
    candidate_asset_ids: tuple[NonEmptyStr, ...]
    candidate_cusips: tuple[NonEmptyStr, ...]
    dataset_accession_hints: tuple[NonEmptyStr, ...]
    dataset_accession_lineage: tuple[NonEmptyStr, ...]
    submissions_calls: int = Field(ge=0, le=1)
    submissions_created: int = Field(ge=0, le=1)
    submissions_reused: int = Field(ge=0, le=1)
    submissions_raw_record_id: UUID | None = None
    submissions_checked_at: UTCDateTime | None = None
    eligible_accessions: tuple[NonEmptyStr, ...] = ()
    reused_accessions: tuple[NonEmptyStr, ...] = ()
    attempted_accessions: tuple[NonEmptyStr, ...] = ()
    created_accessions: tuple[NonEmptyStr, ...] = ()
    rejected_accessions: tuple[NonEmptyStr, ...] = ()
    failed_accessions: tuple[NonEmptyStr, ...] = ()
    failure_codes: tuple[NonEmptyStr, ...] = ()
    report_ids: tuple[UUID, ...] = ()
    pending_before: int = Field(default=0, ge=0)
    backlog_after: int = Field(default=0, ge=0)
    archives_calls: int = Field(default=0, ge=0)
    semantics_examined: int = Field(default=0, ge=0)
    semantics_created: int = Field(default=0, ge=0)
    semantics_reused: int = Field(default=0, ge=0)
    semantics_not_visible: int = Field(default=0, ge=0)
    semantics_rejected: int = Field(default=0, ge=0)

    @field_validator("manager_cik")
    @classmethod
    def normalize_manager_cik(cls, value: str) -> str:
        return normalize_cik(value)

    @model_validator(mode="after")
    def validate_counters(self) -> SecInstitutionalHoldingsDirectedManagerSummary:
        if (self.state == "failed") != (self.reason_code is not None):
            raise ValueError("failed managers declare exactly one reason code")
        if not self.candidate_ids or len(set(self.candidate_ids)) != len(self.candidate_ids):
            raise ValueError("candidate lineage must contain unique candidate identifiers")
        for name in (
            "candidate_asset_ids",
            "candidate_cusips",
            "dataset_accession_hints",
            "dataset_accession_lineage",
        ):
            values = getattr(self, name)
            if not values or values != tuple(sorted(set(values))):
                raise ValueError(f"{name} must be non-empty, sorted, and unique")
        if self.submissions_created + self.submissions_reused > 1:
            raise ValueError("one manager uses at most one Submissions snapshot")
        if self.submissions_created and self.submissions_reused:
            raise ValueError("one Submissions snapshot is created or reused, never both")
        if len(self.failure_codes) != len(self.failed_accessions):
            raise ValueError("failure codes must describe exactly the failed accessions")
        if self.submissions_raw_record_id is None:
            if self.submissions_created or self.submissions_reused:
                raise ValueError("a failed Submissions snapshot cannot claim persistence")
            if any(
                (
                    self.eligible_accessions,
                    self.reused_accessions,
                    self.attempted_accessions,
                    self.created_accessions,
                    self.rejected_accessions,
                    self.failed_accessions,
                    self.report_ids,
                )
            ):
                raise ValueError("a failed Submissions snapshot cannot claim accessions or reports")
            if self.pending_before or self.backlog_after or self.archives_calls:
                raise ValueError("a failed Submissions snapshot cannot claim pending work")
        elif self.submissions_calls != 1 or self.submissions_created + self.submissions_reused != 1:
            raise ValueError("a persisted Submissions snapshot requires exactly one call")
        if (self.submissions_checked_at is None) != (self.submissions_raw_record_id is None):
            raise ValueError("submissions cut must accompany its persisted snapshot")
        for name in (
            "eligible_accessions",
            "reused_accessions",
            "attempted_accessions",
            "created_accessions",
            "rejected_accessions",
            "failed_accessions",
        ):
            values = getattr(self, name)
            if len(set(values)) != len(values):
                raise ValueError(f"{name} must be unique")
        eligible = set(self.eligible_accessions)
        reused = set(self.reused_accessions)
        attempted = set(self.attempted_accessions)
        if not reused.issubset(eligible):
            raise ValueError("reused accessions must be eligible at the cut")
        if not attempted.issubset(eligible - reused):
            raise ValueError("attempted accessions must be pending and eligible")
        outcomes = (
            set(self.created_accessions)
            | set(self.rejected_accessions)
            | set(self.failed_accessions)
        )
        if outcomes != attempted or (
            len(self.created_accessions)
            + len(self.rejected_accessions)
            + len(self.failed_accessions)
            != len(self.attempted_accessions)
        ):
            raise ValueError("accession outcomes must partition the attempted accessions")
        if len(self.report_ids) < len(self.created_accessions) or len(set(self.report_ids)) != len(
            self.report_ids
        ):
            raise ValueError("visible reports must cover the created accessions exactly once")
        if self.pending_before != len(eligible) - len(reused):
            raise ValueError("pending_before must count the non-reused eligible accessions")
        if self.backlog_after != self.pending_before - len(self.created_accessions):
            raise ValueError("backlog_after must subtract only materialized accessions")
        if self.state == "processed" and self.submissions_raw_record_id is None:
            raise ValueError("a processed manager requires one verified Submissions snapshot")
        if (
            self.semantics_examined
            != self.semantics_created
            + self.semantics_reused
            + self.semantics_not_visible
            + self.semantics_rejected
        ):
            raise ValueError("semantic counters must reconcile with the examined reports")
        return self


class SecInstitutionalHoldingsDirectedRefreshSummary(_Strict):
    """Auditable outcome of one directed 13F page over the persisted manager universe."""

    schema_version: Literal["sec-institutional-holdings-directed-refresh-v1"] = (
        SEC_INSTITUTIONAL_HOLDINGS_DIRECTED_REFRESH_SCHEMA_VERSION
    )
    policy_version: Literal["sec-institutional-holdings-directed-page-v1"] = (
        SEC_INSTITUTIONAL_HOLDINGS_DIRECTED_REFRESH_POLICY
    )
    request: SecInstitutionalHoldingsDirectedRefreshRequest
    effective_known_at: UTCDateTime
    snapshot_id: UUID
    snapshot_raw_record_id: UUID
    dataset_revision_id: UUID
    dataset_sha256: NonEmptyStr
    snapshot_period_start: date
    snapshot_period_end: date
    snapshot_available_at: UTCDateTime
    universe_selected_manager_count: int = Field(ge=0)
    universe_coverage_complete: bool
    page_manager_count: int = Field(ge=0)
    managers: tuple[SecInstitutionalHoldingsDirectedManagerSummary, ...]
    submissions_calls: int = Field(ge=0)
    archives_calls: int = Field(ge=0)
    created: int = Field(ge=0)
    reused: int = Field(ge=0)
    rejected_or_failed: int = Field(ge=0)
    backlog_after: int = Field(ge=0)
    semantics_created: int = Field(ge=0)
    semantics_reused: int = Field(ge=0)
    traceability_verified: bool

    @model_validator(mode="after")
    def validate_page(self) -> SecInstitutionalHoldingsDirectedRefreshSummary:
        if self.effective_known_at != self.request.known_at:
            raise ValueError("effective cut must equal the requested cut")
        if self.page_manager_count != len(self.managers):
            raise ValueError("page_manager_count must match the returned managers")
        if self.page_manager_count > self.request.manager_limit:
            raise ValueError("page cannot exceed the requested manager limit")
        ciks = tuple(item.manager_cik for item in self.managers)
        if len(set(ciks)) != len(ciks):
            raise ValueError("one page processes each manager once")
        totals = {
            "submissions_calls": sum(item.submissions_calls for item in self.managers),
            "archives_calls": sum(item.archives_calls for item in self.managers),
            "created": sum(len(item.created_accessions) for item in self.managers),
            "reused": sum(len(item.reused_accessions) for item in self.managers),
            "rejected_or_failed": sum(
                len(item.rejected_accessions) + len(item.failed_accessions)
                for item in self.managers
            ),
            "backlog_after": sum(item.backlog_after for item in self.managers),
            "semantics_created": sum(item.semantics_created for item in self.managers),
            "semantics_reused": sum(item.semantics_reused for item in self.managers),
        }
        for name, expected in totals.items():
            if getattr(self, name) != expected:
                raise ValueError(f"{name} must equal the sum over the returned managers")
        return self

    def to_json_dict(self) -> dict[str, object]:
        """Return JSON-safe metadata without credentials, headers, or document content."""
        return self.model_dump(mode="json")


__all__ = [
    "MAX_DIRECTED_ACCESSIONS_PER_MANAGER",
    "MAX_DIRECTED_MANAGERS_PER_PAGE",
    "SEC_INSTITUTIONAL_HOLDINGS_DIRECTED_REFRESH_POLICY",
    "SEC_INSTITUTIONAL_HOLDINGS_DIRECTED_REFRESH_SCHEMA_VERSION",
    "SecInstitutionalHoldingsDirectedManagerSummary",
    "SecInstitutionalHoldingsDirectedRefreshRequest",
    "SecInstitutionalHoldingsDirectedRefreshSummary",
]
