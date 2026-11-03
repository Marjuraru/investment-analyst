"""Versioned presentation contracts for sealed research experiments.

Two presentation-only schemas live here: a bounded list of discoverable sealed
runs and one verified report view per run. Both are isolated from the sealed ML
contracts they project: this module copies preregistered protocol and sealed
results without recomputing an evaluation, never exposes per-row predictions and
never turns a retrospective result into a signal, a recommendation or a ranking
between studies.
"""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, model_validator

from investment_analyst.core.models.base import ContractModel, NonEmptyStr, UTCDateTime
from investment_analyst.research.ml_funding_models import (
    MODEL_SCORERS,
    FundingCalibrationTable,
    FundingFoldResult,
    FundingHoldoutResult,
    FundingIncrementDecision,
    FundingMetricSummary,
    FundingProtocol,
    FundingReferenceComparison,
)
from investment_analyst.research.ml_models import MlEnvironment, MlTimings
from investment_analyst.research.ml_report_models import MlResearchReportView

EXPERIMENT_LIST_SCHEMA_VERSION = "research-experiment-list-v1"
EXPERIMENT_REPORT_VIEW_SCHEMA_VERSION = "research-experiment-report-view-v1"
EXPERIMENT_CURSOR_VERSION = "research-experiment-cursor-v1"

DEFAULT_EXPERIMENT_PAGE_LIMIT = 20
MIN_EXPERIMENT_PAGE_LIMIT = 1
MAX_EXPERIMENT_PAGE_LIMIT = 50
MAX_EXPERIMENT_ENTRIES_PER_ROOT = 256
MAX_EXPERIMENT_HEADER_BYTES = 64 * 1024
MAX_EXPERIMENT_REPORT_BYTES = 1024 * 1024
MAX_EXPERIMENT_PREDICTIONS_BYTES = 8 * 1024 * 1024
MAX_EXPERIMENT_ARTIFACT_BYTES = 16 * 1024 * 1024
MAX_FUNDING_CACHE_ENTRIES = 4
MAX_FUNDING_CACHE_BYTES = 2 * 1024 * 1024
MAX_EXPORT_VIEW_BYTES = 1024 * 1024

SOURCE_ID_NOTE = (
    "El protocolo sellado identifica asset_id, dataset_id y hashes, pero este estudio"
    " no declara source_id ni venue; la vista no los infiere del ticker ni abre inputs."
)

FUNDING_SOURCE_ID_NOTE = (
    "El protocolo de funding identifica asset_id, dataset y bundle, pero no declara"
    " source_id, venue ni fecha de publicación; la vista no los infiere del ticker."
)

FAMILY_LABELS: tuple[tuple[str, str], ...] = (
    ("baseline", "Baseline técnico (crypto-historical-5d-baselines-v1)"),
    ("funding", "Incremento de funding (crypto-historical-5d-funding-increment-v1)"),
)

_SHA256 = re.compile(r"[0-9a-f]{64}")


def _require_sha256(value: str, label: str) -> str:
    if _SHA256.fullmatch(value) is None:
        raise ValueError(f"{label} must be a lowercase SHA-256 digest")
    return value


def experiment_export_file_name(family: ExperimentFamily, run_id: UUID) -> str:
    """Return the deterministic browser download name for one verified view."""
    return f"{family.value}-{run_id}.json"


class ExperimentFamily(StrEnum):
    """Closed discriminator for the two sealed study families."""

    BASELINE = "baseline"
    FUNDING = "funding"


class ExperimentFamilyFilter(StrEnum):
    """Listing filter; ``all`` is the declared default."""

    ALL = "all"
    BASELINE = "baseline"
    FUNDING = "funding"


class ExperimentArtifactStatus(StrEnum):
    """Normalized status carrying the sealed literal value verbatim."""

    COMPLETED = "completed"
    INSUFFICIENT_EVALUATION_SAMPLE = "insufficient_evaluation_sample"
    INSUFFICIENT_SAMPLE = "insufficient_sample"
    ERROR = "error"


class ExperimentRootStatus(StrEnum):
    """An absent configured root is an explicit empty state, never zero evidence."""

    AVAILABLE = "available"
    MISSING = "missing"


class ExperimentWarningCode(StrEnum):
    """Compact reason for one candidate that could not be presented as a study."""

    INCOMPLETE_ARTIFACT = "incomplete_artifact"
    INCOMPATIBLE_ARTIFACT = "incompatible_artifact"
    UNSUPPORTED_ENTRY = "unsupported_entry"


class ExperimentRootState(ContractModel):
    """Bounded enumeration evidence for one configured root."""

    family: ExperimentFamily
    status: ExperimentRootStatus
    entries: int = Field(ge=0, le=MAX_EXPERIMENT_ENTRIES_PER_ROOT)


