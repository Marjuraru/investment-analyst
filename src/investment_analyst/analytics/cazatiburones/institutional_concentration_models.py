"""Strict ephemeral contracts for point-in-time declared 13F concentration."""

from datetime import date
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import ConfigDict, Field, model_validator

from investment_analyst.analytics.cazatiburones.institutional_composition_definitions import (
    InstitutionalCompositionReason,
    InstitutionalCompositionStatus,
)
from investment_analyst.analytics.cazatiburones.institutional_concentration_definitions import (
    SEC_13F_CONCENTRATION_POLICY_VERSION,
    InstitutionalConcentrationReason,
    InstitutionalConcentrationStatus,
)
from investment_analyst.core.models.base import ContractModel, NonEmptyStr, UTCDateTime
from investment_analyst.core.models.enums import DataQuality


class _Strict(ContractModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, str_strip_whitespace=True)


class InstitutionalConcentrationPosition(_Strict):
    """One original 13F informationTable row, never pre-aggregated."""

    cusip: NonEmptyStr
    title_of_class: NonEmptyStr
    put_call: NonEmptyStr | None = None
    value_as_reported: Decimal = Field(ge=0)

    @property
    def declared_position_key(self) -> tuple[str, str, str | None]:
        return (self.cusip, self.title_of_class, self.put_call)


class InstitutionalConcentrationInput(_Strict):
    """Resolved effective close and its literal as-filed rows for a pure calculation."""

    manager_cik: NonEmptyStr
    report_period: date | None
    known_at: UTCDateTime
    close_status: InstitutionalCompositionStatus
    close_reason: InstitutionalCompositionReason
    effective_artifact_id: UUID | None = None
    effective_accession: NonEmptyStr | None = None
    accepted_at: UTCDateTime | None = None
    effective_close_total: Decimal | None = Field(default=None, ge=0)
    total_quality: DataQuality | None = None
    positions: tuple[InstitutionalConcentrationPosition, ...] = ()

    @model_validator(mode="after")
    def effective_close_is_coherent(self) -> "InstitutionalConcentrationInput":
        selected = self.effective_artifact_id is not None
        if selected != (self.effective_accession is not None):
            raise ValueError("effective artifact identity is incomplete")
        if (self.accepted_at is None) != (self.effective_close_total is None):
            raise ValueError("effective close monetary inputs are incomplete")
        if self.total_quality is None and self.effective_close_total is not None:
            raise ValueError("effective close total requires quality")
        if self.total_quality is not None and self.effective_close_total is None:
            raise ValueError("effective close quality requires total")
        if not selected and (
            self.accepted_at is not None or self.effective_close_total is not None or self.positions
        ):
            raise ValueError("unselected close must not expose rows or monetary inputs")
        return self


class InstitutionalConcentrationResult(_Strict):
    """Read-only declared concentration for one resolved effective close."""

    manager_cik: NonEmptyStr
    report_period: date | None
    known_at: UTCDateTime
    policy_version: Literal["sec-13f-concentration-policy-v1"] = (
        SEC_13F_CONCENTRATION_POLICY_VERSION
    )
    status: InstitutionalConcentrationStatus
    reason: InstitutionalConcentrationReason
    close_status: InstitutionalCompositionStatus
    close_reason: InstitutionalCompositionReason
    effective_artifact_id: UUID | None = None
    effective_accession: NonEmptyStr | None = None
    quality: DataQuality | None = None
    position_count: int | None = Field(default=None, ge=0)
    largest_declared_weight: Decimal | None = Field(default=None, ge=0)
    top_five_declared_weight: Decimal | None = Field(default=None, ge=0)
    top_ten_declared_weight: Decimal | None = Field(default=None, ge=0)
    herfindahl_index: Decimal | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def calculated_values_are_coherent(self) -> "InstitutionalConcentrationResult":
        selected = self.effective_artifact_id is not None
        if selected != (self.effective_accession is not None):
            raise ValueError("effective artifact identity is incomplete")
        magnitudes = (
            self.position_count,
            self.largest_declared_weight,
            self.top_five_declared_weight,
            self.top_ten_declared_weight,
            self.herfindahl_index,
        )
        if self.status == "omitted":
            if (
                self.reason == "calculated"
                or self.quality is not None
                or any(value is not None for value in magnitudes)
            ):
                raise ValueError("omitted result must not expose calculated magnitudes")
            return self
        if (
            self.reason != "calculated"
            or self.close_status not in {"original_complete", "amended"}
            or not selected
            or self.quality is None
            or self.position_count is None
            or self.largest_declared_weight is None
            or self.herfindahl_index is None
        ):
            raise ValueError("calculated result is incomplete")
        if self.position_count < 5 and self.top_five_declared_weight is not None:
            raise ValueError("top five is absent below five positions")
        if self.position_count >= 5 and self.top_five_declared_weight is None:
            raise ValueError("top five is required from five positions")
        if self.position_count < 10 and self.top_ten_declared_weight is not None:
            raise ValueError("top ten is absent below ten positions")
        if self.position_count >= 10 and self.top_ten_declared_weight is None:
            raise ValueError("top ten is required from ten positions")
        return self
