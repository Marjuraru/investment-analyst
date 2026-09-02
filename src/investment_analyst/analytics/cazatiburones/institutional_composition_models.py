"""Strict ephemeral contracts for point-in-time 13F composition."""

from datetime import date
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import ConfigDict, Field, model_validator

from investment_analyst.analytics.cazatiburones.institutional_composition_definitions import (
    SEC_13F_COMPOSITION_POLICY_VERSION,
    InstitutionalCompositionReason,
    InstitutionalCompositionStatus,
)
from investment_analyst.core.models.base import ContractModel, NonEmptyStr, UTCDateTime


class _Strict(ContractModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, str_strip_whitespace=True)


class InstitutionalCompositionCandidate(_Strict):
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


class InstitutionalCompositionResult(_Strict):
    manager_cik: NonEmptyStr
    report_period: date | None
    known_at: UTCDateTime
    policy_version: Literal["sec-13f-composition-policy-v1"] = SEC_13F_COMPOSITION_POLICY_VERSION
    status: InstitutionalCompositionStatus
    reason: InstitutionalCompositionReason
    effective_artifact_id: UUID | None = None
    effective_accession: NonEmptyStr | None = None
    declared_entry_total: int | None = Field(default=None, ge=0)
    observed_entry_total: int | None = Field(default=None, ge=0)
    declared_value_total: Decimal | None = Field(default=None, ge=0)
    observed_value_total: Decimal | None = Field(default=None, ge=0)
    entry_total_matches: bool | None = None
    value_total_matches: bool | None = None

    @model_validator(mode="after")
    def coherent_effective_close(self) -> "InstitutionalCompositionResult":
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
        return self