class ExperimentWarning(ContractModel):
    """Compact, path-free warning about one candidate on the current page."""

    code: ExperimentWarningCode
    family: ExperimentFamily
    run_id: UUID | None
    message: NonEmptyStr


class ExperimentListItem(ContractModel):
    """Verified header of one sealed run; the listing publishes no metrics."""

    family: ExperimentFamily
    run_id: UUID
    asset_id: NonEmptyStr
    protocol_id: NonEmptyStr
    artifact_status: ExperimentArtifactStatus
    created_at: UTCDateTime
    manifest_sha256: str
    protocol_sha256: str
    report_sha256: str
    artifact_bytes: int = Field(gt=0)

    @model_validator(mode="after")
    def validate_item(self) -> ExperimentListItem:
        for value, label in (
            (self.manifest_sha256, "manifest_sha256"),
            (self.protocol_sha256, "protocol_sha256"),
            (self.report_sha256, "report_sha256"),
        ):
            _require_sha256(value, label)
        return self


class ExperimentListContext(ContractModel):
    """Echo of the exact listing request that produced this page."""

    asset_id: NonEmptyStr
    family: ExperimentFamilyFilter
    limit: int = Field(
        ge=MIN_EXPERIMENT_PAGE_LIMIT,
        le=MAX_EXPERIMENT_PAGE_LIMIT,
    )


class ExperimentListPage(ContractModel):
    """One bounded page of discoverable sealed runs for a single catalog asset."""

    schema_version: Literal["research-experiment-list-v1"] = EXPERIMENT_LIST_SCHEMA_VERSION
    context: ExperimentListContext
    items: tuple[ExperimentListItem, ...]
    next_cursor: str | None
    roots: tuple[ExperimentRootState, ...]
    warnings: tuple[ExperimentWarning, ...]
    examined_candidates: int = Field(ge=0, le=MAX_EXPERIMENT_PAGE_LIMIT)

    @model_validator(mode="after")
    def validate_page(self) -> ExperimentListPage:
        declared = tuple(item.family for item in self.roots)
        if declared != tuple(ExperimentFamily):
            raise ValueError("a page declares both configured roots exactly once")
        keys = tuple((item.family.value, str(item.run_id)) for item in self.items)
        if len(keys) != len(set(keys)):
            raise ValueError("a page never repeats a sealed run")
        if keys != tuple(sorted(keys)):
            raise ValueError("items must follow the stable (family, run_id) order")
        if self.examined_candidates > self.context.limit:
            raise ValueError("a page never examines more candidates than its limit")
        return self

    def to_json_dict(self) -> dict[str, object]:
        """Return the exact JSON transport projection of this page."""
        return self.model_dump(mode="json")


class ExperimentReportContext(ContractModel):
    """Exact query identity and sealed descriptor digests of one served view."""

    asset_id: NonEmptyStr
    family: ExperimentFamily
    run_id: UUID
    manifest_sha256: str
    protocol_sha256: str
    report_sha256: str
    artifact_bytes: int = Field(gt=0)
    created_at: UTCDateTime

    @model_validator(mode="after")
    def validate_context(self) -> ExperimentReportContext:
        for value, label in (
            (self.manifest_sha256, "manifest_sha256"),
            (self.protocol_sha256, "protocol_sha256"),
            (self.report_sha256, "report_sha256"),
        ):
            _require_sha256(value, label)
        return self


class FundingProvenanceView(ContractModel):
    """Exact dataset and bundle references available in the sealed funding run."""

    dataset_manifest_sha256: str
    bundle_manifest_sha256: str
    code_material_sha256: str
    code_material_files: tuple[NonEmptyStr, ...]
    source_id: None = None
    source_id_note: NonEmptyStr = FUNDING_SOURCE_ID_NOTE

    @model_validator(mode="after")
    def validate_provenance(self) -> FundingProvenanceView:
        for value, label in (
            (self.dataset_manifest_sha256, "dataset_manifest_sha256"),
            (self.bundle_manifest_sha256, "bundle_manifest_sha256"),
            (self.code_material_sha256, "code_material_sha256"),
        ):
            _require_sha256(value, label)
        if not self.code_material_files:
            raise ValueError("provenance must name the hashed material files")
        return self


class BaselineExperimentPayload(ContractModel):
    """Baseline detail delegated verbatim to the existing sealed-run projection."""

    family: Literal[ExperimentFamily.BASELINE] = ExperimentFamily.BASELINE
    report: MlResearchReportView


