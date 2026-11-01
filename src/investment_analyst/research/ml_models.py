"""Strict, deterministic contracts for the first offline ML baseline study.

The study is a research artifact: probabilities and metrics are not a signal, a
diagnostic result or a recommendation, and nothing here is imported by the
operational product.
"""

from __future__ import annotations

import re
from datetime import date
from decimal import ROUND_FLOOR, Decimal
from enum import StrEnum
from typing import Literal
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import ConfigDict, Field, model_validator

from investment_analyst.analytics.analytical_access_models import canonical_analytical_json
from investment_analyst.core.models.base import ContractModel, NonEmptyStr, UTCDateTime
from investment_analyst.research.historical_models import (
    EXPERIMENT_SPEC_ID,
    FEATURE_COLUMNS,
    FEATURE_SET_VERSION,
    TARGET_VERSION,
)

ML_NAMESPACE = uuid5(NAMESPACE_URL, "investment-analyst/ml-baseline-validation-v1")

PROTOCOL_ID = "crypto-historical-5d-baselines-v1"
PROTOCOL_SCHEMA_VERSION = "ml-baseline-protocol-v1"
SELECTION_SCHEMA_VERSION = "ml-baseline-selection-v1"
EVALUATION_SCHEMA_VERSION = "ml-baseline-evaluation-v1"
MANIFEST_SCHEMA_VERSION = "ml-baseline-manifest-v1"
DATASET_MANIFEST_SCHEMA_VERSION = "research-historical-manifest-v1"

PROTOCOL_FILE_NAME = "protocol.json"
SELECTION_FILE_NAME = "selection.json"
PREDICTIONS_FILE_NAME = "predictions.parquet"
REPORT_FILE_NAME = "report.json"
MANIFEST_FILE_NAME = "manifest.json"

DECISION_START = date(2020, 1, 1)
DECISION_END = date(2025, 12, 25)
WARMUP_START = date(2019, 1, 1)
EXCLUDED_START = date(2026, 1, 1)

BLOCK_LENGTH = 20
BOOTSTRAP_REPLICATES = 2000
BOOTSTRAP_SEED = 1729

DEVELOPMENT_QUANTILE = Decimal("0.0125")
HOLDOUT_QUANTILE = Decimal("0.025")
MIN_RELATIVE_BRIER_IMPROVEMENT = Decimal("0.05")
TIE_TOLERANCE = Decimal("1e-12")
LOG_LOSS_CLIP = Decimal("1e-15")

MAX_DEVELOPMENT_FITS = 6
MAX_HOLDOUT_REFITS = 1
MIN_TRAIN_ROWS = 300
MIN_CLASS_ROWS = 20
MIN_EVALUATION_ROWS = 100
MIN_HOLDOUT_ROWS = 40
MAX_DECISION_ROWS = 2200
MIN_SEGMENT_ROWS = BLOCK_LENGTH

MAX_INPUT_FILE_BYTES = 16 * 1024 * 1024
MAX_OUTPUT_FILE_BYTES = 8 * 1024 * 1024
MAX_ROOT_BYTES = 16 * 1024 * 1024

CALIBRATION_BIN_COUNT = 10

HYPOTHESIS = (
    "A small nonlinear model over one-day close return, RSI(14) and the MACD"
    " histogram may predict the sign of the next five daily intervals better than"
    " naive references; the study measures that value without tuning and keeps the"
    " holdout unopened unless a preregistered development edge is selected."
)

STUDY_LIMITATIONS = (
    "Underlying reconstruction assumes post-close availability; strict point-in-time"
    " verification is false and this study never substitutes the observed-PIT gate.",
    "Labels are five-interval forward returns; overlapping labels induce local"
    " dependence handled only by a moving-block bootstrap conditional on stability.",
    "A confirmed or unconfirmed research result grants no operational promotion,"
    " signal, recommendation, execution or prediction-quality claim.",
    "Statistical independence is restricted to this experiment, asset and venue.",
)

_SHA256 = re.compile(r"[0-9a-f]{64}")


def _require_sha256(value: str, label: str) -> str:
    if _SHA256.fullmatch(value) is None:
        raise ValueError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _decimal(value: float) -> Decimal:
    """Convert one finite float64 result into its exact shortest decimal form."""
    if value != value or value in (float("inf"), float("-inf")):
        raise ValueError("metric values must be finite")
    return Decimal(repr(float(value)))


def metric_decimal(value: float) -> Decimal:
    """Expose float64 metrics as deterministic shortest decimal values."""
    return _decimal(value)


