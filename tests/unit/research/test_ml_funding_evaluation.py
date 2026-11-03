"""Fold planning, paired bootstrap, width adapter and closed-holdout discipline."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import cast
from uuid import uuid4

import numpy
import pytest

from investment_analyst.research import ml_funding_evaluation
from investment_analyst.research.funding_export import (
    FundingTrainingFeatureRow,
    FundingTrainingMetadataRow,
    FundingTrainingTargetRow,
    FundingTrainingView,
)
from investment_analyst.research.ml_adapter import MlAdapterError, MlFitOutcome
from investment_analyst.research.ml_funding_evaluation import (
    FundingEvaluationError,
    FundingInsufficientSampleError,
    FundingSample,
    _calibration,
    _comparisons,
    _draw,
    _fit_fold,
    _RowMatrix,
    evaluate_funding_development,
    funding_samples_from_view,
    plan_funding_folds,
)
from investment_analyst.research.ml_funding_models import (
    ALL_SCORERS,
    CONTROL_SCORER,
    SIMPLE_REFERENCES,
    FundingFoldBoundary,
    FundingIncrementDecision,
    FundingProtocol,
    FundingScorer,
    metric_decimal,
)
from investment_analyst.research.ml_models import development_fold_boundaries

_START = date(2020, 1, 1)
_DATASET_ID = uuid4()
_BUNDLE_ID = uuid4()
_HASH = "a" * 64


def _features(index: int) -> tuple[float, float, float, float, float]:
    return (
        ((index % 17) - 8) / 100.0,
        ((index * 3) % 20) / 20.0,
        ((index * 5) % 11 - 5) / 10.0,
        0.000001 * (index % 7),
        0.000008 * (index % 5),
    )


def _sample(
    index: int,
    *,
    label: int = 0,
    base: bool = True,
    joint: bool | None = None,
    purged: bool = False,
    features: tuple[float, ...] | None = None,
) -> FundingSample:
    day = _START + timedelta(days=index)
    return FundingSample(
        sample_key=f"sample-{index:04d}",
        decision_at=datetime(day.year, day.month, day.day, 12, tzinfo=UTC),
        features=_features(index) if features is None else features,
        label=label,
        eligibility_base=base,
        eligibility_joint=base if joint is None else joint,
        purged=purged,
        split="train" if index < 700 else "validation",
    )


def _samples(rows: int = 800) -> tuple[FundingSample, ...]:
    return tuple(_sample(index, label=index % 2) for index in range(rows))


def _protocol(samples: tuple[FundingSample, ...], rows: int = 1000) -> FundingProtocol:
    bounds = development_fold_boundaries(rows)
    folds: list[FundingFoldBoundary] = []
    for index, (start, end) in enumerate(bounds):
        limit = datetime.combine(samples[start].decision_at.date(), datetime.min.time(), tzinfo=UTC)
        cut = limit - timedelta(days=5)
        train = [
            sample
            for sample in samples[:start]
            if sample.eligibility_joint and sample.exit_bar_end < cut
        ]
        evaluation = [sample for sample in samples[start:end] if sample.eligibility_joint]
        folds.append(
            FundingFoldBoundary(
                fold=index + 1,
                train_end_index=start,
                validation_start_index=start,
                validation_end_index=end,
                validation_start_date=samples[start].decision_at.date(),
                validation_end_date=_START + timedelta(days=end),
                expected_train_rows=len(train),
                expected_evaluation_rows=len(evaluation),
            )
        )
    return FundingProtocol(
        dataset_id=_DATASET_ID,
        dataset_content_sha256=_HASH,
        bundle_id=_BUNDLE_ID,
        bundle_content_sha256="b" * 64,
        decision_rows=rows,
        asset_id="crypto:btc-usd",
        decision_start=_START,
        decision_end=_START + timedelta(days=rows - 1),
        folds=tuple(folds),
    )


def _matrix(
    labels: tuple[int, ...],
    probabilities: dict[FundingScorer, tuple[float, ...]],
    segments: tuple[numpy.ndarray, ...],
) -> _RowMatrix:
    return _RowMatrix(
        labels=numpy.asarray(labels, dtype=numpy.int64),
        probabilities={scorer: numpy.asarray(values) for scorer, values in probabilities.items()},
        segments=segments,
    )


def _fake_fit(*, funding_from_label: bool):
    calls: list[int] = []

    def fit(**kwargs: object) -> MlFitOutcome:
        width = cast(int, kwargs["feature_width"])
        calls.append(width)
        evaluation = cast(list[tuple[float, ...]], kwargs["evaluation_features"])
        if width == 5 and funding_from_label:
            probabilities = tuple(0.9 if values[4] % 2 else 0.1 for values in evaluation)
        else:
            probabilities = tuple(0.5 for _ in evaluation)
        return MlFitOutcome(probabilities=probabilities, diagnostics=("fake",))

    return fit, calls


def _feature_row(
    key: str,
    *,
    decision_at: datetime,
    funding_24h: Decimal | None = Decimal("0.0001"),
) -> FundingTrainingFeatureRow:
    return FundingTrainingFeatureRow(
        sample_key=key,
        decision_at=decision_at,
        close_return_1d=Decimal("0.01"),
        rsi_14_fraction=Decimal("0.5"),
        macd_12_26_9_histogram_close_ratio=Decimal("0.1"),
        funding_sum_1h_24h=funding_24h,
        funding_sum_1h_168h=Decimal("0.0007"),
    )


def _single_view(
    *,
    funding_24h: Decimal | None = Decimal("0.0001"),
    label: int | None = 1,
    base: bool = True,
    joint: bool = True,
    close_return: Decimal | None = Decimal("0.01"),
) -> FundingTrainingView:
    decision = datetime(2020, 1, 1, 12, tzinfo=UTC)
    return cast(
        FundingTrainingView,
        SimpleNamespace(
            features=(
                replace(
                    _feature_row("a", decision_at=decision, funding_24h=funding_24h),
                    close_return_1d=close_return,
                ),
            ),
            targets=(
                FundingTrainingTargetRow(sample_key="a", label=label, return_5d=Decimal("0.02")),
            ),
            metadata=(
                FundingTrainingMetadataRow(
                    sample_key="a",
                    decision_at=decision,
                    split="train",
                    purged=False,
                    eligibility_base=base,
                    eligibility_joint=joint,
                ),
            ),
        ),
    )


def test_samples_join_features_targets_and_metadata_without_mixing() -> None:
    samples = funding_samples_from_view(_single_view())
    assert len(samples) == 1
    assert samples[0].features == pytest.approx((0.01, 0.5, 0.1, 0.0001, 0.0007))
    assert samples[0].label == 1
    unavailable = funding_samples_from_view(_single_view(funding_24h=None, joint=False))
    assert len(unavailable) == 1
    assert unavailable[0].eligibility_joint is False


def test_samples_reject_misaligned_or_inconsistent_projections() -> None:
    view = _single_view()
    duplicate = cast(
        FundingTrainingView,
        SimpleNamespace(
            features=(*view.features, *view.features),
            targets=view.targets,
            metadata=view.metadata,
        ),
    )
    with pytest.raises(FundingEvaluationError, match="duplicate"):
        funding_samples_from_view(duplicate)
    misaligned = cast(
        FundingTrainingView,
        SimpleNamespace(
            features=(replace(view.features[0], sample_key="b"),),
            targets=view.targets,
            metadata=view.metadata,
        ),
    )
    with pytest.raises(FundingEvaluationError, match="disagree"):
        funding_samples_from_view(misaligned)
    missing_funding = cast(
        FundingTrainingView,
        SimpleNamespace(
            features=(replace(view.features[0], funding_sum_1h_24h=None),),
            targets=view.targets,
            metadata=view.metadata,
        ),
    )
    with pytest.raises(FundingEvaluationError, match="joint eligible"):
        funding_samples_from_view(missing_funding)
    missing_technical = cast(
        FundingTrainingView,
        SimpleNamespace(
            features=(replace(view.features[0], close_return_1d=None),),
            targets=view.targets,
            metadata=(replace(view.metadata[0], eligibility_joint=False),),
        ),
    )
    with pytest.raises(FundingEvaluationError, match="base eligible"):
        funding_samples_from_view(missing_technical)
    inconsistent = cast(
        FundingTrainingView,
        SimpleNamespace(
            features=view.features,
            targets=view.targets,
            metadata=(replace(view.metadata[0], eligibility_base=False),),
        ),
    )
    with pytest.raises(FundingEvaluationError, match="joint eligibility"):
        funding_samples_from_view(inconsistent)


def test_plan_recomputes_the_sealed_windows_and_exclusion_counts() -> None:
    base = _samples(800)
    samples = (
        base[:10]
        + (_sample(10, base=False, purged=True),)
        + base[11:25]
        + (_sample(25, base=False),)
        + base[26:30]
        + (_sample(30, joint=False),)
        + base[31:]
    )
    plans = plan_funding_folds(samples, _protocol(samples))
    assert [plan.fold for plan in plans] == [1, 2, 3]
    assert [len(plan.evaluation) for plan in plans] == [100, 100, 100]
    assert all(len(plan.segments) == 1 for plan in plans)
    assert [plan.purged_excluded for plan in plans] == [1, 1, 1]
    assert [plan.ineligible_excluded for plan in plans] == [1, 1, 1]
    assert [plan.funding_unavailable_excluded for plan in plans] == [1, 1, 1]
    assert [plan.embargo_excluded for plan in plans] == [11, 11, 11]


def test_plan_rejects_a_grid_that_disagrees_with_the_protocol() -> None:
    samples = _samples()
    protocol = _protocol(samples)
    shifted = list(samples)
    shifted[10] = _sample(10, label=0, joint=False)
    with pytest.raises(FundingEvaluationError, match="training rows"):
        plan_funding_folds(tuple(shifted), protocol)
    with pytest.raises(FundingEvaluationError, match="last fold"):
        plan_funding_folds(samples[:-1], protocol)
    broken = list(samples)
    broken[5] = _sample(5, label=0)
    broken[5] = replace(broken[5], decision_at=broken[5].decision_at + timedelta(days=1))
    with pytest.raises(FundingEvaluationError, match="contiguous"):
        plan_funding_folds(tuple(broken), protocol)


def test_every_declared_floor_fails_closed() -> None:
    starved = tuple(
        sample if index < 400 else replace(sample, eligibility_joint=False)
        for index, sample in enumerate(_samples())
    )
    with pytest.raises(FundingInsufficientSampleError, match="evaluation rows"):
        plan_funding_folds(starved, _protocol(starved))

    imbalanced = tuple(replace(sample, label=0) for sample in _samples())
    protocol = _protocol(imbalanced)
    with pytest.raises(FundingInsufficientSampleError, match="classes"):
        plan_funding_folds(imbalanced, protocol)

    base = _samples(960)
    gapped = tuple(
        replace(sample, eligibility_joint=False)
        if 640 <= index <= 644 or 650 <= index <= 654
        else sample
        for index, sample in enumerate(base)
    )
    protocol = _protocol(gapped, rows=1200)
    with pytest.raises(FundingInsufficientSampleError, match="contiguous segment"):
        plan_funding_folds(gapped, protocol)


def test_fit_fold_declares_both_widths() -> None:
    samples = _samples()
    protocol = _protocol(samples)
    planned = plan_funding_folds(samples, protocol)[0]
    fit, calls = _fake_fit(funding_from_label=False)
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(ml_funding_evaluation, "fit_and_predict", fit)
    try:
        probabilities, diagnostics = _fit_fold(planned, protocol)
    finally:
        monkeypatch.undo()
    assert calls == [3, 5]
    assert diagnostics == ("fold=1 arm=technical_logistic fake", "fold=1 arm=funding_logistic fake")
    assert probabilities[FundingScorer.HALF_PROBABILITY].tolist() == [0.5] * 100
    expected_prior = sum(1 for sample in planned.train if sample.label == 1) / len(planned.train)
    assert probabilities[FundingScorer.TRAINING_CLASS_PRIOR][0] == pytest.approx(expected_prior)


def test_development_runs_exactly_six_fits_and_observes_the_increment() -> None:
    samples = tuple(
        replace(
            sample,
            features=sample.features[:4] + ((1.0 if sample.label == 1 else 0.0),),
        )
        for sample in _samples()
    )
    protocol = _protocol(samples)
    fit, calls = _fake_fit(funding_from_label=True)
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(ml_funding_evaluation, "fit_and_predict", fit)
    try:
        outcome = evaluate_funding_development(samples, protocol)
    finally:
        monkeypatch.undo()
    assert calls == [3, 5, 3, 5, 3, 5]
    assert outcome.fits == 6
    assert outcome.decision is FundingIncrementDecision.INCREMENTAL_EVIDENCE_OBSERVED
    assert len({row.sample_key for row in outcome.predictions}) == 300
    assert len(outcome.predictions) == 1200
    assert all(comparison.criterion_satisfied for comparison in outcome.pooled_comparisons)
    assert not hasattr(outcome, "holdout")


def test_development_reports_no_increment_without_a_demonstrated_edge() -> None:
    samples = _samples()
    protocol = _protocol(samples)
    fit, _ = _fake_fit(funding_from_label=False)
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(ml_funding_evaluation, "fit_and_predict", fit)
    try:
        outcome = evaluate_funding_development(samples, protocol)
    finally:
        monkeypatch.undo()
    assert outcome.decision is FundingIncrementDecision.NO_DEMONSTRATED_INCREMENT
    for comparison in outcome.pooled_comparisons:
        assert comparison.criterion_satisfied is False
        assert comparison.lower_bound < Decimal("0.001")


def test_a_convergence_failure_is_not_treated_as_negative_evidence() -> None:
    samples = _samples()
    protocol = _protocol(samples)

    def failing(**kwargs: object) -> MlFitOutcome:
        raise MlAdapterError("logistic regression did not converge within max_iter")

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(ml_funding_evaluation, "fit_and_predict", failing)
    try:
        with pytest.raises(MlAdapterError):
            evaluate_funding_development(samples, protocol)
    finally:
        monkeypatch.undo()


def test_paired_bootstrap_draws_one_path_per_segment_and_truncates_the_last_block() -> None:
    segments = (
        numpy.arange(0, 45, dtype=numpy.int64),
        numpy.arange(45, 70, dtype=numpy.int64),
    )
    generator = numpy.random.Generator(numpy.random.PCG64(1729))
    first = _draw(segments, generator)
    second = _draw(segments, generator)
    assert first.size == 70
    assert first[:45].min() >= 0 and first[:45].max() < 45
    assert first[45:].min() >= 45 and first[45:].max() < 70
    assert not numpy.array_equal(first, second)
    with pytest.raises(FundingEvaluationError, match="full block"):
        _draw((numpy.arange(0, 19, dtype=numpy.int64),), generator)


def test_comparisons_share_draws_and_are_deterministic() -> None:
    labels = tuple(index % 2 for index in range(120))
    segments = (
        numpy.arange(0, 60, dtype=numpy.int64),
        numpy.arange(60, 120, dtype=numpy.int64),
    )
    perfect = tuple(0.9 if label else 0.1 for label in labels)
    generator = numpy.random.Generator(numpy.random.PCG64(7))
    technical = tuple(float(value) for value in generator.uniform(0.2, 0.8, len(labels)))
    probabilities = {
        FundingScorer.FUNDING_LOGISTIC: perfect,
        FundingScorer.TECHNICAL_LOGISTIC: technical,
        FundingScorer.HALF_PROBABILITY: tuple(0.5 for _ in labels),
        FundingScorer.TRAINING_CLASS_PRIOR: tuple(0.5 for _ in labels),
    }
    matrix = _matrix(labels, probabilities, segments)
    first = _comparisons(matrix, quantile=Decimal("0.0125"), replicates=200, seed=1729)
    second = _comparisons(matrix, quantile=Decimal("0.0125"), replicates=200, seed=1729)
    assert first == second
    assert [comparison.reference for comparison in first] == [
        CONTROL_SCORER,
        *SIMPLE_REFERENCES,
    ]
    assert all(comparison.lower_bound > 0 for comparison in first)
    assert all(comparison.criterion_satisfied for comparison in first)
    changed = _comparisons(matrix, quantile=Decimal("0.0125"), replicates=200, seed=1730)
    assert changed[0].lower_bound != first[0].lower_bound


def test_identical_probabilities_produce_a_zero_lower_bound() -> None:
    labels = tuple(index % 2 for index in range(120))
    segments = (numpy.arange(0, 120, dtype=numpy.int64),)
    probabilities = {scorer: tuple(0.5 for _ in labels) for scorer in ALL_SCORERS}
    comparisons = _comparisons(
        _matrix(labels, probabilities, segments),
        quantile=Decimal("0.0125"),
        replicates=200,
        seed=1729,
    )
    assert all(comparison.lower_bound == 0 for comparison in comparisons)
    assert all(comparison.criterion_satisfied is False for comparison in comparisons)


def test_calibration_keeps_ten_explicit_bins() -> None:
    labels = numpy.asarray([0, 1, 1, 0], dtype=numpy.int64)
    probabilities = numpy.asarray([0.05, 0.15, 0.55, 0.95], dtype=numpy.float64)
    table = _calibration(FundingScorer.FUNDING_LOGISTIC, labels, probabilities)
    assert len(table.bins) == 10
    assert sum(item.count for item in table.bins) == 4
    assert table.bins[0].mean_probability == metric_decimal(0.05)
    assert table.bins[1].observed_frequency == metric_decimal(1.0)
    assert table.bins[9].includes_upper is True
    assert table.bins[6].count == 0
    assert table.bins[6].mean_probability is None
