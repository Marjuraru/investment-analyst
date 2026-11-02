"""Folds, metrics, paired bootstrap, selection and protected-holdout discipline."""

from __future__ import annotations

import inspect
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID

import numpy
import pytest

from investment_analyst.research import ml_evaluation
from investment_analyst.research.ml_evaluation import (
    MlEvaluationError,
    MlHoldoutAccessError,
    MlHoldoutGuard,
    MlInsufficientSampleError,
    MlSample,
    _calibration,
    _comparisons,
    _holdout_plan,
    _log_loss,
    _quantile,
    _select,
    _summary,
    evaluate_development,
    evaluate_holdout,
    plan_folds,
    samples_from_view,
    unopened_holdout,
)
from investment_analyst.research.ml_models import (
    DEVELOPMENT_QUANTILE,
    MIN_RELATIVE_BRIER_IMPROVEMENT,
    MlBaselineProtocol,
    MlBaselineSelection,
    MlFoldBoundary,
    MlHoldoutStatus,
    MlMetricSummary,
    MlModelFamily,
    MlModelState,
    MlReferenceComparison,
    MlScorer,
    development_fold_boundaries,
    metric_decimal,
)

_START = date(2020, 1, 1)
_DATASET_ID = UUID("0f4f4c1e-4a4b-4d4a-8f0a-2b7a3c5d6e7f")


def _features(index: int) -> tuple[float, float, float]:
    return (
        ((index % 17) - 8) / 100.0,
        ((index * 3) % 20) / 20.0,
        ((index * 5) % 11 - 5) / 10.0,
    )


def _sample(
    index: int,
    *,
    label: int | None,
    eligible: bool = True,
    purged: bool = False,
) -> MlSample:
    day = _START + timedelta(days=index)
    return MlSample(
        sample_key=f"sample-{index:04d}",
        decision_at=datetime(day.year, day.month, day.day, 12, tzinfo=UTC),
        features=_features(index),
        label=label,
        eligible=eligible,
        purged=purged,
        split="train",
    )


def _default_label(index: int) -> int:
    return int((index * 7) % 10 < 5)


def _learnable_label(index: int) -> int:
    return int(((index % 17) - 8) > 0)


def _series(
    count: int,
    *,
    label_rule: object = None,
    eligible_rule: object = None,
) -> tuple[MlSample, ...]:
    resolved_rule = _default_label if label_rule is None else label_rule
    samples = []
    for index in range(count):
        eligible = True if eligible_rule is None else bool(eligible_rule(index))
        samples.append(
            _sample(
                index,
                label=int(resolved_rule(index)) if eligible else None,
                eligible=eligible,
            )
        )
    return tuple(samples)


def _protocol(samples: tuple[MlSample, ...]) -> MlBaselineProtocol:
    boundaries = development_fold_boundaries(len(samples))
    folds = tuple(
        MlFoldBoundary(
            fold=number + 1,
            train_end_index=first,
            validation_start_index=first,
            validation_end_index=second,
            validation_start_date=samples[first].decision_at.date(),
            validation_end_date=samples[second].decision_at.date(),
        )
        for number, (first, second) in enumerate(boundaries)
    )
    return MlBaselineProtocol(
        dataset_id=_DATASET_ID,
        dataset_content_sha256="a" * 64,
        decision_rows=len(samples),
        asset_id="crypto:btc-usd",
        folds=folds,
    )


def _sealed_selection(protocol: MlBaselineProtocol, development: object) -> MlBaselineSelection:
    assert development.selected_model is not None
    return MlBaselineSelection(
        run_id=UUID("33333333-3333-4333-8333-333333333333"),
        dataset_id=protocol.dataset_id,
        dataset_content_sha256=protocol.dataset_content_sha256,
        protocol_sha256="b" * 64,
        code_material_sha256="c" * 64,
        development=development,
        selected_model=development.selected_model,
        sealed_at=datetime(2026, 11, 1, 12, 0, tzinfo=UTC),
    )


def _score(scorer: MlScorer, brier: str) -> MlMetricSummary:
    return MlMetricSummary(
        scorer=scorer,
        rows=100,
        positives=50,
        negatives=50,
        brier=Decimal(brier),
        log_loss=Decimal("0.5"),
    )