def development_fold_boundaries(
    decision_count: int,
) -> tuple[tuple[int, int], tuple[int, int], tuple[int, int]]:
    """Return the three expanding walk-forward evaluation windows over all dates."""
    if decision_count <= 0:
        raise ValueError("decision_count must be positive")
    a = decision_count // 2
    b = int((Decimal(decision_count) * Decimal("0.60")).to_integral_value(rounding=ROUND_FLOOR))
    d = int((Decimal(decision_count) * Decimal("0.80")).to_integral_value(rounding=ROUND_FLOOR))
    c = b + (d - b) // 2
    if not 0 < a < b < c < d < decision_count:
        raise ValueError("decision_count is too small for three walk-forward folds")
    return ((a, b), (b, c), (c, d))


def run_identity(
    *,
    protocol_sha256: str,
    dataset_id: UUID,
    dataset_content_sha256: str,
    code_material_sha256: str,
    environment: MlEnvironment,
) -> UUID:
    """Derive the run identity from sealed inputs, material code and versions."""
    payload = {
        "protocol_id": PROTOCOL_ID,
        "protocol_sha256": _require_sha256(protocol_sha256, "protocol_sha256"),
        "dataset_id": str(dataset_id),
        "dataset_content_sha256": _require_sha256(dataset_content_sha256, "dataset_content_sha256"),
        "code_material_sha256": _require_sha256(code_material_sha256, "code_material_sha256"),
        "environment": environment.model_dump(mode="json"),
    }
    return uuid5(ML_NAMESPACE, canonical_analytical_json(payload).decode("utf-8"))


class MlModelFamily(StrEnum):
    LOGISTIC_REGRESSION = "logistic_regression"
    HIST_GRADIENT_BOOSTING = "hist_gradient_boosting"


class MlScorer(StrEnum):
    LOGISTIC_REGRESSION = "logistic_regression"
    HIST_GRADIENT_BOOSTING = "hist_gradient_boosting"
    HALF_PROBABILITY = "half_probability"
    TRAINING_CLASS_PRIOR = "training_class_prior"


class MlModelState(StrEnum):
    CANDIDATE_SELECTED = "candidate_selected"
    NO_DEMONSTRATED_EDGE = "no_demonstrated_edge"


class MlHoldoutStatus(StrEnum):
    NOT_EVALUATED = "not_evaluated"
    CONFIRMED_RESEARCH_EDGE = "confirmed_research_edge"
    HOLDOUT_NOT_CONFIRMED = "holdout_not_confirmed"


class MlRunStatus(StrEnum):
    EVALUATED = "evaluated"
    REUSED = "reused"


class MlArtifactStatus(StrEnum):
    COMPLETED = "completed"
    INSUFFICIENT_EVALUATION_SAMPLE = "insufficient_evaluation_sample"


class MlLogisticParameters(ContractModel):
    """Preregistered scikit-learn logistic regression parameters."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    C: Decimal = Decimal("1.0")
    l1_ratio: Decimal = Decimal("0.0")
    solver: Literal["lbfgs"] = "lbfgs"
    fit_intercept: Literal[True] = True
    max_iter: Literal[1000] = 1000
    tol: Decimal = Decimal("1e-8")
    class_weight: None = None
    random_state: Literal[1729] = 1729
    penalty_parameter: Literal["omitted"] = "omitted"

    @model_validator(mode="after")
    def validate_parameters(self) -> MlLogisticParameters:
        if (
            self.C,
            self.l1_ratio,
            self.solver,
            self.fit_intercept,
            self.max_iter,
            self.tol,
            self.class_weight,
            self.random_state,
            self.penalty_parameter,
        ) != (
            Decimal("1.0"),
            Decimal("0.0"),
            "lbfgs",
            True,
            1000,
            Decimal("1e-8"),
            None,
            1729,
            "omitted",
        ):
            raise ValueError("logistic parameters are preregistered")
        return self


class MlHistogramParameters(ContractModel):
    """Preregistered scikit-learn histogram gradient boosting parameters."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    loss: Literal["log_loss"] = "log_loss"
    learning_rate: Decimal = Decimal("0.05")
    max_iter: Literal[100] = 100
    max_leaf_nodes: Literal[7] = 7
    max_depth: Literal[3] = 3
    min_samples_leaf: Literal[30] = 30
    l2_regularization: Decimal = Decimal("1.0")
    max_bins: Literal[31] = 31
    max_features: Decimal = Decimal("1.0")
    categorical_features: None = None
    early_stopping: Literal[False] = False
    warm_start: Literal[False] = False
    class_weight: None = None
    random_state: Literal[1729] = 1729

    @model_validator(mode="after")
    def validate_parameters(self) -> MlHistogramParameters:
        if (
            self.loss,
            self.learning_rate,
            self.max_iter,
            self.max_leaf_nodes,
            self.max_depth,
            self.min_samples_leaf,
            self.l2_regularization,
            self.max_bins,
            self.max_features,
            self.categorical_features,
            self.early_stopping,
            self.warm_start,
            self.class_weight,
            self.random_state,
        ) != (
            "log_loss",
            Decimal("0.05"),
            100,
            7,
            3,
            30,
            Decimal("1.0"),
            31,
            Decimal("1.0"),
            None,
            False,
            False,
            None,
            1729,
        ):
            raise ValueError("histogram parameters are preregistered")
        return self


