"""Strict, deterministic contracts for the preregistered volatility study.

The study measures whether a small Ridge model over three log mean-squared
return windows anticipates a five-day forward log variation proxy better than
three fixed references.  Every value, fold and criterion is sealed before the
first fit and no probabilistic or trading meaning is attached to the result.
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, time, timedelta
from decimal import ROUND_HALF_EVEN, Context, Decimal, localcontext
from enum import StrEnum
from typing import Annotated, Literal
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import BeforeValidator, ConfigDict, Field, model_validator

from investment_analyst.analytics.analytical_access_models import canonical_analytical_json
from investment_analyst.core.models.base import ContractModel, NonEmptyStr, UTCDateTime
from investment_analyst.research.ml_models import MlEnvironment, MlTimings

VOLATILITY_NAMESPACE = uuid5(NAMESPACE_URL, "investment-analyst/volatility-research-v1")

PROTOCOL_ID = "crypto-historical-log-variation-5d-v1"
PROTOCOL_SCHEMA_VERSION = "research-volatility-protocol-v1"
REPORT_SCHEMA_VERSION = "research-volatility-report-v1"
MANIFEST_SCHEMA_VERSION = "research-volatility-manifest-v1"
PROGRESS_SCHEMA_VERSION = "research-volatility-progress-v1"

PROTOCOL_FILE_NAME = "protocol.json"
REPORT_FILE_NAME = "report.json"
PREDICTIONS_FILE_NAME = "predictions.jsonl"
MANIFEST_FILE_NAME = "manifest.json"
PROGRESS_FILE_NAME = "progress.json"

ASSET_ID = "crypto:btc-usd"
SOURCE_ID = "coinbase-exchange:btc-usd:daily-candles"
ARCHIVE_REQUEST_START = date(2019, 1, 1)
ARCHIVE_REQUEST_END = date(2025, 12, 31)
DECISION_START = date(2020, 1, 1)
DECISION_END = date(2025, 12, 25)
DECISION_COUNT = 2186
RESERVED_START_INDEX = 1748
RESERVED_START_DATE = date(2024, 10, 14)

DECISION_HOUR_UTC = 12
ASSUMED_AVAILABILITY_DELAY = timedelta(hours=1)
FEATURE_WINDOWS = (1, 7, 30)
REFERENCE_WINDOW = 5
TARGET_HORIZON = 5
VALIDATION_EMBARGO_DAYS = 5
MIN_TRAIN_ROWS = 500
MIN_VALIDATION_ROWS = 100
MIN_SEGMENT_ROWS = 20
BOOTSTRAP_REPLICATES = 2000
BOOTSTRAP_BLOCK_LENGTH = 20
BOOTSTRAP_SEED = 1729
DEVELOPMENT_FITS = 3
MIN_RELATIVE_IMPROVEMENT = Decimal("0.05")
DECIMAL_PRECISION = 34

MAX_INPUT_FILE_BYTES = 4 * 1024 * 1024
MAX_RUN_BYTES = 8 * 1024 * 1024
MAX_PREDICTIONS_BYTES = 5 * 1024 * 1024
MAX_ROOT_BYTES = 16 * 1024 * 1024
MAX_RETAINED_RUNS = 2
MAX_CANDLES = 2557

DECIMAL34 = Context(prec=DECIMAL_PRECISION, rounding=ROUND_HALF_EVEN)

with localcontext(DECIMAL34):
    EPSILON = Decimal("1e-12")
    ONE_SIDED_QUANTILE = Decimal("0.05") / Decimal(3)

_SHA256 = re.compile(r"[0-9a-f]{64}")

AVAILABILITY_POLICY = "historical-close-plus-1h-assumed-v1"
AVAILABILITY_BASIS = "historical_reconstruction_assumed"

HYPOTHESIS = (
    "A small Ridge model over one-, seven- and thirty-day log mean-squared daily"
    " returns may anticipate the next five-day log mean-squared return of BTC"
    " better than persistence and training-mean references; the study measures"
    " that value out of sample without tuning, promotion or trading meaning."
)

STUDY_LIMITATIONS = (
    "Solo hay cierres diarios: el target es un log de media de retornos"
    " logaritmicos al cuadrado, no una varianza realizada intradia ni una"
    " volatilidad anualizada.",
    "La disponibilidad es historical_reconstruction_assumed con"
    " strict_pit_verified=false; no sustituye el gate PIT observado.",
    "El resultado es retrospectivo y condicionado a este archivo, activo y"
    " ventana; no es una recomendacion, una senal ni una promesa de rendimiento.",
    "El periodo reservado no se evalua: holdout_fits=0 y"
    " target_evaluations_reserved=0 en todo resultado.",
)


def _reject_float(value: object) -> object:
    if isinstance(value, (bool, float)):
        raise ValueError("volatility financial values must use Decimal")
    return value


VolatilityDecimal = Annotated[Decimal, BeforeValidator(_reject_float)]


def _require_sha256(value: str, label: str) -> str:
    if _SHA256.fullmatch(value) is None:
        raise ValueError(f"{label} must be a lowercase SHA-256 digest")
    return value


def decision_instant(day: date) -> datetime:
    """Return the preregistered UTC decision instant for one decision date."""
    return datetime.combine(day, time(DECISION_HOUR_UTC, 0), tzinfo=UTC)


def bar_start_instant(day: date) -> datetime:
    """Return the UTC instant that starts one daily bar."""
    return datetime.combine(day, time(0, 0), tzinfo=UTC)


def assumed_available_instant(bar_end: datetime) -> datetime:
    """Apply the single assumed close-plus-one-hour availability policy."""
    return bar_end.astimezone(UTC) + ASSUMED_AVAILABILITY_DELAY


def feature_available_instant(day: date) -> datetime:
    """Return the latest assumed availability of a decision's feature bars."""
    return assumed_available_instant(bar_start_instant(day))


