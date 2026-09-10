"""Strict application contracts for incremental SEC declared-activity refreshes."""

from typing import Literal

from pydantic import ConfigDict, Field, model_validator

from investment_analyst.core.models.base import ContractModel, NonEmptyStr, UTCDateTime


class SecDeclaredActivityRefreshRequest(ContractModel):
    """Request the declared-activity policy for one SEC issuer only.

    Forms, limits, and family selection are not eligible here: they are versioned policy, never
    free payload.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    asset_id: NonEmptyStr


class SecDeclaredActivityFamilySummary(ContractModel):
    """One declared-activity family with separated, exact counters for a single run."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    family: Literal["insider", "beneficial"]
    source_id: NonEmptyStr
    forms_evaluated: tuple[NonEmptyStr, ...]
    forms_missing: tuple[NonEmptyStr, ...]
    accessions_selected: tuple[NonEmptyStr, ...]
    accessions_imported: tuple[NonEmptyStr, ...]
    accessions_reused: tuple[NonEmptyStr, ...]
    accessions_rejected: tuple[NonEmptyStr, ...]
    accessions_incomplete: tuple[NonEmptyStr, ...]
    backlog_count: int = Field(ge=0)
    statements_created: int = Field(ge=0)
    statements_reused: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_family_counts(self) -> "SecDeclaredActivityFamilySummary":
        """Keep every declared counter reconcilable with the selected accessions."""
        for name in (
            "forms_evaluated",
            "forms_missing",
            "accessions_imported",
            "accessions_reused",
            "accessions_rejected",
            "accessions_incomplete",
        ):
            values = getattr(self, name)
            if values != tuple(sorted(set(values))):
                raise ValueError(f"{name} must be sorted and unique")
        if len(set(self.accessions_selected)) != len(self.accessions_selected):
            raise ValueError("accessions_selected must be unique")
        if not set(self.forms_missing).issubset(self.forms_evaluated):
            raise ValueError("forms_missing must be evaluated")
        partition = (
            set(self.accessions_imported)
            | set(self.accessions_reused)
            | set(self.accessions_rejected)
            | set(self.accessions_incomplete)
        )
        if partition != set(self.accessions_selected) or (
            len(self.accessions_imported)
            + len(self.accessions_reused)
            + len(self.accessions_rejected)
            + len(self.accessions_incomplete)
            != len(self.accessions_selected)
        ):
            raise ValueError("family outcome counters must partition the selected accessions")
        if self.statements_created != len(self.accessions_imported) or (
            self.statements_reused != len(self.accessions_reused)
        ):
            raise ValueError("statement counters must match imported and reused accessions")
        return self


class SecDeclaredActivityRefreshSummary(ContractModel):
    """Auditable outcome of one fresh Submissions-driven declared-activity refresh."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["sec-declared-activity-refresh-v1"] = "sec-declared-activity-refresh-v1"
    asset_id: NonEmptyStr
    request: SecDeclaredActivityRefreshRequest
    policy_version: Literal["sec-declared-activity-selection-v1"] = (
        "sec-declared-activity-selection-v1"
    )
    submissions_source_id: NonEmptyStr
    submissions_raw_record_id: NonEmptyStr
    submissions_checked_at: UTCDateTime
    submissions_record_available_at: UTCDateTime
    submissions_created: int = Field(ge=0, le=1)
    submissions_reused: int = Field(ge=0, le=1)
    insider: SecDeclaredActivityFamilySummary
    beneficial: SecDeclaredActivityFamilySummary
    observations_created: int = Field(ge=0)
    observations_reused: int = Field(ge=0)
    observations_skipped: int = Field(ge=0)
    metrics_created: int = Field(ge=0)
    metrics_reused: int = Field(ge=0)
    metrics_skipped: int = Field(ge=0)
    backlog_count: int = Field(ge=0)
    coverage_complete: bool
    traceability_verified: bool

    @model_validator(mode="after")
    def validate_run(self) -> "SecDeclaredActivityRefreshSummary":
        """Keep families separate and coverage honest about the remaining delta."""
        if self.asset_id != self.request.asset_id:
            raise ValueError("summary asset_id must match request")
        if self.insider.family != "insider" or self.beneficial.family != "beneficial":
            raise ValueError("families must be declared with their own identity")
        if self.insider.source_id == self.beneficial.source_id:
            raise ValueError("families must keep separated source identities")
        if self.submissions_created + self.submissions_reused != 1:
            raise ValueError("one fresh submissions snapshot must be persisted or reused")
        if self.backlog_count != self.insider.backlog_count + self.beneficial.backlog_count:
            raise ValueError("backlog_count must sum both families")
        if self.coverage_complete != (self.backlog_count == 0 and self.traceability_verified):
            raise ValueError("coverage requires an empty backlog and verified traceability")
        return self

    def to_json_dict(self) -> dict[str, object]:
        """Return JSON-safe metadata without URLs, headers, or credentials."""
        return self.model_dump(mode="json")


__all__ = [
    "SecDeclaredActivityFamilySummary",
    "SecDeclaredActivityRefreshRequest",
    "SecDeclaredActivityRefreshSummary",
]