class MlResamplingPolicy(ContractModel):
    """Paired moving-block bootstrap policy over contiguous decision days."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    block_length: Literal[20] = BLOCK_LENGTH
    replicates: Literal[2000] = BOOTSTRAP_REPLICATES
    seed: Literal[1729] = BOOTSTRAP_SEED
    generator: Literal["PCG64"] = "PCG64"
    segment_rule: Literal["contiguous-decision-days-within-fold"] = (
        "contiguous-decision-days-within-fold"
    )
    truncation: Literal["truncate-last-draw-to-segment-size"] = "truncate-last-draw-to-segment-size"


class MlDecisionCriteria(ContractModel):
    """Preregistered development and holdout admission criteria."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    min_relative_brier_improvement: Decimal = MIN_RELATIVE_BRIER_IMPROVEMENT
    development_lower_quantile: Decimal = DEVELOPMENT_QUANTILE
    holdout_lower_quantile: Decimal = HOLDOUT_QUANTILE
    tie_tolerance: Decimal = TIE_TOLERANCE
    references: tuple[MlScorer, ...] = (
        MlScorer.HALF_PROBABILITY,
        MlScorer.TRAINING_CLASS_PRIOR,
    )
    improvement_scope: Literal["both-references-and-both-lower-bounds-positive"] = (
        "both-references-and-both-lower-bounds-positive"
    )
    tie_break: Literal["prefer-logistic-regression"] = "prefer-logistic-regression"

    @model_validator(mode="after")
    def validate_criteria(self) -> MlDecisionCriteria:
        if (
            self.min_relative_brier_improvement,
            self.development_lower_quantile,
            self.holdout_lower_quantile,
            self.tie_tolerance,
        ) != (
            MIN_RELATIVE_BRIER_IMPROVEMENT,
            DEVELOPMENT_QUANTILE,
            HOLDOUT_QUANTILE,
            TIE_TOLERANCE,
        ):
            raise ValueError("decision criteria are preregistered")
        if self.references != (MlScorer.HALF_PROBABILITY, MlScorer.TRAINING_CLASS_PRIOR):
            raise ValueError("references are preregistered")
        return self


class MlStudyCaps(ContractModel):
    """Frozen sample floors, fit caps and physical byte caps."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    max_development_fits: Literal[6] = MAX_DEVELOPMENT_FITS
    max_holdout_refits: Literal[1] = MAX_HOLDOUT_REFITS
    min_train_rows: Literal[300] = MIN_TRAIN_ROWS
    min_class_rows: Literal[20] = MIN_CLASS_ROWS
    min_evaluation_rows: Literal[100] = MIN_EVALUATION_ROWS
    min_holdout_rows: Literal[40] = MIN_HOLDOUT_ROWS
    max_decision_rows: Literal[2200] = MAX_DECISION_ROWS
    max_input_file_bytes: Literal[16777216] = MAX_INPUT_FILE_BYTES
    max_output_file_bytes: Literal[8388608] = MAX_OUTPUT_FILE_BYTES
    max_root_bytes: Literal[16777216] = MAX_ROOT_BYTES


class MlProtocolTarget(ContractModel):
    """Binary target definition reused from the sealed dataset contract."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    label_column: Literal["label"] = "label"
    target_version: Literal["crypto-forward-close-return-5d-v1"] = TARGET_VERSION
    horizon_intervals: Literal[5] = 5
    positive_rule: Literal["return_5d > 0"] = "return_5d > 0"
    label_available_rule: Literal["exit_bar_end-then-real-reception"] = (
        "exit_bar_end-then-real-reception"
    )


