"""Strict contracts for the preregistered incremental-funding study.

The study is retrospective research over reconstructed inputs. Probabilities,
metrics and comparisons are not a signal, a recommendation or permission to
promote a predictor, and nothing here is imported by the operational product.
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Literal
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import ConfigDict, Field, model_validator

from investment_analyst.analytics.analytical_access_models import canonical_analytical_json
from investment_analyst.core.models.base import ContractModel, NonEmptyStr, UTCDateTime
from investment_analyst.research.funding_models import (
    FUNDING_COLUMN_24H,
    FUNDING_COLUMN_168H,
)
from investment_analyst.research.historical_models import (
    DECISION_HOUR_UTC,
    EXPERIMENT_SPEC_ID,
    FEATURE_COLUMNS,
    FEATURE_SET_VERSION,
    TARGET_VERSION,
)
from investment_analyst.research.ml_models import (
    DECISION_END,
    DECISION_START,
    MlArtifactFile,
    MlEnvironment,
    MlLogisticParameters,
    MlProtocolTarget,
    MlResamplingPolicy,
    MlTimings,
)

ML_FUNDING_NAMESPACE = uuid5(NAMESPACE_URL, "investment-analyst/ml-funding-validation-v1")

PROTOCOL_ID = "crypto-historical-5d-funding-increment-v1"
PROTOCOL_SCHEMA_VERSION = "ml-funding-protocol-v1"
EVALUATION_SCHEMA_VERSION = "ml-funding-evaluation-v1"
MANIFEST_SCHEMA_VERSION = "ml-funding-manifest-v1"

PROTOCOL_FILE_NAME = "protocol.json"
PREDICTIONS_FILE_NAME = "predictions.parquet"
REPORT_FILE_NAME = "report.json"
MANIFEST_FILE_NAME = "manifest.json"

TECHNICAL_FEATURE_COLUMNS = FEATURE_COLUMNS
FUNDING_FEATURE_COLUMNS = (FUNDING_COLUMN_24H, FUNDING_COLUMN_168H)
STUDY_FEATURE_COLUMNS = TECHNICAL_FEATURE_COLUMNS + FUNDING_FEATURE_COLUMNS
TECHNICAL_WIDTH = len(TECHNICAL_FEATURE_COLUMNS)
STUDY_WIDTH = len(STUDY_FEATURE_COLUMNS)

BLOCK_LENGTH = 20
BOOTSTRAP_REPLICATES = 2000
BOOTSTRAP_SEED = 1729
LOWER_QUANTILE = Decimal("0.0125")
MIN_RELATIVE_BRIER_IMPROVEMENT = Decimal("0.05")
LOG_LOSS_CLIP = Decimal("1e-15")
CALIBRATION_BIN_COUNT = 10

MAX_DEVELOPMENT_FITS = 6
MIN_TRAIN_ROWS = 300
MIN_CLASS_ROWS = 20
MIN_EVALUATION_ROWS = 100
MIN_SEGMENT_ROWS = BLOCK_LENGTH
MAX_DECISION_ROWS = 2200

MAX_INPUT_FILE_BYTES = 16 * 1024 * 1024
MAX_OUTPUT_FILE_BYTES = 8 * 1024 * 1024
MAX_ROOT_BYTES = 16 * 1024 * 1024

HYPOTHESIS = (
    "Causal 24h and 168h funding sums add incremental information for the sign of"
    " the next five daily BTC spot intervals beyond the same technical control,"
    " measured on identical decisions without tuning, holdout access or promotion."
)

HOLDOUT_POLICY = "closed-preregistered-zero-access"
AVAILABILITY_BASIS = "historical_reconstruction_assumed"

STUDY_LIMITATIONS = (
    "La disponibilidad reconstruida es un supuesto; strict_pit_verified es false y"
    " este estudio no sustituye el gate de PIT observado.",
    "Los labels se solapan cinco intervalos; la dependencia local se aborda con un"
    " bootstrap de bloques móviles condicionado a estabilidad, no con independencia.",
    "El funding se reconstruye con la recepción actual del proveedor y no demuestra"
    " la primera publicación histórica de cada punto.",
    "El resultado es evidencia de desarrollo del experimento declarado, no"
    " significancia global, señal, recomendación, rentabilidad ni promoción.",
    "El holdout permanece cerrado con cero accesos; este bloque no lo abre ni autoriza abrirlo.",
)

_CODE_SHA256 = re.compile(r"[0-9a-f]{64}")


def _require_sha256(value: str, label: str) -> str:
    if _CODE_SHA256.fullmatch(value) is None:
        raise ValueError(f"{label} must be a lowercase SHA-256 digest")
    return value


def metric_decimal(value: float) -> Decimal:
    """Expose one finite float64 result as its deterministic shortest decimal."""
    if value != value or value in (float("inf"), float("-inf")):
        raise ValueError("metric values must be finite")
    return Decimal(repr(float(value)))


def funding_run_identity(
    *,
    protocol_sha256: str,
    dataset_id: UUID,
    dataset_content_sha256: str,
    bundle_id: UUID,
    bundle_content_sha256: str,
    code_material_sha256: str,
    environment: MlEnvironment,
) -> UUID:
    """Derive the run identity from sealed inputs, material code and versions."""
    payload = {
        "protocol_id": PROTOCOL_ID,
        "protocol_sha256": _require_sha256(protocol_sha256, "protocol_sha256"),
        "dataset_id": str(dataset_id),
        "dataset_content_sha256": _require_sha256(dataset_content_sha256, "dataset_content_sha256"),
        "bundle_id": str(bundle_id),
        "bundle_content_sha256": _require_sha256(bundle_content_sha256, "bundle_content_sha256"),
        "code_material_sha256": _require_sha256(code_material_sha256, "code_material_sha256"),
        "environment": environment.model_dump(mode="json"),
    }
    return uuid5(ML_FUNDING_NAMESPACE, canonical_analytical_json(payload).decode("utf-8"))


class FundingScorer(StrEnum):
    """Closed scorer set for the incremental-funding comparison."""

    TECHNICAL_LOGISTIC = "technical_logistic"
    FUNDING_LOGISTIC = "funding_logistic"
    HALF_PROBABILITY = "half_probability"
    TRAINING_CLASS_PRIOR = "training_class_prior"


MODEL_SCORERS = (FundingScorer.TECHNICAL_LOGISTIC, FundingScorer.FUNDING_LOGISTIC)
SIMPLE_REFERENCES = (FundingScorer.HALF_PROBABILITY, FundingScorer.TRAINING_CLASS_PRIOR)
CONTROL_SCORER = FundingScorer.TECHNICAL_LOGISTIC
ALL_SCORERS = MODEL_SCORERS + SIMPLE_REFERENCES


class FundingIncrementDecision(StrEnum):
    """Closed development decision for the incremental hypothesis."""

    INCREMENTAL_EVIDENCE_OBSERVED = "incremental_evidence_observed"
    NO_DEMONSTRATED_INCREMENT = "no_demonstrated_increment"


class FundingArtifactStatus(StrEnum):
    """Explicit disposition of one evaluation attempt."""

    COMPLETED = "completed"
    INSUFFICIENT_SAMPLE = "insufficient_sample"
    ERROR = "error"


class FundingRunStatus(StrEnum):
    """Whether the CLI evaluated or reused one sealed run."""

    EVALUATED = "evaluated"
    REUSED = "reused"


class FundingHoldoutStatus(StrEnum):
    """The holdout is closed for this study."""

    NOT_EVALUATED = "not_evaluated"


class _FundingModel(ContractModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class FundingFoldBoundary(_FundingModel):
    """One expanding walk-forward window with its preregistered row accounting."""

    fold: int = Field(ge=1, le=3)
    train_end_index: int = Field(ge=0)
    validation_start_index: int = Field(ge=0)
    validation_end_index: int = Field(gt=0)
    validation_start_date: date
    validation_end_date: date
    expected_train_rows: int = Field(ge=0)
    expected_evaluation_rows: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_boundary(self) -> FundingFoldBoundary:
        if self.train_end_index != self.validation_start_index:
            raise ValueError("training ends where the validation window starts")
        if self.validation_end_index <= self.validation_start_index:
            raise ValueError("validation windows must be non-empty")
        if self.validation_end_date <= self.validation_start_date:
            raise ValueError("validation dates must advance")
        return self


class FundingCriteria(_FundingModel):
    """Preregistered decision rule over pooled paired comparisons."""

    min_relative_brier_improvement: Decimal = MIN_RELATIVE_BRIER_IMPROVEMENT
    lower_quantile: Decimal = LOWER_QUANTILE
    reference_rule: Literal["five-percent-brier-improvement-against-both-simple-references"] = (
        "five-percent-brier-improvement-against-both-simple-references"
    )
    control_rule: Literal["positive-brier-delta-against-the-technical-control"] = (
        "positive-brier-delta-against-the-technical-control"
    )
    lower_bound_rule: Literal["all-three-lower-bounds-positive"] = "all-three-lower-bounds-positive"

    @model_validator(mode="after")
    def validate_criteria(self) -> FundingCriteria:
        if (
            self.min_relative_brier_improvement,
            self.lower_quantile,
        ) != (
            MIN_RELATIVE_BRIER_IMPROVEMENT,
            LOWER_QUANTILE,
        ):
            raise ValueError("funding decision criteria are preregistered")
        return self


class FundingStudyCaps(_FundingModel):
    """Frozen sample floors, fit cap and physical byte caps."""

    max_development_fits: Literal[6] = MAX_DEVELOPMENT_FITS
    min_train_rows: Literal[300] = MIN_TRAIN_ROWS
    min_class_rows: Literal[20] = MIN_CLASS_ROWS
    min_evaluation_rows: Literal[100] = MIN_EVALUATION_ROWS
    min_segment_rows: Literal[20] = MIN_SEGMENT_ROWS
    max_decision_rows: Literal[2200] = MAX_DECISION_ROWS
    max_input_file_bytes: Literal[16777216] = MAX_INPUT_FILE_BYTES
    max_output_file_bytes: Literal[8388608] = MAX_OUTPUT_FILE_BYTES
    max_root_bytes: Literal[16777216] = MAX_ROOT_BYTES


class FundingProtocol(_FundingModel):
    """Complete preregistration sealed before the first development fit."""

    schema_version: Literal["ml-funding-protocol-v1"] = PROTOCOL_SCHEMA_VERSION
    protocol_id: Literal["crypto-historical-5d-funding-increment-v1"] = PROTOCOL_ID
    hypothesis: NonEmptyStr = HYPOTHESIS
    dataset_schema_version: Literal["research-historical-manifest-v1"] = (
        "research-historical-manifest-v1"
    )
    dataset_id: UUID
    dataset_content_sha256: str
    bundle_id: UUID
    bundle_content_sha256: str
    decision_rows: int = Field(gt=0)
    asset_id: NonEmptyStr
    experiment_spec_id: Literal["crypto-daily-positive-return-5d-historical-v1"] = (
        EXPERIMENT_SPEC_ID
    )
    target_version: Literal["crypto-forward-close-return-5d-v1"] = TARGET_VERSION
    feature_set_version: Literal["crypto-market-reconstructed-3-v1"] = FEATURE_SET_VERSION
    technical_feature_columns: tuple[NonEmptyStr, ...] = TECHNICAL_FEATURE_COLUMNS
    funding_feature_columns: tuple[NonEmptyStr, ...] = FUNDING_FEATURE_COLUMNS
    feature_columns: tuple[NonEmptyStr, ...] = STUDY_FEATURE_COLUMNS
    target: MlProtocolTarget = MlProtocolTarget()
    decision_start: date = DECISION_START
    decision_end: date = DECISION_END
    decision_hour_utc: Literal[12] = DECISION_HOUR_UTC
    availability_basis: Literal["historical_reconstruction_assumed"] = AVAILABILITY_BASIS
    strict_pit_verified: Literal[False] = False
    holdout_policy: Literal["closed-preregistered-zero-access"] = HOLDOUT_POLICY
    folds: tuple[FundingFoldBoundary, ...]
    logistic: MlLogisticParameters = MlLogisticParameters()
    criteria: FundingCriteria = FundingCriteria()
    resampling: MlResamplingPolicy = MlResamplingPolicy()
    caps: FundingStudyCaps = FundingStudyCaps()
    dtype: Literal["float64"] = "float64"
    blas_threads: Literal[1] = 1
    limitations: tuple[NonEmptyStr, ...] = STUDY_LIMITATIONS

    @model_validator(mode="after")
    def validate_protocol(self) -> FundingProtocol:
        _require_sha256(self.dataset_content_sha256, "dataset_content_sha256")
        _require_sha256(self.bundle_content_sha256, "bundle_content_sha256")
        if self.technical_feature_columns != TECHNICAL_FEATURE_COLUMNS:
            raise ValueError("technical features are exactly the three reconstructed columns")
        if self.funding_feature_columns != FUNDING_FEATURE_COLUMNS:
            raise ValueError("funding features are exactly the two declared columns")
        if self.feature_columns != STUDY_FEATURE_COLUMNS:
            raise ValueError("the study matrix is the technical control plus funding")
        if len(self.folds) != 3 or tuple(item.fold for item in self.folds) != (1, 2, 3):
            raise ValueError("protocol declares exactly three ordered folds")
        for previous, current in zip(self.folds, self.folds[1:], strict=False):
            if previous.validation_end_index != current.validation_start_index:
                raise ValueError("walk-forward windows must be adjacent and disjoint")
        if self.decision_rows > self.caps.max_decision_rows:
            raise ValueError("protocol rows exceed the frozen cap")
        if self.decision_rows != (self.decision_end - self.decision_start).days + 1:
            raise ValueError("protocol rows must match the decision window")
        if self.decision_start > self.decision_end:
            raise ValueError("decision window must be non-empty")
        end_boundary = self.decision_end + timedelta(days=1)
        for fold in self.folds:
            if fold.validation_start_date < self.decision_start:
                raise ValueError("fold windows start inside the decision range")
            if fold.validation_end_date > end_boundary:
                raise ValueError("fold windows end inside the decision range")
        if not self.limitations:
            raise ValueError("protocol must declare its limitations")
        return self


class FundingMetricSummary(_FundingModel):
    """Brier and clipped log-loss for one scorer over one row set."""

    scorer: FundingScorer
    rows: int = Field(ge=0)
    positives: int = Field(ge=0)
    negatives: int = Field(ge=0)
    brier: Decimal = Field(ge=0)
    log_loss: Decimal = Field(ge=0)

    @model_validator(mode="after")
    def validate_summary(self) -> FundingMetricSummary:
        if self.positives + self.negatives != self.rows:
            raise ValueError("class counts must add up to the row count")
        return self


class FundingReferenceComparison(_FundingModel):
    """Paired Brier comparison of the funding arm against one reference."""

    model: Literal[FundingScorer.FUNDING_LOGISTIC] = FundingScorer.FUNDING_LOGISTIC
    reference: FundingScorer
    model_brier: Decimal = Field(ge=0)
    reference_brier: Decimal = Field(ge=0)
    absolute_improvement: Decimal
    relative_improvement: Decimal | None
    mean_paired_difference: Decimal
    lower_bound: Decimal
    quantile: Decimal = Field(gt=0, lt=1)
    improved_at_least_five_percent: bool
    lower_bound_positive: bool
    criterion_satisfied: bool

    @model_validator(mode="after")
    def validate_comparison(self) -> FundingReferenceComparison:
        if self.reference not in (
            FundingScorer.TECHNICAL_LOGISTIC,
            FundingScorer.HALF_PROBABILITY,
            FundingScorer.TRAINING_CLASS_PRIOR,
        ):
            raise ValueError("funding is compared against the control and both references")
        if self.quantile != LOWER_QUANTILE:
            raise ValueError("funding comparisons use the preregistered lower quantile")
        if self.absolute_improvement != self.reference_brier - self.model_brier:
            raise ValueError("absolute improvement must match the reported Brier values")
        expected = None
        if self.reference_brier != 0:
            expected = metric_decimal(
                float(self.reference_brier - self.model_brier) / float(self.reference_brier)
            )
        if (self.relative_improvement is None) != (self.reference_brier == 0):
            raise ValueError("relative improvement is undefined only for a zero denominator")
        if expected is not None and self.relative_improvement != expected:
            raise ValueError("relative improvement must match the reported Brier values")
        improved = (
            self.relative_improvement is not None
            and self.relative_improvement >= MIN_RELATIVE_BRIER_IMPROVEMENT
        )
        if self.improved_at_least_five_percent is not improved:
            raise ValueError("five-percent verdict must match the relative improvement")
        if self.lower_bound_positive is not (self.lower_bound > 0):
            raise ValueError("lower-bound verdict must match the reported bound")
        if self.reference is CONTROL_SCORER:
            satisfied = self.absolute_improvement > 0 and self.lower_bound_positive
        else:
            satisfied = improved and self.lower_bound_positive
        if self.criterion_satisfied is not satisfied:
            raise ValueError("criterion verdict must match the preregistered decision rule")
        return self


class FundingCalibrationBin(_FundingModel):
    """One fixed-width probability bin of the descriptive reliability table."""

    index: int = Field(ge=0, lt=CALIBRATION_BIN_COUNT)
    lower_bound: Decimal = Field(ge=0, le=1)
    upper_bound: Decimal = Field(gt=0, le=1)
    includes_upper: bool
    count: int = Field(ge=0)
    mean_probability: Decimal | None
    observed_frequency: Decimal | None

    @model_validator(mode="after")
    def validate_bin(self) -> FundingCalibrationBin:
        expected_lower = Decimal(self.index) / Decimal(CALIBRATION_BIN_COUNT)
        expected_upper = Decimal(self.index + 1) / Decimal(CALIBRATION_BIN_COUNT)
        if self.lower_bound != expected_lower or self.upper_bound != expected_upper:
            raise ValueError("calibration bins use fixed equal-width edges")
        if self.includes_upper is not (self.index == CALIBRATION_BIN_COUNT - 1):
            raise ValueError("only the last calibration bin includes its upper edge")
        if self.count == 0:
            if self.mean_probability is not None or self.observed_frequency is not None:
                raise ValueError("empty calibration bins expose no frequencies")
        elif self.mean_probability is None or self.observed_frequency is None:
            raise ValueError("non-empty calibration bins expose both frequencies")
        return self


class FundingCalibrationTable(_FundingModel):
    """Ten explicit descriptive bins for one fitted arm."""

    scorer: FundingScorer
    bins: tuple[FundingCalibrationBin, ...]

    @model_validator(mode="after")
    def validate_table(self) -> FundingCalibrationTable:
        if self.scorer not in MODEL_SCORERS:
            raise ValueError("calibration covers fitted logistic arms only")
        if len(self.bins) != CALIBRATION_BIN_COUNT:
            raise ValueError("calibration tables declare ten bins")
        if tuple(item.index for item in self.bins) != tuple(range(CALIBRATION_BIN_COUNT)):
            raise ValueError("calibration bins must be ordered and complete")
        return self


class FundingFoldResult(_FundingModel):
    """Per-fold sample accounting, scores and paired comparisons."""

    fold: int = Field(ge=1, le=3)
    train_rows: int = Field(ge=0)
    train_positives: int = Field(ge=0)
    train_negatives: int = Field(ge=0)
    purged_excluded: int = Field(ge=0)
    ineligible_excluded: int = Field(ge=0)
    funding_unavailable_excluded: int = Field(ge=0)
    embargo_excluded: int = Field(ge=0)
    evaluation_rows: int = Field(ge=0)
    evaluation_positives: int = Field(ge=0)
    evaluation_negatives: int = Field(ge=0)
    segments: int = Field(ge=1)
    scores: tuple[FundingMetricSummary, ...]
    comparisons: tuple[FundingReferenceComparison, ...]
    calibration: tuple[FundingCalibrationTable, ...]
    diagnostics: tuple[NonEmptyStr, ...] = ()

    @model_validator(mode="after")
    def validate_fold(self) -> FundingFoldResult:
        if self.train_positives + self.train_negatives != self.train_rows:
            raise ValueError("training class counts must add up")
        if self.evaluation_positives + self.evaluation_negatives != self.evaluation_rows:
            raise ValueError("evaluation class counts must add up")
        if tuple(item.scorer for item in self.scores) != ALL_SCORERS:
            raise ValueError("each fold scores the four declared scorers")
        if len(self.comparisons) != 3:
            raise ValueError("funding is compared against the control and both references")
        if tuple(item.scorer for item in self.calibration) != MODEL_SCORERS:
            raise ValueError("each fold calibrates both logistic arms")
        return self


class FundingHoldoutResult(_FundingModel):
    """Explicit closed holdout with zero accesses for this study."""

    status: Literal[FundingHoldoutStatus.NOT_EVALUATED] = FundingHoldoutStatus.NOT_EVALUATED
    accesses: Literal[0] = 0
    fits: Literal[0] = 0
    reason: NonEmptyStr = "the preregistered incremental study never opens the holdout"


class FundingReport(_FundingModel):
    """Complete or explicitly non-evaluable development report."""

    schema_version: Literal["ml-funding-evaluation-v1"] = EVALUATION_SCHEMA_VERSION
    run_id: UUID
    status: FundingArtifactStatus
    protocol_id: Literal["crypto-historical-5d-funding-increment-v1"] = PROTOCOL_ID
    protocol_sha256: str
    dataset_id: UUID
    dataset_content_sha256: str
    dataset_manifest_sha256: str
    bundle_id: UUID
    bundle_content_sha256: str
    bundle_manifest_sha256: str
    code_material_sha256: str
    code_material_files: tuple[NonEmptyStr, ...]
    environment: MlEnvironment
    criteria: FundingCriteria = FundingCriteria()
    decision: FundingIncrementDecision | None
    folds: tuple[FundingFoldResult, ...]
    pooled_scores: tuple[FundingMetricSummary, ...]
    pooled_comparisons: tuple[FundingReferenceComparison, ...]
    calibration: tuple[FundingCalibrationTable, ...]
    holdout: FundingHoldoutResult = FundingHoldoutResult()
    disposition_reason: NonEmptyStr | None
    prediction_rows: int = Field(ge=0)
    development_fits: int = Field(ge=0, le=MAX_DEVELOPMENT_FITS)
    timings: MlTimings
    peak_rss_bytes: int | None = Field(default=None, ge=0)
    workspace_opened: Literal[False] = False
    domain_writes: Literal[0] = 0
    provider_requests: Literal[0] = 0

    @model_validator(mode="after")
    def validate_report(self) -> FundingReport:
        for value, label in (
            (self.protocol_sha256, "protocol_sha256"),
            (self.dataset_content_sha256, "dataset_content_sha256"),
            (self.dataset_manifest_sha256, "dataset_manifest_sha256"),
            (self.bundle_content_sha256, "bundle_content_sha256"),
            (self.bundle_manifest_sha256, "bundle_manifest_sha256"),
            (self.code_material_sha256, "code_material_sha256"),
        ):
            _require_sha256(value, label)
        if not self.code_material_files:
            raise ValueError("report must declare the hashed material files")
        if self.status is FundingArtifactStatus.COMPLETED:
            if (
                self.decision is None
                or len(self.folds) != 3
                or tuple(item.fold for item in self.folds) != (1, 2, 3)
                or tuple(item.scorer for item in self.pooled_scores) != ALL_SCORERS
                or len(self.pooled_comparisons) != 3
                or tuple(item.scorer for item in self.calibration) != MODEL_SCORERS
                or self.disposition_reason is not None
                or self.prediction_rows == 0
                or self.development_fits != MAX_DEVELOPMENT_FITS
            ):
                raise ValueError("a completed report requires the full evaluation evidence")
            return self
        if (
            self.decision is not None
            or self.folds
            or self.pooled_scores
            or self.pooled_comparisons
            or self.calibration
            or self.disposition_reason is None
            or self.prediction_rows != 0
            or self.development_fits != 0
        ):
            raise ValueError("a non-evaluable report contains no evaluation evidence")
        return self


class FundingManifest(_FundingModel):
    """Final content-addressed descriptor of one completed run directory."""

    schema_version: Literal["ml-funding-manifest-v1"] = MANIFEST_SCHEMA_VERSION
    protocol_id: Literal["crypto-historical-5d-funding-increment-v1"] = PROTOCOL_ID
    run_id: UUID
    status: Literal[FundingArtifactStatus.COMPLETED] = FundingArtifactStatus.COMPLETED
    dataset_id: UUID
    dataset_content_sha256: str
    dataset_manifest_sha256: str
    bundle_id: UUID
    bundle_content_sha256: str
    bundle_manifest_sha256: str
    code_material_sha256: str
    code_material_files: tuple[NonEmptyStr, ...]
    environment: MlEnvironment
    criteria: FundingCriteria = FundingCriteria()
    protocol: MlArtifactFile
    predictions: MlArtifactFile
    report: MlArtifactFile
    root_bytes: int = Field(gt=0)
    created_at: UTCDateTime

    @model_validator(mode="after")
    def validate_manifest(self) -> FundingManifest:
        for value, label in (
            (self.dataset_content_sha256, "dataset_content_sha256"),
            (self.dataset_manifest_sha256, "dataset_manifest_sha256"),
            (self.bundle_content_sha256, "bundle_content_sha256"),
            (self.bundle_manifest_sha256, "bundle_manifest_sha256"),
            (self.code_material_sha256, "code_material_sha256"),
        ):
            _require_sha256(value, label)
        if not self.code_material_files:
            raise ValueError("manifest must declare the hashed material files")
        if self.protocol.name != PROTOCOL_FILE_NAME:
            raise ValueError("manifest must name the sealed protocol file")
        if self.predictions.name != PREDICTIONS_FILE_NAME:
            raise ValueError("manifest must name the sealed predictions file")
        if self.report.name != REPORT_FILE_NAME:
            raise ValueError("manifest must name the sealed report file")
        declared = self.protocol.bytes + self.predictions.bytes + self.report.bytes
        if declared != self.root_bytes:
            raise ValueError("root bytes must equal the sum of sealed files")
        if self.root_bytes > MAX_ROOT_BYTES:
            raise ValueError("artifact root exceeds the byte cap")
        return self


class FundingReceipt(_FundingModel):
    """Compact CLI receipt without probabilities or feature values."""

    run_id: UUID
    status: FundingRunStatus
    report_status: FundingArtifactStatus
    decision: FundingIncrementDecision | None
    holdout_status: FundingHoldoutStatus
    protocol_id: Literal["crypto-historical-5d-funding-increment-v1"] = PROTOCOL_ID
    dataset_id: UUID
    dataset_content_sha256: str
    bundle_id: UUID
    bundle_content_sha256: str
    output_dir: NonEmptyStr
    artifact_dir: NonEmptyStr
    protocol_sha256: str
    report_sha256: str
    manifest_sha256: str | None
    prediction_rows: int = Field(ge=0)
    development_fits: int = Field(ge=0)
    holdout_accesses: Literal[0] = 0
    artifact_bytes: int = Field(ge=0)
    elapsed_seconds: Decimal = Field(ge=0)
    peak_rss_bytes: int | None = Field(default=None, ge=0)

    def to_json_dict(self) -> dict[str, object]:
        """Return the deterministic JSON projection used by the CLI."""
        return self.model_dump(mode="json")


__all__ = [
    "ALL_SCORERS",
    "AVAILABILITY_BASIS",
    "BLOCK_LENGTH",
    "BOOTSTRAP_REPLICATES",
    "BOOTSTRAP_SEED",
    "CALIBRATION_BIN_COUNT",
    "CONTROL_SCORER",
    "EVALUATION_SCHEMA_VERSION",
    "FUNDING_FEATURE_COLUMNS",
    "HOLDOUT_POLICY",
    "HYPOTHESIS",
    "LOG_LOSS_CLIP",
    "LOWER_QUANTILE",
    "MANIFEST_FILE_NAME",
    "MANIFEST_SCHEMA_VERSION",
    "MAX_DEVELOPMENT_FITS",
    "MAX_DECISION_ROWS",
    "MAX_INPUT_FILE_BYTES",
    "MAX_OUTPUT_FILE_BYTES",
    "MAX_ROOT_BYTES",
    "MIN_CLASS_ROWS",
    "MIN_EVALUATION_ROWS",
    "MIN_RELATIVE_BRIER_IMPROVEMENT",
    "MIN_SEGMENT_ROWS",
    "MIN_TRAIN_ROWS",
    "ML_FUNDING_NAMESPACE",
    "MODEL_SCORERS",
    "PREDICTIONS_FILE_NAME",
    "PROTOCOL_FILE_NAME",
    "PROTOCOL_ID",
    "PROTOCOL_SCHEMA_VERSION",
    "REPORT_FILE_NAME",
    "SIMPLE_REFERENCES",
    "STUDY_FEATURE_COLUMNS",
    "STUDY_LIMITATIONS",
    "STUDY_WIDTH",
    "TECHNICAL_FEATURE_COLUMNS",
    "TECHNICAL_WIDTH",
    "FundingArtifactStatus",
    "FundingCalibrationBin",
    "FundingCalibrationTable",
    "FundingCriteria",
    "FundingFoldBoundary",
    "FundingFoldResult",
    "FundingHoldoutResult",
    "FundingHoldoutStatus",
    "FundingIncrementDecision",
    "FundingManifest",
    "FundingMetricSummary",
    "FundingProtocol",
    "FundingReceipt",
    "FundingReferenceComparison",
    "FundingReport",
    "FundingRunStatus",
    "FundingScorer",
    "FundingStudyCaps",
    "funding_run_identity",
    "metric_decimal",
]