def label_maturity_instant(day: date) -> datetime:
    """Return the instant at which the five-day forward label is mature."""
    return assumed_available_instant(bar_start_instant(day + timedelta(days=TARGET_HORIZON + 1)))


def reserved_label_cut() -> datetime:
    """Return the strict cut that keeps labels out of the reserved period."""
    return bar_start_instant(RESERVED_START_DATE) - timedelta(days=VALIDATION_EMBARGO_DAYS)


def fold_label_cut(validation_end_date: date) -> datetime:
    """Return the label-maturity cut of one validation window."""
    return bar_start_instant(validation_end_date) - timedelta(days=VALIDATION_EMBARGO_DAYS)


def decision_dates() -> tuple[date, ...]:
    """Return the complete preregistered decision calendar."""
    return tuple(DECISION_START + timedelta(days=offset) for offset in range(DECISION_COUNT))


def volatility_sample_key(
    *,
    asset_id: str,
    source_id: str,
    decision_at: datetime,
) -> UUID:
    """Derive the stable sample identity from the decision, asset and source."""
    instant = decision_at.astimezone(UTC).isoformat()
    payload = {
        "asset_id": asset_id,
        "decision_at": instant,
        "protocol_id": PROTOCOL_ID,
        "source_id": source_id,
    }
    return uuid5(VOLATILITY_NAMESPACE, canonical_analytical_json(payload).decode("utf-8"))


def volatility_row_id(
    *,
    sample_key: UUID,
    feature_hash: str,
    target_hash: str,
    feature_bar_ids: tuple[UUID, ...],
    target_bar_ids: tuple[UUID, ...],
) -> UUID:
    """Derive the row identity from feature, target and bar evidence."""
    payload = {
        "feature_bar_ids": [str(value) for value in feature_bar_ids],
        "feature_hash": _require_sha256(feature_hash, "feature_hash"),
        "protocol_id": PROTOCOL_ID,
        "sample_key": str(sample_key),
        "target_bar_ids": [str(value) for value in target_bar_ids],
        "target_hash": _require_sha256(target_hash, "target_hash"),
    }
    return uuid5(VOLATILITY_NAMESPACE, canonical_analytical_json(payload).decode("utf-8"))


def volatility_run_id(
    *,
    protocol_sha256: str,
    archive_id: UUID,
    archive_semantic_sha256: str,
    code_material_sha256: str,
    environment: MlEnvironment,
) -> UUID:
    """Derive the run identity without paths or the execution clock."""
    payload = {
        "archive_id": str(archive_id),
        "archive_semantic_sha256": _require_sha256(
            archive_semantic_sha256, "archive_semantic_sha256"
        ),
        "code_material_sha256": _require_sha256(code_material_sha256, "code_material_sha256"),
        "environment": environment.model_dump(mode="json"),
        "protocol_id": PROTOCOL_ID,
        "protocol_sha256": _require_sha256(protocol_sha256, "protocol_sha256"),
    }
    return uuid5(VOLATILITY_NAMESPACE, canonical_analytical_json(payload).decode("utf-8"))


class VolatilityModel(StrEnum):
    RIDGE = "ridge"
    PERSISTENCE_5D = "persistence_5d"
    PERSISTENCE_30D = "persistence_30d"
    TRAINING_MEAN = "training_mean"


REFERENCE_MODELS: tuple[VolatilityModel, ...] = (
    VolatilityModel.PERSISTENCE_5D,
    VolatilityModel.PERSISTENCE_30D,
    VolatilityModel.TRAINING_MEAN,
)
ALL_MODELS: tuple[VolatilityModel, ...] = (VolatilityModel.RIDGE,) + REFERENCE_MODELS