class MlFoldBoundary(ContractModel):
    """One expanding walk-forward window declared before any fit."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    fold: int = Field(ge=1, le=3)
    train_end_index: int = Field(ge=0)
    validation_start_index: int = Field(ge=0)
    validation_end_index: int = Field(gt=0)
    validation_start_date: date
    validation_end_date: date

    @model_validator(mode="after")
    def validate_boundary(self) -> MlFoldBoundary:
        if self.train_end_index != self.validation_start_index:
            raise ValueError("training ends where the validation window starts")
        if self.validation_end_index <= self.validation_start_index:
            raise ValueError("validation windows must be non-empty")
        if self.validation_end_date <= self.validation_start_date:
            raise ValueError("validation dates must advance")
        if self.validation_start_date < DECISION_START:
            raise ValueError("validation windows start inside the decision range")
        if self.validation_end_date > date.fromordinal(DECISION_END.toordinal() + 1):
            raise ValueError("validation windows end inside the decision range")
        return self


class MlBaselineProtocol(ContractModel):
    """Complete preregistration sealed before the first development fit."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["ml-baseline-protocol-v1"] = PROTOCOL_SCHEMA_VERSION
    protocol_id: Literal["crypto-historical-5d-baselines-v1"] = PROTOCOL_ID
    hypothesis: NonEmptyStr = HYPOTHESIS
    dataset_schema_version: Literal["research-historical-manifest-v1"] = (
        DATASET_MANIFEST_SCHEMA_VERSION
    )
    dataset_id: UUID
    dataset_content_sha256: str
    decision_rows: int = Field(gt=0)
    asset_id: NonEmptyStr
    experiment_spec_id: Literal["crypto-daily-positive-return-5d-historical-v1"] = (
        EXPERIMENT_SPEC_ID
    )
    feature_set_version: Literal["crypto-market-reconstructed-3-v1"] = FEATURE_SET_VERSION
    feature_columns: tuple[NonEmptyStr, ...] = FEATURE_COLUMNS
    target: MlProtocolTarget = MlProtocolTarget()
    warmup_start: date = WARMUP_START
    decision_start: date = DECISION_START
    decision_end: date = DECISION_END
    excluded_start: date = EXCLUDED_START
    availability_basis: Literal["historical_reconstruction_assumed"] = (
        "historical_reconstruction_assumed"
    )
    strict_pit_verified: Literal[False] = False
    folds: tuple[MlFoldBoundary, ...]
    logistic: MlLogisticParameters = MlLogisticParameters()
    histogram: MlHistogramParameters = MlHistogramParameters()
    resampling: MlResamplingPolicy = MlResamplingPolicy()
    criteria: MlDecisionCriteria = MlDecisionCriteria()
    caps: MlStudyCaps = MlStudyCaps()
    dtype: Literal["float64"] = "float64"
    blas_threads: Literal[1] = 1
    limitations: tuple[NonEmptyStr, ...] = STUDY_LIMITATIONS

    @model_validator(mode="after")
    def validate_protocol(self) -> MlBaselineProtocol:
        _require_sha256(self.dataset_content_sha256, "dataset_content_sha256")
        if self.feature_columns != FEATURE_COLUMNS:
            raise ValueError("features are exactly the three reconstructed columns")
        if len(self.folds) != 3 or tuple(item.fold for item in self.folds) != (1, 2, 3):
            raise ValueError("protocol declares exactly three ordered folds")
        for previous, current in zip(self.folds, self.folds[1:], strict=False):
            if previous.validation_end_index != current.validation_start_index:
                raise ValueError("walk-forward windows must be adjacent and disjoint")
        if self.decision_rows > self.caps.max_decision_rows:
            raise ValueError("protocol rows exceed the frozen cap")
        if self.decision_start <= self.warmup_start:
            raise ValueError("decision window must follow warm-up")
        if self.decision_end >= self.excluded_start:
            raise ValueError("decision window excludes 2026")
        if not self.limitations:
            raise ValueError("protocol must declare its limitations")
        return self


class MlEnvironment(ContractModel):
    """Effective interpreter and library versions bound to one run identity."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    python_version: NonEmptyStr
    scikit_learn_version: NonEmptyStr
    numpy_version: NonEmptyStr
    scipy_version: NonEmptyStr
    threadpoolctl_version: NonEmptyStr
    duckdb_version: NonEmptyStr
    blas_threads: Literal[1] = 1


class MlMetricSummary(ContractModel):
    """Brier and clipped log-loss for one scorer over one row set."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    scorer: MlScorer
    rows: int = Field(ge=0)
    positives: int = Field(ge=0)
    negatives: int = Field(ge=0)
    brier: Decimal = Field(ge=0)
    log_loss: Decimal = Field(ge=0)

    @model_validator(mode="after")
    def validate_summary(self) -> MlMetricSummary:
        if self.positives + self.negatives != self.rows:
            raise ValueError("class counts must add up to the row count")
        return self


