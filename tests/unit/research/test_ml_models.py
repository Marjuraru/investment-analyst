"""Strict contracts, deterministic identity and core-only import boundaries."""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import pytest
from pydantic import ValidationError

from investment_analyst.research import ml_models
from investment_analyst.research.ml_models import (
    CALIBRATION_BIN_COUNT,
    DATASET_MANIFEST_SCHEMA_VERSION,
    DECISION_END,
    DECISION_START,
    EVALUATION_SCHEMA_VERSION,
    HYPOTHESIS,
    MANIFEST_SCHEMA_VERSION,
    PROTOCOL_ID,
    PROTOCOL_SCHEMA_VERSION,
    SELECTION_SCHEMA_VERSION,
    MlArtifactFile,
    MlArtifactStatus,
    MlBaselineManifest,
    MlBaselineProtocol,
    MlBaselineReport,
    MlCalibrationBin,
    MlEnvironment,
    MlFoldBoundary,
    MlLogisticParameters,
    MlTimings,
    development_fold_boundaries,
    metric_decimal,
    run_identity,
)

ROOT = Path(__file__).resolve().parents[3]
_DATASET_ID = UUID("0f4f4c1e-4a4b-4d4a-8f0a-2b7a3c5d6e7f")


def _fold_boundaries(row_count: int = 1200) -> tuple[MlFoldBoundary, ...]:
    start = date(2020, 1, 1)
    return tuple(
        MlFoldBoundary(
            fold=number + 1,
            train_end_index=first,
            validation_start_index=first,
            validation_end_index=second,
            validation_start_date=start + timedelta(days=first),
            validation_end_date=start + timedelta(days=second),
        )
        for number, (first, second) in enumerate(development_fold_boundaries(row_count))
    )


def _protocol(row_count: int = 1200) -> MlBaselineProtocol:
    return MlBaselineProtocol(
        dataset_id=_DATASET_ID,
        dataset_content_sha256="a" * 64,
        decision_rows=row_count,
        asset_id="crypto:btc-usd",
        folds=_fold_boundaries(row_count),
    )


def _environment(scikit_learn_version: str = "1.9.1") -> MlEnvironment:
    return MlEnvironment(
        python_version="3.12.3",
        scikit_learn_version=scikit_learn_version,
        numpy_version="2.5.4",
        scipy_version="1.18.1",
        threadpoolctl_version="3.7.0",
        duckdb_version="1.5.4",
    )


def test_fold_boundaries_preserve_the_sealed_walk_forward_windows() -> None:
    assert development_fold_boundaries(2186) == ((1093, 1311), (1311, 1529), (1529, 1748))
    assert development_fold_boundaries(1200) == ((600, 720), (720, 840), (840, 960))


@pytest.mark.parametrize("row_count", [0, -1, 4, 5])
def test_fold_boundaries_reject_unusable_row_counts(row_count: int) -> None:
    with pytest.raises(ValueError):
        development_fold_boundaries(row_count)


def test_protocol_pins_every_preregistered_value() -> None:
    protocol = _protocol()
    assert protocol.schema_version == PROTOCOL_SCHEMA_VERSION == "ml-baseline-protocol-v1"
    assert protocol.protocol_id == PROTOCOL_ID
    assert protocol.hypothesis == HYPOTHESIS
    assert protocol.dataset_schema_version == DATASET_MANIFEST_SCHEMA_VERSION
    assert protocol.target.horizon_intervals == 5
    assert protocol.target.positive_rule == "return_5d > 0"
    assert protocol.feature_columns == (
        "close_return_1d",
        "rsi_14_fraction",
        "macd_12_26_9_histogram_close_ratio",
    )
    assert protocol.decision_start == DECISION_START == date(2020, 1, 1)
    assert protocol.decision_end == DECISION_END == date(2025, 12, 25)
    assert protocol.strict_pit_verified is False
    assert protocol.availability_basis == "historical_reconstruction_assumed"
    assert protocol.dtype == "float64"
    assert protocol.blas_threads == 1
    assert protocol.resampling.block_length == 20
    assert protocol.resampling.replicates == 2000
    assert protocol.resampling.seed == 1729
    assert protocol.resampling.generator == "PCG64"
    assert protocol.criteria.min_relative_brier_improvement == Decimal("0.05")
    assert protocol.criteria.development_lower_quantile == Decimal("0.0125")
    assert protocol.criteria.holdout_lower_quantile == Decimal("0.025")
    assert protocol.criteria.tie_tolerance == Decimal("1e-12")
    assert protocol.caps.max_development_fits == 6
    assert protocol.caps.max_holdout_refits == 1
    assert protocol.caps.min_train_rows == 300
    assert protocol.caps.min_evaluation_rows == 100
    assert protocol.caps.min_holdout_rows == 40
    assert protocol.logistic == MlLogisticParameters()
    assert protocol.limitations