class VolatilityDecision(StrEnum):
    DEVELOPMENT_EVIDENCE = "development_evidence"
    NO_DEMONSTRATED_INCREMENT = "no_demonstrated_increment"


class VolatilityArtifactStatus(StrEnum):
    COMPLETED = "completed"
    INSUFFICIENT_SAMPLE = "insufficient_sample"
    ERROR = "error"


class VolatilityRunStatus(StrEnum):
    EVALUATED = "evaluated"
    REUSED = "reused"


class VolatilityModelParameters(ContractModel):
    """The single preregistered numerical candidate."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    family: Literal["ridge"] = "ridge"
    scaler: Literal["standard_scaler_train_only"] = "standard_scaler_train_only"
    alpha: VolatilityDecimal = Decimal("1.0")
    fit_intercept: Literal[True] = True
    solver: Literal["svd"] = "svd"
    folds: Literal[3] = 3
    fits_per_fold: Literal[1] = 1
    threads: Literal[1] = 1

    @model_validator(mode="after")
    def validate_parameters(self) -> VolatilityModelParameters:
        if self.alpha != Decimal("1.0"):
            raise ValueError("ridge alpha is preregistered as 1.0")
        return self


class VolatilityCriteria(ContractModel):
    """Fixed decision rule, including the one-sided joint confidence split."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    primary_metric: Literal["mse"] = "mse"
    secondary_metric: Literal["mae"] = "mae"
    minimum_relative_improvement: VolatilityDecimal = MIN_RELATIVE_IMPROVEMENT
    quantile: VolatilityDecimal = ONE_SIDED_QUANTILE
    require_positive_lower_bounds: Literal[3] = 3
    require_positive_fold_improvement: Literal[True] = True

    @model_validator(mode="after")
    def validate_criteria(self) -> VolatilityCriteria:
        if self.minimum_relative_improvement != MIN_RELATIVE_IMPROVEMENT:
            raise ValueError("the minimum relative improvement is preregistered")
        if self.quantile != ONE_SIDED_QUANTILE:
            raise ValueError("the one-sided quantile is preregistered as 0.05/3")
        return self


class VolatilityResampling(ContractModel):
    """Paired moving-block bootstrap over contiguous validation segments."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    replicates: Literal[2000] = 2000
    block_length: Literal[20] = 20
    seed: Literal[1729] = 1729
    generator: Literal["PCG64"] = "PCG64"
    quantile_method: Literal["linear"] = "linear"
    segment_rule: Literal["contiguous-decision-days-within-fold"] = (
        "contiguous-decision-days-within-fold"
    )
    truncation: Literal["truncate-last-draw-to-segment-size"] = "truncate-last-draw-to-segment-size"


class VolatilityHoldoutPolicy(ContractModel):
    """The reserved period is declared but never evaluated in this block."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["not_evaluated"] = "not_evaluated"
    holdout_fits: Literal[0] = 0
    target_evaluations_reserved: Literal[0] = 0
    promoted: Literal[False] = False