class MlReferenceComparison(ContractModel):
    """Paired Brier comparison of one model against one naive reference."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    model: MlScorer
    reference: MlScorer
    model_brier: Decimal = Field(ge=0)
    reference_brier: Decimal = Field(ge=0)
    absolute_improvement: Decimal
    relative_improvement: Decimal | None
    mean_paired_difference: Decimal
    lower_bound: Decimal
    quantile: Decimal = Field(gt=0, lt=1)
    improved_at_least_five_percent: bool
    lower_bound_positive: bool

    @model_validator(mode="after")
    def validate_comparison(self) -> MlReferenceComparison:
        if self.model not in (
            MlScorer.LOGISTIC_REGRESSION,
            MlScorer.HIST_GRADIENT_BOOSTING,
        ):
            raise ValueError("comparison model must be a fitted candidate")
        if self.reference not in (MlScorer.HALF_PROBABILITY, MlScorer.TRAINING_CLASS_PRIOR):
            raise ValueError("comparison reference must be a naive baseline")
        expected = None
        if self.reference_brier != 0:
            expected = _decimal(
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
        return self


class MlCalibrationBin(ContractModel):
    """One fixed-width probability bin of the reliability table."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    index: int = Field(ge=0, lt=CALIBRATION_BIN_COUNT)
    lower_bound: Decimal = Field(ge=0, le=1)
    upper_bound: Decimal = Field(gt=0, le=1)
    includes_upper: bool
    count: int = Field(ge=0)
    mean_probability: Decimal | None
    observed_frequency: Decimal | None

    @model_validator(mode="after")
    def validate_bin(self) -> MlCalibrationBin:
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


class MlCalibrationTable(ContractModel):
    """Ten explicit bins for one scorer; empty bins stay visible."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    scorer: MlScorer
    bins: tuple[MlCalibrationBin, ...]

    @model_validator(mode="after")
    def validate_table(self) -> MlCalibrationTable:
        if len(self.bins) != CALIBRATION_BIN_COUNT:
            raise ValueError("calibration tables declare ten bins")
        if tuple(item.index for item in self.bins) != tuple(range(CALIBRATION_BIN_COUNT)):
            raise ValueError("calibration bins must be ordered and complete")
        return self


class MlFoldResult(ContractModel):
    """Per-fold sample accounting, scores and paired comparisons."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    fold: int = Field(ge=1, le=3)
    train_rows: int = Field(ge=0)
    train_positives: int = Field(ge=0)
    train_negatives: int = Field(ge=0)
    purged_excluded: int = Field(ge=0)
    embargo_excluded: int = Field(ge=0)
    evaluation_rows: int = Field(ge=0)
    evaluation_positives: int = Field(ge=0)
    evaluation_negatives: int = Field(ge=0)
    segments: int = Field(ge=1)
    scores: tuple[MlMetricSummary, ...]
    comparisons: tuple[MlReferenceComparison, ...]
    diagnostics: tuple[NonEmptyStr, ...] = ()

    @model_validator(mode="after")
    def validate_fold(self) -> MlFoldResult:
        if self.train_positives + self.train_negatives != self.train_rows:
            raise ValueError("training class counts must add up")
        if self.evaluation_positives + self.evaluation_negatives != self.evaluation_rows:
            raise ValueError("evaluation class counts must add up")
        if len(self.scores) != 4:
            raise ValueError("each fold reports two models and two references")
        if len(self.comparisons) != 4:
            raise ValueError("each fold compares two models against two references")
        return self


