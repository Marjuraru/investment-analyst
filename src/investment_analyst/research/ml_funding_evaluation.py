"""Preregistered temporal evaluation of the incremental-funding hypothesis.

Exactly six development fits run before the pooled decision. The holdout is
never materialized: the offline view only exposes TRAIN/VALIDATION rows.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from datetime import time as day_time
from decimal import Decimal

import numpy

from investment_analyst.research.funding_export import FundingTrainingView
from investment_analyst.research.historical_models import (
    DECISION_HOUR_UTC,
    EMBARGO_DAYS,
    label_exit_bar_end,
)
from investment_analyst.research.ml_adapter import fit_and_predict
from investment_analyst.research.ml_funding_models import (
    ALL_SCORERS,
    BLOCK_LENGTH,
    CALIBRATION_BIN_COUNT,
    CONTROL_SCORER,
    LOG_LOSS_CLIP,
    MAX_DEVELOPMENT_FITS,
    MIN_RELATIVE_BRIER_IMPROVEMENT,
    MODEL_SCORERS,
    SIMPLE_REFERENCES,
    STUDY_LIMITATIONS,
    STUDY_WIDTH,
    TECHNICAL_WIDTH,
    FundingCalibrationBin,
    FundingCalibrationTable,
    FundingFoldBoundary,
    FundingFoldResult,
    FundingIncrementDecision,
    FundingMetricSummary,
    FundingProtocol,
    FundingReferenceComparison,
    FundingScorer,
    metric_decimal,
)
from investment_analyst.research.ml_models import (
    MlHistogramParameters,
    MlModelFamily,
    development_fold_boundaries,
)


class FundingEvaluationError(RuntimeError):
    """The evaluation violated the preregistered funding method."""


class FundingInsufficientSampleError(FundingEvaluationError):
    """A declared sample floor or resampling precondition is not satisfied."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class FundingSample:
    """One reconstructed decision with the technical control and both funding features."""

    sample_key: str
    decision_at: datetime
    features: tuple[float, ...]
    label: int | None
    eligibility_base: bool
    eligibility_joint: bool
    purged: bool
    split: str

    @property
    def exit_bar_end(self) -> datetime:
        return label_exit_bar_end(self.decision_at.date())


@dataclass(frozen=True, slots=True)
class FundingFoldSamples:
    """Planned training and evaluation rows plus their contiguous segments."""

    fold: int
    train: tuple[FundingSample, ...]
    evaluation: tuple[FundingSample, ...]
    segments: tuple[tuple[FundingSample, ...], ...]
    purged_excluded: int
    ineligible_excluded: int
    funding_unavailable_excluded: int
    embargo_excluded: int


@dataclass(frozen=True, slots=True)
class FundingPredictionRow:
    """One out-of-sample development probability for one scorer."""

    sample_key: str
    decision_at: datetime
    fold: int
    scorer: FundingScorer
    label: int
    probability: float


@dataclass(frozen=True, slots=True)
class FundingEvaluationOutcome:
    """Sealed development result with per-row probabilities and observed effort."""

    decision: FundingIncrementDecision
    folds: tuple[FundingFoldResult, ...]
    pooled_scores: tuple[FundingMetricSummary, ...]
    pooled_comparisons: tuple[FundingReferenceComparison, ...]
    calibration: tuple[FundingCalibrationTable, ...]
    predictions: tuple[FundingPredictionRow, ...]
    fits: int
    fit_seconds: float
    evaluation_seconds: float


@dataclass(frozen=True, slots=True)
class _RowMatrix:
    labels: numpy.ndarray
    probabilities: dict[FundingScorer, numpy.ndarray]
    segments: tuple[numpy.ndarray, ...]


def _finite(value: Decimal) -> float:
    converted = float(value)
    if not numpy.isfinite(converted):
        raise FundingEvaluationError("feature values must be finite in float64")
    return converted


