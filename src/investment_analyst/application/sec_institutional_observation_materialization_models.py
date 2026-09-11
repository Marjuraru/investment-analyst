"""Strict contracts for directed institutional-observation materialization.

The request admits only a point-in-time cut and operational page bounds. Manager CIK, asset, CUSIP,
accession, report ID, row ID, URL and reported period are never free payload: the persisted universe
snapshot, the persisted 13F evidence and the live catalog remain the only authorities.
"""

from __future__ import annotations

from datetime import date
from typing import Literal
from uuid import UUID

from pydantic import ConfigDict, Field, field_validator, model_validator

from investment_analyst.core.models.base import ContractModel, NonEmptyStr, UTCDateTime
from investment_analyst.evidence.sec_documents.models import normalize_cik

SEC_INSTITUTIONAL_MATERIALIZATION_SCHEMA_VERSION = (
    "sec-institutional-observation-materialization-v1"
)
SEC_INSTITUTIONAL_MATERIALIZATION_POLICY_VERSION = (
    "sec-institutional-observation-materialization-page-v1"
)
MAX_MATERIALIZATION_MANAGERS_PER_PAGE = 25


class _Strict(ContractModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, str_strip_whitespace=True)


class SecInstitutionalObservationMaterializationRequest(_Strict):
    """Operational bounds of one directed materialization over the persisted manager universe."""

    known_at: UTCDateTime
    manager_offset: int = Field(default=0, ge=0)
    manager_limit: int = Field(default=1, ge=1, le=MAX_MATERIALIZATION_MANAGERS_PER_PAGE)


class SecInstitutionalMaterializationCandidateSummary(_Strict):
    """Per-candidate accounting of one directed materialization page.

    A candidate is exactly one ``(asset_id, cusip, manager_cik, report_period)`` tuple, so no
    cartesian product between managers and assets can be formed from this summary.
    """

    candidate_id: UUID
    asset_id: NonEmptyStr
    cusip: NonEmptyStr
    manager_cik: NonEmptyStr
    manager_name: NonEmptyStr
    report_period: date
    state: Literal["processed", "missing_report", "not_enriched", "missing_rows", "failed"]
    reason_code: NonEmptyStr | None = None
    report_ids: tuple[UUID, ...] = ()
    artifact_ids: tuple[UUID, ...] = ()
    rows_examined: int = Field(default=0, ge=0)
    rows_matched: int = Field(default=0, ge=0)
    rows_unmatched: int = Field(default=0, ge=0)
    claims_created: int = Field(default=0, ge=0)
    claims_reused: int = Field(default=0, ge=0)
    claims_ambiguous: int = Field(default=0, ge=0)
    claim_ids: tuple[UUID, ...] = ()
    skipped_by_reason: dict[NonEmptyStr, int] = Field(default_factory=dict)

    @field_validator("manager_cik")
    @classmethod
    def normalize_manager_cik(cls, value: str) -> str:
        return normalize_cik(value)

    @model_validator(mode="after")
    def reconcile(self) -> SecInstitutionalMaterializationCandidateSummary:
        if (self.state == "failed") != (self.reason_code is not None):
            raise ValueError("only a failed candidate declares a reason code")
        if self.rows_examined != self.rows_matched + self.rows_unmatched:
            raise ValueError("row counters must reconcile")
        if self.claims_created + self.claims_reused != len(self.claim_ids):
            raise ValueError("persisted claim counters must reconcile with the claim identifiers")
        if len(set(self.claim_ids)) != len(self.claim_ids):
            raise ValueError("claim identifiers must be unique")
        if len(set(self.report_ids)) != len(self.report_ids) or len(set(self.artifact_ids)) != len(
            self.artifact_ids
        ):
            raise ValueError("report and artifact identifiers must be unique")
        if self.claims_ambiguous and self.state == "processed":
            raise ValueError("an ambiguous claim set cannot be reported as processed")
        if any(count < 0 for count in self.skipped_by_reason.values()):
            raise ValueError("skipped_by_reason counts must not be negative")
        return self


