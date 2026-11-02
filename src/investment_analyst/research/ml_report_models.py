"""Read-only projection of one sealed ML baseline run for the local interface.

The projection is an isolated presentation contract: it reuses the immutable ML
v1 models but never reinterprets them, never exposes per-row predictions and
never changes the sealed artifact. A negative research result is represented as
exactly that, not as an inferred success or a zero.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Literal
from uuid import UUID

from pydantic import Field, model_validator

from investment_analyst.core.models.base import ContractModel, NonEmptyStr, UTCDateTime
from investment_analyst.research.historical_models import (
    EXPERIMENT_SPEC_ID,
    FEATURE_SET_VERSION,
)
from investment_analyst.research.ml_models import (
    DATASET_MANIFEST_SCHEMA_VERSION,
    PROTOCOL_ID,
    MlArtifactStatus,
    MlBaselineProtocol,
    MlDecisionCriteria,
    MlDevelopmentResult,
    MlEnvironment,
    MlFoldBoundary,
    MlHoldoutResult,
    MlProtocolTarget,
    MlTimings,
)

REPORT_VIEW_SCHEMA_VERSION = "ml-research-report-view-v1"

SOURCE_ID_NOTE = (
    "El protocolo ML sellado identifica asset_id, dataset_id y hashes, pero no source_id,"
    " venue ni fecha de publicación; esta vista no los infiere del ticker ni abre el dataset."
)

_SHA256 = re.compile(r"[0-9a-f]{64}")


def _require_sha256(value: str, label: str) -> str:
    if _SHA256.fullmatch(value) is None:
        raise ValueError(f"{label} must be a lowercase SHA-256 digest")
    return value


class MlReportProvenance(ContractModel):
    """Exact dataset and asset references available in the sealed artifact."""

    asset_id: NonEmptyStr
    dataset_id: UUID
    dataset_content_sha256: str
    dataset_manifest_sha256: str
    dataset_rows_sha256: str
    source_id: None = None
    source_id_note: NonEmptyStr = SOURCE_ID_NOTE

    @model_validator(mode="after")
    def validate_provenance(self) -> MlReportProvenance:
        for value, label in (
            (self.dataset_content_sha256, "dataset_content_sha256"),
            (self.dataset_manifest_sha256, "dataset_manifest_sha256"),
            (self.dataset_rows_sha256, "dataset_rows_sha256"),
        ):
            _require_sha256(value, label)
        return self


class MlReportArtifact(ContractModel):
    """Identity and physical evidence of the selected sealed run directory."""

    run_id: UUID
    status: MlArtifactStatus
    protocol_id: Literal["crypto-historical-5d-baselines-v1"] = PROTOCOL_ID
    created_at: UTCDateTime
    protocol_sha256: str
    report_sha256: str
    manifest_sha256: str
    selection_sha256: str | None
    predictions_sha256: str | None
    code_material_sha256: str
    artifact_bytes: int = Field(gt=0)
    prediction_rows: int = Field(ge=0)
    environment: MlEnvironment

    @model_validator(mode="after")
    def validate_artifact(self) -> MlReportArtifact:
        for value, label in (
            (self.protocol_sha256, "protocol_sha256"),
            (self.report_sha256, "report_sha256"),
            (self.manifest_sha256, "manifest_sha256"),
            (self.code_material_sha256, "code_material_sha256"),
        ):
            _require_sha256(value, label)
        for value, label in (
            (self.selection_sha256, "selection_sha256"),
            (self.predictions_sha256, "predictions_sha256"),
        ):
            if value is not None:
                _require_sha256(value, label)
        return self


class MlReportProtocol(ContractModel):
    """Preregistered research scope exposed without operational interpretation."""

    protocol_id: Literal["crypto-historical-5d-baselines-v1"] = PROTOCOL_ID
    hypothesis: NonEmptyStr
    dataset_schema_version: Literal["research-historical-manifest-v1"] = (
        DATASET_MANIFEST_SCHEMA_VERSION
    )
    experiment_spec_id: Literal["crypto-daily-positive-return-5d-historical-v1"] = (
        EXPERIMENT_SPEC_ID
    )
    feature_set_version: Literal["crypto-market-reconstructed-3-v1"] = FEATURE_SET_VERSION
    feature_columns: tuple[NonEmptyStr, ...]
    target: MlProtocolTarget
    warmup_start: date
    decision_start: date
    decision_end: date
    excluded_start: date
    decision_rows: int = Field(gt=0)
    availability_basis: Literal["historical_reconstruction_assumed"]
    strict_pit_verified: Literal[False]
    folds: tuple[MlFoldBoundary, ...]
    criteria: MlDecisionCriteria

    @model_validator(mode="after")
    def validate_protocol(self) -> MlReportProtocol:
        if len(self.folds) != 3 or tuple(item.fold for item in self.folds) != (1, 2, 3):
            raise ValueError("the projection exposes the three sealed folds")
        if self.decision_start < self.warmup_start:
            raise ValueError("decision range must follow warm-up")
        if self.decision_end >= self.excluded_start:
            raise ValueError("decision range must exclude the excluded start")
        return self

    @classmethod
    def from_protocol(cls, protocol: MlBaselineProtocol) -> MlReportProtocol:
        """Copy only the preregistered fields that the local view may expose."""
        return cls(
            hypothesis=protocol.hypothesis,
            feature_columns=protocol.feature_columns,
            target=protocol.target,
            warmup_start=protocol.warmup_start,
            decision_start=protocol.decision_start,
            decision_end=protocol.decision_end,
            excluded_start=protocol.excluded_start,
            decision_rows=protocol.decision_rows,
            availability_basis=protocol.availability_basis,
            strict_pit_verified=protocol.strict_pit_verified,
            folds=protocol.folds,
            criteria=protocol.criteria,
        )


class MlResearchReportView(ContractModel):
    """Versioned, bounded projection served by the local research endpoint."""

    schema_version: Literal["ml-research-report-view-v1"] = REPORT_VIEW_SCHEMA_VERSION
    provenance: MlReportProvenance
    artifact: MlReportArtifact
    protocol: MlReportProtocol
    development: MlDevelopmentResult | None
    holdout: MlHoldoutResult | None
    insufficiency_reason: NonEmptyStr | None
    timings: MlTimings
    peak_rss_bytes: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def validate_report(self) -> MlResearchReportView:
        if self.artifact.status is MlArtifactStatus.INSUFFICIENT_EVALUATION_SAMPLE:
            if (
                self.development is not None
                or self.holdout is not None
                or self.insufficiency_reason is None
                or self.artifact.prediction_rows != 0
            ):
                raise ValueError("an insufficient artifact exposes no evaluation evidence")
            return self
        if self.development is None or self.holdout is None:
            raise ValueError("a completed artifact requires development and holdout sections")
        if self.insufficiency_reason is not None:
            raise ValueError("a completed artifact declares no insufficiency reason")
        return self

    def to_json_dict(self) -> dict[str, object]:
        """Return the exact JSON transport projection of this view."""
        return self.model_dump(mode="json")


__all__ = [
    "MlReportArtifact",
    "MlReportProvenance",
    "MlReportProtocol",
    "MlResearchReportView",
    "REPORT_VIEW_SCHEMA_VERSION",
    "SOURCE_ID_NOTE",
]