class MlDevelopmentResult(ContractModel):
    """Pooled development outcome sealed before the holdout is opened."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    decision: MlModelState
    folds: tuple[MlFoldResult, ...]
    pooled_scores: tuple[MlMetricSummary, ...]
    pooled_comparisons: tuple[MlReferenceComparison, ...]
    calibration: tuple[MlCalibrationTable, ...]
    selected_model: MlModelFamily | None
    selection_reason: NonEmptyStr
    development_fits: int = Field(ge=0, le=MAX_DEVELOPMENT_FITS)

    @model_validator(mode="after")
    def validate_development(self) -> MlDevelopmentResult:
        if len(self.folds) != 3 or tuple(item.fold for item in self.folds) != (1, 2, 3):
            raise ValueError("development declares three ordered folds")
        if len(self.pooled_scores) != 4 or len(self.pooled_comparisons) != 4:
            raise ValueError("pooled development reports four scores and comparisons")
        if tuple(item.scorer for item in self.calibration) != (
            MlScorer.LOGISTIC_REGRESSION,
            MlScorer.HIST_GRADIENT_BOOSTING,
        ):
            raise ValueError("calibration covers both fitted candidates")
        if self.selected_model is None:
            if self.decision is not MlModelState.NO_DEMONSTRATED_EDGE:
                raise ValueError("only a selected candidate requires a model")
        elif self.decision is not MlModelState.CANDIDATE_SELECTED:
            raise ValueError("a selected model requires the candidate decision")
        if self.selected_model is not None and self.development_fits != MAX_DEVELOPMENT_FITS:
            raise ValueError("development uses exactly the six preregistered fits")
        return self


class MlHoldoutResult(ContractModel):
    """Single protected holdout evaluation of the selected candidate only."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: MlHoldoutStatus
    selected_model: MlModelFamily | None
    train_rows: int = Field(ge=0)
    train_positives: int = Field(ge=0)
    train_negatives: int = Field(ge=0)
    purged_excluded: int = Field(ge=0)
    embargo_excluded: int = Field(ge=0)
    evaluation_rows: int = Field(ge=0)
    evaluation_positives: int = Field(ge=0)
    evaluation_negatives: int = Field(ge=0)
    segments: int = Field(ge=0)
    scores: tuple[MlMetricSummary, ...]
    comparisons: tuple[MlReferenceComparison, ...]
    calibration: tuple[MlCalibrationTable, ...]
    quantile: Decimal = Field(gt=0, lt=1)
    holdout_fits: int = Field(ge=0, le=MAX_HOLDOUT_REFITS)
    diagnostics: tuple[NonEmptyStr, ...] = ()

    @model_validator(mode="after")
    def validate_holdout(self) -> MlHoldoutResult:
        if self.train_positives + self.train_negatives != self.train_rows:
            raise ValueError("holdout training class counts must add up")
        if self.evaluation_positives + self.evaluation_negatives != self.evaluation_rows:
            raise ValueError("holdout evaluation class counts must add up")
        if self.quantile != HOLDOUT_QUANTILE:
            raise ValueError("holdout uses the preregistered lower quantile")
        if self.status is MlHoldoutStatus.NOT_EVALUATED:
            if (
                self.holdout_fits != 0
                or self.evaluation_rows != 0
                or self.train_rows != 0
                or self.segments != 0
                or self.scores
                or self.comparisons
                or self.calibration
                or self.selected_model is not None
            ):
                raise ValueError("an unopened holdout exposes no evaluation")
            return self
        if self.holdout_fits != MAX_HOLDOUT_REFITS:
            raise ValueError("an opened holdout uses exactly one refit")
        if self.selected_model is None or len(self.scores) != 3 or len(self.comparisons) != 2:
            raise ValueError("an opened holdout scores the winner and two references")
        if len(self.calibration) != 1 or self.calibration[0].scorer.value != (
            self.selected_model.value
        ):
            raise ValueError("holdout calibration covers only the selected candidate")
        return self


class MlBaselineSelection(ContractModel):
    """Selection sealed after development and before any holdout access."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["ml-baseline-selection-v1"] = SELECTION_SCHEMA_VERSION
    run_id: UUID
    dataset_id: UUID
    dataset_content_sha256: str
    protocol_sha256: str
    code_material_sha256: str
    development: MlDevelopmentResult
    selected_model: MlModelFamily
    holdout_state: Literal["not_opened"] = "not_opened"
    sealed_at: UTCDateTime

    @model_validator(mode="after")
    def validate_selection(self) -> MlBaselineSelection:
        _require_sha256(self.dataset_content_sha256, "dataset_content_sha256")
        _require_sha256(self.protocol_sha256, "protocol_sha256")
        _require_sha256(self.code_material_sha256, "code_material_sha256")
        if self.development.decision is not MlModelState.CANDIDATE_SELECTED:
            raise ValueError("a selection requires an admitted development candidate")
        if self.development.selected_model is not self.selected_model:
            raise ValueError("selection must match the development decision")
        return self


class MlTimings(ContractModel):
    """Observed wall-clock phases; never part of the semantic identity."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    read_seconds: Decimal = Field(ge=0)
    fit_seconds: Decimal = Field(ge=0)
    evaluation_seconds: Decimal = Field(ge=0)
    total_seconds: Decimal = Field(ge=0)


