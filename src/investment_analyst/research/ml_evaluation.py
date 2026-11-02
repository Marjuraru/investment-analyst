"""Preregistered temporal evaluation of the study's small ML candidates."""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from datetime import time as day_time
from decimal import Decimal

import numpy

from investment_analyst.research.historical_export import HistoricalOfflineView
from investment_analyst.research.historical_models import EMBARGO_DAYS, label_exit_bar_end
from investment_analyst.research.ml_adapter import fit_and_predict
from investment_analyst.research.ml_models import (
    BLOCK_LENGTH,
    CALIBRATION_BIN_COUNT,
    DEVELOPMENT_QUANTILE,
    HOLDOUT_QUANTILE,
    LOG_LOSS_CLIP,
    MAX_DEVELOPMENT_FITS,
    MAX_HOLDOUT_REFITS,
    MIN_CLASS_ROWS,
    MIN_EVALUATION_ROWS,
    MIN_HOLDOUT_ROWS,
    MIN_RELATIVE_BRIER_IMPROVEMENT,
    MIN_SEGMENT_ROWS,
    MIN_TRAIN_ROWS,
    TIE_TOLERANCE,
    MlBaselineProtocol,
    MlBaselineSelection,
    MlCalibrationBin,
    MlCalibrationTable,
    MlDevelopmentResult,
    MlFoldResult,
    MlHoldoutResult,
    MlHoldoutStatus,
    MlMetricSummary,
    MlModelFamily,
    MlModelState,
    MlReferenceComparison,
    MlScorer,
    development_fold_boundaries,
    metric_decimal,
)

MODEL_SCORERS: tuple[MlScorer, ...] = (
    MlScorer.LOGISTIC_REGRESSION,
    MlScorer.HIST_GRADIENT_BOOSTING,
)
REFERENCE_SCORERS: tuple[MlScorer, ...] = (
    MlScorer.HALF_PROBABILITY,
    MlScorer.TRAINING_CLASS_PRIOR,
)
ALL_SCORERS: tuple[MlScorer, ...] = MODEL_SCORERS + REFERENCE_SCORERS


class MlEvaluationError(RuntimeError):
    """The evaluation violated the preregistered method."""


