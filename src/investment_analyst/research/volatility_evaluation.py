"""Preregistered development evaluation of the volatility study.

Exactly one Ridge fit is executed per fold.  The three persistence/training-mean
references are never fitted.  Uncertainty is a paired moving-block bootstrap
that resamples contiguous validation segments inside each fold and reuses the
same draws for the candidate and every reference.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, localcontext
from uuid import UUID

from investment_analyst.research.ml_models import metric_decimal
from investment_analyst.research.volatility_dataset import VolatilityFoldPlan
from investment_analyst.research.volatility_models import (
    ALL_MODELS,
    DECIMAL34,
    MIN_RELATIVE_IMPROVEMENT,
    REFERENCE_MODELS,
    VolatilityComparison,
    VolatilityDecision,
    VolatilityFoldProgress,
    VolatilityFoldResult,
    VolatilityMetric,
    VolatilityModel,
    VolatilityPredictionRecord,
    VolatilityProtocol,
)

_MODEL_ORDER = {model: index for index, model in enumerate(ALL_MODELS)}


class VolatilityEvaluationError(RuntimeError):
    """The evaluation violated the sealed method or its progress."""


@dataclass(frozen=True, slots=True)
class VolatilityPrediction:
    """One out-of-sample prediction with its exact Decimal losses."""

    sample_key: UUID
    row_id: UUID
    decision_at: datetime
    fold: int
    model: VolatilityModel
    target: Decimal
    estimate: Decimal
    squared_error: Decimal
    absolute_error: Decimal


@dataclass(frozen=True, slots=True)
class VolatilityFoldOutcome:
    """One completed fold, reusable through a sealed progress checkpoint."""

    result: VolatilityFoldResult
    validation_sample_keys: tuple[UUID, ...]
    segments: tuple[tuple[UUID, ...], ...]
    predictions: tuple[VolatilityPrediction, ...]
    fit_seconds: float
    evaluation_seconds: float


@dataclass(frozen=True, slots=True)
class VolatilityEvaluationOutcome:
    """Pooled development result and the preregistered decision."""

    folds: tuple[VolatilityFoldOutcome, ...]
    pooled_scores: tuple[VolatilityMetric, ...]
    pooled_comparisons: tuple[VolatilityComparison, ...]
    decision: VolatilityDecision
    decision_reason: str
    fit_seconds: float
    evaluation_seconds: float


def _mean(values: list[float]) -> float:
    if not values:
        raise VolatilityEvaluationError("a metric cannot be computed over zero rows")
    return math.fsum(values) / len(values)


def _prediction_records(
    fold: VolatilityFoldOutcome,
) -> tuple[VolatilityPredictionRecord, ...]:
    return tuple(
        VolatilityPredictionRecord(
            sample_key=prediction.sample_key,
            row_id=prediction.row_id,
            decision_at=prediction.decision_at,
            fold=prediction.fold,
            model=prediction.model,
            target=prediction.target,
            estimate=prediction.estimate,
            squared_error=prediction.squared_error,
            absolute_error=prediction.absolute_error,
        )
        for prediction in fold.predictions
    )


def _metric(
    model: VolatilityModel,
    predictions: tuple[VolatilityPrediction, ...],
) -> VolatilityMetric:
    selected = [prediction for prediction in predictions if prediction.model is model]
    if not selected:
        raise VolatilityEvaluationError("a metric requires at least one prediction")
    return VolatilityMetric(
        model=model,
        rows=len(selected),
        mse=metric_decimal(_mean([float(item.squared_error) for item in selected])),
        mae=metric_decimal(_mean([float(item.absolute_error) for item in selected])),
    )


def _fold_comparisons(
    scores: tuple[VolatilityMetric, ...],
    *,
    fold: int,
) -> tuple[VolatilityComparison, ...]:
    by_model = {metric.model: metric for metric in scores}
    candidate = by_model[VolatilityModel.RIDGE]
    comparisons: list[VolatilityComparison] = []
    for reference in REFERENCE_MODELS:
        other = by_model[reference]
        absolute = other.mse - candidate.mse
        with localcontext(DECIMAL34):
            relative = None if other.mse == 0 else absolute / other.mse
        comparisons.append(
            VolatilityComparison(
                reference=reference,
                fold=fold,
                model_mse=candidate.mse,
                reference_mse=other.mse,
                absolute_improvement=absolute,
                relative_improvement=relative,
                admissible=relative is not None,
                improved_at_least_five_percent=(
                    relative is not None and relative >= MIN_RELATIVE_IMPROVEMENT
                ),
            )
        )
    return tuple(comparisons)


def _estimate_decimal(value: float) -> Decimal:
    return metric_decimal(value)


def evaluate_fold(
    plan: VolatilityFoldPlan,
    protocol: VolatilityProtocol,
) -> VolatilityFoldOutcome:
    """Execute the single preregistered fit of one fold and score it."""
    from investment_analyst.research.volatility_adapter import fit_ridge

    if not plan.train or not plan.validation:
        raise VolatilityEvaluationError("a fold requires training and validation rows")
    if any(row.features is None or row.target is None for row in plan.train):
        raise VolatilityEvaluationError("training rows must carry features and targets")
    if any(row.features is None or row.target is None for row in plan.validation):
        raise VolatilityEvaluationError("validation rows must carry features and targets")
    started = time.perf_counter()
    fit_started = time.perf_counter()
    estimates = fit_ridge(
        train_features=[row.feature_values for row in plan.train],
        train_targets=[row.target for row in plan.train if row.target is not None],
        evaluation_features=[row.feature_values for row in plan.validation],
    )
    fit_seconds = time.perf_counter() - fit_started
    if len(estimates) != len(plan.validation):
        raise VolatilityEvaluationError("the adapter returned an unexpected row count")
    with localcontext(DECIMAL34):
        training_mean = sum(
            (row.target for row in plan.train if row.target is not None),
            Decimal(0),
        ) / Decimal(len(plan.train))
    mean_estimate = _estimate_decimal(float(training_mean))
    predictions: list[VolatilityPrediction] = []
    for row, ridge_estimate in zip(plan.validation, estimates, strict=True):
        if row.target is None or row.reference_5d is None or row.features is None:
            raise VolatilityEvaluationError("validation row evidence is incomplete")
        reference_30d = next(feature for feature in row.features if feature.window == 30)
        values = {
            VolatilityModel.RIDGE: _estimate_decimal(ridge_estimate),
            VolatilityModel.PERSISTENCE_5D: row.reference_5d.value,
            VolatilityModel.PERSISTENCE_30D: reference_30d.value,
            VolatilityModel.TRAINING_MEAN: mean_estimate,
        }
        for model in ALL_MODELS:
            estimate = values[model]
            if not estimate.is_finite():
                raise VolatilityEvaluationError("predictions must be finite Decimals")
            with localcontext(DECIMAL34):
                squared = (estimate - row.target) ** 2
                absolute = abs(estimate - row.target)
            if row.row_id is None:
                raise VolatilityEvaluationError("an evaluated row requires a deterministic row id")
            predictions.append(
                VolatilityPrediction(
                    sample_key=row.sample_key,
                    row_id=row.row_id,
                    decision_at=row.decision_at,
                    fold=plan.fold,
                    model=model,
                    target=row.target,
                    estimate=estimate,
                    squared_error=squared,
                    absolute_error=absolute,
                )
            )
    ordered = tuple(
        sorted(
            predictions,
            key=lambda item: (
                item.fold,
                item.decision_at,
                str(item.sample_key),
                _MODEL_ORDER[item.model],
            ),
        )
    )
    scores = tuple(_metric(model, ordered) for model in ALL_MODELS)
    comparisons = _fold_comparisons(scores, fold=plan.fold)
    evaluation_seconds = time.perf_counter() - started - fit_seconds
    result = VolatilityFoldResult(
        fold=plan.fold,
        train_rows=len(plan.train),
        validation_rows=len(plan.validation),
        train_embargo_excluded=plan.train_embargo_excluded,
        train_ineligible_excluded=plan.train_ineligible_excluded,
        validation_embargo_excluded=plan.validation_embargo_excluded,
        validation_ineligible_excluded=plan.validation_ineligible_excluded,
        segments=len(plan.segments),
        segment_lengths=tuple(len(segment) for segment in plan.segments),
        scores=scores,
        comparisons=comparisons,
        diagnostics=(
            "ridge=StandardScaler(train)+Ridge(alpha=1.0,fit_intercept=True,solver=svd)",
            "references=persistence_5d,persistence_30d,training_mean",
            f"training_mean={mean_estimate}",
        ),
    )
    return VolatilityFoldOutcome(
        result=result,
        validation_sample_keys=tuple(row.sample_key for row in plan.validation),
        segments=tuple(tuple(row.sample_key for row in segment) for segment in plan.segments),
        predictions=ordered,
        fit_seconds=fit_seconds,
        evaluation_seconds=evaluation_seconds,
    )


def _differences(
    fold: VolatilityFoldOutcome,
) -> dict[VolatilityModel, list[float]]:
    by_key = {(item.sample_key, item.model): item for item in fold.predictions}
    differences: dict[VolatilityModel, list[float]] = {
        reference: [] for reference in REFERENCE_MODELS
    }
    for sample_key in fold.validation_sample_keys:
        candidate = by_key.get((sample_key, VolatilityModel.RIDGE))
        if candidate is None:
            raise VolatilityEvaluationError("a validation row lacks a Ridge prediction")
        for reference in REFERENCE_MODELS:
            other = by_key.get((sample_key, reference))
            if other is None:
                raise VolatilityEvaluationError("a validation row lacks a reference prediction")
            with localcontext(DECIMAL34):
                difference = other.squared_error - candidate.squared_error
            differences[reference].append(float(difference))
    return differences


def _segment_positions(fold: VolatilityFoldOutcome) -> tuple[tuple[int, ...], ...]:
    positions = {key: index for index, key in enumerate(fold.validation_sample_keys)}
    if len(positions) != len(fold.validation_sample_keys):
        raise VolatilityEvaluationError("validation sample keys are not unique")
    segments: list[tuple[int, ...]] = []
    for segment in fold.segments:
        if not segment:
            raise VolatilityEvaluationError("bootstrap segments must not be empty")
        try:
            segments.append(tuple(positions[key] for key in segment))
        except KeyError as error:
            raise VolatilityEvaluationError(
                "a bootstrap segment references a foreign row"
            ) from error
    return tuple(segments)


def pooled_scores(folds: tuple[VolatilityFoldOutcome, ...]) -> tuple[VolatilityMetric, ...]:
    """Score every model over the concatenated development out-of-sample rows."""
    if not folds:
        raise VolatilityEvaluationError("pooled scores require completed folds")
    predictions = tuple(prediction for fold in folds for prediction in fold.predictions)
    return tuple(_metric(model, predictions) for model in ALL_MODELS)


def _bootstrap_lower_bounds(
    folds: tuple[VolatilityFoldOutcome, ...],
    protocol: VolatilityProtocol,
) -> dict[VolatilityModel, float]:
    import numpy

    if not folds:
        raise VolatilityEvaluationError("bootstrap requires completed folds")
    differences = [_differences(fold) for fold in folds]
    segments = [_segment_positions(fold) for fold in folds]
    block = protocol.resampling.block_length
    replicates = protocol.resampling.replicates
    generator = numpy.random.Generator(numpy.random.PCG64(protocol.resampling.seed))
    samples = {
        reference: numpy.empty(replicates, dtype=numpy.float64) for reference in REFERENCE_MODELS
    }
    for index in range(replicates):
        totals = {reference: 0.0 for reference in REFERENCE_MODELS}
        count = 0
        for fold_differences, fold_segments in zip(differences, segments, strict=True):
            for segment in fold_segments:
                length = len(segment)
                if length < block:
                    raise VolatilityEvaluationError(
                        "every resampled segment must hold a full block"
                    )
                draws = generator.integers(0, length - block + 1, size=-(-length // block))
                offsets = draws[:, None] + numpy.arange(block)[None, :]
                selected = offsets.ravel()[:length]
                for reference in REFERENCE_MODELS:
                    values = fold_differences[reference]
                    totals[reference] += math.fsum(values[position] for position in selected)
                count += length
        if count == 0:
            raise VolatilityEvaluationError("bootstrap resampled zero rows")
        for reference in REFERENCE_MODELS:
            samples[reference][index] = totals[reference] / count
    quantile = float(protocol.criteria.quantile)
    return {
        reference: float(numpy.quantile(samples[reference], quantile, method="linear"))
        for reference in REFERENCE_MODELS
    }


def _pooled_comparisons(
    scores: tuple[VolatilityMetric, ...],
    folds: tuple[VolatilityFoldOutcome, ...],
    protocol: VolatilityProtocol,
) -> tuple[VolatilityComparison, ...]:
    by_model = {metric.model: metric for metric in scores}
    candidate = by_model[VolatilityModel.RIDGE]
    lower_bounds = _bootstrap_lower_bounds(folds, protocol)
    differences = [_differences(fold) for fold in folds]
    comparisons: list[VolatilityComparison] = []
    for reference in REFERENCE_MODELS:
        other = by_model[reference]
        absolute = other.mse - candidate.mse
        with localcontext(DECIMAL34):
            relative = None if other.mse == 0 else absolute / other.mse
        paired = _mean(
            [value for fold_differences in differences for value in fold_differences[reference]]
        )
        lower = lower_bounds[reference]
        comparisons.append(
            VolatilityComparison(
                reference=reference,
                fold=None,
                model_mse=candidate.mse,
                reference_mse=other.mse,
                absolute_improvement=absolute,
                relative_improvement=relative,
                mean_paired_difference=metric_decimal(paired),
                lower_bound=metric_decimal(lower),
                lower_bound_positive=lower > 0,
                admissible=relative is not None,
                improved_at_least_five_percent=(
                    relative is not None and relative >= MIN_RELATIVE_IMPROVEMENT
                ),
            )
        )
    return tuple(comparisons)


def decide(
    folds: tuple[VolatilityFoldResult, ...],
    comparisons: tuple[VolatilityComparison, ...],
) -> tuple[VolatilityDecision, str]:
    """Apply the preregistered conservative development criterion literally."""
    if len(folds) != 3:
        raise VolatilityEvaluationError("the decision requires the three completed folds")
    failures: list[str] = []
    for comparison in comparisons:
        if not comparison.admissible:
            failures.append(f"{comparison.reference.value}: zero reference MSE")
        elif not comparison.improved_at_least_five_percent:
            failures.append(f"{comparison.reference.value}: pooled improvement below five percent")
        if comparison.lower_bound_positive is not True:
            failures.append(f"{comparison.reference.value}: lower bound not positive")
    by_fold = {fold.fold: {metric.model: metric for metric in fold.scores} for fold in folds}
    for fold in folds:
        models = by_fold[fold.fold]
        candidate = models[VolatilityModel.RIDGE]
        for reference in REFERENCE_MODELS:
            if not candidate.mse < models[reference].mse:
                failures.append(
                    f"fold {fold.fold}: no positive improvement against {reference.value}"
                )
    if failures:
        return VolatilityDecision.NO_DEMONSTRATED_INCREMENT, "; ".join(failures)
    return (
        VolatilityDecision.DEVELOPMENT_EVIDENCE,
        "pooled MSE improved at least five percent against the three references,"
        " every one-sided lower bound is positive and every fold improves against"
        " each reference",
    )


def combine_folds(
    folds: tuple[VolatilityFoldOutcome, ...],
    protocol: VolatilityProtocol,
) -> VolatilityEvaluationOutcome:
    """Pool the completed folds and produce the sealed development decision."""
    if tuple(fold.result.fold for fold in folds) != (1, 2, 3):
        raise VolatilityEvaluationError("folds must be the ordered three development folds")
    started = time.perf_counter()
    scores = pooled_scores(folds)
    comparisons = _pooled_comparisons(scores, folds, protocol)
    decision, reason = decide(tuple(fold.result for fold in folds), comparisons)
    evaluation_seconds = time.perf_counter() - started
    return VolatilityEvaluationOutcome(
        folds=folds,
        pooled_scores=scores,
        pooled_comparisons=comparisons,
        decision=decision,
        decision_reason=reason,
        fit_seconds=sum(fold.fit_seconds for fold in folds),
        evaluation_seconds=sum(fold.evaluation_seconds for fold in folds) + evaluation_seconds,
    )


def progress_from_fold(fold: VolatilityFoldOutcome) -> VolatilityFoldProgress:
    """Seal one completed fold as a validated progress checkpoint."""
    return VolatilityFoldProgress(
        result=fold.result,
        validation_sample_keys=fold.validation_sample_keys,
        segments=fold.segments,
        predictions=_prediction_records(fold),
    )


def fold_from_progress(
    progress: VolatilityFoldProgress,
    plan: VolatilityFoldPlan,
) -> VolatilityFoldOutcome:
    """Verify a sealed fold checkpoint against the current planned fold."""
    if progress.result.fold != plan.fold:
        raise VolatilityEvaluationError("progress fold differs from the planned fold")
    if progress.result.train_rows != len(plan.train):
        raise VolatilityEvaluationError("progress training rows differ from the plan")
    if progress.result.validation_rows != len(plan.validation):
        raise VolatilityEvaluationError("progress validation rows differ from the plan")
    if (
        progress.result.train_embargo_excluded != plan.train_embargo_excluded
        or progress.result.train_ineligible_excluded != plan.train_ineligible_excluded
        or progress.result.validation_embargo_excluded != plan.validation_embargo_excluded
        or progress.result.validation_ineligible_excluded != plan.validation_ineligible_excluded
    ):
        raise VolatilityEvaluationError("progress exclusion counts differ from the plan")
    expected_keys = tuple(row.sample_key for row in plan.validation)
    if progress.validation_sample_keys != expected_keys:
        raise VolatilityEvaluationError("progress validation keys differ from the plan")
    expected_segments = tuple(tuple(row.sample_key for row in segment) for segment in plan.segments)
    if progress.segments != expected_segments:
        raise VolatilityEvaluationError("progress segments differ from the plan")
    if progress.result.segment_lengths != tuple(len(segment) for segment in plan.segments):
        raise VolatilityEvaluationError("progress segment lengths differ from the plan")
    predictions = tuple(
        VolatilityPrediction(
            sample_key=item.sample_key,
            row_id=item.row_id,
            decision_at=item.decision_at,
            fold=item.fold,
            model=item.model,
            target=item.target,
            estimate=item.estimate,
            squared_error=item.squared_error,
            absolute_error=item.absolute_error,
        )
        for item in progress.predictions
    )
    return VolatilityFoldOutcome(
        result=progress.result,
        validation_sample_keys=progress.validation_sample_keys,
        segments=progress.segments,
        predictions=predictions,
        fit_seconds=0.0,
        evaluation_seconds=0.0,
    )


__all__ = [
    "VolatilityEvaluationError",
    "VolatilityEvaluationOutcome",
    "VolatilityFoldOutcome",
    "VolatilityPrediction",
    "combine_folds",
    "decide",
    "evaluate_fold",
    "fold_from_progress",
    "pooled_scores",
    "progress_from_fold",
]