class MlBaselineReport(ContractModel):
    """Complete or explicitly insufficient evaluation report."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["ml-baseline-evaluation-v1"] = EVALUATION_SCHEMA_VERSION
    run_id: UUID
    status: MlArtifactStatus
    dataset_id: UUID
    dataset_content_sha256: str
    dataset_manifest_sha256: str
    protocol_sha256: str
    selection_sha256: str | None
    development: MlDevelopmentResult | None
    holdout: MlHoldoutResult | None
    insufficiency_reason: NonEmptyStr | None
    environment: MlEnvironment
    predictions_sha256: str | None
    prediction_rows: int = Field(ge=0)
    holdout_accesses: int = Field(ge=0)
    development_fits: int = Field(ge=0, le=MAX_DEVELOPMENT_FITS)
    holdout_fits: int = Field(ge=0, le=MAX_HOLDOUT_REFITS)
    timings: MlTimings
    peak_rss_bytes: int | None = Field(default=None, ge=0)
    workspace_opened: Literal[False] = False
    domain_writes: Literal[0] = 0

    @model_validator(mode="after")
    def validate_report(self) -> MlBaselineReport:
        for value, label in (
            (self.dataset_content_sha256, "dataset_content_sha256"),
            (self.dataset_manifest_sha256, "dataset_manifest_sha256"),
            (self.protocol_sha256, "protocol_sha256"),
        ):
            _require_sha256(value, label)
        if self.selection_sha256 is not None:
            _require_sha256(self.selection_sha256, "selection_sha256")
        if self.predictions_sha256 is not None:
            _require_sha256(self.predictions_sha256, "predictions_sha256")
        if self.status is MlArtifactStatus.INSUFFICIENT_EVALUATION_SAMPLE:
            if (
                self.development is not None
                or self.holdout is not None
                or self.selection_sha256 is not None
                or self.predictions_sha256 is not None
                or self.insufficiency_reason is None
                or self.prediction_rows != 0
                or self.holdout_accesses != 0
                or self.development_fits != 0
                or self.holdout_fits != 0
            ):
                raise ValueError("an insufficient report contains no evaluation evidence")
            return self
        if (
            self.development is None
            or self.holdout is None
            or self.insufficiency_reason is not None
            or self.predictions_sha256 is None
            or self.prediction_rows == 0
        ):
            raise ValueError("a completed report requires development and holdout sections")
        if self.development_fits != self.development.development_fits:
            raise ValueError("development fit count must match its result")
        if self.development.selected_model is None:
            if (
                self.selection_sha256 is not None
                or self.holdout.status is not MlHoldoutStatus.NOT_EVALUATED
                or self.holdout_accesses != 0
                or self.holdout_fits != 0
            ):
                raise ValueError("an unselected development result never opens the holdout")
        else:
            if self.selection_sha256 is None:
                raise ValueError("a selected candidate requires a sealed selection digest")
            if self.holdout.status is MlHoldoutStatus.NOT_EVALUATED:
                raise ValueError("a selected candidate requires one holdout decision")
            if self.holdout_accesses != 1 or self.holdout_fits != self.holdout.holdout_fits:
                raise ValueError("the opened holdout records exactly one access and refit")
        return self


class MlArtifactFile(ContractModel):
    """Digest and size of one sealed artifact file."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: NonEmptyStr
    sha256: str
    bytes: int = Field(gt=0)

    @model_validator(mode="after")
    def validate_file(self) -> MlArtifactFile:
        _require_sha256(self.sha256, "artifact file digest")
        if self.bytes > MAX_OUTPUT_FILE_BYTES:
            raise ValueError("artifact file exceeds the output byte cap")
        return self