class MlInsufficientSampleError(MlEvaluationError):
    """A declared sample floor or resampling precondition is not satisfied."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class MlHoldoutAccessError(MlEvaluationError):
    """The protected holdout was requested without a sealed selection."""


@dataclass(frozen=True, slots=True)
class MlSample:
    """One eligible or excluded reconstructed decision with its three features."""

    sample_key: str
    decision_at: datetime
    features: tuple[float, float, float]
    label: int | None
    eligible: bool
    purged: bool
    split: str

    @property
    def exit_bar_end(self) -> datetime:
        return label_exit_bar_end(self.decision_at.date())


@dataclass(frozen=True, slots=True)
class MlFoldSamples:
    """Planned training and evaluation rows plus their contiguous segments."""

    fold: int
    train: tuple[MlSample, ...]
    evaluation: tuple[MlSample, ...]
    segments: tuple[tuple[MlSample, ...], ...]
    purged_excluded: int
    ineligible_excluded: int
    embargo_excluded: int


@dataclass(frozen=True, slots=True)
class MlPredictionRow:
    """One out-of-sample probability for one scorer."""

    sample_key: str
    decision_at: datetime
    stage: str
    fold: int
    scorer: MlScorer
    label: int
    probability: float


@dataclass(frozen=True, slots=True)
class MlEvaluationOutcome:
    """Development result with per-row probabilities and observed effort."""

    development: MlDevelopmentResult
    predictions: tuple[MlPredictionRow, ...]
    fits: int
    fit_seconds: float
    evaluation_seconds: float


@dataclass(frozen=True, slots=True)
class MlHoldoutOutcome:
    """Single holdout result, its probabilities and observed effort."""

    holdout: MlHoldoutResult
    predictions: tuple[MlPredictionRow, ...]
    fits: int
    fit_seconds: float
    evaluation_seconds: float
    accesses: int


@dataclass(frozen=True, slots=True)
class _RowMatrix:
    labels: numpy.ndarray
    probabilities: dict[MlScorer, numpy.ndarray]
    segments: tuple[numpy.ndarray, ...]


class MlHoldoutGuard:
    """Single-use gate in front of the protected holdout rows."""

    def __init__(self, selection: MlBaselineSelection | None) -> None:
        self._selection = selection
        self.accesses = 0

    def open(self) -> MlBaselineSelection:
        if self._selection is None:
            raise MlHoldoutAccessError("holdout access requires a development selection")
        if self.accesses != 0:
            raise MlHoldoutAccessError("the holdout may be accessed only once per run")
        self.accesses += 1
        return self._selection


def samples_from_view(view: HistoricalOfflineView) -> tuple[MlSample, ...]:
    """Join the three explicit offline projections without mixing their meaning."""
    features = {row.sample_key: row for row in view.features}
    targets = {row.sample_key: row for row in view.targets}
    if len(features) != len(view.features) or len(targets) != len(view.targets):
        raise MlEvaluationError("offline projections contain duplicate sample keys")
    samples: list[MlSample] = []
    for metadata in view.metadata:
        feature = features.get(metadata.sample_key)
        target = targets.get(metadata.sample_key)
        if feature is None or target is None:
            raise MlEvaluationError("offline projections disagree on sample identity")
        if feature.decision_at != metadata.decision_at:
            raise MlEvaluationError("feature and metadata decision instants disagree")
        values = (
            feature.close_return_1d,
            feature.rsi_14_fraction,
            feature.macd_12_26_9_histogram_close_ratio,
        )
        if all(value is not None for value in values):
            features_tuple = (
                float(values[0]),  # type: ignore[arg-type]
                float(values[1]),  # type: ignore[arg-type]
                float(values[2]),  # type: ignore[arg-type]
            )
            if not all(numpy.isfinite(features_tuple)):
                raise MlEvaluationError("available features must be finite")
        else:
            features_tuple = (float("nan"), float("nan"), float("nan"))
        if metadata.eligible and (
            target.label not in (0, 1) or not all(numpy.isfinite(value) for value in features_tuple)
        ):
            raise MlEvaluationError("eligible samples require a label and finite features")
        samples.append(
            MlSample(
                sample_key=metadata.sample_key,
                decision_at=metadata.decision_at,
                features=features_tuple,
                label=target.label,
                eligible=metadata.eligible,
                purged=metadata.purged,
                split=metadata.split,
            )
        )
    return tuple(samples)


def _segments(evaluation: tuple[MlSample, ...]) -> tuple[tuple[MlSample, ...], ...]:
    if not evaluation:
        return ()
    segments: list[list[MlSample]] = [[evaluation[0]]]
    for previous, current in zip(evaluation, evaluation[1:], strict=False):
        gap = current.decision_at.date() - previous.decision_at.date()
        if gap == timedelta(days=1):
            segments[-1].append(current)
        else:
            segments.append([current])
    return tuple(tuple(segment) for segment in segments)


def plan_folds(
    samples: tuple[MlSample, ...], protocol: MlBaselineProtocol
) -> tuple[MlFoldSamples, ...]:
    """Recompute the sealed windows and enforce every sample floor."""
    if len(samples) != protocol.decision_rows:
        raise MlEvaluationError("dataset rows disagree with the sealed protocol")
    expected = development_fold_boundaries(len(samples))
    declared = tuple(
        (item.validation_start_index, item.validation_end_index) for item in protocol.folds
    )
    if declared != expected:
        raise MlEvaluationError("dataset length disagrees with the sealed fold boundaries")
    for fold, _index in zip(protocol.folds, range(3), strict=True):
        start = fold.validation_start_index
        end = fold.validation_end_index
        if samples[start].decision_at.date() != fold.validation_start_date:
            raise MlEvaluationError("sealed validation start date disagrees with the dataset")
        if samples[end].decision_at.date() != fold.validation_end_date:
            raise MlEvaluationError("sealed validation end date disagrees with the dataset")
    planned: list[MlFoldSamples] = []
    for fold in protocol.folds:
        start = fold.validation_start_index
        boundary = datetime.combine(samples[start].decision_at.date(), day_time(0, 0), tzinfo=UTC)
        label_cut = boundary - timedelta(days=EMBARGO_DAYS)
        train: list[MlSample] = []
        embargo_excluded = 0
        purged_excluded = 0
        ineligible_excluded = 0
        for sample in samples[:start]:
            if not sample.eligible:
                if sample.purged:
                    purged_excluded += 1
                else:
                    ineligible_excluded += 1
                continue
            if sample.exit_bar_end < label_cut:
                train.append(sample)
            else:
                embargo_excluded += 1
        evaluation = tuple(
            sample for sample in samples[start : fold.validation_end_index] if sample.eligible
        )
        positives = sum(1 for sample in train if sample.label == 1)
        negatives = len(train) - positives
        if len(train) < MIN_TRAIN_ROWS:
            raise MlInsufficientSampleError(
                f"fold {fold.fold} training rows below the declared floor"
            )
        if min(positives, negatives) < MIN_CLASS_ROWS:
            raise MlInsufficientSampleError(
                f"fold {fold.fold} training classes below the declared floor"
            )
        if len(evaluation) < MIN_EVALUATION_ROWS:
            raise MlInsufficientSampleError(
                f"fold {fold.fold} evaluation rows below the declared floor"
            )
        segments = _segments(evaluation)
        if not segments or min(len(segment) for segment in segments) < MIN_SEGMENT_ROWS:
            raise MlInsufficientSampleError(
                f"fold {fold.fold} has no resampleable contiguous segment"
            )
        planned.append(
            MlFoldSamples(
                fold=fold.fold,
                train=tuple(train),
                evaluation=evaluation,
                segments=segments,
                purged_excluded=purged_excluded,
                ineligible_excluded=ineligible_excluded,
                embargo_excluded=embargo_excluded,
            )
        )
    return tuple(planned)


def _array(probabilities: tuple[float, ...]) -> numpy.ndarray:
    return numpy.asarray(probabilities, dtype=numpy.float64)


def _brier(labels: numpy.ndarray, probabilities: numpy.ndarray) -> float:
    return float(numpy.mean((probabilities - labels) ** 2))


def _log_loss(labels: numpy.ndarray, probabilities: numpy.ndarray) -> float:
    clip = float(LOG_LOSS_CLIP)
    bounded = numpy.clip(probabilities, clip, 1.0 - clip)
    return float(-numpy.mean(labels * numpy.log(bounded) + (1 - labels) * numpy.log(1 - bounded)))


def _summary(
    scorer: MlScorer, labels: numpy.ndarray, probabilities: numpy.ndarray
) -> MlMetricSummary:
    positives = int(labels.sum())
    return MlMetricSummary(
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
            raise MlEvaluationError("every resampled segment must hold a full block")
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
    models: tuple[MlScorer, ...],
    quantile: Decimal,
    replicates: int,
    seed: int,
) -> dict[tuple[MlScorer, MlScorer], tuple[float, float, float]]:
    """Return point, paired mean and lower bound for every (model, reference) pair."""
    labels = matrix.labels
    differences: dict[tuple[MlScorer, MlScorer], numpy.ndarray] = {}
    for model in models:
        model_brier_loss = (matrix.probabilities[model] - labels) ** 2
        for reference in REFERENCE_SCORERS:
            reference_brier_loss = (matrix.probabilities[reference] - labels) ** 2
            differences[(model, reference)] = reference_brier_loss - model_brier_loss
    samples = {key: numpy.empty(replicates, dtype=numpy.float64) for key in differences}
    generator = numpy.random.Generator(numpy.random.PCG64(seed))
    for index in range(replicates):
        draw = _draw(matrix.segments, generator)
        for key, values in differences.items():
            samples[key][index] = float(values[draw].mean())
    return {
        key: (
            float(differences[key].mean()),
            _quantile(samples[key], quantile),
            float(numpy.mean(differences[key])),
        )
        for key in differences
    }


def _comparison_models(
    matrix: _RowMatrix,
    summaries: dict[MlScorer, MlMetricSummary],
    results: dict[tuple[MlScorer, MlScorer], tuple[float, float, float]],
    quantile: Decimal,
    models: tuple[MlScorer, ...],
) -> tuple[MlReferenceComparison, ...]:
    comparisons: list[MlReferenceComparison] = []
    for model in models:
        for reference in REFERENCE_SCORERS:
            mean_difference, lower_bound, _ = results[(model, reference)]
            model_brier = summaries[model].brier
            reference_brier = summaries[reference].brier
            relative = None
            if reference_brier != 0:
                relative = metric_decimal(
                    float(reference_brier - model_brier) / float(reference_brier)
                )
            comparisons.append(
                MlReferenceComparison(
                    model=model,
                    reference=reference,
                    model_brier=model_brier,
                    reference_brier=reference_brier,
                    absolute_improvement=reference_brier - model_brier,
                    relative_improvement=relative,
                    mean_paired_difference=metric_decimal(mean_difference),
                    lower_bound=metric_decimal(lower_bound),
                    quantile=quantile,
                    improved_at_least_five_percent=(
                        relative is not None and relative >= MIN_RELATIVE_BRIER_IMPROVEMENT
                    ),
                    lower_bound_positive=lower_bound > 0,
                )
            )
    return tuple(comparisons)


def _calibration(
    scorer: MlScorer, labels: numpy.ndarray, probabilities: numpy.ndarray
) -> MlCalibrationTable:
    bins: list[MlCalibrationBin] = []
    for index in range(CALIBRATION_BIN_COUNT):
        lower = Decimal(index) / Decimal(CALIBRATION_BIN_COUNT)
        upper = Decimal(index + 1) / Decimal(CALIBRATION_BIN_COUNT)
        if index == CALIBRATION_BIN_COUNT - 1:
            selected = (probabilities >= float(lower)) & (probabilities <= 1.0)
        else:
            selected = (probabilities >= float(lower)) & (probabilities < float(upper))
        count = int(selected.sum())
        bins.append(
            MlCalibrationBin(
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
    return MlCalibrationTable(scorer=scorer, bins=tuple(bins))


def _fold_matrix(fold: MlFoldSamples, probabilities: dict[MlScorer, numpy.ndarray]) -> _RowMatrix:
    labels = numpy.asarray([sample.label for sample in fold.evaluation], dtype=numpy.int64)
    positions: list[numpy.ndarray] = []
    offset = 0
    for segment in fold.segments:
        positions.append(numpy.arange(offset, offset + len(segment), dtype=numpy.int64))
        offset += len(segment)
    return _RowMatrix(labels=labels, probabilities=probabilities, segments=tuple(positions))


def _fit_fold(
    fold: MlFoldSamples, protocol: MlBaselineProtocol
) -> tuple[dict[MlScorer, numpy.ndarray], tuple[str, ...]]:
    train_labels = [sample.label for sample in fold.train]
    if any(label is None for label in train_labels):
        raise MlEvaluationError("planned training rows must carry a label")
    labels = [int(label) for label in train_labels if label is not None]
    prior = float(sum(labels)) / float(len(labels))
    evaluation_size = len(fold.evaluation)
    probabilities = {
        MlScorer.HALF_PROBABILITY: numpy.full(evaluation_size, 0.5, dtype=numpy.float64),
        MlScorer.TRAINING_CLASS_PRIOR: numpy.full(evaluation_size, prior, dtype=numpy.float64),
    }
    diagnostics: list[str] = []
    for model in MlModelFamily:
        outcome = fit_and_predict(
            model=model,
            train_features=[sample.features for sample in fold.train],
            train_labels=labels,
            evaluation_features=[sample.features for sample in fold.evaluation],
            logistic=protocol.logistic,
            histogram=protocol.histogram,
        )
        probabilities[MlScorer(model.value)] = _array(outcome.probabilities)
        diagnostics.extend(f"fold={fold.fold} {line}" for line in outcome.diagnostics)
    return probabilities, tuple(diagnostics)


def _select(
    pooled_scores: tuple[MlMetricSummary, ...],
    pooled_comparisons: tuple[MlReferenceComparison, ...],
) -> tuple[MlModelState, MlModelFamily | None, str]:
    brier = {score.scorer: score.brier for score in pooled_scores}
    admissible: list[MlModelFamily] = []
    for model in MlModelFamily:
        scorer = MlScorer(model.value)
        relevant = [comparison for comparison in pooled_comparisons if comparison.model is scorer]
        if len(relevant) == len(REFERENCE_SCORERS) and all(
            comparison.improved_at_least_five_percent and comparison.lower_bound_positive
            for comparison in relevant
        ):
            admissible.append(model)
    if not admissible:
        return (
            MlModelState.NO_DEMONSTRATED_EDGE,
            None,
            "no candidate reached five percent Brier improvement against both references"
            " with both one-sided lower bounds above zero",
        )
    ordered = sorted(admissible, key=lambda model: brier[MlScorer(model.value)])
    if len(ordered) == 2:
        first, second = ordered
        gap = brier[MlScorer(first.value)] - brier[MlScorer(second.value)]
        if abs(gap) <= TIE_TOLERANCE:
            ordered = [MlModelFamily.LOGISTIC_REGRESSION, MlModelFamily.HIST_GRADIENT_BOOSTING]
    selected = ordered[0]
    return (
        MlModelState.CANDIDATE_SELECTED,
        selected,
        f"{selected.value} selected by the lowest admissible pooled Brier"
        " using the preregistered tie tolerance",
    )


def evaluate_development(
    samples: tuple[MlSample, ...], protocol: MlBaselineProtocol
) -> MlEvaluationOutcome:
    """Run exactly six development fits and never touch holdout rows."""
    planned = plan_folds(samples, protocol)
    fit_seconds = 0.0
    evaluation_seconds = 0.0
    folds: list[MlFoldResult] = []
    predictions: list[MlPredictionRow] = []
    pooled_labels: list[numpy.ndarray] = []
    pooled_probabilities: dict[MlScorer, list[numpy.ndarray]] = {
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
        raw = _comparisons(
            matrix,
            models=MODEL_SCORERS,
            quantile=DEVELOPMENT_QUANTILE,
            replicates=protocol.resampling.replicates,
            seed=protocol.resampling.seed,
        )
        comparisons = _comparison_models(
            matrix,
            summaries,
            raw,
            DEVELOPMENT_QUANTILE,
            MODEL_SCORERS,
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
                raise MlEvaluationError("evaluated rows must carry a label")
            for scorer in ALL_SCORERS:
                predictions.append(
                    MlPredictionRow(
                        sample_key=sample.sample_key,
                        decision_at=sample.decision_at,
                        stage="development",
                        fold=fold.fold,
                        scorer=scorer,
                        label=sample.label,
                        probability=float(matrix.probabilities[scorer][row]),
                    )
                )
        folds.append(
            MlFoldResult(
                fold=fold.fold,
                train_rows=len(fold.train),
                train_positives=sum(1 for sample in fold.train if sample.label == 1),
                train_negatives=sum(1 for sample in fold.train if sample.label == 0),
                purged_excluded=fold.purged_excluded,
                embargo_excluded=fold.embargo_excluded,
                evaluation_rows=int(matrix.labels.size),
                evaluation_positives=int(matrix.labels.sum()),
                evaluation_negatives=int(matrix.labels.size - matrix.labels.sum()),
                segments=len(fold.segments),
                scores=tuple(summaries[scorer] for scorer in ALL_SCORERS),
                comparisons=comparisons,
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
    raw_pooled = _comparisons(
        pooled_matrix,
        models=MODEL_SCORERS,
        quantile=DEVELOPMENT_QUANTILE,
        replicates=protocol.resampling.replicates,
        seed=protocol.resampling.seed,
    )
    pooled_comparisons = _comparison_models(
        pooled_matrix,
        {score.scorer: score for score in pooled_scores},
        raw_pooled,
        DEVELOPMENT_QUANTILE,
        MODEL_SCORERS,
    )
    calibration = tuple(_calibration(scorer, labels, combined[scorer]) for scorer in MODEL_SCORERS)
    evaluation_seconds += time.perf_counter() - started
    decision, selected, reason = _select(pooled_scores, pooled_comparisons)
    development = MlDevelopmentResult(
        decision=decision,
        folds=tuple(folds),
        pooled_scores=pooled_scores,
        pooled_comparisons=pooled_comparisons,
        calibration=calibration,
        selected_model=selected,
        selection_reason=reason,
        development_fits=MAX_DEVELOPMENT_FITS,
    )
    return MlEvaluationOutcome(
        development=development,
        predictions=tuple(predictions),
        fits=MAX_DEVELOPMENT_FITS,
        fit_seconds=fit_seconds,
        evaluation_seconds=evaluation_seconds,
    )


def _holdout_plan(
    samples: tuple[MlSample, ...], protocol: MlBaselineProtocol
) -> tuple[tuple[MlSample, ...], tuple[MlSample, ...], int, int]:
    start = protocol.folds[2].validation_end_index
    boundary = datetime.combine(samples[start].decision_at.date(), day_time(0, 0), tzinfo=UTC)
    label_cut = boundary - timedelta(days=EMBARGO_DAYS)
    train: list[MlSample] = []
    embargo_excluded = 0
    purged_excluded = 0
    for sample in samples[:start]:
        if not sample.eligible:
            if sample.purged:
                purged_excluded += 1
            continue
        if sample.exit_bar_end < label_cut:
            train.append(sample)
        else:
            embargo_excluded += 1
    evaluation = tuple(sample for sample in samples[start:] if sample.eligible)
    if len(train) < MIN_TRAIN_ROWS:
        raise MlInsufficientSampleError("holdout training rows below the declared floor")
    positives = sum(1 for sample in train if sample.label == 1)
    if min(positives, len(train) - positives) < MIN_CLASS_ROWS:
        raise MlInsufficientSampleError("holdout training classes below the declared floor")
    if len(evaluation) < MIN_HOLDOUT_ROWS:
        raise MlInsufficientSampleError("holdout rows below the declared floor")
    segments = _segments(evaluation)
    if not segments or min(len(segment) for segment in segments) < MIN_SEGMENT_ROWS:
        raise MlInsufficientSampleError("holdout has no resampleable contiguous segment")
    return tuple(train), evaluation, purged_excluded, embargo_excluded


def evaluate_holdout(
    samples: tuple[MlSample, ...],
    protocol: MlBaselineProtocol,
    guard: MlHoldoutGuard,
) -> MlHoldoutOutcome:
    """Refit the winner once and open the protected holdout exactly once."""
    selection = guard.open()
    model = selection.selected_model
    train, evaluation, purged_excluded, embargo_excluded = _holdout_plan(samples, protocol)
    train_labels = [sample.label for sample in train]
    if any(label is None for label in train_labels):
        raise MlEvaluationError("planned holdout training rows must carry a label")
    labels = [int(label) for label in train_labels if label is not None]
    prior = float(sum(labels)) / float(len(labels))
    started = time.perf_counter()
    outcome = fit_and_predict(
        model=model,
        train_features=[sample.features for sample in train],
        train_labels=labels,
        evaluation_features=[sample.features for sample in evaluation],
        logistic=protocol.logistic,
        histogram=protocol.histogram,
    )
    fit_seconds = time.perf_counter() - started
    probabilities = {
        MlScorer(model.value): _array(outcome.probabilities),
        MlScorer.HALF_PROBABILITY: numpy.full(len(evaluation), 0.5, dtype=numpy.float64),
        MlScorer.TRAINING_CLASS_PRIOR: numpy.full(len(evaluation), prior, dtype=numpy.float64),
    }
    started = time.perf_counter()
    labels_array = numpy.asarray([sample.label for sample in evaluation], dtype=numpy.int64)
    positions: list[numpy.ndarray] = []
    offset = 0
    for segment in _segments(evaluation):
        positions.append(numpy.arange(offset, offset + len(segment), dtype=numpy.int64))
        offset += len(segment)
    matrix = _RowMatrix(labels=labels_array, probabilities=probabilities, segments=tuple(positions))
    scorers = (MlScorer(model.value),) + REFERENCE_SCORERS
    summaries = {
        scorer: _summary(scorer, labels_array, probabilities[scorer]) for scorer in scorers
    }
    raw = _comparisons(
        matrix,
        models=(MlScorer(model.value),),
        quantile=HOLDOUT_QUANTILE,
        replicates=protocol.resampling.replicates,
        seed=protocol.resampling.seed,
    )
    comparisons = _comparison_models(
        matrix,
        summaries,
        raw,
        HOLDOUT_QUANTILE,
        (MlScorer(model.value),),
    )
    confirmed = all(
        comparison.improved_at_least_five_percent and comparison.lower_bound_positive
        for comparison in comparisons
    )
    calibration = _calibration(
        MlScorer(model.value), labels_array, probabilities[MlScorer(model.value)]
    )
    evaluation_seconds = time.perf_counter() - started
    predictions = tuple(
        MlPredictionRow(
            sample_key=sample.sample_key,
            decision_at=sample.decision_at,
            stage="holdout",
            fold=0,
            scorer=scorer,
            label=int(labels_array[index]),
            probability=float(probabilities[scorer][index]),
        )
        for index, sample in enumerate(evaluation)
        for scorer in scorers
    )
    holdout = MlHoldoutResult(
        status=(
            MlHoldoutStatus.CONFIRMED_RESEARCH_EDGE
            if confirmed
            else MlHoldoutStatus.HOLDOUT_NOT_CONFIRMED
        ),
        selected_model=model,
        train_rows=len(train),
        train_positives=sum(1 for sample in train if sample.label == 1),
        train_negatives=sum(1 for sample in train if sample.label == 0),
        purged_excluded=purged_excluded,
        embargo_excluded=embargo_excluded,
        evaluation_rows=int(labels_array.size),
        evaluation_positives=int(labels_array.sum()),
        evaluation_negatives=int(labels_array.size - labels_array.sum()),
        segments=len(positions),
        scores=tuple(summaries[scorer] for scorer in scorers),
        comparisons=comparisons,
        calibration=(calibration,),
        quantile=HOLDOUT_QUANTILE,
        holdout_fits=MAX_HOLDOUT_REFITS,
        diagnostics=tuple(f"holdout {line}" for line in outcome.diagnostics),
    )
    return MlHoldoutOutcome(
        holdout=holdout,
        predictions=predictions,
        fits=MAX_HOLDOUT_REFITS,
        fit_seconds=fit_seconds,
        evaluation_seconds=evaluation_seconds,
        accesses=guard.accesses,
    )


def unopened_holdout() -> MlHoldoutResult:
    """Explicit placeholder so a missing holdout is never inferred as zero value."""
    return MlHoldoutResult(
        status=MlHoldoutStatus.NOT_EVALUATED,
        selected_model=None,
        train_rows=0,
        train_positives=0,
        train_negatives=0,
        purged_excluded=0,
        embargo_excluded=0,
        evaluation_rows=0,
        evaluation_positives=0,
        evaluation_negatives=0,
        segments=0,
        scores=(),
        comparisons=(),
        calibration=(),
        quantile=HOLDOUT_QUANTILE,
        holdout_fits=0,
    )


__all__ = [
    "ALL_SCORERS",
    "MODEL_SCORERS",
    "REFERENCE_SCORERS",
    "MlEvaluationError",
    "MlEvaluationOutcome",
    "MlFoldSamples",
    "MlHoldoutAccessError",
    "MlHoldoutGuard",
    "MlHoldoutOutcome",
    "MlInsufficientSampleError",
    "MlPredictionRow",
    "MlSample",
    "evaluate_development",
    "evaluate_holdout",
    "plan_folds",
    "samples_from_view",
    "unopened_holdout",
]
