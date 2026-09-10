"""Strict application contracts for incremental SEC primary-document refreshes."""

from typing import Literal

from pydantic import ConfigDict, Field, model_validator

from investment_analyst.core.models.base import ContractModel, NonEmptyStr, UTCDateTime


class SecPrimaryDocumentRefreshRequest(ContractModel):
    """Request the declared primary-document policy for one SEC issuer only."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    asset_id: NonEmptyStr


class SecPrimaryDocumentRefreshSummary(ContractModel):
    """Auditable outcome of one fresh Submissions-driven document refresh."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["sec-primary-document-refresh-v1"] = "sec-primary-document-refresh-v1"
    asset_id: NonEmptyStr
    request: SecPrimaryDocumentRefreshRequest
    policy_version: Literal["sec-primary-document-policy-v1"] = "sec-primary-document-policy-v1"
    source_id: NonEmptyStr
    submissions_source_id: NonEmptyStr
    submissions_raw_record_id: NonEmptyStr
    submissions_checked_at: UTCDateTime
    submissions_record_available_at: UTCDateTime
    forms_evaluated: tuple[NonEmptyStr, ...]
    forms_missing: tuple[NonEmptyStr, ...]
    accessions_selected: tuple[NonEmptyStr, ...]
    accessions_fetched: tuple[NonEmptyStr, ...]
    accessions_reused: tuple[NonEmptyStr, ...]
    submissions_created: int = Field(ge=0, le=1)
    submissions_reused: int = Field(ge=0, le=1)
    revisions_created: int = Field(ge=0)
    revisions_reused: int = Field(ge=0)
    blobs_created: int = Field(ge=0)
    blobs_reused: int = Field(ge=0)
    document_fetch_calls: int = Field(ge=0)
    coverage_complete: bool
    traceability_verified: bool

    @model_validator(mode="after")
    def validate_counts(self) -> "SecPrimaryDocumentRefreshSummary":
        if self.asset_id != self.request.asset_id:
            raise ValueError("summary asset_id must match request")
        if self.forms_evaluated != tuple(sorted(set(self.forms_evaluated))):
            raise ValueError("forms_evaluated must be sorted and unique")
        if self.forms_missing != tuple(sorted(set(self.forms_missing))):
            raise ValueError("forms_missing must be sorted and unique")
        if not set(self.forms_missing).issubset(self.forms_evaluated):
            raise ValueError("forms_missing must be evaluated")
        if len(self.accessions_selected) != len(set(self.accessions_selected)):
            raise ValueError("selected accessions must be unique")
        if set(self.accessions_fetched) | set(self.accessions_reused) != set(
            self.accessions_selected
        ):
            raise ValueError("fetched and reused accessions must partition selected accessions")
        if set(self.accessions_fetched) & set(self.accessions_reused):
            raise ValueError("fetched and reused accessions must not overlap")
        if self.document_fetch_calls != len(self.accessions_fetched):
            raise ValueError("document fetch calls must match fetched accessions")
        if self.revisions_created + self.revisions_reused != len(self.accessions_selected):
            raise ValueError("revision counts must match selected accessions")
        if self.submissions_created + self.submissions_reused != 1:
            raise ValueError("one fresh submissions snapshot must be persisted or reused")
        if self.coverage_complete != self.traceability_verified:
            raise ValueError("coverage requires verified traceability")
        return self

    def to_json_dict(self) -> dict[str, object]:
        """Return JSON-safe metadata without URLs, headers, or credentials."""
        return self.model_dump(mode="json")


__all__ = ["SecPrimaryDocumentRefreshRequest", "SecPrimaryDocumentRefreshSummary"]