def funding_samples_from_view(view: FundingTrainingView) -> tuple[FundingSample, ...]:
    """Join the three offline projections without mixing features, targets or metadata."""
    features = {row.sample_key: row for row in view.features}
    targets = {row.sample_key: row for row in view.targets}
    if len(features) != len(view.features) or len(targets) != len(view.targets):
        raise FundingEvaluationError("offline projections contain duplicate sample keys")
    samples: list[FundingSample] = []
    for metadata in view.metadata:
        feature = features.get(metadata.sample_key)
        target = targets.get(metadata.sample_key)
        if feature is None or target is None:
            raise FundingEvaluationError("offline projections disagree on sample identity")
        if feature.decision_at != metadata.decision_at:
            raise FundingEvaluationError("feature and metadata decision instants disagree")
        technical = (
            feature.close_return_1d,
            feature.rsi_14_fraction,
            feature.macd_12_26_9_histogram_close_ratio,
        )
        funding = (feature.funding_sum_1h_24h, feature.funding_sum_1h_168h)
        values = technical + funding
        features_tuple = tuple(
            float("nan") if value is None else _finite(value) for value in values
        )
        if metadata.eligibility_joint and (
            any(value is None for value in values)
            or not all(numpy.isfinite(value) for value in features_tuple)
            or target.label not in (0, 1)
        ):
            raise FundingEvaluationError(
                "joint eligible samples require a label and all finite features"
            )
        if metadata.eligibility_base and (
            any(value is None for value in technical)
            or not all(numpy.isfinite(value) for value in features_tuple[:TECHNICAL_WIDTH])
            or target.label not in (0, 1)
        ):
            raise FundingEvaluationError("base eligible samples require the technical features")
        if metadata.eligibility_joint and not metadata.eligibility_base:
            raise FundingEvaluationError("joint eligibility requires base eligibility")
        samples.append(
            FundingSample(
                sample_key=metadata.sample_key,
                decision_at=metadata.decision_at,
                features=features_tuple,
                label=target.label,
                eligibility_base=metadata.eligibility_base,
                eligibility_joint=metadata.eligibility_joint,
                purged=metadata.purged,
                split=metadata.split,
            )
        )
    return tuple(samples)


def _validate_grid(
    samples: tuple[FundingSample, ...],
    *,
    decision_rows: int,
    decision_start: date,
    decision_hour_utc: int,
) -> None:
    if not samples:
        raise FundingEvaluationError("the offline view is empty")
    if decision_rows - len(samples) <= 0:
        raise FundingEvaluationError("the offline view must exclude at least the holdout rows")
    first = samples[0].decision_at
    if first.date() != decision_start or first != datetime.combine(
        decision_start, day_time(hour=decision_hour_utc), tzinfo=UTC
    ):
        raise FundingEvaluationError("offline view does not start on the sealed grid")
    for index, sample in enumerate(samples):
        expected_day = decision_start + timedelta(days=index)
        if sample.decision_at.date() != expected_day:
            raise FundingEvaluationError("offline view must follow a contiguous daily grid")
        if sample.decision_at != datetime.combine(
            expected_day, day_time(hour=decision_hour_utc), tzinfo=UTC
        ):
            raise FundingEvaluationError("offline decisions must use the 12:00 UTC instant")


def _validate_fold_windows(protocol: FundingProtocol) -> None:
    expected = development_fold_boundaries(protocol.decision_rows)
    declared = tuple(
        (item.validation_start_index, item.validation_end_index) for item in protocol.folds
    )
    if declared != expected:
        raise FundingEvaluationError("declared folds disagree with the sealed grid")


def _boundary_instant(samples: tuple[FundingSample, ...], index: int) -> datetime:
    return datetime.combine(samples[index].decision_at.date(), day_time(0, 0), tzinfo=UTC)