def _comparison(
    model: MlScorer,
    reference: MlScorer,
    model_brier: str,
    reference_brier: str,
    lower: str,
) -> MlReferenceComparison:
    model_value = Decimal(model_brier)
    reference_value = Decimal(reference_brier)
    relative = None
    if reference_value != 0:
        relative = metric_decimal(float(reference_value - model_value) / float(reference_value))
    return MlReferenceComparison(
        model=model,
        reference=reference,
        model_brier=model_value,
        reference_brier=reference_value,
        absolute_improvement=reference_value - model_value,
        relative_improvement=relative,
        mean_paired_difference=reference_value - model_value,
        lower_bound=Decimal(lower),
        quantile=DEVELOPMENT_QUANTILE,
        improved_at_least_five_percent=(
            relative is not None and relative >= MIN_RELATIVE_BRIER_IMPROVEMENT
        ),
        lower_bound_positive=Decimal(lower) > 0,
    )


def _selection_inputs(
    *,
    logistic_brier: str = "0.20",
    histogram_brier: str = "0.21",
    logistic_lower: str = "0.01",
    histogram_lower: str = "0.01",
) -> tuple[tuple[MlMetricSummary, ...], tuple[MlReferenceComparison, ...]]:
    scores = (
        _score(MlScorer.LOGISTIC_REGRESSION, logistic_brier),
        _score(MlScorer.HIST_GRADIENT_BOOSTING, histogram_brier),
        _score(MlScorer.HALF_PROBABILITY, "0.25"),
        _score(MlScorer.TRAINING_CLASS_PRIOR, "0.25"),
    )
    comparisons = (
        _comparison(
            MlScorer.LOGISTIC_REGRESSION,
            MlScorer.HALF_PROBABILITY,
            logistic_brier,
            "0.25",
            logistic_lower,
        ),
        _comparison(
            MlScorer.LOGISTIC_REGRESSION,
            MlScorer.TRAINING_CLASS_PRIOR,
            logistic_brier,
            "0.25",
            logistic_lower,
        ),
        _comparison(
            MlScorer.HIST_GRADIENT_BOOSTING,
            MlScorer.HALF_PROBABILITY,
            histogram_brier,
            "0.25",
            histogram_lower,
        ),
        _comparison(
            MlScorer.HIST_GRADIENT_BOOSTING,
            MlScorer.TRAINING_CLASS_PRIOR,
            histogram_brier,
            "0.25",
            histogram_lower,
        ),
    )
    return scores, comparisons


def test_plan_folds_uses_all_dates_and_applies_the_label_embargo() -> None:
    samples = _series(1200)
    planned = plan_folds(samples, _protocol(samples))
    assert [
        (fold.fold, len(fold.train), len(fold.evaluation), len(fold.segments)) for fold in planned
    ] == [(1, 589, 120, 1), (2, 709, 120, 1), (3, 829, 120, 1)]
    assert [fold.embargo_excluded for fold in planned] == [11, 11, 11]
    assert planned[0].train[-1].decision_at.date() == _START + timedelta(days=588)
    assert planned[0].evaluation[0].decision_at.date() == _START + timedelta(days=600)
    assert planned[1].evaluation[0].decision_at.date() == _START + timedelta(days=720)
    assert planned[2].evaluation[-1].decision_at.date() == _START + timedelta(days=959)


def test_plan_folds_counts_purged_and_ineligible_rows_once() -> None:
    samples = list(_series(1200))
    samples[10] = _sample(10, label=None, eligible=False, purged=True)
    samples[20] = _sample(20, label=None, eligible=False, purged=True)
    samples[30] = _sample(30, label=None, eligible=False, purged=False)
    planned = plan_folds(tuple(samples), _protocol(tuple(samples)))
    assert planned[0].purged_excluded == 2
    assert planned[0].ineligible_excluded == 1
    assert planned[0].embargo_excluded == 11
    assert len(planned[0].train) == 589 - 3


