"""Strict contracts, identities and decision rules for the funding study."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from investment_analyst.research.ml_funding_models import (
    CALIBRATION_BIN_COUNT,
    CONTROL_SCORER,
    FUNDING_FEATURE_COLUMNS,
    MANIFEST_FILE_NAME,
    PREDICTIONS_FILE_NAME,
    PROTOCOL_FILE_NAME,
    REPORT_FILE_NAME,
    SIMPLE_REFERENCES,
    STUDY_FEATURE_COLUMNS,
    STUDY_WIDTH,
    TECHNICAL_FEATURE_COLUMNS,
    TECHNICAL_WIDTH,
    FundingArtifactStatus,
    FundingCalibrationTable,
    FundingCriteria,
    FundingFoldBoundary,
    FundingFoldResult,
    FundingIncrementDecision,
    FundingManifest,
    FundingMetricSummary,
    FundingProtocol,
    FundingReceipt,
    FundingReferenceComparison,
    FundingReport,
    FundingRunStatus,
    FundingScorer,
    funding_run_identity,
    metric_decimal,
)
from investment_analyst.research.ml_models import (
    MlArtifactFile,
    MlEnvironment,
    MlTimings,
    development_fold_boundaries,
)

_START = date(2020, 1, 1)
_END = date(2025, 12, 25)
_ROWS = 2186
_DATASET_ID = UUID("0f4f4c1e-4a4b-4d4a-8f0a-2b7a3c5d6e7f")
_BUNDLE_ID = UUID("7c9a1b2d-3e4f-4a5b-8c6d-7e8f9a0b1c2d")
_HASH = "a" * 64
_ENVIRONMENT = MlEnvironment(
    python_version="3.12.3",
    scikit_learn_version="1.9.1",
    numpy_version="2.5.4",
    scipy_version="1.18.1",
    threadpoolctl_version="3.7.0",
    duckdb_version="1.5.4",
)


def _folds(rows: int = _ROWS) -> tuple[FundingFoldBoundary, ...]:
    return tuple(
        FundingFoldBoundary(
            fold=index + 1,
            train_end_index=start,
            validation_start_index=start,
            validation_end_index=end,
            validation_start_date=date.fromordinal(_START.toordinal() + start),
            validation_end_date=date.fromordinal(_START.toordinal() + end),
            expected_train_rows=400,
            expected_evaluation_rows=120,
        )
        for index, (start, end) in enumerate(development_fold_boundaries(rows))
    )


def _protocol(**overrides: object) -> FundingProtocol:
    payload: dict[str, object] = {
        "dataset_id": _DATASET_ID,
        "dataset_content_sha256": _HASH,
        "bundle_id": _BUNDLE_ID,
        "bundle_content_sha256": "b" * 64,
        "decision_rows": _ROWS,
        "asset_id": "crypto:btc-usd",
        "decision_start": _START,
        "decision_end": _END,
        "folds": _folds(),
    }
    payload.update(overrides)
    return FundingProtocol.model_validate(payload)


def _metrics(scorer: FundingScorer, *, brier: str = "0.20") -> FundingMetricSummary:
    return FundingMetricSummary(
        scorer=scorer,
        rows=100,
        positives=50,
        negatives=50,
        brier=Decimal(brier),
        log_loss=Decimal("0.60"),
    )


def _comparison(
    reference: FundingScorer,
    *,
    model_brier: str,
    reference_brier: str,
    lower_bound: str,
) -> FundingReferenceComparison:
    model = Decimal(model_brier)
    reference_value = Decimal(reference_brier)
    relative = (
        None
        if reference_value == 0
        else metric_decimal(float(reference_value - model) / float(reference_value))
    )
    improved = relative is not None and relative >= Decimal("0.05")
    lower_positive = Decimal(lower_bound) > 0
    criterion = (
        (reference_value - model > 0 and lower_positive)
        if reference is CONTROL_SCORER
        else (improved and lower_positive)
    )
    return FundingReferenceComparison(
        reference=reference,
        model_brier=model,
        reference_brier=reference_value,
        absolute_improvement=reference_value - model,
        relative_improvement=relative,
        mean_paired_difference=reference_value - model,
        lower_bound=Decimal(lower_bound),
        quantile=Decimal("0.0125"),
        improved_at_least_five_percent=improved,
        lower_bound_positive=lower_positive,
        criterion_satisfied=criterion,
    )


def test_feature_matrix_is_the_technical_control_plus_both_funding_columns() -> None:
    assert TECHNICAL_WIDTH == 3
    assert STUDY_WIDTH == 5
    assert STUDY_FEATURE_COLUMNS == TECHNICAL_FEATURE_COLUMNS + FUNDING_FEATURE_COLUMNS
    assert FUNDING_FEATURE_COLUMNS == ("funding_sum_1h_24h", "funding_sum_1h_168h")
    assert SIMPLE_REFERENCES == (
        FundingScorer.HALF_PROBABILITY,
        FundingScorer.TRAINING_CLASS_PRIOR,
    )


def test_protocol_seals_the_exact_fold_grid_and_closed_holdout() -> None:
    protocol = _protocol()
    assert tuple(item.fold for item in protocol.folds) == (1, 2, 3)
    assert protocol.decision_rows == _ROWS
    assert protocol.holdout_policy == "closed-preregistered-zero-access"
    assert protocol.strict_pit_verified is False
    assert protocol.caps.max_development_fits == 6
    assert protocol.criteria.lower_quantile == Decimal("0.0125")
    with pytest.raises(ValidationError):
        _protocol(extra_field=True)
    with pytest.raises(ValidationError):
        _protocol(feature_columns=TECHNICAL_FEATURE_COLUMNS)
    with pytest.raises(ValidationError):
        _protocol(funding_feature_columns=("funding_sum_1h_24h",))
    with pytest.raises(ValidationError):
        _protocol(decision_rows=_ROWS - 1)
    with pytest.raises(ValidationError):
        _protocol(folds=_folds()[:2])
    with pytest.raises(ValidationError):
        _protocol(folds=(_folds()[0], _folds()[2], _folds()[1]))
    with pytest.raises(ValidationError):
        _protocol(strict_pit_verified=True)
    with pytest.raises(ValidationError):
        _protocol(holdout_policy="open")
    with pytest.raises(ValidationError):
        _protocol(decision_rows=_ROWS + 100)


def test_protocol_rejects_a_fold_outside_the_declared_window() -> None:
    folds = list(_folds())
    folds[0] = FundingFoldBoundary(
        fold=1,
        train_end_index=folds[0].train_end_index,
        validation_start_index=folds[0].validation_start_index,
        validation_end_index=folds[0].validation_end_index,
        validation_start_date=date(2019, 12, 31),
        validation_end_date=folds[0].validation_end_date,
        expected_train_rows=400,
        expected_evaluation_rows=120,
    )
    with pytest.raises(ValidationError):
        _protocol(folds=tuple(folds))


def test_fold_boundary_requires_contiguous_training_and_advancing_dates() -> None:
    with pytest.raises(ValidationError):
        FundingFoldBoundary(
            fold=1,
            train_end_index=10,
            validation_start_index=11,
            validation_end_index=20,
            validation_start_date=_START,
            validation_end_date=date(2020, 1, 21),
            expected_train_rows=10,
            expected_evaluation_rows=10,
        )
    with pytest.raises(ValidationError):
        FundingFoldBoundary(
            fold=1,
            train_end_index=10,
            validation_start_index=10,
            validation_end_index=10,
            validation_start_date=_START,
            validation_end_date=date(2020, 1, 11),
            expected_train_rows=10,
            expected_evaluation_rows=10,
        )
    with pytest.raises(ValidationError):
        FundingFoldBoundary(
            fold=1,
            train_end_index=10,
            validation_start_index=10,
            validation_end_index=20,
            validation_start_date=date(2020, 1, 21),
            validation_end_date=date(2020, 1, 20),
            expected_train_rows=10,
            expected_evaluation_rows=10,
        )


def test_metric_decimal_rejects_non_finite_values() -> None:
    assert metric_decimal(0.5) == Decimal("0.5")
    with pytest.raises(ValueError):
        metric_decimal(float("nan"))
    with pytest.raises(ValueError):
        metric_decimal(float("inf"))


def test_run_identity_is_deterministic_and_binds_every_input() -> None:
    base = {
        "protocol_sha256": "1" * 64,
        "dataset_id": _DATASET_ID,
        "dataset_content_sha256": "2" * 64,
        "bundle_id": _BUNDLE_ID,
        "bundle_content_sha256": "3" * 64,
        "code_material_sha256": "4" * 64,
        "environment": _ENVIRONMENT,
    }
    identity = funding_run_identity(**base)
    assert identity == funding_run_identity(**base)
    assert identity.version == 5
    for field in (
        "protocol_sha256",
        "dataset_content_sha256",
        "bundle_content_sha256",
        "code_material_sha256",
    ):
        changed = dict(base)
        changed[field] = "f" * 64
        assert funding_run_identity(**changed) != identity
    assert funding_run_identity(**base | {"bundle_id": uuid4()}) != identity
    assert (
        funding_run_identity(
            **base | {"environment": _ENVIRONMENT.model_copy(update={"numpy_version": "2.5.5"})}
        )
        != identity
    )
    with pytest.raises(ValueError):
        funding_run_identity(**base | {"protocol_sha256": "short"})


def test_comparison_enforces_the_preregistered_criteria() -> None:
    comparison = _comparison(
        FundingScorer.TRAINING_CLASS_PRIOR,
        model_brier="0.20",
        reference_brier="0.25",
        lower_bound="0.01",
    )
    assert comparison.improved_at_least_five_percent is True
    assert comparison.criterion_satisfied is True
    weak = _comparison(
        FundingScorer.TRAINING_CLASS_PRIOR,
        model_brier="0.249",
        reference_brier="0.25",
        lower_bound="0.01",
    )
    assert weak.improved_at_least_five_percent is False
    assert weak.criterion_satisfied is False
    control = _comparison(
        CONTROL_SCORER,
        model_brier="0.249",
        reference_brier="0.25",
        lower_bound="0.01",
    )
    assert control.criterion_satisfied is True
    with pytest.raises(ValidationError):
        FundingReferenceComparison.model_validate(
            control.model_dump(mode="python") | {"criterion_satisfied": False}
        )
    with pytest.raises(ValidationError):
        FundingReferenceComparison.model_validate(
            comparison.model_dump(mode="python") | {"lower_bound_positive": False}
        )
    with pytest.raises(ValidationError):
        FundingReferenceComparison.model_validate(
            comparison.model_dump(mode="python") | {"reference": FundingScorer.FUNDING_LOGISTIC}
        )


def _fold_result(fold: int = 1) -> FundingFoldResult:
    return FundingFoldResult(
        fold=fold,
        train_rows=400,
        train_positives=200,
        train_negatives=200,
        purged_excluded=0,
        ineligible_excluded=0,
        funding_unavailable_excluded=0,
        embargo_excluded=0,
        evaluation_rows=100,
        evaluation_positives=50,
        evaluation_negatives=50,
        segments=1,
        scores=tuple(
            _metrics(scorer)
            for scorer in (
                FundingScorer.TECHNICAL_LOGISTIC,
                FundingScorer.FUNDING_LOGISTIC,
                FundingScorer.HALF_PROBABILITY,
                FundingScorer.TRAINING_CLASS_PRIOR,
            )
        ),
        comparisons=(
            _comparison(
                CONTROL_SCORER, model_brier="0.2", reference_brier="0.25", lower_bound="0.01"
            ),
            _comparison(
                FundingScorer.HALF_PROBABILITY,
                model_brier="0.2",
                reference_brier="0.25",
                lower_bound="0.01",
            ),
            _comparison(
                FundingScorer.TRAINING_CLASS_PRIOR,
                model_brier="0.2",
                reference_brier="0.25",
                lower_bound="0.01",
            ),
        ),
        calibration=(
            FundingCalibrationTable(
                scorer=FundingScorer.TECHNICAL_LOGISTIC,
                bins=_bins(),
            ),
            FundingCalibrationTable(
                scorer=FundingScorer.FUNDING_LOGISTIC,
                bins=_bins(),
            ),
        ),
    )


def _bins():
    from investment_analyst.research.ml_funding_models import FundingCalibrationBin

    return tuple(
        FundingCalibrationBin(
            index=index,
            lower_bound=Decimal(index) / Decimal(CALIBRATION_BIN_COUNT),
            upper_bound=Decimal(index + 1) / Decimal(CALIBRATION_BIN_COUNT),
            includes_upper=index == CALIBRATION_BIN_COUNT - 1,
            count=0,
            mean_probability=None,
            observed_frequency=None,
        )
        for index in range(CALIBRATION_BIN_COUNT)
    )


def _report(**overrides: object) -> FundingReport:
    payload: dict[str, object] = {
        "run_id": uuid4(),
        "status": FundingArtifactStatus.COMPLETED,
        "protocol_sha256": "1" * 64,
        "dataset_id": _DATASET_ID,
        "dataset_content_sha256": "2" * 64,
        "dataset_manifest_sha256": "3" * 64,
        "bundle_id": _BUNDLE_ID,
        "bundle_content_sha256": "4" * 64,
        "bundle_manifest_sha256": "5" * 64,
        "code_material_sha256": "6" * 64,
        "code_material_files": ("ml_funding_models.py",),
        "environment": _ENVIRONMENT,
        "decision": FundingIncrementDecision.NO_DEMONSTRATED_INCREMENT,
        "folds": (_fold_result(1), _fold_result(2), _fold_result(3)),
        "pooled_scores": tuple(
            _metrics(scorer)
            for scorer in (
                FundingScorer.TECHNICAL_LOGISTIC,
                FundingScorer.FUNDING_LOGISTIC,
                FundingScorer.HALF_PROBABILITY,
                FundingScorer.TRAINING_CLASS_PRIOR,
            )
        ),
        "pooled_comparisons": (
            _comparison(
                CONTROL_SCORER, model_brier="0.2", reference_brier="0.25", lower_bound="0.01"
            ),
            _comparison(
                FundingScorer.HALF_PROBABILITY,
                model_brier="0.2",
                reference_brier="0.25",
                lower_bound="0.01",
            ),
            _comparison(
                FundingScorer.TRAINING_CLASS_PRIOR,
                model_brier="0.2",
                reference_brier="0.25",
                lower_bound="0.01",
            ),
        ),
        "calibration": (
            FundingCalibrationTable(scorer=FundingScorer.TECHNICAL_LOGISTIC, bins=_bins()),
            FundingCalibrationTable(scorer=FundingScorer.FUNDING_LOGISTIC, bins=_bins()),
        ),
        "disposition_reason": None,
        "prediction_rows": 1200,
        "development_fits": 6,
        "timings": MlTimings(
            read_seconds=Decimal("0.1"),
            fit_seconds=Decimal("0.2"),
            evaluation_seconds=Decimal("0.3"),
            total_seconds=Decimal("0.6"),
        ),
    }
    payload.update(overrides)
    return FundingReport.model_validate(payload)


def test_report_distinguishes_completed_from_nonevaluable_runs() -> None:
    report = _report()
    assert report.holdout.accesses == 0
    assert report.holdout.status.value == "not_evaluated"
    assert report.workspace_opened is False
    assert report.provider_requests == 0
    with pytest.raises(ValidationError):
        _report(decision=None)
    with pytest.raises(ValidationError):
        _report(folds=())
    with pytest.raises(ValidationError):
        _report(development_fits=5)
    with pytest.raises(ValidationError):
        _report(unknown_field=True)
    failed = _report(
        status=FundingArtifactStatus.INSUFFICIENT_SAMPLE,
        decision=None,
        folds=(),
        pooled_scores=(),
        pooled_comparisons=(),
        calibration=(),
        disposition_reason="fold 1 is below the floor",
        prediction_rows=0,
        development_fits=0,
    )
    assert failed.decision is None
    with pytest.raises(ValidationError):
        _report(
            status=FundingArtifactStatus.ERROR,
            decision=FundingIncrementDecision.NO_DEMONSTRATED_INCREMENT,
            disposition_reason="boom",
        )
    with pytest.raises(ValidationError):
        _report(status=FundingArtifactStatus.ERROR)


def test_manifest_binds_files_sizes_and_the_closed_criteria() -> None:
    manifest = FundingManifest(
        run_id=uuid4(),
        dataset_id=_DATASET_ID,
        dataset_content_sha256="2" * 64,
        dataset_manifest_sha256="3" * 64,
        bundle_id=_BUNDLE_ID,
        bundle_content_sha256="4" * 64,
        bundle_manifest_sha256="5" * 64,
        code_material_sha256="6" * 64,
        code_material_files=("ml_funding_models.py",),
        environment=_ENVIRONMENT,
        criteria=FundingCriteria(),
        protocol=MlArtifactFile(name=PROTOCOL_FILE_NAME, sha256="7" * 64, bytes=100),
        predictions=MlArtifactFile(name=PREDICTIONS_FILE_NAME, sha256="8" * 64, bytes=200),
        report=MlArtifactFile(name=REPORT_FILE_NAME, sha256="9" * 64, bytes=300),
        root_bytes=600,
        created_at="2026-11-03T00:00:00Z",
    )
    assert manifest.status is FundingArtifactStatus.COMPLETED
    with pytest.raises(ValidationError):
        FundingManifest.model_validate(manifest.model_dump(mode="python") | {"root_bytes": 601})
    with pytest.raises(ValidationError):
        FundingManifest.model_validate(
            manifest.model_dump(mode="python")
            | {"protocol": {"name": MANIFEST_FILE_NAME, "sha256": "7" * 64, "bytes": 100}}
        )


def test_receipt_projection_is_stable_and_holdout_is_zero() -> None:
    receipt = FundingReceipt(
        run_id=uuid4(),
        status=FundingRunStatus.REUSED,
        report_status=FundingArtifactStatus.COMPLETED,
        decision=FundingIncrementDecision.NO_DEMONSTRATED_INCREMENT,
        holdout_status="not_evaluated",
        dataset_id=_DATASET_ID,
        dataset_content_sha256="2" * 64,
        bundle_id=_BUNDLE_ID,
        bundle_content_sha256="4" * 64,
        output_dir="/tmp/output",
        artifact_dir="/tmp/output/run",
        protocol_sha256="1" * 64,
        report_sha256="3" * 64,
        manifest_sha256="5" * 64,
        prediction_rows=1200,
        development_fits=6,
        artifact_bytes=600,
        elapsed_seconds=Decimal("0.5"),
    )
    payload = receipt.to_json_dict()
    assert payload["holdout_accesses"] == 0
    assert payload["status"] == "reused"
    with pytest.raises(ValidationError):
        FundingReceipt.model_validate(receipt.model_dump(mode="python") | {"holdout_accesses": 1})


def test_timezone_aware_dates_and_utc_only_values() -> None:
    assert _ENVIRONMENT.python_version == "3.12.3"
    with pytest.raises(ValidationError):
        FundingManifest(
            run_id=uuid4(),
            dataset_id=_DATASET_ID,
            dataset_content_sha256="2" * 64,
            dataset_manifest_sha256="3" * 64,
            bundle_id=_BUNDLE_ID,
            bundle_content_sha256="4" * 64,
            bundle_manifest_sha256="5" * 64,
            code_material_sha256="6" * 64,
            code_material_files=("ml_funding_models.py",),
            environment=_ENVIRONMENT,
            protocol=MlArtifactFile(name=PROTOCOL_FILE_NAME, sha256="7" * 64, bytes=100),
            predictions=MlArtifactFile(name=PREDICTIONS_FILE_NAME, sha256="8" * 64, bytes=200),
            report=MlArtifactFile(name=REPORT_FILE_NAME, sha256="9" * 64, bytes=300),
            root_bytes=600,
            created_at="2026-11-03T00:00:00",
        )