def _plan_fold(
    samples: tuple[FundingSample, ...],
    *,
    fold: int,
    start: int,
    end: int,
    limit: datetime,
) -> FundingFoldSamples:
    label_cut = limit - timedelta(days=EMBARGO_DAYS)
    train: list[FundingSample] = []
    purged_excluded = 0
    ineligible_excluded = 0
    funding_unavailable_excluded = 0
    embargo_excluded = 0
    for sample in samples[:start]:
        if sample.purged:
            purged_excluded += 1
            continue
        if not sample.eligibility_base:
            ineligible_excluded += 1
            continue
        if not sample.eligibility_joint:
            funding_unavailable_excluded += 1
            continue
        if sample.exit_bar_end < label_cut:
            train.append(sample)
        else:
            embargo_excluded += 1
    evaluation = tuple(sample for sample in samples[start:end] if sample.eligibility_joint)
    return FundingFoldSamples(
        fold=fold,
        train=tuple(train),
        evaluation=evaluation,
        segments=_segments(evaluation),
        purged_excluded=purged_excluded,
        ineligible_excluded=ineligible_excluded,
        funding_unavailable_excluded=funding_unavailable_excluded,
        embargo_excluded=embargo_excluded,
    )


def _segments(
    evaluation: tuple[FundingSample, ...],
) -> tuple[tuple[FundingSample, ...], ...]:
    if not evaluation:
        return ()
    segments: list[list[FundingSample]] = [[evaluation[0]]]
    for previous, current in zip(evaluation, evaluation[1:], strict=False):
        gap = current.decision_at.date() - previous.decision_at.date()
        if gap == timedelta(days=1):
            segments[-1].append(current)
        else:
            segments.append([current])
    return tuple(tuple(segment) for segment in segments)


def _enforce_floors(plan: FundingFoldSamples, protocol: FundingProtocol) -> None:
    caps = protocol.caps
    positives = sum(1 for sample in plan.train if sample.label == 1)
    negatives = len(plan.train) - positives
    if len(plan.train) < caps.min_train_rows:
        raise FundingInsufficientSampleError(
            f"fold {plan.fold} training rows below the declared floor"
        )
    if min(positives, negatives) < caps.min_class_rows:
        raise FundingInsufficientSampleError(
            f"fold {plan.fold} training classes below the declared floor"
        )
    if len(plan.evaluation) < caps.min_evaluation_rows:
        raise FundingInsufficientSampleError(
            f"fold {plan.fold} evaluation rows below the declared floor"
        )
    if not plan.segments or min(len(segment) for segment in plan.segments) < caps.min_segment_rows:
        raise FundingInsufficientSampleError(
            f"fold {plan.fold} has no resampleable contiguous segment"
        )


def plan_funding_folds(
    samples: tuple[FundingSample, ...], protocol: FundingProtocol
) -> tuple[FundingFoldSamples, ...]:
    """Recompute the sealed windows and enforce every sample floor."""
    _validate_grid(
        samples,
        decision_rows=protocol.decision_rows,
        decision_start=protocol.decision_start,
        decision_hour_utc=protocol.decision_hour_utc,
    )
    _validate_fold_windows(protocol)
    if len(samples) != protocol.folds[-1].validation_end_index:
        raise FundingEvaluationError("offline view must end where the last fold ends")
    planned: list[FundingFoldSamples] = []
    for fold in protocol.folds:
        start = fold.validation_start_index
        if samples[start].decision_at.date() != fold.validation_start_date:
            raise FundingEvaluationError("sealed validation start date disagrees with the view")
        boundary = _boundary_instant(samples, start)
        plan = _plan_fold(
            samples,
            fold=fold.fold,
            start=start,
            end=fold.validation_end_index,
            limit=boundary,
        )
        if len(plan.train) != fold.expected_train_rows:
            raise FundingEvaluationError("observed training rows disagree with the protocol")
        if len(plan.evaluation) != fold.expected_evaluation_rows:
            raise FundingEvaluationError("observed evaluation rows disagree with the protocol")
        _enforce_floors(plan, protocol)
        planned.append(plan)
    return tuple(planned)