def test_plan_folds_rejects_every_declared_floor() -> None:
    too_small = _series(100)
    with pytest.raises(MlInsufficientSampleError):
        plan_folds(too_small, _protocol(too_small))
    single_class = _series(1200, label_rule=lambda index: 1)
    with pytest.raises(MlInsufficientSampleError):
        plan_folds(single_class, _protocol(single_class))
    short_validation = _series(900)
    with pytest.raises(MlInsufficientSampleError):
        plan_folds(short_validation, _protocol(short_validation))
    fragmenting = _series(
        1200,
        eligible_rule=lambda index: not (600 <= index < 720 and index % 2 == 1),
    )
    with pytest.raises(MlInsufficientSampleError):
        plan_folds(fragmenting, _protocol(fragmenting))


def test_plan_folds_rejects_tampered_sealed_boundaries() -> None:
    samples = _series(1200)
    protocol = _protocol(samples)
    tampered = protocol.model_copy(update={"folds": (protocol.folds[0],), "decision_rows": 1200})
    with pytest.raises(MlEvaluationError):
        plan_folds(samples, tampered)


def _view(rows: tuple[SimpleNamespace, ...]) -> SimpleNamespace:
    features = tuple(
        SimpleNamespace(
            sample_key=row.sample_key,
            decision_at=row.decision_at,
            close_return_1d=Decimal("0.01"),
            rsi_14_fraction=Decimal("0.5"),
            macd_12_26_9_histogram_close_ratio=Decimal("-0.02"),
        )
        for row in rows
    )
    targets = tuple(
        SimpleNamespace(sample_key=row.sample_key, label=1, return_5d=Decimal("0.02"))
        for row in rows
    )
    metadata = tuple(
        SimpleNamespace(
            sample_key=row.sample_key,
            decision_at=row.decision_at,
            split="train",
            purged=False,
            purge_reason=None,
            eligible=True,
        )
        for row in rows
    )
    return SimpleNamespace(features=features, targets=targets, metadata=metadata)


def _row(index: int, *, key: str | None = None) -> SimpleNamespace:
    day = _START + timedelta(days=index)
    return SimpleNamespace(
        sample_key=key or f"sample-{index:04d}",
        decision_at=datetime(day.year, day.month, day.day, 12, tzinfo=UTC),
    )


def test_samples_from_view_converts_decimal_features_once() -> None:
    view = _view((_row(0), _row(1)))
    samples = samples_from_view(view)
    assert len(samples) == 2
    assert samples[0].features == (0.01, 0.5, -0.02)
    assert samples[0].label == 1
    assert samples[0].eligible is True


def test_samples_from_view_rejects_identity_and_label_conflicts() -> None:
    duplicate = _view((_row(0), _row(0)))
    with pytest.raises(MlEvaluationError):
        samples_from_view(duplicate)
    missing = _view((_row(0),))
    missing.targets = ()
    with pytest.raises(MlEvaluationError):
        samples_from_view(missing)
    mismatch = _view((_row(0),))
    mismatch.features[0].decision_at = datetime(2020, 1, 2, 12, tzinfo=UTC)
    with pytest.raises(MlEvaluationError):
        samples_from_view(mismatch)
    missing_label = _view((_row(0),))
    missing_label.targets[0].label = None
    with pytest.raises(MlEvaluationError):
        samples_from_view(missing_label)
    missing_feature = _view((_row(0),))
    missing_feature.features[0].rsi_14_fraction = None
    with pytest.raises(MlEvaluationError):
        samples_from_view(missing_feature)


def test_brier_log_loss_and_calibration_are_explicit() -> None:
    labels = numpy.asarray([0, 1, 1, 0], dtype=numpy.int64)
    probabilities = numpy.asarray([0.0, 0.55, 1.0, 1.0], dtype=numpy.float64)
    summary = _summary(MlScorer.HALF_PROBABILITY, labels, numpy.full(4, 0.5))
    assert summary.brier == Decimal("0.25")
    assert summary.log_loss == metric_decimal(float(numpy.log(2)))
    assert summary.positives == 2 and summary.negatives == 2
    clipped = _log_loss(numpy.asarray([1], dtype=numpy.int64), numpy.asarray([0.0]))
    assert metric_decimal(clipped) == metric_decimal(float(-numpy.log(1e-15)))
    table = _calibration(MlScorer.LOGISTIC_REGRESSION, labels, probabilities)
    assert len(table.bins) == 10
    assert table.bins[0].count == 1
    assert table.bins[5].count == 1
    assert table.bins[9].count == 2
    assert table.bins[9].includes_upper is True
    assert table.bins[1].count == 0 and table.bins[1].mean_probability is None
    assert table.bins[9].observed_frequency == Decimal("0.5")