class FundingExperimentPayload(ContractModel):
    """Funding detail copied from one fully verified sealed artifact."""

    family: Literal[ExperimentFamily.FUNDING] = ExperimentFamily.FUNDING
    protocol: FundingProtocol
    provenance: FundingProvenanceView
    environment: MlEnvironment
    decision: FundingIncrementDecision
    decision_rows: int = Field(gt=0)
    prediction_rows: int = Field(gt=0)
    development_fits: int = Field(ge=0)
    folds: tuple[FundingFoldResult, ...]
    pooled_scores: tuple[FundingMetricSummary, ...]
    pooled_comparisons: tuple[FundingReferenceComparison, ...]
    calibration: tuple[FundingCalibrationTable, ...]
    holdout: FundingHoldoutResult
    timings: MlTimings
    peak_rss_bytes: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def validate_payload(self) -> FundingExperimentPayload:
        if len(self.folds) != 3 or tuple(item.fold for item in self.folds) != (1, 2, 3):
            raise ValueError("funding detail exposes the three sealed folds")
        if len(self.pooled_comparisons) != 3:
            raise ValueError("funding detail exposes the three sealed comparisons")
        if tuple(table.scorer for table in self.calibration) != MODEL_SCORERS:
            raise ValueError("funding detail exposes both sealed calibration tables")
        if self.decision_rows != self.protocol.decision_rows:
            raise ValueError("decision rows must match the sealed protocol")
        return self


ExperimentPayload = Annotated[
    BaselineExperimentPayload | FundingExperimentPayload,
    Field(discriminator="family"),
]


class ExperimentReportView(ContractModel):
    """Versioned, bounded projection served for one explicitly requested run."""

    schema_version: Literal["research-experiment-report-view-v1"] = (
        EXPERIMENT_REPORT_VIEW_SCHEMA_VERSION
    )
    context: ExperimentReportContext
    payload: ExperimentPayload

    @model_validator(mode="after")
    def validate_view(self) -> ExperimentReportView:
        if self.payload.family is not self.context.family:
            raise ValueError("the payload family must match the query identity")
        if isinstance(self.payload, BaselineExperimentPayload):
            report = self.payload.report
            if (
                report.provenance.asset_id != self.context.asset_id
                or report.artifact.run_id != self.context.run_id
                or report.artifact.manifest_sha256 != self.context.manifest_sha256
                or report.artifact.protocol_sha256 != self.context.protocol_sha256
                or report.artifact.report_sha256 != self.context.report_sha256
                or report.artifact.artifact_bytes != self.context.artifact_bytes
                or report.artifact.created_at != self.context.created_at
            ):
                raise ValueError("baseline payload disagrees with the query identity")
            return self
        if self.payload.protocol.asset_id != self.context.asset_id:
            raise ValueError("funding protocol belongs to a different catalog asset")
        return self

    def to_json_dict(self) -> dict[str, object]:
        """Return the exact JSON transport projection of this view."""
        return self.model_dump(mode="json")

    def export_file_name(self) -> str:
        """Return the deterministic download name of exactly this projection."""
        return experiment_export_file_name(self.context.family, self.context.run_id)


__all__ = [
    "BaselineExperimentPayload",
    "DEFAULT_EXPERIMENT_PAGE_LIMIT",
    "EXPERIMENT_CURSOR_VERSION",
    "EXPERIMENT_LIST_SCHEMA_VERSION",
    "EXPERIMENT_REPORT_VIEW_SCHEMA_VERSION",
    "ExperimentArtifactStatus",
    "ExperimentFamily",
    "ExperimentFamilyFilter",
    "ExperimentListItem",
    "ExperimentListContext",
    "ExperimentListPage",
    "ExperimentPayload",
    "ExperimentReportContext",
    "ExperimentReportView",
    "ExperimentRootState",
    "ExperimentRootStatus",
    "ExperimentWarning",
    "ExperimentWarningCode",
    "FAMILY_LABELS",
    "FUNDING_SOURCE_ID_NOTE",
    "FundingExperimentPayload",
    "FundingProvenanceView",
    "MAX_EXPERIMENT_ARTIFACT_BYTES",
    "MAX_EXPERIMENT_ENTRIES_PER_ROOT",
    "MAX_EXPERIMENT_HEADER_BYTES",
    "MAX_EXPERIMENT_PAGE_LIMIT",
    "MAX_EXPERIMENT_PREDICTIONS_BYTES",
    "MAX_EXPERIMENT_REPORT_BYTES",
    "MAX_EXPORT_VIEW_BYTES",
    "MAX_FUNDING_CACHE_BYTES",
    "MAX_FUNDING_CACHE_ENTRIES",
    "MIN_EXPERIMENT_PAGE_LIMIT",
    "SOURCE_ID_NOTE",
    "experiment_export_file_name",
]
