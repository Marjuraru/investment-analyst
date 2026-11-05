"""Focused checks for train-only fitting, references, criteria and progress."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal, localcontext
from uuid import UUID

import pytest

from investment_analyst.core.models.enums import AssetClass
from investment_analyst.research import volatility_adapter
from investment_analyst.research.historical_archive import build_historical_archive
from investment_analyst.research.historical_models import (
    DECIMAL34,
    HistoricalArchive,
    HistoricalArchiveRequest,
    HistoricalAssetIdentity,
    HistoricalCandle,
    assumed_available_instant,
    bar_start_instant,
    historical_bar_content_id,
)
from investment_analyst.research.ml_models import metric_decimal
from investment_analyst.research.volatility_dataset import (
    VolatilityFoldPlan,
    build_volatility_decisions,
    plan_folds,
)
from investment_analyst.research.volatility_evaluation import (
    VolatilityEvaluationError,
    VolatilityFoldOutcome,
    combine_folds,
    decide,
    evaluate_fold,
    fold_from_progress,
    progress_from_fold,
)
from investment_analyst.research.volatility_export import build_protocol
from investment_analyst.research.volatility_models import (
    ARCHIVE_REQUEST_END,
    ARCHIVE_REQUEST_START,
    ASSET_ID,
    DECISION_START,
    EPSILON,
    HYPOTHESIS,
    PREREGISTERED_FOLDS,
    REFERENCE_MODELS,
    RESERVED_START_DATE,
    SOURCE_ID,
    STUDY_LIMITATIONS,
    TARGET_HORIZON,
    VolatilityCriteria,
    VolatilityDecision,
    VolatilityHoldoutPolicy,
    VolatilityMetric,
    VolatilityModel,
    VolatilityModelParameters,
    VolatilityProtocol,
    VolatilityResampling,
)

_RETRIEVED = datetime(2026, 11, 1, 2, 37, 32, tzinfo=UTC)
_SCOPE = HistoricalAssetIdentity(
    asset_id=ASSET_ID,
    source_id=SOURCE_ID,
    product_id="BTC-USD",
    asset_class=AssetClass.CRYPTO,
    quote_currency="USD",
    crypto_profile="bitcoin",
    exchange="COINBASE",
    granularity_seconds=86_400,
)
_REQUEST = HistoricalArchiveRequest(
    asset_id=ASSET_ID,
    start_date=ARCHIVE_REQUEST_START,
    end_date=ARCHIVE_REQUEST_END,
)


def _bar(start: datetime, open_value: Decimal, close: Decimal) -> HistoricalCandle:
    high = (max(open_value, close) * Decimal("1.01")).quantize(Decimal("0.01"))
    low = (min(open_value, close) * Decimal("0.99")).quantize(Decimal("0.01"))
    volume = Decimal("100")
    raw = (str(open_value), str(high), str(low), str(close), str(volume), "0")
    return HistoricalCandle(
        bar_content_id=historical_bar_content_id(
            asset_id=_SCOPE.asset_id,
            source_id=_SCOPE.source_id,
            bar_start=start,
            open_value=open_value,
            high=high,
            low=low,
            close=close,
            volume=volume,
            raw_values=raw,
        ),
        bar_start=start,
        bar_end=start + timedelta(days=1),
        open=open_value,
        high=high,
        low=low,
        close=close,
        volume=volume,
        raw_values=raw,
        assumed_available_at=assumed_available_instant(start + timedelta(days=1)),
        retrieved_at=_RETRIEVED,
    )


def _build_archive() -> HistoricalArchive:
    candles: list[HistoricalCandle] = []
    day = ARCHIVE_REQUEST_START
    with localcontext(DECIMAL34):
        for index in range(2557):
            base = (Decimal(10000) * (Decimal("1.0005") ** index)).quantize(Decimal("0.01"))
            close = (base * (Decimal(1) + Decimal((index % 7) - 3) / Decimal(1000))).quantize(
                Decimal("0.01")
            )
            candles.append(_bar(bar_start_instant(day), base, close))
            day += timedelta(days=1)
    return build_historical_archive(
        _SCOPE,
        _REQUEST,
        request_urls=("https://example.invalid/candles",),
        retrieved_at=_RETRIEVED,
        candles=tuple(candles),
    )


def _protocol(**overrides: object) -> VolatilityProtocol:
    payload: dict[str, object] = {
        "asset_id": ASSET_ID,
        "source_id": SOURCE_ID,
        "archive_id": UUID("00000000-0000-4000-8000-000000000001"),
        "archive_semantic_sha256": "a" * 64,
        "archive_candles_sha256": "b" * 64,
        "archive_retrieved_at": _RETRIEVED,
        "archive_request_start": ARCHIVE_REQUEST_START,
        "archive_request_end": ARCHIVE_REQUEST_END,
        "study_cut_at": _RETRIEVED,
        "decision_start": DECISION_START,
        "decision_end": ARCHIVE_REQUEST_END - timedelta(days=6),
        "decision_count": 2186,
        "reserved_start_index": 1748,
        "reserved_start_date": RESERVED_START_DATE,
        "folds": PREREGISTERED_FOLDS,
        "feature_windows": (1, 7, 30),
        "reference_window": 5,
        "target_horizon": TARGET_HORIZON,
        "epsilon": EPSILON,
        "model": VolatilityModelParameters(),
        "references": REFERENCE_MODELS,
        "criteria": VolatilityCriteria(),
        "resampling": VolatilityResampling(),
        "holdout": VolatilityHoldoutPolicy(),
        "hypothesis": HYPOTHESIS,
        "limitations": STUDY_LIMITATIONS,
    }
    payload.update(overrides)
    return VolatilityProtocol(**payload)


@pytest.fixture(scope="module")
def planned() -> tuple[VolatilityProtocol, tuple[VolatilityFoldPlan, ...]]:
    archive = _build_archive()
    protocol = build_protocol(archive)
    dataset = build_volatility_decisions(archive, protocol)
    return protocol, plan_folds(dataset, protocol)


@pytest.fixture(scope="module")
def completed(
    planned: tuple[VolatilityProtocol, tuple[VolatilityFoldPlan, ...]],
) -> tuple[
    VolatilityProtocol,
    tuple[VolatilityFoldPlan, ...],
    tuple[VolatilityFoldOutcome, ...],
]:
    protocol, plans = planned
    return protocol, plans, tuple(evaluate_fold(plan, protocol) for plan in plans)


def test_evaluate_fold_fits_exactly_once_and_only_on_train_rows(
    planned: tuple[VolatilityProtocol, tuple[VolatilityFoldPlan, ...]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    protocol, plans = planned
    plan = plans[0]
    captured: dict[str, object] = {}

    def fake_fit(**kwargs: object) -> tuple[float, ...]:
        captured.update(kwargs)
        return tuple(0.0 for _ in range(len(plan.validation)))

    monkeypatch.setattr(volatility_adapter, "fit_ridge", fake_fit)
    outcome = evaluate_fold(plan, protocol)

    assert captured["train_features"] == [row.feature_values for row in plan.train]
    assert captured["train_targets"] == [row.target for row in plan.train]
    assert captured["evaluation_features"] == [row.feature_values for row in plan.validation]
    assert outcome.result.train_rows == len(plan.train)
    assert outcome.result.validation_rows == len(plan.validation)
    assert len(outcome.predictions) == len(plan.validation) * 4


def test_references_are_seeded_from_the_row_evidence(
    completed: tuple[
        VolatilityProtocol,
        tuple[VolatilityFoldPlan, ...],
        tuple[VolatilityFoldOutcome, ...],
    ],
) -> None:
    protocol, plans, folds = completed
    del protocol
    for fold, plan in zip(folds, plans, strict=True):
        by_key = {(item.sample_key, item.model): item for item in fold.predictions}
        with localcontext(DECIMAL34):
            expected_mean = sum(
                (row.target for row in plan.train if row.target is not None),
                Decimal(0),
            ) / Decimal(len(plan.train))
        for row in plan.validation:
            ridge = by_key[(row.sample_key, VolatilityModel.RIDGE)]
            persistence_5d = by_key[(row.sample_key, VolatilityModel.PERSISTENCE_5D)]
            persistence_30d = by_key[(row.sample_key, VolatilityModel.PERSISTENCE_30D)]
            training_mean = by_key[(row.sample_key, VolatilityModel.TRAINING_MEAN)]
            assert persistence_5d.estimate == row.reference_5d.value
            assert row.features is not None
            assert persistence_30d.estimate == next(
                feature.value for feature in row.features if feature.window == 30
            )
            assert training_mean.estimate == metric_decimal(float(expected_mean))
            assert row.target is not None
            for prediction in (ridge, persistence_5d, persistence_30d, training_mean):
                with localcontext(DECIMAL34):
                    expected_squared = (prediction.estimate - row.target) ** 2
                    expected_absolute = abs(prediction.estimate - row.target)
                assert prediction.squared_error == expected_squared
                assert prediction.absolute_error == expected_absolute


def test_combined_evaluation_is_deterministic(
    completed: tuple[
        VolatilityProtocol,
        tuple[VolatilityFoldPlan, ...],
        tuple[VolatilityFoldOutcome, ...],
    ],
) -> None:
    protocol, plans, folds = completed
    del plans
    first = combine_folds(folds, protocol)
    second = combine_folds(folds, protocol)

    assert first.pooled_scores == second.pooled_scores
    assert first.pooled_comparisons == second.pooled_comparisons
    assert first.decision == second.decision
    assert first.decision_reason == second.decision_reason


def test_decision_requires_lower_bounds_and_every_fold_to_improve(
    completed: tuple[
        VolatilityProtocol,
        tuple[VolatilityFoldPlan, ...],
        tuple[VolatilityFoldOutcome, ...],
    ],
) -> None:
    protocol, plans, folds = completed
    del plans
    outcome = combine_folds(folds, protocol)
    results = tuple(fold.result for fold in folds)
    decision, reason = decide(results, outcome.pooled_comparisons)
    assert decision is VolatilityDecision.DEVELOPMENT_EVIDENCE
    assert "every fold improves" in reason

    weak = tuple(
        comparison.model_copy(update={"improved_at_least_five_percent": False})
        if comparison.reference is VolatilityModel.PERSISTENCE_5D
        else comparison
        for comparison in outcome.pooled_comparisons
    )
    decision, reason = decide(results, weak)
    assert decision is VolatilityDecision.NO_DEMONSTRATED_INCREMENT
    assert "below five percent" in reason

    non_positive = tuple(
        comparison.model_copy(update={"lower_bound_positive": False})
        if comparison.reference is VolatilityModel.PERSISTENCE_30D
        else comparison
        for comparison in outcome.pooled_comparisons
    )
    decision, reason = decide(results, non_positive)
    assert decision is VolatilityDecision.NO_DEMONSTRATED_INCREMENT
    assert "lower bound not positive" in reason

    worse_scores = tuple(
        VolatilityMetric(
            model=metric.model,
            rows=metric.rows,
            mse=Decimal("100") if metric.model is VolatilityModel.RIDGE else metric.mse,
            mae=metric.mae,
        )
        if metric.model is VolatilityModel.RIDGE
        else metric
        for metric in results[1].scores
    )
    worse_fold = results[1].model_copy(update={"scores": worse_scores})
    decision, reason = decide((results[0], worse_fold, results[2]), outcome.pooled_comparisons)
    assert decision is VolatilityDecision.NO_DEMONSTRATED_INCREMENT
    assert "fold 2: no positive improvement" in reason


def test_non_finite_adapter_output_is_rejected(
    planned: tuple[VolatilityProtocol, tuple[VolatilityFoldPlan, ...]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    protocol, plans = planned
    plan = plans[0]

    def fake_fit(**kwargs: object) -> tuple[float, ...]:
        return tuple(float("nan") for _ in range(len(plan.validation)))

    monkeypatch.setattr(volatility_adapter, "fit_ridge", fake_fit)
    with pytest.raises(ValueError, match="finite"):
        evaluate_fold(plan, protocol)


def test_progress_round_trips_and_rejects_a_changed_plan(
    completed: tuple[
        VolatilityProtocol,
        tuple[VolatilityFoldPlan, ...],
        tuple[VolatilityFoldOutcome, ...],
    ],
) -> None:
    protocol, plans, folds = completed
    del protocol
    plan = plans[0]
    progress = progress_from_fold(folds[0])
    restored = fold_from_progress(progress, plan)
    assert restored.result == folds[0].result
    assert restored.predictions == folds[0].predictions
    assert restored.segments == folds[0].segments

    changed = replace(plan, validation_embargo_excluded=plan.validation_embargo_excluded + 1)
    with pytest.raises(VolatilityEvaluationError, match="exclusion counts"):
        fold_from_progress(progress, changed)

    tampered = progress.model_copy(
        update={"result": progress.result.model_copy(update={"train_rows": 0})}
    )
    with pytest.raises(VolatilityEvaluationError, match="training rows"):
        fold_from_progress(tampered, plan)


def test_every_validation_row_has_all_four_models(
    completed: tuple[
        VolatilityProtocol,
        tuple[VolatilityFoldPlan, ...],
        tuple[VolatilityFoldOutcome, ...],
    ],
) -> None:
    protocol, plans, folds = completed
    del protocol
    for fold, plan in zip(folds, plans, strict=True):
        keys = {(item.sample_key, item.model) for item in fold.predictions}
        expected = {
            (row.sample_key, model)
            for row in plan.validation
            for model in (
                VolatilityModel.RIDGE,
                VolatilityModel.PERSISTENCE_5D,
                VolatilityModel.PERSISTENCE_30D,
                VolatilityModel.TRAINING_MEAN,
            )
        }
        assert keys == expected


def test_decision_rejects_an_incomplete_fold_set() -> None:
    with pytest.raises(VolatilityEvaluationError, match="three completed folds"):
        decide((), ())