def _matrix(*, paired: bool) -> ml_evaluation._RowMatrix:
    generator = numpy.random.default_rng(7)
    labels = generator.integers(0, 2, size=120).astype(numpy.int64)
    base = generator.uniform(0.2, 0.8, size=120)
    probabilities = {
        MlScorer.LOGISTIC_REGRESSION: base,
        MlScorer.HIST_GRADIENT_BOOSTING: 1.0 - base,
        MlScorer.HALF_PROBABILITY: numpy.full(120, 0.5),
        MlScorer.TRAINING_CLASS_PRIOR: numpy.full(120, float(labels.mean())),
    }
    if paired:
        probabilities[MlScorer.LOGISTIC_REGRESSION] = probabilities[MlScorer.HALF_PROBABILITY]
    return ml_evaluation._RowMatrix(
        labels=labels,
        probabilities=probabilities,
        segments=(numpy.arange(120, dtype=numpy.int64),),
    )


def test_paired_bootstrap_is_deterministic_and_shared_across_comparisons() -> None:
    matrix = _matrix(paired=False)
    first = _comparisons(
        matrix,
        models=(MlScorer.LOGISTIC_REGRESSION, MlScorer.HIST_GRADIENT_BOOSTING),
        quantile=DEVELOPMENT_QUANTILE,
        replicates=60,
        seed=1729,
    )
    second = _comparisons(
        matrix,
        models=(MlScorer.LOGISTIC_REGRESSION, MlScorer.HIST_GRADIENT_BOOSTING),
        quantile=DEVELOPMENT_QUANTILE,
        replicates=60,
        seed=1729,
    )
    assert first == second
    other = _comparisons(
        matrix,
        models=(MlScorer.LOGISTIC_REGRESSION, MlScorer.HIST_GRADIENT_BOOSTING),
        quantile=DEVELOPMENT_QUANTILE,
        replicates=60,
        seed=1730,
    )
    assert other != first
    paired = _comparisons(
        _matrix(paired=True),
        models=(MlScorer.LOGISTIC_REGRESSION,),
        quantile=DEVELOPMENT_QUANTILE,
        replicates=60,
        seed=1729,
    )
    mean, lower, _point = paired[(MlScorer.LOGISTIC_REGRESSION, MlScorer.HALF_PROBABILITY)]
    assert mean == 0.0
    assert lower == 0.0


def test_quantile_uses_linear_interpolation() -> None:
    values = numpy.asarray([0.0, 1.0, 2.0, 3.0])
    assert _quantile(values, Decimal("0.5")) == 1.5
    assert _quantile(values, Decimal("0.0125")) == pytest.approx(0.0375)


def test_selection_requires_both_references_and_positive_bounds() -> None:
    scores, comparisons = _selection_inputs()
    decision, selected, _reason = _select(scores, comparisons)
    assert decision is MlModelState.CANDIDATE_SELECTED
    assert selected is MlModelFamily.LOGISTIC_REGRESSION

    weak_scores, weak_comparisons = _selection_inputs(
        logistic_brier="0.245", histogram_brier="0.245"
    )
    decision, selected, reason = _select(weak_scores, weak_comparisons)
    assert decision is MlModelState.NO_DEMONSTRATED_EDGE
    assert selected is None and "five percent" in reason

    scores, comparisons = _selection_inputs(logistic_lower="0", histogram_lower="0")
    decision, selected, _reason = _select(scores, comparisons)
    assert decision is MlModelState.NO_DEMONSTRATED_EDGE
    assert selected is None