class MlBaselineManifest(ContractModel):
    """Final content-addressed descriptor of one sealed run directory."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["ml-baseline-manifest-v1"] = MANIFEST_SCHEMA_VERSION
    protocol_id: Literal["crypto-historical-5d-baselines-v1"] = PROTOCOL_ID
    run_id: UUID
    status: MlArtifactStatus
    dataset_id: UUID
    dataset_content_sha256: str
    dataset_manifest_sha256: str
    dataset_rows_sha256: str
    code_material_sha256: str
    environment: MlEnvironment
    protocol: MlArtifactFile
    selection: MlArtifactFile | None
    predictions: MlArtifactFile | None
    report: MlArtifactFile
    root_bytes: int = Field(gt=0)
    created_at: UTCDateTime

    @model_validator(mode="after")
    def validate_manifest(self) -> MlBaselineManifest:
        for value, label in (
            (self.dataset_content_sha256, "dataset_content_sha256"),
            (self.dataset_manifest_sha256, "dataset_manifest_sha256"),
            (self.dataset_rows_sha256, "dataset_rows_sha256"),
            (self.code_material_sha256, "code_material_sha256"),
        ):
            _require_sha256(value, label)
        if self.protocol.name != PROTOCOL_FILE_NAME or self.report.name != REPORT_FILE_NAME:
            raise ValueError("manifest must name the sealed protocol and report files")
        if self.selection is not None and self.selection.name != SELECTION_FILE_NAME:
            raise ValueError("manifest must name the sealed selection file")
        if self.predictions is not None and self.predictions.name != PREDICTIONS_FILE_NAME:
            raise ValueError("manifest must name the sealed predictions file")
        if self.status is MlArtifactStatus.COMPLETED:
            if self.predictions is None:
                raise ValueError("a completed artifact requires sealed predictions")
            if self.selection is None and self.report.bytes == 0:
                raise ValueError("a completed artifact requires a non-empty report")
        elif self.predictions is not None or self.selection is not None:
            raise ValueError("an insufficient artifact seals no predictions or selection")
        declared = self.protocol.bytes + self.report.bytes
        declared += self.selection.bytes if self.selection is not None else 0
        declared += self.predictions.bytes if self.predictions is not None else 0
        if declared != self.root_bytes:
            raise ValueError("root bytes must equal the sum of sealed files")
        if self.root_bytes > MAX_ROOT_BYTES:
            raise ValueError("artifact root exceeds the byte cap")
        return self


class MlBaselineReceipt(ContractModel):
    """Compact CLI receipt without probabilities or holdout values."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: UUID
    status: MlRunStatus
    report_status: MlArtifactStatus
    decision: MlModelState | None
    holdout_status: MlHoldoutStatus
    dataset_id: UUID
    dataset_content_sha256: str
    output_dir: NonEmptyStr
    artifact_dir: NonEmptyStr
    protocol_sha256: str
    report_sha256: str
    manifest_sha256: str
    prediction_rows: int = Field(ge=0)
    development_fits: int = Field(ge=0)
    holdout_fits: int = Field(ge=0)
    holdout_accesses: int = Field(ge=0)
    artifact_bytes: int = Field(gt=0)
    elapsed_seconds: Decimal = Field(ge=0)
    peak_rss_bytes: int | None = Field(default=None, ge=0)

    def to_json_dict(self) -> dict[str, object]:
        """Return the deterministic JSON projection used by the CLI."""
        return self.model_dump(mode="json")


__all__ = [
    "BLOCK_LENGTH",
    "BOOTSTRAP_REPLICATES",
    "BOOTSTRAP_SEED",
    "CALIBRATION_BIN_COUNT",
    "DATASET_MANIFEST_SCHEMA_VERSION",
    "DECISION_END",
    "DECISION_START",
    "DEVELOPMENT_QUANTILE",
    "EXCLUDED_START",
    "HOLDOUT_QUANTILE",
    "HYPOTHESIS",
    "LOG_LOSS_CLIP",
    "MANIFEST_FILE_NAME",
    "MANIFEST_SCHEMA_VERSION",
    "MAX_DECISION_ROWS",
    "MAX_DEVELOPMENT_FITS",
    "MAX_HOLDOUT_REFITS",
    "MAX_INPUT_FILE_BYTES",
    "MAX_OUTPUT_FILE_BYTES",
    "MAX_ROOT_BYTES",
    "MIN_CLASS_ROWS",
    "MIN_EVALUATION_ROWS",
    "MIN_HOLDOUT_ROWS",
    "MIN_RELATIVE_BRIER_IMPROVEMENT",
    "MIN_SEGMENT_ROWS",
    "MIN_TRAIN_ROWS",
    "ML_NAMESPACE",
    "MlArtifactFile",
    "MlArtifactStatus",
    "MlBaselineManifest",
    "MlBaselineProtocol",
    "MlBaselineReceipt",
    "MlBaselineReport",
    "MlBaselineSelection",
    "MlCalibrationBin",
    "MlCalibrationTable",
    "MlDecisionCriteria",
    "MlDevelopmentResult",
    "MlEnvironment",
    "MlFoldBoundary",
    "MlFoldResult",
    "MlHistogramParameters",
    "MlHoldoutResult",
    "MlHoldoutStatus",
    "MlLogisticParameters",
    "MlMetricSummary",
    "MlModelFamily",
    "MlModelState",
    "MlProtocolTarget",
    "MlReferenceComparison",
    "MlResamplingPolicy",
    "MlRunStatus",
    "MlScorer",
    "MlStudyCaps",
    "MlTimings",
    "PREDICTIONS_FILE_NAME",
    "PROTOCOL_FILE_NAME",
    "PROTOCOL_ID",
    "PROTOCOL_SCHEMA_VERSION",
    "REPORT_FILE_NAME",
    "SELECTION_FILE_NAME",
    "SELECTION_SCHEMA_VERSION",
    "STUDY_LIMITATIONS",
    "TIE_TOLERANCE",
    "WARMUP_START",
    "development_fold_boundaries",
    "metric_decimal",
    "run_identity",
]