class VolatilityFoldBoundary(ContractModel):
    """One preregistered expanding train and validation window."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    fold: int = Field(ge=1, le=3)
    train_start_index: Literal[0] = 0
    validation_start_index: int = Field(ge=1)
    validation_end_index: int = Field(ge=2)
    validation_start_date: date
    validation_end_date: date

    @model_validator(mode="after")
    def validate_window(self) -> VolatilityFoldBoundary:
        if self.validation_end_index <= self.validation_start_index:
            raise ValueError("the validation window must be non-empty")
        expected_start = DECISION_START + timedelta(days=self.validation_start_index)
        expected_end = DECISION_START + timedelta(days=self.validation_end_index)
        if self.validation_start_date != expected_start:
            raise ValueError("the validation start date disagrees with its index")
        if self.validation_end_date != expected_end:
            raise ValueError("the validation end date disagrees with its index")
        return self


PREREGISTERED_FOLDS: tuple[VolatilityFoldBoundary, ...] = (
    VolatilityFoldBoundary(
        fold=1,
        validation_start_index=1093,
        validation_end_index=1311,
        validation_start_date=date(2022, 12, 29),
        validation_end_date=date(2023, 8, 4),
    ),
    VolatilityFoldBoundary(
        fold=2,
        validation_start_index=1311,
        validation_end_index=1529,
        validation_start_date=date(2023, 8, 4),
        validation_end_date=date(2024, 3, 9),
    ),
    VolatilityFoldBoundary(
        fold=3,
        validation_start_index=1529,
        validation_end_index=1748,
        validation_start_date=date(2024, 3, 9),
        validation_end_date=date(2024, 10, 14),
    ),
)


class VolatilityProtocol(ContractModel):
    """The complete immutable preregistration sealed before the first fit."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["research-volatility-protocol-v1"] = PROTOCOL_SCHEMA_VERSION
    protocol_id: Literal["crypto-historical-log-variation-5d-v1"] = PROTOCOL_ID
    asset_id: NonEmptyStr
    source_id: NonEmptyStr
    archive_id: UUID
    archive_semantic_sha256: str
    archive_candles_sha256: str
    archive_retrieved_at: UTCDateTime
    archive_request_start: date
    archive_request_end: date
    study_cut_at: UTCDateTime
    decision_start: date
    decision_end: date
    decision_count: int = Field(ge=1)
    reserved_start_index: int = Field(ge=1)
    reserved_start_date: date
    folds: tuple[VolatilityFoldBoundary, ...]
    feature_windows: tuple[int, ...]
    reference_window: int = Field(ge=1)
    target_horizon: int = Field(ge=1)
    epsilon: VolatilityDecimal
    decimal_precision: Literal[34] = DECIMAL_PRECISION
    decision_hour_utc: Literal[12] = DECISION_HOUR_UTC
    availability_policy: Literal["historical-close-plus-1h-assumed-v1"] = AVAILABILITY_POLICY
    availability_basis: Literal["historical_reconstruction_assumed"] = AVAILABILITY_BASIS
    strict_pit_verified: Literal[False] = False
    validation_embargo_days: Literal[5] = VALIDATION_EMBARGO_DAYS
    minimum_train_rows: Literal[500] = MIN_TRAIN_ROWS
    minimum_validation_rows: Literal[100] = MIN_VALIDATION_ROWS
    minimum_segment_rows: Literal[20] = MIN_SEGMENT_ROWS
    model: VolatilityModelParameters
    references: tuple[VolatilityModel, ...]
    criteria: VolatilityCriteria
    resampling: VolatilityResampling
    holdout: VolatilityHoldoutPolicy
    hypothesis: NonEmptyStr
    limitations: tuple[NonEmptyStr, ...]

    @model_validator(mode="after")
    def validate_protocol(self) -> VolatilityProtocol:
        if self.asset_id != ASSET_ID:
            raise ValueError("the study supports only the preregistered BTC asset")
        if self.source_id != SOURCE_ID:
            raise ValueError("the study supports only the preregistered Coinbase source")
        _require_sha256(self.archive_semantic_sha256, "archive_semantic_sha256")
        _require_sha256(self.archive_candles_sha256, "archive_candles_sha256")
        if self.archive_request_start != ARCHIVE_REQUEST_START:
            raise ValueError("the archive request start is preregistered")
        if self.archive_request_end != ARCHIVE_REQUEST_END:
            raise ValueError("the archive request end is preregistered")
        if self.study_cut_at < self.archive_retrieved_at:
            raise ValueError("the study cut cannot precede the archive retrieval")
        if self.decision_start != DECISION_START or self.decision_end != DECISION_END:
            raise ValueError("the decision calendar is preregistered")
        expected_count = (DECISION_END - DECISION_START).days + 1
        if self.decision_count != expected_count or self.decision_count != DECISION_COUNT:
            raise ValueError("the decision count is preregistered")
        if self.reserved_start_index != RESERVED_START_INDEX:
            raise ValueError("the reserved start index is preregistered")
        if self.reserved_start_date != RESERVED_START_DATE:
            raise ValueError("the reserved start date is preregistered")
        if self.folds != PREREGISTERED_FOLDS:
            raise ValueError("the fold boundaries are preregistered")
        if self.feature_windows != FEATURE_WINDOWS:
            raise ValueError("the feature windows are preregistered")
        if self.reference_window != REFERENCE_WINDOW:
            raise ValueError("the persistence reference window is preregistered")
        if self.target_horizon != TARGET_HORIZON:
            raise ValueError("the target horizon is preregistered")
        if self.epsilon != EPSILON:
            raise ValueError("epsilon is preregistered as 1e-12")
        if self.references != REFERENCE_MODELS:
            raise ValueError("the reference models are preregistered")
        if not self.hypothesis.strip():
            raise ValueError("the hypothesis must be explicit")
        if not self.limitations:
            raise ValueError("the limitations must be explicit")
        return self