def test_selection_ties_prefer_the_preregistered_logistic_model() -> None:
    scores, comparisons = _selection_inputs(logistic_brier="0.20", histogram_brier="0.20")
    decision, selected, _reason = _select(scores, comparisons)
    assert decision is MlModelState.CANDIDATE_SELECTED
    assert selected is MlModelFamily.LOGISTIC_REGRESSION


def test_selection_prefers_the_lower_pooled_brier_and_handles_zero_denominator() -> None:
    scores, comparisons = _selection_inputs(logistic_brier="0.20", histogram_brier="0.10")
    decision, selected, _reason = _select(scores, comparisons)
    assert decision is MlModelState.CANDIDATE_SELECTED
    assert selected is MlModelFamily.HIST_GRADIENT_BOOSTING

    scores = (
        _score(MlScorer.LOGISTIC_REGRESSION, "0"),
        _score(MlScorer.HIST_GRADIENT_BOOSTING, "0.20"),
        _score(MlScorer.HALF_PROBABILITY, "0"),
        _score(MlScorer.TRAINING_CLASS_PRIOR, "0"),
    )
    comparisons = (
        _comparison(MlScorer.LOGISTIC_REGRESSION, MlScorer.HALF_PROBABILITY, "0", "0", "0.01"),
        _comparison(MlScorer.LOGISTIC_REGRESSION, MlScorer.TRAINING_CLASS_PRIOR, "0", "0", "0.01"),
        _comparison(
            MlScorer.HIST_GRADIENT_BOOSTING,
            MlScorer.HALF_PROBABILITY,
            "0.20",
            "0",
            "0.01",
        ),
        _comparison(
            MlScorer.HIST_GRADIENT_BOOSTING,
            MlScorer.TRAINING_CLASS_PRIOR,
            "0.20",
            "0",
            "0.01",
        ),
    )
    decision, selected, _reason = _select(scores, comparisons)
    assert decision is MlModelState.NO_DEMONSTRATED_EDGE
    assert selected is None


def test_development_signature_cannot_receive_holdout_rows() -> None:
    parameters = tuple(inspect.signature(evaluate_development).parameters)
    assert parameters == ("samples", "protocol")


@pytest.fixture(scope="module")
def learnable() -> tuple[tuple[MlSample, ...], MlBaselineProtocol, object]:
    samples = _series(1200, label_rule=_learnable_label)
    protocol = _protocol(samples)
    outcome = evaluate_development(samples, protocol)
    return samples, protocol, outcome


def test_development_selects_a_candidate_and_never_touches_holdout(
    learnable: tuple[tuple[MlSample, ...], MlBaselineProtocol, object],
) -> None:
    _samples, protocol, outcome = learnable
    development = outcome.development
    assert development.decision is MlModelState.CANDIDATE_SELECTED
    assert development.selected_model is not None
    assert development.development_fits == 6
    assert outcome.fits == 6
    assert len(outcome.predictions) == 360 * 4
    assert {row.stage for row in outcome.predictions} == {"development"}
    selection = _sealed_selection(protocol, development)
    assert selection.holdout_state == "not_opened"
    assert selection.selected_model is development.selected_model


def test_holdout_confirmed_path_uses_one_refit_and_one_access(
    learnable: tuple[tuple[MlSample, ...], MlBaselineProtocol, object],
) -> None:
    samples, protocol, outcome = learnable
    selection = _sealed_selection(protocol, outcome.development)
    guard = MlHoldoutGuard(selection)
    holdout = evaluate_holdout(samples, protocol, guard)
    assert holdout.holdout.status is MlHoldoutStatus.CONFIRMED_RESEARCH_EDGE
    assert holdout.fits == 1
    assert holdout.accesses == 1
    assert len(holdout.predictions) == 240 * 3
    assert {row.stage for row in holdout.predictions} == {"holdout"}
    assert len(holdout.holdout.comparisons) == 2
    assert guard.accesses == 1