def build_funding_protocol(view: FundingTrainingView) -> FundingProtocol:
    """Seal the complete preregistration before the first development fit."""
    manifest = view.manifest
    samples = funding_samples_from_view(view)
    holdout_requested = sum(
        item.requested for item in manifest.splits if item.split.value == "holdout"
    )
    if holdout_requested <= 0:
        raise FundingEvaluationError("the funding manifest declares no closed holdout")
    if manifest.base_dataset.row_count != len(samples) + holdout_requested:
        raise FundingEvaluationError("offline view disagrees with the funding row accounting")
    _validate_grid(
        samples,
        decision_rows=manifest.base_dataset.row_count,
        decision_start=manifest.base_dataset.start_date,
        decision_hour_utc=DECISION_HOUR_UTC,
    )
    boundaries = development_fold_boundaries(manifest.base_dataset.row_count)
    if len(samples) != boundaries[-1][1]:
        raise FundingEvaluationError("offline view must end where the last fold ends")
    folds: list[FundingFoldBoundary] = []
    for index, (start, end) in enumerate(boundaries):
        start_date = manifest.base_dataset.start_date + timedelta(days=start)
        end_date = manifest.base_dataset.start_date + timedelta(days=end)
        plan = _plan_fold(
            samples,
            fold=index + 1,
            start=start,
            end=end,
            limit=datetime.combine(start_date, day_time(0, 0), tzinfo=UTC),
        )
        folds.append(
            FundingFoldBoundary(
                fold=index + 1,
                train_end_index=start,
                validation_start_index=start,
                validation_end_index=end,
                validation_start_date=start_date,
                validation_end_date=end_date,
                expected_train_rows=len(plan.train),
                expected_evaluation_rows=len(plan.evaluation),
            )
        )
    return FundingProtocol(
        dataset_id=manifest.base_dataset.dataset_id,
        dataset_content_sha256=manifest.base_dataset.dataset_content_sha256,
        bundle_id=manifest.bundle_id,
        bundle_content_sha256=manifest.bundle_content_sha256,
        decision_rows=manifest.base_dataset.row_count,
        asset_id=manifest.base_dataset.asset_id,
        decision_start=manifest.base_dataset.start_date,
        decision_end=manifest.base_dataset.end_date,
        folds=tuple(folds),
        limitations=STUDY_LIMITATIONS,
    )


def _array(probabilities: tuple[float, ...]) -> numpy.ndarray:
    return numpy.asarray(probabilities, dtype=numpy.float64)


def _brier(labels: numpy.ndarray, probabilities: numpy.ndarray) -> float:
    return float(numpy.mean((probabilities - labels) ** 2))


def _log_loss(labels: numpy.ndarray, probabilities: numpy.ndarray) -> float:
    clip = float(LOG_LOSS_CLIP)
    bounded = numpy.clip(probabilities, clip, 1.0 - clip)
    return float(-numpy.mean(labels * numpy.log(bounded) + (1 - labels) * numpy.log(1 - bounded)))


def _summary(
    scorer: FundingScorer, labels: numpy.ndarray, probabilities: numpy.ndarray
) -> FundingMetricSummary:
    positives = int(labels.sum())
    return FundingMetricSummary(
        scorer=scorer,
        rows=int(labels.size),
        positives=positives,
        negatives=int(labels.size) - positives,
        brier=metric_decimal(_brier(labels, probabilities)),
        log_loss=metric_decimal(_log_loss(labels, probabilities)),
    )


