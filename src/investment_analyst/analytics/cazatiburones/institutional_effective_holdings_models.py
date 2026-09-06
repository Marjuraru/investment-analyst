"""Strict, non-persisted public-effective Form 13F contracts."""

from datetime import date
from typing import Literal
from uuid import UUID

from pydantic import ConfigDict, Field, model_validator

from investment_analyst.analytics.cazatiburones import (
    institutional_effective_holdings_definitions as definitions,
)
from investment_analyst.core.models.base import ContractModel, NonEmptyStr, UTCDateTime
from investment_analyst.evidence.sec_documents.models import normalize_cik
from investment_analyst.evidence.sec_institutional_semantics.models import InstitutionalSemanticsRow


class _Strict(ContractModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, str_strip_whitespace=True)


class InstitutionalEffectiveHoldingsQuery(_Strict):
    manager_cik: NonEmptyStr
    report_period: date
    known_at: UTCDateTime
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=1000, ge=1, le=10_000)

    @model_validator(mode="after")
    def normalized_manager(self) -> "InstitutionalEffectiveHoldingsQuery":
        if self.manager_cik != normalize_cik(self.manager_cik):
            raise ValueError("manager_cik must be normalized")
        return self


class InstitutionalEffectiveContributor(_Strict):
    artifact_id: UUID
    accession: NonEmptyStr
    is_amendment: bool
    amendment_number: NonEmptyStr | None = None
    amendment_type: NonEmptyStr | None = None
    available_at: UTCDateTime
    report_type: NonEmptyStr | None = None
    confidential_omitted: bool | None = None
    other_manager_count: int = Field(ge=0)


class InstitutionalEffectiveHoldingRow(_Strict):
    source_artifact_id: UUID
    source_accession: NonEmptyStr
    source_row_id: UUID
    source_row_number: int = Field(ge=1)
    contributor_index: int = Field(ge=0)
    row: InstitutionalSemanticsRow

    @model_validator(mode="after")
    def retains_literal_row_identity(self) -> "InstitutionalEffectiveHoldingRow":
        if self.source_row_id != self.row.row_id or self.source_row_number != self.row.row_number:
            raise ValueError("effective row must retain source identity")
        return self


class InstitutionalEffectiveHoldingsResult(_Strict):
    manager_cik: NonEmptyStr
    report_period: date
    known_at: UTCDateTime
    policy_version: Literal["sec-13f-effective-public-holdings-policy-v1"] = (
        definitions.SEC_13F_EFFECTIVE_PUBLIC_HOLDINGS_POLICY_VERSION
    )
    status: definitions.InstitutionalEffectiveHoldingsStatus
    reason: definitions.InstitutionalEffectiveHoldingsReason
    contributors: tuple[InstitutionalEffectiveContributor, ...] = ()
    total_rows: int = Field(ge=0)
    offset: int = Field(ge=0)
    limit: int = Field(ge=1)
    truncated: bool
    rows: tuple[InstitutionalEffectiveHoldingRow, ...] = ()
    disclosure_limitations: tuple[NonEmptyStr, ...] = ()
    monetary_scale_status: Literal["unresolved"] = "unresolved"

    @model_validator(mode="after")
    def coherent_state(self) -> "InstitutionalEffectiveHoldingsResult":
        resolved = self.status == "effective"
        if not resolved and (self.contributors or self.rows or self.total_rows or self.truncated):
            raise ValueError("unresolved result cannot expose effective holdings")
        if len(self.rows) > self.limit or self.truncated != (
            self.offset + len(self.rows) < self.total_rows
        ):
            raise ValueError("effective holdings pagination is inconsistent")
        return self