class VolatilityMetric(ContractModel):
    """MSE and MAE of one model over a fold or the pooled out-of-sample rows."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    model: VolatilityModel
    rows: int = Field(ge=0)
    mse: VolatilityDecimal = Field(ge=0)
    mae: VolatilityDecimal = Field(ge=0)


class VolatilityComparison(ContractModel):
    """One paired candidate-to-reference comparison."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    model: Literal[VolatilityModel.RIDGE] = VolatilityModel.RIDGE
    reference: VolatilityModel
    fold: int | None = Field(default=None, ge=1, le=3)
    model_mse: VolatilityDecimal = Field(ge=0)
    reference_mse: VolatilityDecimal = Field(ge=0)
    absolute_improvement: VolatilityDecimal
    relative_improvement: VolatilityDecimal | None = None
    mean_paired_difference: VolatilityDecimal | None = None
    lower_bound: VolatilityDecimal | None = None
    lower_bound_positive: bool | None = None
    admissible: bool
    improved_at_least_five_percent: bool

    @model_validator(mode="after")
    def validate_comparison(self) -> VolatilityComparison:
        if self.reference not in REFERENCE_MODELS:
            raise ValueError("comparisons require one of the three references")
        if self.absolute_improvement != self.reference_mse - self.model_mse:
            raise ValueError("absolute improvement must be the paired MSE difference")
        if self.reference_mse == 0:
            if self.relative_improvement is not None:
                raise ValueError("a zero reference MSE has no admissible relative improvement")
            if self.admissible:
                raise ValueError("a zero reference MSE is not an admissible comparison")
            if self.improved_at_least_five_percent:
                raise ValueError("a zero reference MSE cannot demonstrate improvement")
        else:
            with localcontext(DECIMAL34):
                relative = (self.reference_mse - self.model_mse) / self.reference_mse
            if self.relative_improvement != relative:
                raise ValueError("relative improvement must match the sealed MSEs")
            if self.admissible != (self.relative_improvement is not None):
                raise ValueError("admissibility must follow the relative improvement")
            expected = relative >= MIN_RELATIVE_IMPROVEMENT
            if self.improved_at_least_five_percent != expected:
                raise ValueError("the five percent flag must follow the sealed criterion")
        if self.fold is None:
            if (
                self.mean_paired_difference is None
                or self.lower_bound is None
                or self.lower_bound_positive is None
            ):
                raise ValueError("pooled comparisons require bootstrap evidence")
        elif any(
            value is not None
            for value in (
                self.mean_paired_difference,
                self.lower_bound,
                self.lower_bound_positive,
            )
        ):
            raise ValueError("fold comparisons do not carry bootstrap evidence")
        return self


class VolatilityFoldResult(ContractModel):
    """Selection counts and out-of-sample scores of one fold."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    fold: int = Field(ge=1, le=3)
    train_rows: int = Field(ge=0)
    validation_rows: int = Field(ge=0)
    train_embargo_excluded: int = Field(ge=0)
    train_ineligible_excluded: int = Field(ge=0)
    validation_embargo_excluded: int = Field(ge=0)
    validation_ineligible_excluded: int = Field(ge=0)
    segments: int = Field(ge=0)
    segment_lengths: tuple[int, ...]
    scores: tuple[VolatilityMetric, ...]
    comparisons: tuple[VolatilityComparison, ...]
    diagnostics: tuple[NonEmptyStr, ...]

    @model_validator(mode="after")
    def validate_fold(self) -> VolatilityFoldResult:
        if tuple(metric.model for metric in self.scores) != ALL_MODELS:
            raise ValueError("fold scores must cover the candidate and three references")
        if tuple(comparison.reference for comparison in self.comparisons) != REFERENCE_MODELS:
            raise ValueError("fold comparisons must cover the three references")
        if any(comparison.fold != self.fold for comparison in self.comparisons):
            raise ValueError("fold comparisons must carry their fold")
        if self.segments != len(self.segment_lengths):
            raise ValueError("segment count must match the recorded lengths")
        if any(metric.rows != self.validation_rows for metric in self.scores):
            raise ValueError("fold scores must cover exactly the validation rows")
        return self


class VolatilityPredictionRecord(ContractModel):
    """One out-of-sample development prediction and its paired losses."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    sample_key: UUID
    row_id: UUID
    decision_at: UTCDateTime
    fold: int = Field(ge=1, le=3)
    model: VolatilityModel
    target: VolatilityDecimal
    estimate: VolatilityDecimal
    squared_error: VolatilityDecimal = Field(ge=0)
    absolute_error: VolatilityDecimal = Field(ge=0)

    @model_validator(mode="after")
    def validate_record(self) -> VolatilityPredictionRecord:
        if not all(
            value.is_finite()
            for value in (
                self.target,
                self.estimate,
                self.squared_error,
                self.absolute_error,
            )
        ):
            raise ValueError("prediction values must be finite Decimals")
        with localcontext(DECIMAL34):
            squared = (self.estimate - self.target) ** 2
            absolute = abs(self.estimate - self.target)
        if self.squared_error != squared:
            raise ValueError("squared error must match the prediction")
        if self.absolute_error != absolute:
            raise ValueError("absolute error must match the prediction")
        return self