class SecInstitutionalMaterializationRunSummary(_Strict):
    """Outcome of one integrated observation pass for one asset and one manager."""

    asset_id: NonEmptyStr
    manager_cik: NonEmptyStr
    report_ids: tuple[UUID, ...]
    state: Literal["processed", "failed"]
    reason_code: NonEmptyStr | None = None
    rows_linked: int = Field(default=0, ge=0)
    rows_unlinked: int = Field(default=0, ge=0)
    observations_created: int = Field(default=0, ge=0)
    observations_reused: int = Field(default=0, ge=0)
    skipped_by_reason: dict[NonEmptyStr, int] = Field(default_factory=dict)

    @field_validator("manager_cik")
    @classmethod
    def normalize_manager_cik(cls, value: str) -> str:
        return normalize_cik(value)

    @model_validator(mode="after")
    def reconcile(self) -> SecInstitutionalMaterializationRunSummary:
        if (self.state == "failed") != (self.reason_code is not None):
            raise ValueError("only a failed run declares a reason code")
        if len(set(self.report_ids)) != len(self.report_ids):
            raise ValueError("run report identifiers must be unique")
        return self


class SecInstitutionalObservationMaterializationSummary(_Strict):
    """Auditable outcome of one directed materialization page over the persisted universe."""

    schema_version: Literal["sec-institutional-observation-materialization-v1"] = (
        SEC_INSTITUTIONAL_MATERIALIZATION_SCHEMA_VERSION
    )
    policy_version: Literal["sec-institutional-observation-materialization-page-v1"] = (
        SEC_INSTITUTIONAL_MATERIALIZATION_POLICY_VERSION
    )
    request: SecInstitutionalObservationMaterializationRequest
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
    candidate_count: int = Field(ge=0)
    candidates: tuple[SecInstitutionalMaterializationCandidateSummary, ...]
    runs: tuple[SecInstitutionalMaterializationRunSummary, ...]
    claims_created: int = Field(ge=0)
    claims_reused: int = Field(ge=0)
    claims_ambiguous: int = Field(ge=0)
    observations_created: int = Field(ge=0)
    observations_reused: int = Field(ge=0)
    failed_candidates: int = Field(ge=0)
    failed_runs: int = Field(ge=0)
    traceability_verified: bool

    @model_validator(mode="after")
    def reconcile(self) -> SecInstitutionalObservationMaterializationSummary:
        if self.effective_known_at != self.request.known_at:
            raise ValueError("effective cut must equal the requested cut")
        if self.candidate_count != len(self.candidates):
            raise ValueError("candidate_count must equal the returned candidates")
        tuples = {(item.manager_cik, item.report_period) for item in self.candidates}
        if self.page_manager_count != len(tuples):
            raise ValueError("page_manager_count must match the distinct manager and period page")
        if self.page_manager_count > self.request.manager_limit:
            raise ValueError("page cannot exceed the requested manager limit")
        totals = {
            "claims_created": sum(item.claims_created for item in self.candidates),
            "claims_reused": sum(item.claims_reused for item in self.candidates),
            "claims_ambiguous": sum(item.claims_ambiguous for item in self.candidates),
            "observations_created": sum(item.observations_created for item in self.runs),
            "observations_reused": sum(item.observations_reused for item in self.runs),
            "failed_candidates": sum(item.state == "failed" for item in self.candidates),
            "failed_runs": sum(item.state == "failed" for item in self.runs),
        }
        for name, expected in totals.items():
            if getattr(self, name) != expected:
                raise ValueError(f"{name} must equal the sum over the page")
        return self

    def to_json_dict(self) -> dict[str, object]:
        """Return JSON-safe metadata without credentials, document content, or secrets."""
        return self.model_dump(mode="json")


__all__ = [
    "MAX_MATERIALIZATION_MANAGERS_PER_PAGE",
    "SEC_INSTITUTIONAL_MATERIALIZATION_POLICY_VERSION",
    "SEC_INSTITUTIONAL_MATERIALIZATION_SCHEMA_VERSION",
    "SecInstitutionalMaterializationCandidateSummary",
    "SecInstitutionalMaterializationRunSummary",
    "SecInstitutionalObservationMaterializationRequest",
    "SecInstitutionalObservationMaterializationSummary",
]
