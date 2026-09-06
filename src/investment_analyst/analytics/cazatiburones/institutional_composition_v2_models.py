"""Strict ephemeral contracts for point-in-time 13F composition v2."""

from datetime import date
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import ConfigDict, Field, model_validator

from investment_analyst.analytics.cazatiburones import (
    institutional_composition_v2_definitions as definitions,
)
from investment_analyst.core.models.base import ContractModel, NonEmptyStr, UTCDateTime


class _Strict(ContractModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, str_strip_whitespace=True)


class InstitutionalCompositionV2Candidate(_Strict):
    artifact_id: UUID
    accession: NonEmptyStr
    manager_cik: NonEmptyStr
    report_period: date | None
    available_at: UTCDateTime
    is_amendment: bool
    amendment_number: NonEmptyStr | None = None
    amendment_type: NonEmptyStr | None = None
    declared_entry_total: int | None = Field(default=None, ge=0)
    declared_value_total: Decimal | None = Field(default=None, ge=0)
    observed_entry_total: int | None = Field(default=None, ge=0)
    observed_value_total: Decimal | None = Field(default=None, ge=0)


class InstitutionalCompositionV2Result(_Strict):
    manager_cik: NonEmptyStr
    report_period: date | None
    known_at: UTCDateTime
    policy_version: Literal["sec-13f-composition-policy-v2"] = (
        definitions.SEC_13F_COMPOSITION_V2_POLICY_VERSION
    )
    status: definitions.InstitutionalCompositionV2Status
    reason: definitions.InstitutionalCompositionV2Reason
    effective_artifact_id: UUID | None = None
    effective_accession: NonEmptyStr | None = None
    source_literal: definitions.Sec13fV2SourceLiteral | None = None
    operation: definitions.Sec13fV2Operation | None = None
    declared_entry_total: int | None = Field(default=None, ge=0)
    observed_entry_total: int | None = Field(default=None, ge=0)
    declared_value_total: Decimal | None = Field(default=None, ge=0)
    observed_value_total: Decimal | None = Field(default=None, ge=0)
    entry_total_matches: bool | None = None
    value_total_matches: bool | None = None

    @model_validator(mode="after")
    def coherent_effective_close(self) -> "InstitutionalCompositionV2Result":
        selected = self.effective_artifact_id is not None
        if selected != (self.effective_accession is not None):
            raise ValueError("effective artifact identity is incomplete")
        values_present = any(
            value is not None
            for value in (
                self.declared_entry_total,
                self.observed_entry_total,
                self.declared_value_total,
                self.observed_value_total,
                self.entry_total_matches,
                self.value_total_matches,
            )
        )
        if self.status in {"ambiguous", "insufficient"} and (selected or values_present):
            raise ValueError("unresolved result must not select or expose a close")
        if selected is False and values_present:
            raise ValueError("close totals require an effective artifact")
        if self.source_literal is not None and self.operation is None:
            raise ValueError("source literal requires an operation")
        if self.source_literal is None and self.operation is not None:
            raise ValueError("operation requires a source literal")
        return self