class VolatilityReportCounts(ContractModel):
    """Honest reading and construction counts without inferred values."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    planned_decisions: int = Field(ge=1)
    constructed_decisions: int = Field(ge=0)
    reserved_decisions_not_constructed: int = Field(ge=0)
    feature_ready: int = Field(ge=0)
    target_ready: int = Field(ge=0)
    reserved_target_constructions: Literal[0] = 0
    candles_read: int = Field(ge=0)
    integrity_read_only: Literal[True] = True

    @model_validator(mode="after")
    def validate_counts(self) -> VolatilityReportCounts:
        if (
            self.constructed_decisions + self.reserved_decisions_not_constructed
            != self.planned_decisions
        ):
            raise ValueError("constructed plus reserved decisions must match the calendar")
        if self.target_ready > self.feature_ready:
            raise ValueError("a target cannot exist without its features")
        return self


class VolatilityReport(ContractModel):
    """Sealed development report, including a statistically negative outcome."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["research-volatility-report-v1"] = REPORT_SCHEMA_VERSION
    protocol_id: Literal["crypto-historical-log-variation-5d-v1"] = PROTOCOL_ID
    run_id: UUID
    status: VolatilityArtifactStatus
    protocol_sha256: str
    archive_id: UUID
    archive_semantic_sha256: str
    archive_candles_sha256: str
    archive_retrieved_at: UTCDateTime
    code_material_sha256: str
    code_material_files: tuple[NonEmptyStr, ...]
    environment: MlEnvironment
    criteria: VolatilityCriteria
    decision: VolatilityDecision | None
    decision_reason: NonEmptyStr | None
    folds: tuple[VolatilityFoldResult, ...]
    pooled_scores: tuple[VolatilityMetric, ...]
    pooled_comparisons: tuple[VolatilityComparison, ...]
    holdout_status: Literal["not_evaluated"] = "not_evaluated"
    holdout_fits: Literal[0] = 0
    target_evaluations_reserved: Literal[0] = 0
    promoted: Literal[False] = False
    counts: VolatilityReportCounts
    prediction_rows: int = Field(ge=0)
    development_fits: int = Field(ge=0)
    fits_executed_this_invocation: int = Field(ge=0)
    resumed_from_progress: bool
    timings: MlTimings
    peak_rss_bytes: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def validate_report(self) -> VolatilityReport:
        _require_sha256(self.protocol_sha256, "protocol_sha256")
        _require_sha256(self.archive_semantic_sha256, "archive_semantic_sha256")
        _require_sha256(self.archive_candles_sha256, "archive_candles_sha256")
        _require_sha256(self.code_material_sha256, "code_material_sha256")
        if self.status is VolatilityArtifactStatus.COMPLETED:
            if len(self.folds) != 3:
                raise ValueError("a completed report requires the three folds")
            if self.decision is None or self.decision_reason is None:
                raise ValueError("a completed report requires a decision")
            if self.development_fits != DEVELOPMENT_FITS:
                raise ValueError("a completed report requires exactly three finalist fits")
            if self.prediction_rows <= 0:
                raise ValueError("a completed report requires out-of-sample predictions")
            if tuple(metric.model for metric in self.pooled_scores) != ALL_MODELS:
                raise ValueError("pooled scores must cover the candidate and references")
            if (
                tuple(comparison.reference for comparison in self.pooled_comparisons)
                != REFERENCE_MODELS
            ):
                raise ValueError("pooled comparisons must cover the three references")
            if any(comparison.fold is not None for comparison in self.pooled_comparisons):
                raise ValueError("pooled comparisons must not carry a fold")
        else:
            if self.decision is not None or self.folds or self.pooled_scores:
                raise ValueError("a non-completed report must not claim evaluation evidence")
            if self.pooled_comparisons or self.prediction_rows:
                raise ValueError("a non-completed report seals no predictions")
            if self.development_fits != 0:
                raise ValueError("a non-completed report has no finalist fits")
            if self.decision_reason is None:
                raise ValueError("a non-completed report must state its reason")
        return self


