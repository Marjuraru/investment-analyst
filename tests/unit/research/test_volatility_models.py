"""Focused checks for the preregistered volatility contracts and identities."""

from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID

import pytest
from pydantic import ValidationError

from investment_analyst.research.ml_models import MlEnvironment
from investment_analyst.research.volatility_models import (
    ARCHIVE_REQUEST_END,
    ARCHIVE_REQUEST_START,
    ASSET_ID,
    DECIMAL_PRECISION,
    DECISION_COUNT,
    DECISION_END,
    DECISION_START,
    EPSILON,
    FEATURE_WINDOWS,
    HYPOTHESIS,
    ONE_SIDED_QUANTILE,
    PREREGISTERED_FOLDS,
    REFERENCE_MODELS,
    REFERENCE_WINDOW,
    RESERVED_START_DATE,
    RESERVED_START_INDEX,
    SOURCE_ID,
    STUDY_LIMITATIONS,
    TARGET_HORIZON,
    VolatilityComparison,
    VolatilityCriteria,
    VolatilityHoldoutPolicy,
    VolatilityModel,
    VolatilityModelParameters,
    VolatilityPredictionRecord,
    VolatilityProtocol,
    VolatilityResampling,
    decision_dates,
    decision_instant,
    feature_available_instant,
    fold_label_cut,
    label_maturity_instant,
    reserved_label_cut,
    volatility_row_id,
    volatility_run_id,
    volatility_sample_key,
)

_REVIEWED_AT = datetime(2026, 11, 1, 2, 37, 32, tzinfo=UTC)