def _draw(segments: tuple[numpy.ndarray, ...], generator: numpy.random.Generator) -> numpy.ndarray:
    parts: list[numpy.ndarray] = []
    for segment in segments:
        length = int(segment.size)
        if length < BLOCK_LENGTH:
            raise FundingEvaluationError("every resampled segment must hold a full block")
        count = -(-length // BLOCK_LENGTH)
        starts = generator.integers(0, length - BLOCK_LENGTH + 1, size=count)
        offsets = starts[:, None] + numpy.arange(BLOCK_LENGTH)[None, :]
        parts.append(segment[offsets.ravel()[:length]])
    return numpy.concatenate(parts)


def _quantile(values: numpy.ndarray, quantile: Decimal) -> float:
    return float(numpy.quantile(values, float(quantile), method="linear"))


def _comparisons(
    matrix: _RowMatrix,
    *,
    quantile: Decimal,
    replicates: int,
    seed: int,
) -> tuple[FundingReferenceComparison, ...]:
    """Return the three paired comparisons over one shared bootstrap draw."""
    labels = matrix.labels
    references = (CONTROL_SCORER,) + SIMPLE_REFERENCES
    funding_loss = (matrix.probabilities[FundingScorer.FUNDING_LOGISTIC] - labels) ** 2
    differences = {
        reference: (matrix.probabilities[reference] - labels) ** 2 - funding_loss
        for reference in references
    }
    samples = {reference: numpy.empty(replicates, dtype=numpy.float64) for reference in references}
    generator = numpy.random.Generator(numpy.random.PCG64(seed))
    for index in range(replicates):
        draw = _draw(matrix.segments, generator)
        for reference, values in differences.items():
            samples[reference][index] = float(values[draw].mean())
    summaries = {
        scorer: _summary(scorer, labels, matrix.probabilities[scorer]) for scorer in ALL_SCORERS
    }
    model_brier = summaries[FundingScorer.FUNDING_LOGISTIC].brier
    comparisons: list[FundingReferenceComparison] = []
    for reference in references:
        reference_brier = summaries[reference].brier
        relative: Decimal | None = None
        if reference_brier != 0:
            relative = metric_decimal(float(reference_brier - model_brier) / float(reference_brier))
        mean_difference = float(differences[reference].mean())
        lower_bound = _quantile(samples[reference], quantile)
        improved = relative is not None and relative >= MIN_RELATIVE_BRIER_IMPROVEMENT
        lower_bound_positive = lower_bound > 0
        absolute = reference_brier - model_brier
        criterion = (
            (absolute > 0 and lower_bound_positive)
            if reference is CONTROL_SCORER
            else (improved and lower_bound_positive)
        )
        comparisons.append(
            FundingReferenceComparison(
                reference=reference,
                model_brier=model_brier,
                reference_brier=reference_brier,
                absolute_improvement=absolute,
                relative_improvement=relative,
                mean_paired_difference=metric_decimal(mean_difference),
                lower_bound=metric_decimal(lower_bound),
                quantile=quantile,
                improved_at_least_five_percent=improved,
                lower_bound_positive=lower_bound_positive,
                criterion_satisfied=criterion,
            )
        )
    return tuple(comparisons)


def _calibration(
    scorer: FundingScorer, labels: numpy.ndarray, probabilities: numpy.ndarray
) -> FundingCalibrationTable:
    bins: list[FundingCalibrationBin] = []
    for index in range(CALIBRATION_BIN_COUNT):
        lower = Decimal(index) / Decimal(CALIBRATION_BIN_COUNT)
        upper = Decimal(index + 1) / Decimal(CALIBRATION_BIN_COUNT)
        if index == CALIBRATION_BIN_COUNT - 1:
            selected = (probabilities >= float(lower)) & (probabilities <= 1.0)
        else:
            selected = (probabilities >= float(lower)) & (probabilities < float(upper))
        count = int(selected.sum())
        bins.append(
            FundingCalibrationBin(
                index=index,
                lower_bound=lower,
                upper_bound=upper,
                includes_upper=index == CALIBRATION_BIN_COUNT - 1,
                count=count,
                mean_probability=(
                    None if count == 0 else metric_decimal(float(probabilities[selected].mean()))
                ),
                observed_frequency=(
                    None if count == 0 else metric_decimal(float(labels[selected].mean()))
                ),
            )
        )
    return FundingCalibrationTable(scorer=scorer, bins=tuple(bins))


def _fold_matrix(
    fold: FundingFoldSamples, probabilities: dict[FundingScorer, numpy.ndarray]
) -> _RowMatrix:
    labels = numpy.asarray([sample.label for sample in fold.evaluation], dtype=numpy.int64)
    positions: list[numpy.ndarray] = []
    offset = 0
    for segment in fold.segments:
        positions.append(numpy.arange(offset, offset + len(segment), dtype=numpy.int64))
        offset += len(segment)
    return _RowMatrix(labels=labels, probabilities=probabilities, segments=tuple(positions))


def _fit_fold(
    fold: FundingFoldSamples, protocol: FundingProtocol
) -> tuple[dict[FundingScorer, numpy.ndarray], tuple[str, ...]]:
    labels = [sample.label for sample in fold.train]
    if any(label is None for label in labels):
        raise FundingEvaluationError("planned training rows must carry a label")
    integers = [int(label) for label in labels if label is not None]
    prior = float(sum(integers)) / float(len(integers))
    size = len(fold.evaluation)
    probabilities = {
        FundingScorer.HALF_PROBABILITY: numpy.full(size, 0.5, dtype=numpy.float64),
        FundingScorer.TRAINING_CLASS_PRIOR: numpy.full(size, prior, dtype=numpy.float64),
    }
    diagnostics: list[str] = []
    arms = (
        (FundingScorer.TECHNICAL_LOGISTIC, TECHNICAL_WIDTH),
        (FundingScorer.FUNDING_LOGISTIC, STUDY_WIDTH),
    )
    for scorer, width in arms:
        outcome = fit_and_predict(
            model=MlModelFamily.LOGISTIC_REGRESSION,
            train_features=[sample.features[:width] for sample in fold.train],
            train_labels=integers,
            evaluation_features=[sample.features[:width] for sample in fold.evaluation],
            logistic=protocol.logistic,
            histogram=MlHistogramParameters(),
            feature_width=width,
        )
        probabilities[scorer] = _array(outcome.probabilities)
        diagnostics.extend(
            f"fold={fold.fold} arm={scorer.value} {line}" for line in outcome.diagnostics
        )
    return probabilities, tuple(diagnostics)


def _decide(
    pooled_comparisons: tuple[FundingReferenceComparison, ...],
) -> FundingIncrementDecision:
    if len(pooled_comparisons) != 3:
        raise FundingEvaluationError("the decision requires exactly three comparisons")
    if all(comparison.criterion_satisfied for comparison in pooled_comparisons):
        return FundingIncrementDecision.INCREMENTAL_EVIDENCE_OBSERVED
    return FundingIncrementDecision.NO_DEMONSTRATED_INCREMENT


def evaluate_funding_development(
    samples: tuple[FundingSample, ...], protocol: FundingProtocol
) -> FundingEvaluationOutcome:
    """Run exactly six development fits and never materialize holdout rows."""
    planned = plan_funding_folds(samples, protocol)
    fit_seconds = 0.0
    evaluation_seconds = 0.0
    folds: list[FundingFoldResult] = []
    predictions: list[FundingPredictionRow] = []
    pooled_labels: list[numpy.ndarray] = []
    pooled_probabilities: dict[FundingScorer, list[numpy.ndarray]] = {
        scorer: [] for scorer in ALL_SCORERS
    }
    pooled_segments: list[numpy.ndarray] = []
    offset = 0
    for fold in planned:
        started = time.perf_counter()
        probabilities, diagnostics = _fit_fold(fold, protocol)
        fit_seconds += time.perf_counter() - started
        started = time.perf_counter()
        matrix = _fold_matrix(fold, probabilities)
        summaries = {
            scorer: _summary(scorer, matrix.labels, matrix.probabilities[scorer])
            for scorer in ALL_SCORERS
        }
        comparisons = _comparisons(
            matrix,
            quantile=protocol.criteria.lower_quantile,
            replicates=protocol.resampling.replicates,
            seed=protocol.resampling.seed,
        )
        positions: list[numpy.ndarray] = []
        for segment in matrix.segments:
            positions.append(segment + offset)
        pooled_segments.extend(positions)
        offset += int(matrix.labels.size)
        pooled_labels.append(matrix.labels)
        for scorer in ALL_SCORERS:
            pooled_probabilities[scorer].append(matrix.probabilities[scorer])
        for row, sample in enumerate(fold.evaluation):
            if sample.label is None:
                raise FundingEvaluationError("evaluated rows must carry a label")
            for scorer in ALL_SCORERS:
                predictions.append(
                    FundingPredictionRow(
                        sample_key=sample.sample_key,
                        decision_at=sample.decision_at,
                        fold=fold.fold,
                        scorer=scorer,
                        label=sample.label,
                        probability=float(matrix.probabilities[scorer][row]),
                    )
                )
        folds.append(
            FundingFoldResult(
                fold=fold.fold,
                train_rows=len(fold.train),
                train_positives=sum(1 for sample in fold.train if sample.label == 1),
                train_negatives=sum(1 for sample in fold.train if sample.label == 0),
                purged_excluded=fold.purged_excluded,
                ineligible_excluded=fold.ineligible_excluded,
                funding_unavailable_excluded=fold.funding_unavailable_excluded,
                embargo_excluded=fold.embargo_excluded,
                evaluation_rows=int(matrix.labels.size),
                evaluation_positives=int(matrix.labels.sum()),
                evaluation_negatives=int(matrix.labels.size - matrix.labels.sum()),
                segments=len(fold.segments),
                scores=tuple(summaries[scorer] for scorer in ALL_SCORERS),
                comparisons=comparisons,
                calibration=tuple(
                    _calibration(scorer, matrix.labels, matrix.probabilities[scorer])
                    for scorer in MODEL_SCORERS
                ),
                diagnostics=diagnostics,
            )
        )
        evaluation_seconds += time.perf_counter() - started
    labels = numpy.concatenate(pooled_labels)
    combined = {scorer: numpy.concatenate(pooled_probabilities[scorer]) for scorer in ALL_SCORERS}
    pooled_matrix = _RowMatrix(
        labels=labels, probabilities=combined, segments=tuple(pooled_segments)
    )
    started = time.perf_counter()
    pooled_scores = tuple(_summary(scorer, labels, combined[scorer]) for scorer in ALL_SCORERS)
    pooled_comparisons = _comparisons(
        pooled_matrix,
        quantile=protocol.criteria.lower_quantile,
        replicates=protocol.resampling.replicates,
        seed=protocol.resampling.seed,
    )
    calibration = tuple(_calibration(scorer, labels, combined[scorer]) for scorer in MODEL_SCORERS)
    evaluation_seconds += time.perf_counter() - started
    return FundingEvaluationOutcome(
        decision=_decide(pooled_comparisons),
        folds=tuple(folds),
        pooled_scores=pooled_scores,
        pooled_comparisons=pooled_comparisons,
        calibration=calibration,
        predictions=tuple(predictions),
        fits=MAX_DEVELOPMENT_FITS,
        fit_seconds=fit_seconds,
        evaluation_seconds=evaluation_seconds,
    )


__all__ = [
    "FundingEvaluationError",
    "FundingEvaluationOutcome",
    "FundingFoldSamples",
    "FundingInsufficientSampleError",
    "FundingPredictionRow",
    "FundingSample",
    "build_funding_protocol",
    "evaluate_funding_development",
    "funding_samples_from_view",
    "plan_funding_folds",
]