class VolatilityArtifactFile(ContractModel):
    """Digest and size of one sealed artifact file."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: NonEmptyStr
    sha256: str
    bytes: int = Field(gt=0)

    @model_validator(mode="after")
    def validate_file(self) -> VolatilityArtifactFile:
        _require_sha256(self.sha256, "artifact file digest")
        if self.bytes > MAX_RUN_BYTES:
            raise ValueError("artifact file exceeds the run byte cap")
        return self


class VolatilityManifest(ContractModel):
    """Final content-addressed descriptor of one completed run directory."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["research-volatility-manifest-v1"] = MANIFEST_SCHEMA_VERSION
    protocol_id: Literal["crypto-historical-log-variation-5d-v1"] = PROTOCOL_ID
    run_id: UUID
    status: Literal[VolatilityArtifactStatus.COMPLETED] = VolatilityArtifactStatus.COMPLETED
    archive_id: UUID
    archive_semantic_sha256: str
    archive_candles_sha256: str
    code_material_sha256: str
    code_material_files: tuple[NonEmptyStr, ...]
    environment: MlEnvironment
    criteria: VolatilityCriteria
    protocol: VolatilityArtifactFile
    predictions: VolatilityArtifactFile
    report: VolatilityArtifactFile
    root_bytes: int = Field(gt=0)
    created_at: UTCDateTime

    @model_validator(mode="after")
    def validate_manifest(self) -> VolatilityManifest:
        for value, label in (
            (self.archive_semantic_sha256, "archive_semantic_sha256"),
            (self.archive_candles_sha256, "archive_candles_sha256"),
            (self.code_material_sha256, "code_material_sha256"),
        ):
            _require_sha256(value, label)
        if self.protocol.name != PROTOCOL_FILE_NAME:
            raise ValueError("manifest must name the sealed protocol file")
        if self.predictions.name != PREDICTIONS_FILE_NAME:
            raise ValueError("manifest must name the sealed predictions file")
        if self.report.name != REPORT_FILE_NAME:
            raise ValueError("manifest must name the sealed report file")
        declared = self.protocol.bytes + self.predictions.bytes + self.report.bytes
        if declared != self.root_bytes:
            raise ValueError("root bytes must equal the sum of the sealed files")
        if self.root_bytes > MAX_RUN_BYTES:
            raise ValueError("artifact root exceeds the run byte cap")
        return self


class VolatilityReceipt(ContractModel):
    """Compact CLI receipt without predictions or per-row values."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: UUID
    status: VolatilityRunStatus
    report_status: VolatilityArtifactStatus
    decision: VolatilityDecision | None
    holdout_status: Literal["not_evaluated"] = "not_evaluated"
    archive_id: UUID
    archive_semantic_sha256: str
    output_dir: NonEmptyStr
    artifact_dir: NonEmptyStr
    protocol_sha256: str
    report_sha256: str
    manifest_sha256: str | None = None
    predictions_sha256: str | None = None
    prediction_rows: int = Field(ge=0)
    development_fits: int = Field(ge=0)
    fits_executed_this_invocation: int = Field(ge=0)
    resumed_from_progress: bool
    artifact_bytes: int = Field(gt=0)
    root_bytes: int = Field(gt=0)
    retained_run_ids: tuple[UUID, ...]
    final_run_id: UUID
    superseded_run_ids: tuple[UUID, ...]
    elapsed_seconds: VolatilityDecimal = Field(ge=0)
    peak_rss_bytes: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def validate_receipt(self) -> VolatilityReceipt:
        _require_sha256(self.protocol_sha256, "protocol_sha256")
        _require_sha256(self.report_sha256, "report_sha256")
        if self.final_run_id != self.run_id:
            raise ValueError("the final retained run must be the resolved run")
        if self.superseded_run_ids and self.run_id in self.superseded_run_ids:
            raise ValueError("the current run cannot be superseded")
        if set(self.retained_run_ids) != {self.run_id, *self.superseded_run_ids}:
            raise ValueError("retained runs must match the current and superseded runs")
        if self.report_status is VolatilityArtifactStatus.COMPLETED:
            if self.manifest_sha256 is None or self.predictions_sha256 is None:
                raise ValueError("a completed run requires sealed manifest and predictions")
            if self.decision is None:
                raise ValueError("a completed run requires a decision")
        elif self.decision is not None:
            raise ValueError("a non-completed run cannot declare a decision")
        return self

    def to_json_dict(self) -> dict[str, object]:
        """Return the deterministic JSON projection used by the CLI."""
        return self.model_dump(mode="json")


class VolatilityFoldProgress(ContractModel):
    """Verified progress of one completed fold, reusable after interruption."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    result: VolatilityFoldResult
    validation_sample_keys: tuple[UUID, ...]
    segments: tuple[tuple[UUID, ...], ...]
    predictions: tuple[VolatilityPredictionRecord, ...]

    @model_validator(mode="after")
    def validate_progress(self) -> VolatilityFoldProgress:
        if len(self.validation_sample_keys) != self.result.validation_rows:
            raise ValueError("progress keys must match the validation rows")
        if len(set(self.validation_sample_keys)) != len(self.validation_sample_keys):
            raise ValueError("progress validation keys must be unique")
        if any(len(segment) < MIN_SEGMENT_ROWS for segment in self.segments):
            raise ValueError("progress segments must satisfy the declared floor")
        if sum(len(segment) for segment in self.segments) != self.result.validation_rows:
            raise ValueError("progress segments must cover the validation rows")
        flat = tuple(key for segment in self.segments for key in segment)
        if set(flat) != set(self.validation_sample_keys) or len(flat) != len(flat):
            raise ValueError("progress segments must partition the validation keys")
        expected = len(self.validation_sample_keys) * len(ALL_MODELS)
        if len(self.predictions) != expected:
            raise ValueError("progress predictions must cover every sample and model")
        if any(prediction.fold != self.result.fold for prediction in self.predictions):
            raise ValueError("progress predictions must carry the fold")
        keys = {(prediction.sample_key, prediction.model) for prediction in self.predictions}
        if keys != {
            (sample_key, model)
            for sample_key in self.validation_sample_keys
            for model in ALL_MODELS
        }:
            raise ValueError("progress predictions must be unique and complete")
        return self