def test_holdout_failure_does_not_change_the_sealed_selection(
    learnable: tuple[tuple[MlSample, ...], MlBaselineProtocol, object],
) -> None:
    samples, protocol, outcome = learnable
    altered = tuple(
        sample if index < 960 else replace(sample, label=1 - int(sample.label))
        for index, sample in enumerate(samples)
    )
    recomputed = evaluate_development(altered, protocol)
    assert recomputed.development == outcome.development
    selection = _sealed_selection(protocol, outcome.development)
    holdout = evaluate_holdout(altered, protocol, MlHoldoutGuard(selection))
    assert holdout.holdout.status is MlHoldoutStatus.HOLDOUT_NOT_CONFIRMED
    assert selection.development == outcome.development
    assert selection.selected_model is outcome.development.selected_model


def test_holdout_guard_requires_a_selection_and_a_single_access(
    learnable: tuple[tuple[MlSample, ...], MlBaselineProtocol, object],
) -> None:
    _samples, protocol, outcome = learnable
    with pytest.raises(MlHoldoutAccessError):
        MlHoldoutGuard(None).open()
    guard = MlHoldoutGuard(_sealed_selection(protocol, outcome.development))
    assert guard.accesses == 0
    guard.open()
    assert guard.accesses == 1
    with pytest.raises(MlHoldoutAccessError):
        guard.open()
    placeholder = unopened_holdout()
    assert placeholder.status is MlHoldoutStatus.NOT_EVALUATED
    assert placeholder.scores == () and placeholder.evaluation_rows == 0


def test_holdout_floors_reject_short_or_fragmented_rows() -> None:
    short = _series(
        1200,
        eligible_rule=lambda index: index < 960 or index < 990,
    )
    protocol = _protocol(short)
    with pytest.raises(MlInsufficientSampleError):
        _holdout_plan(short, protocol)
    fragmented = _series(
        1200,
        eligible_rule=lambda index: index < 960 or (index >= 960 and index % 2 == 0),
    )
    with pytest.raises(MlInsufficientSampleError):
        _holdout_plan(fragmented, _protocol(fragmented))


def test_plan_folds_rejects_a_foreign_protocol_row_count() -> None:
    samples = _series(1200)
    protocol = _protocol(samples)
    foreign = protocol.model_copy(update={"decision_rows": 1199})
    with pytest.raises(MlEvaluationError):
        plan_folds(samples, foreign)


def test_adapter_rejects_invalid_training_inputs_before_fitting() -> None:
    from investment_analyst.research.ml_adapter import MlAdapterError, fit_and_predict
    from investment_analyst.research.ml_models import (
        MlHistogramParameters,
        MlLogisticParameters,
    )

    logistic = MlLogisticParameters()
    histogram = MlHistogramParameters()
    options = dict(logistic=logistic, histogram=histogram)
    with pytest.raises(MlAdapterError):
        fit_and_predict(
            model=MlModelFamily.LOGISTIC_REGRESSION,
            train_features=[(0.1, 0.2, 0.3)] * 40,
            train_labels=[1] * 40,
            evaluation_features=[(0.1, 0.2, 0.3)],
            **options,
        )
    with pytest.raises(MlAdapterError):
        fit_and_predict(
            model=MlModelFamily.LOGISTIC_REGRESSION,
            train_features=[(0.1, 0.2)] * 40,
            train_labels=[0, 1] * 20,
            evaluation_features=[(0.1, 0.2)],
            **options,
        )
    with pytest.raises(MlAdapterError):
        fit_and_predict(
            model=MlModelFamily.HIST_GRADIENT_BOOSTING,
            train_features=[(float("nan"), 0.2, 0.3)] * 40,
            train_labels=[0, 1] * 20,
            evaluation_features=[(0.1, 0.2, 0.3)],
            **options,
        )
    with pytest.raises(MlAdapterError):
        fit_and_predict(
            model=MlModelFamily.HIST_GRADIENT_BOOSTING,
            train_features=[(0.1, 0.2, 0.3)] * 40,
            train_labels=[0, 1] * 20,
            evaluation_features=[],
            **options,
        )
    with pytest.raises(MlAdapterError):
        fit_and_predict(
            model=MlModelFamily.HIST_GRADIENT_BOOSTING,
            train_features=[(0.1, 0.2, 0.3)] * 40,
            train_labels=[2] * 40,
            evaluation_features=[(0.1, 0.2, 0.3)],
            **options,
        )