def test_protocol_rejects_unknown_fields_and_foreign_digests() -> None:
    with pytest.raises(ValidationError):
        MlBaselineProtocol(
            dataset_id=_DATASET_ID,
            dataset_content_sha256="a" * 64,
            decision_rows=1200,
            asset_id="crypto:btc-usd",
            folds=_fold_boundaries(),
            unexpected=True,
        )
    with pytest.raises(ValidationError):
        MlBaselineProtocol(
            dataset_id=_DATASET_ID,
            dataset_content_sha256="A" * 64,
            decision_rows=1200,
            asset_id="crypto:btc-usd",
            folds=_fold_boundaries(),
        )
    with pytest.raises(ValidationError):
        MlBaselineProtocol(
            dataset_id=_DATASET_ID,
            dataset_content_sha256="a" * 64,
            decision_rows=2201,
            asset_id="crypto:btc-usd",
            folds=_fold_boundaries(),
        )


def test_protocol_rejects_foreign_features_dates_and_windows() -> None:
    with pytest.raises(ValidationError):
        MlBaselineProtocol(
            dataset_id=_DATASET_ID,
            dataset_content_sha256="a" * 64,
            decision_rows=1200,
            asset_id="crypto:btc-usd",
            feature_columns=("close_return_1d", "rsi_14_fraction"),
            folds=_fold_boundaries(),
        )
    with pytest.raises(ValidationError):
        MlBaselineProtocol(
            dataset_id=_DATASET_ID,
            dataset_content_sha256="a" * 64,
            decision_rows=1200,
            asset_id="crypto:btc-usd",
            decision_end=date(2026, 1, 1),
            folds=_fold_boundaries(),
        )
    folds = list(_fold_boundaries())
    folds[1] = folds[1].model_copy(update={"train_end_index": 721, "validation_start_index": 721})
    with pytest.raises(ValidationError):
        MlBaselineProtocol(
            dataset_id=_DATASET_ID,
            dataset_content_sha256="a" * 64,
            decision_rows=1200,
            asset_id="crypto:btc-usd",
            folds=tuple(folds),
        )


def test_run_identity_binds_protocol_dataset_code_and_versions() -> None:
    base = dict(
        protocol_sha256="1" * 64,
        dataset_id=_DATASET_ID,
        dataset_content_sha256="2" * 64,
        code_material_sha256="3" * 64,
        environment=_environment(),
    )
    first = run_identity(**base)
    assert first == run_identity(**base)
    assert first != run_identity(**{**base, "environment": _environment("1.8.0")})
    assert first != run_identity(**{**base, "protocol_sha256": "4" * 64})
    assert first != run_identity(**{**base, "code_material_sha256": "5" * 64})
    assert first.version == 5


def test_run_identity_rejects_incomplete_digests() -> None:
    with pytest.raises(ValueError):
        run_identity(
            protocol_sha256="short",
            dataset_id=_DATASET_ID,
            dataset_content_sha256="2" * 64,
            code_material_sha256="3" * 64,
            environment=_environment(),
        )


def test_metric_decimal_is_finite_and_deterministic() -> None:
    assert metric_decimal(0.1) == Decimal("0.1")
    assert metric_decimal(0.0) == Decimal("0")
    with pytest.raises(ValueError):
        metric_decimal(float("nan"))
    with pytest.raises(ValueError):
        metric_decimal(float("inf"))