class VolatilityProgress(ContractModel):
    """Prefix of completed folds kept across an interrupted execution."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["research-volatility-progress-v1"] = PROGRESS_SCHEMA_VERSION
    run_id: UUID
    protocol_sha256: str
    folds: tuple[VolatilityFoldProgress, ...]

    @model_validator(mode="after")
    def validate_progress(self) -> VolatilityProgress:
        _require_sha256(self.protocol_sha256, "protocol_sha256")
        if tuple(item.result.fold for item in self.folds) != tuple(range(1, len(self.folds) + 1)):
            raise ValueError("progress folds must form an ordered prefix")
        if len(self.folds) > DEVELOPMENT_FITS:
            raise ValueError("progress cannot exceed the three finalist folds")
        return self


__all__ = [
    "ALL_MODELS",
    "ARCHIVE_REQUEST_END",
    "ARCHIVE_REQUEST_START",
    "ASSET_ID",
    "AVAILABILITY_BASIS",
    "AVAILABILITY_POLICY",
    "BOOTSTRAP_BLOCK_LENGTH",
    "BOOTSTRAP_REPLICATES",
    "BOOTSTRAP_SEED",
    "DECIMAL34",
    "DECIMAL_PRECISION",
    "DECISION_COUNT",
    "DECISION_END",
    "DECISION_HOUR_UTC",
    "DECISION_START",
    "DEVELOPMENT_FITS",
    "EPSILON",
    "FEATURE_WINDOWS",
    "HYPOTHESIS",
    "MANIFEST_FILE_NAME",
    "MAX_CANDLES",
    "MAX_INPUT_FILE_BYTES",
    "MAX_PREDICTIONS_BYTES",
    "MAX_RETAINED_RUNS",
    "MAX_ROOT_BYTES",
    "MAX_RUN_BYTES",
    "MIN_RELATIVE_IMPROVEMENT",
    "MIN_SEGMENT_ROWS",
    "MIN_TRAIN_ROWS",
    "MIN_VALIDATION_ROWS",
    "ONE_SIDED_QUANTILE",
    "PREDICTIONS_FILE_NAME",
    "PREREGISTERED_FOLDS",
    "PROGRESS_FILE_NAME",
    "PROGRESS_SCHEMA_VERSION",
    "PROTOCOL_FILE_NAME",
    "PROTOCOL_ID",
    "PROTOCOL_SCHEMA_VERSION",
    "REFERENCE_MODELS",
    "REFERENCE_WINDOW",
    "REPORT_FILE_NAME",
    "REPORT_SCHEMA_VERSION",
    "RESERVED_START_DATE",
    "RESERVED_START_INDEX",
    "SOURCE_ID",
    "STUDY_LIMITATIONS",
    "TARGET_HORIZON",
    "VALIDATION_EMBARGO_DAYS",
    "VOLATILITY_NAMESPACE",
    "VolatilityArtifactFile",
    "VolatilityArtifactStatus",
    "VolatilityComparison",
    "VolatilityCriteria",
    "VolatilityDecimal",
    "VolatilityDecision",
    "VolatilityFoldBoundary",
    "VolatilityFoldProgress",
    "VolatilityFoldResult",
    "VolatilityHoldoutPolicy",
    "VolatilityManifest",
    "VolatilityMetric",
    "VolatilityModel",
    "VolatilityModelParameters",
    "VolatilityPredictionRecord",
    "VolatilityProgress",
    "VolatilityProtocol",
    "VolatilityReceipt",
    "VolatilityReport",
    "VolatilityReportCounts",
    "VolatilityResampling",
    "VolatilityRunStatus",
    "assumed_available_instant",
    "bar_start_instant",
    "decision_dates",
    "decision_instant",
    "feature_available_instant",
    "fold_label_cut",
    "label_maturity_instant",
    "reserved_label_cut",
    "volatility_row_id",
    "volatility_run_id",
    "volatility_sample_key",
]