def _protocol(**overrides: object) -> VolatilityProtocol:
    payload: dict[str, object] = {
        "asset_id": ASSET_ID,
        "source_id": SOURCE_ID,
        "archive_id": UUID("00000000-0000-4000-8000-000000000001"),
        "archive_semantic_sha256": "a" * 64,
        "archive_candles_sha256": "b" * 64,
        "archive_retrieved_at": _REVIEWED_AT,
        "archive_request_start": ARCHIVE_REQUEST_START,
        "archive_request_end": ARCHIVE_REQUEST_END,
        "study_cut_at": _REVIEWED_AT,
        "decision_start": DECISION_START,
        "decision_end": DECISION_END,
        "decision_count": DECISION_COUNT,
        "reserved_start_index": RESERVED_START_INDEX,
        "reserved_start_date": RESERVED_START_DATE,
        "folds": PREREGISTERED_FOLDS,
        "feature_windows": FEATURE_WINDOWS,
        "reference_window": REFERENCE_WINDOW,
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


def _environment(version: str = "1.9.1") -> MlEnvironment:
    return MlEnvironment(
        python_version="3.12.0",
        scikit_learn_version=version,
        numpy_version="2.5.4",
        scipy_version="1.18.1",
        threadpoolctl_version="3.7.0",
        duckdb_version="1.4.3",
    )


def test_protocol_preregisters_folds_windows_and_criteria() -> None:
    protocol = _protocol()

    assert protocol.asset_id == ASSET_ID
    assert protocol.source_id == SOURCE_ID
    assert protocol.decision_start == date(2020, 1, 1)
    assert protocol.decision_end == date(2025, 12, 25)
    assert protocol.decision_count == 2186
    assert protocol.reserved_start_index == 1748
    assert protocol.reserved_start_date == date(2024, 10, 14)
    assert protocol.feature_windows == (1, 7, 30)
    assert protocol.reference_window == 5
    assert protocol.target_horizon == 5
    assert protocol.epsilon == Decimal("1e-12")
    assert protocol.decimal_precision == 34
    assert protocol.references == (
        VolatilityModel.PERSISTENCE_5D,
        VolatilityModel.PERSISTENCE_30D,
        VolatilityModel.TRAINING_MEAN,
    )
    assert protocol.criteria.primary_metric == "mse"
    assert protocol.criteria.secondary_metric == "mae"
    assert protocol.criteria.quantile == ONE_SIDED_QUANTILE
    assert protocol.criteria.minimum_relative_improvement == Decimal("0.05")
    assert protocol.resampling.replicates == 2000
    assert protocol.resampling.block_length == 20
    assert protocol.resampling.seed == 1729
    assert protocol.holdout.holdout_fits == 0
    assert protocol.holdout.target_evaluations_reserved == 0
    assert protocol.holdout.promoted is False
    assert protocol.strict_pit_verified is False
    assert [
        (fold.validation_start_index, fold.validation_end_index) for fold in protocol.folds
    ] == [
        (1093, 1311),
        (1311, 1529),
        (1529, 1748),
    ]
    assert [fold.validation_start_date for fold in protocol.folds] == [
        date(2022, 12, 29),
        date(2023, 8, 4),
        date(2024, 3, 9),
    ]


def test_protocol_is_frozen() -> None:
    protocol = _protocol()
    with pytest.raises(ValidationError):
        protocol.epsilon = Decimal("1e-11")


@pytest.mark.parametrize(
    "overrides",
    [
        {"epsilon": Decimal("1e-11")},
        {"feature_windows": (1, 5, 30)},
        {"reference_window": 7},
        {"target_horizon": 6},
        {"decision_count": 2185},
        {"reserved_start_index": 1747},
        {"references": (VolatilityModel.PERSISTENCE_30D, VolatilityModel.PERSISTENCE_5D)},
        {"minimum_train_rows": 100},
        {"study_cut_at": datetime(2026, 10, 31, tzinfo=UTC)},
        {"asset_id": "crypto:eth-usd"},
    ],
)
def test_protocol_rejects_any_changed_preregistration(overrides: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        _protocol(**overrides)


def test_protocol_decimal_fields_reject_floats() -> None:
    with pytest.raises(ValidationError):
        _protocol(epsilon=1e-12)
    with pytest.raises(ValidationError):
        VolatilityCriteria(minimum_relative_improvement=0.05)


def test_time_helpers_apply_utc_and_the_declared_maturity() -> None:
    day = date(2024, 1, 1)

    assert decision_instant(day) == datetime(2024, 1, 1, 12, 0, tzinfo=UTC)
    assert feature_available_instant(day) == datetime(2024, 1, 1, 1, 0, tzinfo=UTC)
    assert label_maturity_instant(day) == datetime(2024, 1, 7, 1, 0, tzinfo=UTC)
    assert fold_label_cut(date(2024, 3, 9)) == datetime(2024, 3, 4, tzinfo=UTC)
    assert reserved_label_cut() == datetime(2024, 10, 9, tzinfo=UTC)
    dates = decision_dates()
    assert len(dates) == 2186
    assert dates[0] == date(2020, 1, 1)
    assert dates[-1] == date(2025, 12, 25)
    assert dates[RESERVED_START_INDEX - 1] == date(2024, 10, 13)
    assert dates[RESERVED_START_INDEX] == date(2024, 10, 14)


def test_identities_are_stable_and_sensitive_to_evidence() -> None:
    decision = datetime(2024, 1, 1, 12, 0, tzinfo=UTC)
    sample = volatility_sample_key(asset_id=ASSET_ID, source_id=SOURCE_ID, decision_at=decision)
    assert sample == volatility_sample_key(
        asset_id=ASSET_ID, source_id=SOURCE_ID, decision_at=decision
    )
    assert sample != volatility_sample_key(
        asset_id=ASSET_ID,
        source_id=SOURCE_ID,
        decision_at=datetime(2024, 1, 2, 12, 0, tzinfo=UTC),
    )
    bar_a = UUID("00000000-0000-4000-8000-000000000010")
    bar_b = UUID("00000000-0000-4000-8000-000000000011")
    row = volatility_row_id(
        sample_key=sample,
        feature_hash="c" * 64,
        target_hash="d" * 64,
        feature_bar_ids=(bar_a,),
        target_bar_ids=(bar_b,),
    )
    assert row == volatility_row_id(
        sample_key=sample,
        feature_hash="c" * 64,
        target_hash="d" * 64,
        feature_bar_ids=(bar_a,),
        target_bar_ids=(bar_b,),
    )
    assert row != volatility_row_id(
        sample_key=sample,
        feature_hash="e" * 64,
        target_hash="d" * 64,
        feature_bar_ids=(bar_a,),
        target_bar_ids=(bar_b,),
    )
    assert row != volatility_row_id(
        sample_key=sample,
        feature_hash="c" * 64,
        target_hash="d" * 64,
        feature_bar_ids=(bar_b,),
        target_bar_ids=(bar_b,),
    )
    archive_id = UUID("00000000-0000-4000-8000-000000000020")
    run = volatility_run_id(
        protocol_sha256="f" * 64,
        archive_id=archive_id,
        archive_semantic_sha256="1" * 64,
        code_material_sha256="2" * 64,
        environment=_environment(),
    )
    assert run == volatility_run_id(
        protocol_sha256="f" * 64,
        archive_id=archive_id,
        archive_semantic_sha256="1" * 64,
        code_material_sha256="2" * 64,
        environment=_environment(),
    )
    assert run != volatility_run_id(
        protocol_sha256="f" * 64,
        archive_id=archive_id,
        archive_semantic_sha256="1" * 64,
        code_material_sha256="3" * 64,
        environment=_environment(),
    )
    assert run != volatility_run_id(
        protocol_sha256="f" * 64,
        archive_id=archive_id,
        archive_semantic_sha256="1" * 64,
        code_material_sha256="2" * 64,
        environment=_environment(version="1.9.2"),
    )


def test_comparison_recomputes_relative_improvement_and_admissibility() -> None:
    with pytest.raises(ValidationError):
        VolatilityComparison(
            reference=VolatilityModel.PERSISTENCE_5D,
            fold=1,
            model_mse=Decimal("1"),
            reference_mse=Decimal("2"),
            absolute_improvement=Decimal("1"),
            relative_improvement=Decimal("0.4"),
            admissible=True,
            improved_at_least_five_percent=True,
        )
    zero = VolatilityComparison(
        reference=VolatilityModel.PERSISTENCE_30D,
        fold=1,
        model_mse=Decimal("1"),
        reference_mse=Decimal("0"),
        absolute_improvement=Decimal("-1"),
        relative_improvement=None,
        admissible=False,
        improved_at_least_five_percent=False,
    )
    assert zero.admissible is False
    with pytest.raises(ValidationError):
        VolatilityComparison(
            reference=VolatilityModel.PERSISTENCE_5D,
            fold=None,
            model_mse=Decimal("1"),
            reference_mse=Decimal("2"),
            absolute_improvement=Decimal("1"),
            relative_improvement=Decimal("0.5"),
            admissible=True,
            improved_at_least_five_percent=True,
        )


def test_prediction_record_recomputes_both_losses() -> None:
    sample = volatility_sample_key(
        asset_id=ASSET_ID,
        source_id=SOURCE_ID,
        decision_at=datetime(2024, 1, 1, 12, 0, tzinfo=UTC),
    )
    row = UUID("00000000-0000-4000-8000-000000000030")
    valid = VolatilityPredictionRecord(
        sample_key=sample,
        row_id=row,
        decision_at=datetime(2024, 1, 1, 12, 0, tzinfo=UTC),
        fold=1,
        model=VolatilityModel.RIDGE,
        target=Decimal("0.5"),
        estimate=Decimal("0.25"),
        squared_error=Decimal("0.0625"),
        absolute_error=Decimal("0.25"),
    )
    assert valid.squared_error == Decimal("0.0625")
    with pytest.raises(ValidationError):
        VolatilityPredictionRecord(
            sample_key=sample,
            row_id=row,
            decision_at=datetime(2024, 1, 1, 12, 0, tzinfo=UTC),
            fold=1,
            model=VolatilityModel.RIDGE,
            target=Decimal("0.5"),
            estimate=Decimal("0.25"),
            squared_error=Decimal("0.0626"),
            absolute_error=Decimal("0.25"),
        )
    with pytest.raises(ValidationError):
        VolatilityPredictionRecord(
            sample_key=sample,
            row_id=row,
            decision_at=datetime(2024, 1, 1, 12, 0, tzinfo=UTC),
            fold=1,
            model=VolatilityModel.RIDGE,
            target=0.5,
            estimate=Decimal("0.25"),
            squared_error=Decimal("0.0625"),
            absolute_error=Decimal("0.25"),
        )


def test_decimal_precision_constant_is_thirty_four() -> None:
    assert DECIMAL_PRECISION == 34
