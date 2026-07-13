"""Diagnostic results, components, and supporting evidence."""

from decimal import Decimal
from uuid import UUID, uuid4

from pydantic import Field, model_validator

from investment_analyst.core.models.base import ContractModel, NonEmptyStr, UTCDateTime
from investment_analyst.core.models.enums import (
    DataQuality,
    DiagnosticMode,
    DiagnosticVerdict,
    EvidenceDirection,
)

DECIMAL_TOLERANCE = Decimal("0.0001")


class DiagnosticEvidence(ContractModel):
    """Explanation of how one metric result affects a diagnostic."""

    metric_result_id: UUID
    direction: EvidenceDirection
    contribution: Decimal
    reason: NonEmptyStr


class DiagnosticComponent(ContractModel):
    """Explicitly weighted score for one diagnostic component."""

    component_key: NonEmptyStr
    score: Decimal = Field(ge=Decimal("0"), le=Decimal("100"))
    weight: Decimal = Field(ge=Decimal("0"), le=Decimal("1"))
    weighted_contribution: Decimal
    metric_result_ids: list[UUID] = Field(default_factory=list)
    explanation: NonEmptyStr

    @model_validator(mode="after")
    def validate_weighted_contribution(self) -> "DiagnosticComponent":
        """Verify the caller-provided contribution instead of deriving it silently."""
        expected = self.score * self.weight
        if abs(self.weighted_contribution - expected) > DECIMAL_TOLERANCE:
            raise ValueError("weighted_contribution must equal score multiplied by weight")
        return self


class DiagnosticResult(ContractModel):
    """Auditable deterministic diagnostic assembled from components and evidence."""

    diagnostic_id: UUID = Field(default_factory=uuid4)
    asset_id: NonEmptyStr
    mode: DiagnosticMode
    verdict: DiagnosticVerdict
    final_score: Decimal = Field(ge=Decimal("0"), le=Decimal("100"))
    confidence: Decimal = Field(ge=Decimal("0"), le=Decimal("1"))
    as_of: UTCDateTime
    available_at: UTCDateTime
    computed_at: UTCDateTime
    components: list[DiagnosticComponent] = Field(default_factory=list)
    evidence: list[DiagnosticEvidence] = Field(default_factory=list)
    algorithm_version: NonEmptyStr
    summary: NonEmptyStr
    quality: DataQuality

    @model_validator(mode="after")
    def validate_diagnostic_consistency(self) -> "DiagnosticResult":
        """Validate timing, component weights, score aggregation, and evidence."""
        if self.available_at > self.computed_at:
            raise ValueError("available_at must not be later than computed_at")

        if self.components:
            weight_sum = sum((component.weight for component in self.components), Decimal("0"))
            if abs(weight_sum - Decimal("1")) > DECIMAL_TOLERANCE:
                raise ValueError("component weights must sum to 1")

        if self.verdict is not DiagnosticVerdict.INSUFFICIENT_DATA:
            if not self.components or not self.evidence:
                raise ValueError("a normal diagnostic requires components and evidence")
            contribution_sum = sum(
                (component.weighted_contribution for component in self.components),
                Decimal("0"),
            )
            if abs(self.final_score - contribution_sum) > DECIMAL_TOLERANCE:
                raise ValueError("final_score must equal weighted component contributions")

        return self