def test_calibration_bins_require_fixed_edges_and_explicit_empties() -> None:
    empty = MlCalibrationBin(
        index=0,
        lower_bound=Decimal("0"),
        upper_bound=Decimal("0.1"),
        includes_upper=False,
        count=0,
        mean_probability=None,
        observed_frequency=None,
    )
    assert empty.count == 0
    with pytest.raises(ValidationError):
        MlCalibrationBin(
            index=3,
            lower_bound=Decimal("0.2"),
            upper_bound=Decimal("0.4"),
            includes_upper=False,
            count=0,
            mean_probability=None,
            observed_frequency=None,
        )
    with pytest.raises(ValidationError):
        MlCalibrationBin(
            index=4,
            lower_bound=Decimal("0.4"),
            upper_bound=Decimal("0.5"),
            includes_upper=True,
            count=1,
            mean_probability=Decimal("0.42"),
            observed_frequency=Decimal("1"),
        )
    with pytest.raises(ValidationError):
        MlCalibrationBin(
            index=CALIBRATION_BIN_COUNT - 1,
            lower_bound=Decimal("0.9"),
            upper_bound=Decimal("1"),
            includes_upper=True,
            count=0,
            mean_probability=Decimal("0.95"),
            observed_frequency=None,
        )


def _insufficient_report() -> MlBaselineReport:
    return MlBaselineReport(
        run_id=UUID("11111111-1111-4111-8111-111111111111"),
        status=MlArtifactStatus.INSUFFICIENT_EVALUATION_SAMPLE,
        dataset_id=_DATASET_ID,
        dataset_content_sha256="a" * 64,
        dataset_manifest_sha256="b" * 64,
        protocol_sha256="c" * 64,
        selection_sha256=None,
        development=None,
        holdout=None,
        insufficiency_reason="fold 1 evaluation rows below the declared floor",
        environment=_environment(),
        predictions_sha256=None,
        prediction_rows=0,
        holdout_accesses=0,
        development_fits=0,
        holdout_fits=0,
        timings=MlTimings(
            read_seconds=Decimal("0.1"),
            fit_seconds=Decimal("0"),
            evaluation_seconds=Decimal("0"),
            total_seconds=Decimal("0.1"),
        ),
    )


def test_insufficient_report_never_pretends_to_be_complete() -> None:
    report = _insufficient_report()
    assert report.status is MlArtifactStatus.INSUFFICIENT_EVALUATION_SAMPLE
    with pytest.raises(ValidationError):
        MlBaselineReport(
            **{
                **report.model_dump(),
                "status": MlArtifactStatus.COMPLETED,
                "insufficiency_reason": None,
                "predictions_sha256": "d" * 64,
                "prediction_rows": 1,
            }
        )
    with pytest.raises(ValidationError):
        MlBaselineReport(**{**report.model_dump(), "holdout_accesses": 1})


def test_manifest_counts_every_sealed_byte() -> None:
    manifest = MlBaselineManifest(
        run_id=UUID("22222222-2222-4222-8222-222222222222"),
        status=MlArtifactStatus.INSUFFICIENT_EVALUATION_SAMPLE,
        dataset_id=_DATASET_ID,
        dataset_content_sha256="a" * 64,
        dataset_manifest_sha256="b" * 64,
        dataset_rows_sha256="c" * 64,
        code_material_sha256="d" * 64,
        environment=_environment(),
        protocol=MlArtifactFile(name="protocol.json", sha256="e" * 64, bytes=10),
        selection=None,
        predictions=None,
        report=MlArtifactFile(name="report.json", sha256="f" * 64, bytes=20),
        root_bytes=30,
        created_at=datetime(2026, 11, 1, 12, 0, tzinfo=UTC),
    )
    assert manifest.root_bytes == 30
    with pytest.raises(ValidationError):
        MlBaselineManifest(**{**manifest.model_dump(), "root_bytes": 31})
    with pytest.raises(ValidationError):
        MlBaselineManifest(
            **{
                **manifest.model_dump(),
                "predictions": MlArtifactFile(name="predictions.parquet", sha256="e" * 64, bytes=5),
            }
        )


def test_schema_identifiers_are_stable() -> None:
    assert SELECTION_SCHEMA_VERSION == "ml-baseline-selection-v1"
    assert EVALUATION_SCHEMA_VERSION == "ml-baseline-evaluation-v1"
    assert MANIFEST_SCHEMA_VERSION == "ml-baseline-manifest-v1"
    assert ml_models.ML_NAMESPACE.version == 5


def test_contract_module_imports_without_ml_dependencies() -> None:
    script = (
        "import sys\n"
        "from investment_analyst.research import ml_models\n"
        "assert 'numpy' not in sys.modules, 'numpy imported'\n"
        "assert 'sklearn' not in sys.modules, 'sklearn imported'\n"
        "print('ok')\n"
    )
    environment = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
    completed = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        env=environment,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "ok"
