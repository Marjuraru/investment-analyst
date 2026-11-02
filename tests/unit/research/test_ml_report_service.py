"""Read-only ML research report projection: integrity, states, cache and cost."""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import threading
import time
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import duckdb
import pytest

from investment_analyst.analytics.analytical_access_models import canonical_analytical_json
from investment_analyst.research import ml_report_service
from investment_analyst.research.ml_export import MlExportConflictError
from investment_analyst.research.ml_export import (
    read_ml_baseline_artifact as _read_ml_baseline_artifact,
)
from investment_analyst.research.ml_models import (
    MANIFEST_FILE_NAME,
    PREDICTIONS_FILE_NAME,
    PROTOCOL_FILE_NAME,
    REPORT_FILE_NAME,
    SELECTION_FILE_NAME,
    STUDY_LIMITATIONS,
    MlArtifactFile,
    MlArtifactStatus,
    MlBaselineManifest,
    MlBaselineProtocol,
    MlBaselineReport,
    MlBaselineSelection,
    MlCalibrationBin,
    MlCalibrationTable,
    MlDecisionCriteria,
    MlDevelopmentResult,
    MlEnvironment,
    MlFoldBoundary,
    MlFoldResult,
    MlHoldoutResult,
    MlHoldoutStatus,
    MlMetricSummary,
    MlModelFamily,
    MlModelState,
    MlReferenceComparison,
    MlScorer,
    MlTimings,
    metric_decimal,
)
from investment_analyst.research.ml_report_models import REPORT_VIEW_SCHEMA_VERSION
from investment_analyst.research.ml_report_service import (
    MAX_CACHE_BYTES,
    MAX_CACHE_ENTRIES,
    MlReportConflictError,
    MlReportNotFoundError,
    MlResearchReportService,
)

_ASSET = "crypto:btc-usd"
_DATASET_ID = UUID("d927c7f1-794a-571c-b729-6bb68d311115")
_DATASET_CONTENT = "1" * 64
_DATASET_MANIFEST = "2" * 64
_DATASET_ROWS = "3" * 64
_CODE_MATERIAL = "4" * 64
_CREATED_AT = datetime(2026, 11, 1, 22, 25, 48, 162976, tzinfo=UTC)
_CRITERIA = MlDecisionCriteria()
_ENVIRONMENT = MlEnvironment(
    python_version="3.12.3",
    scikit_learn_version="1.9.1",
    numpy_version="2.5.4",
    scipy_version="1.18.1",
    threadpoolctl_version="3.7.0",
    duckdb_version="1.5.4",
)
_TIMINGS = MlTimings(
    read_seconds=Decimal("0.1"),
    fit_seconds=Decimal("0.2"),
    evaluation_seconds=Decimal("0.13"),
    total_seconds=Decimal("3.66"),
)


def _sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _write(path: Path, payload: bytes) -> MlArtifactFile:
    path.write_bytes(payload)
    return MlArtifactFile(name=path.name, sha256=_sha(payload), bytes=len(payload))


def _descriptor(path: Path) -> MlArtifactFile:
    payload = path.read_bytes()
    return MlArtifactFile(name=path.name, sha256=_sha(payload), bytes=len(payload))


def _folds() -> tuple[MlFoldBoundary, ...]:
    return (
        MlFoldBoundary(
            fold=1,
            train_end_index=50,
            validation_start_index=50,
            validation_end_index=60,
            validation_start_date=date(2021, 1, 1),
            validation_end_date=date(2021, 3, 1),
        ),
        MlFoldBoundary(
            fold=2,
            train_end_index=60,
            validation_start_index=60,
            validation_end_index=70,
            validation_start_date=date(2021, 3, 1),
            validation_end_date=date(2021, 6, 1),
        ),
        MlFoldBoundary(
            fold=3,
            train_end_index=70,
            validation_start_index=70,
            validation_end_index=80,
            validation_start_date=date(2021, 6, 1),
            validation_end_date=date(2021, 10, 1),
        ),
    )


def _score(
    scorer: MlScorer,
    brier: str,
    *,
    rows: int = 100,
    positives: int = 50,
    negatives: int = 50,
) -> MlMetricSummary:
    return MlMetricSummary(
        scorer=scorer,
        rows=rows,
        positives=positives,
        negatives=negatives,
        brier=Decimal(brier),
        log_loss=Decimal("0.69"),
    )


def _relative(model_brier: Decimal, reference_brier: Decimal) -> Decimal:
    return metric_decimal(float(reference_brier - model_brier) / float(reference_brier))


def _comparison(
    model: MlScorer,
    reference: MlScorer,
    model_brier: str,
    reference_brier: str,
    lower: str,
    *,
    quantile: str = "0.0125",
) -> MlReferenceComparison:
    model_value = Decimal(model_brier)
    reference_value = Decimal(reference_brier)
    relative = _relative(model_value, reference_value)
    return MlReferenceComparison(
        model=model,
        reference=reference,
        model_brier=model_value,
        reference_brier=reference_value,
        absolute_improvement=reference_value - model_value,
        relative_improvement=relative,
        mean_paired_difference=Decimal(lower) / Decimal(2),
        lower_bound=Decimal(lower),
        quantile=Decimal(quantile),
        improved_at_least_five_percent=relative >= Decimal("0.05"),
        lower_bound_positive=Decimal(lower) > 0,
    )


def _bins() -> tuple[MlCalibrationBin, ...]:
    return tuple(
        MlCalibrationBin(
            index=index,
            lower_bound=Decimal(index) / Decimal(10),
            upper_bound=Decimal(index + 1) / Decimal(10),
            includes_upper=index == 9,
            count=10 if index == 5 else 0,
            mean_probability=Decimal("0.55") if index == 5 else None,
            observed_frequency=Decimal("0.53") if index == 5 else None,
        )
        for index in range(10)
    )


def _calibration(scorer: MlScorer) -> MlCalibrationTable:
    return MlCalibrationTable(scorer=scorer, bins=_bins())


def _scores(*, candidate: bool) -> tuple[MlMetricSummary, ...]:
    if candidate:
        return (
            _score(MlScorer.LOGISTIC_REGRESSION, "0.20"),
            _score(MlScorer.HIST_GRADIENT_BOOSTING, "0.27"),
            _score(MlScorer.HALF_PROBABILITY, "0.25"),
            _score(MlScorer.TRAINING_CLASS_PRIOR, "0.22"),
        )
    return (
        _score(MlScorer.LOGISTIC_REGRESSION, "0.249321"),
        _score(MlScorer.HIST_GRADIENT_BOOSTING, "0.251702"),
        _score(MlScorer.HALF_PROBABILITY, "0.25"),
        _score(MlScorer.TRAINING_CLASS_PRIOR, "0.248527"),
    )


def _comparisons(*, candidate: bool, quantile: str = "0.0125") -> tuple[MlReferenceComparison, ...]:
    if candidate:
        return (
            _comparison(
                MlScorer.LOGISTIC_REGRESSION,
                MlScorer.HALF_PROBABILITY,
                "0.20",
                "0.25",
                "0.01",
                quantile=quantile,
            ),
            _comparison(
                MlScorer.LOGISTIC_REGRESSION,
                MlScorer.TRAINING_CLASS_PRIOR,
                "0.20",
                "0.22",
                "0.005",
                quantile=quantile,
            ),
            _comparison(
                MlScorer.HIST_GRADIENT_BOOSTING,
                MlScorer.HALF_PROBABILITY,
                "0.27",
                "0.25",
                "-0.02",
                quantile=quantile,
            ),
            _comparison(
                MlScorer.HIST_GRADIENT_BOOSTING,
                MlScorer.TRAINING_CLASS_PRIOR,
                "0.27",
                "0.22",
                "-0.05",
                quantile=quantile,
            ),
        )
    return (
        _comparison(
            MlScorer.LOGISTIC_REGRESSION,
            MlScorer.HALF_PROBABILITY,
            "0.249321",
            "0.25",
            "-0.006855",
        ),
        _comparison(
            MlScorer.LOGISTIC_REGRESSION,
            MlScorer.TRAINING_CLASS_PRIOR,
            "0.249321",
            "0.248527",
            "-0.003212",
        ),
        _comparison(
            MlScorer.HIST_GRADIENT_BOOSTING,
            MlScorer.HALF_PROBABILITY,
            "0.251702",
            "0.25",
            "-0.016330",
        ),
        _comparison(
            MlScorer.HIST_GRADIENT_BOOSTING,
            MlScorer.TRAINING_CLASS_PRIOR,
            "0.251702",
            "0.248527",
            "-0.016935",
        ),
    )


def _development(
    *,
    decision: MlModelState,
    diagnostics: tuple[str, ...] = (),
) -> MlDevelopmentResult:
    candidate = decision is MlModelState.CANDIDATE_SELECTED
    folds = tuple(
        MlFoldResult(
            fold=fold,
            train_rows=400 + fold * 10,
            train_positives=200,
            train_negatives=200 + fold * 10,
            purged_excluded=10,
            embargo_excluded=10,
            evaluation_rows=100,
            evaluation_positives=50,
            evaluation_negatives=50,
            segments=5,
            scores=_scores(candidate=candidate),
            comparisons=_comparisons(candidate=candidate),
            diagnostics=diagnostics,
        )
        for fold in (1, 2, 3)
    )
    return MlDevelopmentResult(
        decision=decision,
        folds=folds,
        pooled_scores=_scores(candidate=candidate),
        pooled_comparisons=_comparisons(candidate=candidate),
        calibration=(
            _calibration(MlScorer.LOGISTIC_REGRESSION),
            _calibration(MlScorer.HIST_GRADIENT_BOOSTING),
        ),
        selected_model=MlModelFamily.LOGISTIC_REGRESSION if candidate else None,
        selection_reason=(
            "fixture candidate reached the preregistered development criterion"
            if candidate
            else "no candidate reached five percent Brier improvement against both references"
        ),
        development_fits=6,
    )


def _holdout(status: MlHoldoutStatus) -> MlHoldoutResult:
    if status is MlHoldoutStatus.NOT_EVALUATED:
        return MlHoldoutResult(
            status=status,
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
            quantile=Decimal("0.025"),
            holdout_fits=0,
        )
    confirmed = status is MlHoldoutStatus.CONFIRMED_RESEARCH_EDGE
    scores = (
        _score(
            MlScorer.LOGISTIC_REGRESSION,
            "0.20" if confirmed else "0.26",
            rows=120,
            positives=60,
            negatives=60,
        ),
        _score(
            MlScorer.HALF_PROBABILITY,
            "0.25",
            rows=120,
            positives=60,
            negatives=60,
        ),
        _score(
            MlScorer.TRAINING_CLASS_PRIOR,
            "0.22",
            rows=120,
            positives=60,
            negatives=60,
        ),
    )
    comparisons = (
        _comparison(
            MlScorer.LOGISTIC_REGRESSION,
            MlScorer.HALF_PROBABILITY,
            "0.20" if confirmed else "0.26",
            "0.25",
            "0.01" if confirmed else "-0.02",
            quantile="0.025",
        ),
        _comparison(
            MlScorer.LOGISTIC_REGRESSION,
            MlScorer.TRAINING_CLASS_PRIOR,
            "0.20" if confirmed else "0.26",
            "0.22",
            "0.005" if confirmed else "-0.04",
            quantile="0.025",
        ),
    )
    return MlHoldoutResult(
        status=status,
        selected_model=MlModelFamily.LOGISTIC_REGRESSION,
        train_rows=600,
        train_positives=300,
        train_negatives=300,
        purged_excluded=10,
        embargo_excluded=10,
        evaluation_rows=120,
        evaluation_positives=60,
        evaluation_negatives=60,
        segments=5,
        scores=scores,
        comparisons=comparisons,
        calibration=(_calibration(MlScorer.LOGISTIC_REGRESSION),),
        quantile=Decimal("0.025"),
        holdout_fits=1,
    )


def _write_predictions(path: Path, rows: int = 2) -> None:
    if path.exists():
        path.unlink()
    connection = duckdb.connect(":memory:")
    try:
        connection.execute("SET threads=1")
        connection.execute(
            "CREATE TABLE predictions (sample_key VARCHAR, decision_at VARCHAR, stage VARCHAR,"
            " fold INTEGER, scorer VARCHAR, label INTEGER, probability DOUBLE)"
        )
        for index in range(rows):
            connection.execute(
                "INSERT INTO predictions VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    f"sample-{index}",
                    "2026-01-01T12:00:00Z",
                    "development",
                    1,
                    "logistic_regression",
                    index % 2,
                    0.5,
                ],
            )
        escaped = str(path).replace("'", "''")
        connection.execute(f"COPY predictions TO '{escaped}' (FORMAT PARQUET, COMPRESSION ZSTD)")
    finally:
        connection.close()


def _build_artifact(
    root: Path,
    *,
    run_id: UUID | None = None,
    asset_id: str = _ASSET,
    decision: MlModelState = MlModelState.NO_DEMONSTRATED_EDGE,
    holdout_status: MlHoldoutStatus = MlHoldoutStatus.NOT_EVALUATED,
    insufficient: bool = False,
    limitations: tuple[str, ...] = STUDY_LIMITATIONS,
    diagnostic_chunks: int = 0,
    overwrite: bool = False,
) -> Path:
    run_id = run_id or uuid4()
    run_dir = root / str(run_id)
    run_dir.mkdir(parents=True, exist_ok=overwrite)
    protocol = MlBaselineProtocol(
        dataset_id=_DATASET_ID,
        dataset_content_sha256=_DATASET_CONTENT,
        decision_rows=100,
        asset_id=asset_id,
        folds=_folds(),
        limitations=limitations,
    )
    protocol_path = run_dir / PROTOCOL_FILE_NAME
    protocol_descriptor = _write(
        protocol_path,
        canonical_analytical_json(protocol.model_dump(mode="json")),
    )
    protocol_sha = protocol_descriptor.sha256

    diagnostics = tuple(
        f"fold=1 diagnostic={index} " + ("x" * 8000) for index in range(diagnostic_chunks)
    )
    selection_descriptor: MlArtifactFile | None = None
    predictions_descriptor: MlArtifactFile | None = None
    if insufficient:
        report = MlBaselineReport(
            run_id=run_id,
            status=MlArtifactStatus.INSUFFICIENT_EVALUATION_SAMPLE,
            dataset_id=_DATASET_ID,
            dataset_content_sha256=_DATASET_CONTENT,
            dataset_manifest_sha256=_DATASET_MANIFEST,
            protocol_sha256=protocol_sha,
            selection_sha256=None,
            development=None,
            holdout=None,
            insufficiency_reason="fixture sample below the preregistered floors",
            environment=_ENVIRONMENT,
            predictions_sha256=None,
            prediction_rows=0,
            holdout_accesses=0,
            development_fits=0,
            holdout_fits=0,
            timings=_TIMINGS,
            peak_rss_bytes=1024,
        )
    else:
        development = _development(
            decision=decision,
            diagnostics=diagnostics,
        )
        holdout = _holdout(holdout_status)
        selection_sha: str | None = None
        if development.selected_model is not None:
            selection = MlBaselineSelection(
                run_id=run_id,
                dataset_id=_DATASET_ID,
                dataset_content_sha256=_DATASET_CONTENT,
                protocol_sha256=protocol_sha,
                code_material_sha256=_CODE_MATERIAL,
                development=development,
                selected_model=development.selected_model,
                sealed_at=_CREATED_AT,
            )
            selection_descriptor = _write(
                run_dir / SELECTION_FILE_NAME,
                canonical_analytical_json(selection.model_dump(mode="json")),
            )
            selection_sha = selection_descriptor.sha256
        _write_predictions(run_dir / PREDICTIONS_FILE_NAME)
        predictions_descriptor = _descriptor(run_dir / PREDICTIONS_FILE_NAME)
        report = MlBaselineReport(
            run_id=run_id,
            status=MlArtifactStatus.COMPLETED,
            dataset_id=_DATASET_ID,
            dataset_content_sha256=_DATASET_CONTENT,
            dataset_manifest_sha256=_DATASET_MANIFEST,
            protocol_sha256=protocol_sha,
            selection_sha256=selection_sha,
            development=development,
            holdout=holdout,
            insufficiency_reason=None,
            environment=_ENVIRONMENT,
            predictions_sha256=predictions_descriptor.sha256,
            prediction_rows=2,
            holdout_accesses=0 if holdout.status is MlHoldoutStatus.NOT_EVALUATED else 1,
            development_fits=development.development_fits,
            holdout_fits=holdout.holdout_fits,
            timings=_TIMINGS,
            peak_rss_bytes=2048,
        )
    report_descriptor = _write(
        run_dir / REPORT_FILE_NAME,
        canonical_analytical_json(report.model_dump(mode="json")),
    )
    descriptors = [protocol_descriptor, report_descriptor]
    for optional in (selection_descriptor, predictions_descriptor):
        if optional is not None:
            descriptors.append(optional)
    manifest = MlBaselineManifest(
        run_id=run_id,
        status=report.status,
        dataset_id=_DATASET_ID,
        dataset_content_sha256=_DATASET_CONTENT,
        dataset_manifest_sha256=_DATASET_MANIFEST,
        dataset_rows_sha256=_DATASET_ROWS,
        code_material_sha256=_CODE_MATERIAL,
        environment=_ENVIRONMENT,
        protocol=protocol_descriptor,
        selection=selection_descriptor,
        predictions=predictions_descriptor,
        report=report_descriptor,
        root_bytes=sum(item.bytes for item in descriptors),
        created_at=_CREATED_AT,
    )
    _write(
        run_dir / MANIFEST_FILE_NAME,
        canonical_analytical_json(manifest.model_dump(mode="json")),
    )
    return run_dir


@pytest.fixture()
def root(tmp_path: Path) -> Path:
    return tmp_path / "ml-results"


def test_projection_exposes_negative_result_without_inventing_source_id(root: Path) -> None:
    run_id = uuid4()
    _build_artifact(root, run_id=run_id)
    service = MlResearchReportService(root)

    view = service.report(asset_id=_ASSET, run_id=run_id)

    assert view.schema_version == REPORT_VIEW_SCHEMA_VERSION
    assert view.provenance.asset_id == _ASSET
    assert view.provenance.source_id is None
    assert "source_id" in view.provenance.source_id_note
    assert view.artifact.run_id == run_id
    assert view.artifact.status is MlArtifactStatus.COMPLETED
    assert view.artifact.prediction_rows == 2
    assert view.development is not None
    assert view.development.decision is MlModelState.NO_DEMONSTRATED_EDGE
    assert view.development.selected_model is None
    assert view.holdout is not None
    assert view.holdout.status is MlHoldoutStatus.NOT_EVALUATED
    assert view.holdout.scores == ()
    assert view.protocol.strict_pit_verified is False
    assert view.insufficiency_reason is None
    payload = view.to_json_dict()
    scores = payload["development"]["pooled_scores"]  # type: ignore[index]
    assert scores[0]["brier"] == "0.249321"  # type: ignore[index]
    assert payload["provenance"]["source_id"] is None  # type: ignore[index]
    assert "predictions" not in payload


@pytest.mark.parametrize(
    "holdout_status, expected",
    [
        (MlHoldoutStatus.HOLDOUT_NOT_CONFIRMED, MlHoldoutStatus.HOLDOUT_NOT_CONFIRMED),
        (MlHoldoutStatus.CONFIRMED_RESEARCH_EDGE, MlHoldoutStatus.CONFIRMED_RESEARCH_EDGE),
    ],
)
def test_candidate_selection_and_holdout_states_are_represented(
    root: Path, holdout_status: MlHoldoutStatus, expected: MlHoldoutStatus
) -> None:
    run_id = uuid4()
    _build_artifact(
        root,
        run_id=run_id,
        decision=MlModelState.CANDIDATE_SELECTED,
        holdout_status=holdout_status,
    )
    service = MlResearchReportService(root)

    view = service.report(asset_id=_ASSET, run_id=run_id)

    assert view.development is not None
    assert view.development.decision is MlModelState.CANDIDATE_SELECTED
    assert view.development.selected_model is MlModelFamily.LOGISTIC_REGRESSION
    assert view.artifact.selection_sha256 is not None
    assert view.holdout is not None
    assert view.holdout.status is expected


def test_insufficient_artifact_exposes_absence_instead_of_zero_metrics(root: Path) -> None:
    run_id = uuid4()
    _build_artifact(root, run_id=run_id, insufficient=True)
    service = MlResearchReportService(root)

    view = service.report(asset_id=_ASSET, run_id=run_id)

    assert view.artifact.status is MlArtifactStatus.INSUFFICIENT_EVALUATION_SAMPLE
    assert view.development is None
    assert view.holdout is None
    assert view.insufficiency_reason is not None
    assert view.artifact.prediction_rows == 0
    payload = view.to_json_dict()
    assert payload["development"] is None
    assert payload["artifact"]["prediction_rows"] == 0


def test_asset_mismatch_is_rejected_before_report_hydration(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = uuid4()
    _build_artifact(root, run_id=run_id)
    service = MlResearchReportService(root)
    hydrated: list[Path] = []

    def _forbidden(path: Path) -> tuple[object, object]:
        hydrated.append(path)
        raise AssertionError("report must not be hydrated for a foreign asset")

    monkeypatch.setattr(ml_report_service, "read_ml_baseline_artifact", _forbidden)

    with pytest.raises(MlReportConflictError, match="different asset"):
        service.report(asset_id="equity:us:aapl", run_id=run_id)
    assert hydrated == []


def test_missing_root_and_run_are_not_found(root: Path) -> None:
    with pytest.raises(MlReportNotFoundError):
        MlResearchReportService(root).report(asset_id=_ASSET, run_id=uuid4())

    root.mkdir()
    with pytest.raises(MlReportNotFoundError):
        MlResearchReportService(root).report(asset_id=_ASSET, run_id=uuid4())


def test_corrupt_missing_symlink_directory_and_size_mismatch_fail_closed(root: Path) -> None:
    run_id = uuid4()
    run_dir = _build_artifact(root, run_id=run_id)
    report_path = run_dir / REPORT_FILE_NAME
    original_report = report_path.read_bytes()

    report_path.write_bytes(original_report + b"\n")
    with pytest.raises(MlReportConflictError):
        MlResearchReportService(root).report(asset_id=_ASSET, run_id=run_id)
    report_path.write_bytes(original_report)

    manifest_path = run_dir / MANIFEST_FILE_NAME
    manifest_payload = manifest_path.read_bytes()
    manifest = MlBaselineManifest.model_validate_json(manifest_payload)
    false_size = manifest.model_copy(
        update={
            "report": manifest.report.model_copy(update={"bytes": manifest.report.bytes - 1}),
            "root_bytes": manifest.root_bytes - 1,
        }
    )
    manifest_path.write_bytes(canonical_analytical_json(false_size.model_dump(mode="json")))
    with pytest.raises(MlReportConflictError):
        MlResearchReportService(root).report(asset_id=_ASSET, run_id=run_id)
    manifest_path.write_bytes(manifest_payload)

    predictions = run_dir / PREDICTIONS_FILE_NAME
    predictions.unlink()
    with pytest.raises(MlReportConflictError):
        MlResearchReportService(root).report(asset_id=_ASSET, run_id=run_id)
    _write_predictions(predictions)
    manifest_path.write_bytes(manifest_payload)

    unexpected = run_dir / "notes.txt"
    unexpected.write_text("foreign", encoding="utf-8")
    with pytest.raises(MlReportConflictError):
        MlResearchReportService(root).report(asset_id=_ASSET, run_id=run_id)
    unexpected.unlink()

    replaced = run_dir / SELECTION_FILE_NAME
    replaced.mkdir()
    with pytest.raises(MlReportConflictError):
        MlResearchReportService(root).report(asset_id=_ASSET, run_id=run_id)
    replaced.rmdir()

    target = run_dir / REPORT_FILE_NAME
    backup = root / "report-backup"
    shutil.copy2(target, backup)
    target.unlink()
    target.symlink_to(backup)
    with pytest.raises(MlReportConflictError):
        MlResearchReportService(root).report(asset_id=_ASSET, run_id=run_id)


def test_caps_are_enforced_before_report_hydration(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = uuid4()
    run_dir = _build_artifact(root, run_id=run_id)
    report_path = run_dir / REPORT_FILE_NAME
    report_path.write_bytes(b"x" * (ml_report_service.MAX_REPORT_BYTES + 1))
    with pytest.raises(MlReportConflictError):
        MlResearchReportService(root).report(asset_id=_ASSET, run_id=run_id)

    protocol_path = run_dir / PROTOCOL_FILE_NAME
    protocol_path.write_bytes(b"x" * (ml_report_service.MAX_PROTOCOL_BYTES + 1))
    with pytest.raises(MlReportConflictError):
        MlResearchReportService(root).report(asset_id=_ASSET, run_id=run_id)

    run_id = uuid4()
    _build_artifact(root, run_id=run_id)
    monkeypatch.setattr(ml_report_service, "MAX_ARTIFACT_BYTES", 1024)
    with pytest.raises(MlReportConflictError):
        MlResearchReportService(root).report(asset_id=_ASSET, run_id=run_id)


def test_symlinked_root_and_run_are_rejected(root: Path, tmp_path: Path) -> None:
    actual = tmp_path / "actual"
    run_id = uuid4()
    _build_artifact(actual, run_id=run_id)

    linked_root = tmp_path / "linked-root"
    linked_root.symlink_to(actual)
    with pytest.raises(MlReportConflictError):
        MlResearchReportService(linked_root).report(asset_id=_ASSET, run_id=run_id)

    root.mkdir(parents=True, exist_ok=True)
    linked_run = root / str(run_id)
    linked_run.symlink_to(actual / str(run_id))
    with pytest.raises(MlReportConflictError):
        MlResearchReportService(root).report(asset_id=_ASSET, run_id=run_id)


def test_relative_root_is_rejected() -> None:
    with pytest.raises(ValueError, match="absolute"):
        MlResearchReportService(Path("relative"))


def test_cache_reuses_projection_without_full_verification(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = uuid4()
    _build_artifact(root, run_id=run_id)
    service = MlResearchReportService(root)
    calls: list[Path] = []
    original = ml_report_service.read_ml_baseline_artifact

    def _counting(path: Path) -> tuple[object, object]:
        calls.append(path)
        return original(path)

    monkeypatch.setattr(ml_report_service, "read_ml_baseline_artifact", _counting)

    first = service.report(asset_id=_ASSET, run_id=run_id)
    second = service.report(asset_id=_ASSET, run_id=run_id)

    assert first == second
    assert len(calls) == 1
    performance = service.performance()
    assert performance["cache_hits"] == 1
    assert performance["cache_misses"] == 1
    assert performance["full_verifications"] == 1
    assert performance["cold_load_ms"] is not None
    assert performance["warm_load_ms"] is not None
    assert performance["cache_bytes"] > 0


def test_content_change_with_restored_mtime_invalidates_cache(root: Path) -> None:
    run_id = uuid4()
    run_dir = _build_artifact(root, run_id=run_id)
    service = MlResearchReportService(root)
    first = service.report(asset_id=_ASSET, run_id=run_id)
    assert first.development is not None
    assert first.development.folds[0].diagnostics == ()

    stamps = {
        path.name: (path.stat().st_atime_ns, path.stat().st_mtime_ns) for path in run_dir.iterdir()
    }
    second_run = _build_artifact(
        root,
        run_id=run_id,
        diagnostic_chunks=1,
        overwrite=True,
    )
    for path in second_run.iterdir():
        atime_ns, mtime_ns = stamps[path.name]
        os.utime(path, ns=(atime_ns, mtime_ns))

    second = service.report(asset_id=_ASSET, run_id=run_id)

    assert second != first
    assert second.development is not None
    assert second.development.folds[0].diagnostics != ()
    assert service.performance()["full_verifications"] == 2


def test_cache_entry_limit_is_enforced(root: Path) -> None:
    service = MlResearchReportService(root)
    run_ids = [uuid4() for _ in range(MAX_CACHE_ENTRIES + 1)]
    for run_id in run_ids:
        _build_artifact(root, run_id=run_id, insufficient=True)
    for run_id in run_ids:
        service.report(asset_id=_ASSET, run_id=run_id)

    performance = service.performance()
    assert performance["cache_entries"] <= MAX_CACHE_ENTRIES
    assert performance["cache_bytes"] <= MAX_CACHE_BYTES


def test_cache_byte_limit_evicts_large_projections(root: Path) -> None:
    service = MlResearchReportService(root)
    run_ids = [uuid4() for _ in range(4)]
    for run_id in run_ids:
        _build_artifact(root, run_id=run_id, diagnostic_chunks=25)
    for run_id in run_ids:
        service.report(asset_id=_ASSET, run_id=run_id)

    performance = service.performance()
    assert performance["cache_entries"] <= 3
    assert performance["cache_entries"] <= MAX_CACHE_ENTRIES
    assert performance["cache_bytes"] <= MAX_CACHE_BYTES


def test_foreign_corpus_is_never_read(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    run_id = uuid4()
    target = _build_artifact(root, run_id=run_id)
    foreign = []
    for _ in range(10):
        foreign_id = uuid4()
        directory = root / str(foreign_id)
        directory.mkdir()
        (directory / MANIFEST_FILE_NAME).write_text("foreign", encoding="utf-8")
        foreign.append(directory.resolve())
    service = MlResearchReportService(root)
    read_paths: list[Path] = []
    original = MlResearchReportService._read_file

    def _recording(self: MlResearchReportService, path: Path, *, name: str, retain: bool):
        read_paths.append(path.resolve())
        return original(self, path, name=name, retain=retain)

    monkeypatch.setattr(MlResearchReportService, "_read_file", _recording)
    first = service.report(asset_id=_ASSET, run_id=run_id)
    second = service.report(asset_id=_ASSET, run_id=run_id)

    assert first == second
    assert read_paths
    assert all(path.is_relative_to(target.resolve()) for path in read_paths)
    assert not any(path.is_relative_to(item) for path in read_paths for item in foreign)


def test_identical_concurrent_requests_coalesce(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = uuid4()
    _build_artifact(root, run_id=run_id)
    service = MlResearchReportService(root)
    calls: list[Path] = []
    original = ml_report_service.read_ml_baseline_artifact

    def _slow(path: Path) -> tuple[object, object]:
        calls.append(path)
        time.sleep(0.2)
        return original(path)

    monkeypatch.setattr(ml_report_service, "read_ml_baseline_artifact", _slow)
    barrier = threading.Barrier(5)
    results: list[object] = []
    errors: list[BaseException] = []

    def _query() -> None:
        barrier.wait()
        try:
            results.append(service.report(asset_id=_ASSET, run_id=run_id))
        except BaseException as error:  # pragma: no cover - asserted below
            errors.append(error)

    threads = [threading.Thread(target=_query) for _ in range(5)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert not errors
    assert len(results) == 5
    assert all(item == results[0] for item in results)
    assert len(calls) == 1


def test_reader_works_without_ml_dependencies_and_without_writes(root: Path) -> None:
    run_id = uuid4()
    run_dir = _build_artifact(root, run_id=run_id)
    before = {
        path.relative_to(root): (path.stat().st_size, _sha(path.read_bytes()))
        for path in root.rglob("*")
        if path.is_file()
    }
    script = (
        "import importlib.abc, sys\n"
        "class Blocker(importlib.abc.MetaPathFinder):\n"
        "    def find_spec(self, name, path=None, target=None):\n"
        "        if name.split('.')[0] in {'sklearn','numpy','scipy','threadpoolctl'}:\n"
        "            raise ImportError('blocked ' + name)\n"
        "        return None\n"
        "sys.meta_path.insert(0, Blocker())\n"
        "from pathlib import Path\n"
        "from uuid import UUID\n"
        "from investment_analyst.research.ml_report_service import MlResearchReportService\n"
        "service = MlResearchReportService(Path(sys.argv[1]))\n"
        "view = service.report(asset_id='crypto:btc-usd', run_id=UUID(sys.argv[2]))\n"
        "print(view.artifact.status.value, view.development.decision.value)\n"
    )
    environment = {
        **os.environ,
        "PYTHONPATH": str(Path(__file__).resolve().parents[3] / "src"),
    }
    completed = subprocess.run(
        [sys.executable, "-c", script, str(root), str(run_id)],
        capture_output=True,
        text=True,
        env=environment,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.split() == ["completed", "no_demonstrated_edge"]
    after = {
        path.relative_to(root): (path.stat().st_size, _sha(path.read_bytes()))
        for path in root.rglob("*")
        if path.is_file()
    }
    assert after == before
    assert not list(root.rglob("*.duckdb"))
    assert not list(root.rglob("*.wal"))
    assert run_dir.is_dir()


def test_read_ml_baseline_artifact_remains_the_verifier(root: Path) -> None:
    run_id = uuid4()
    run_dir = _build_artifact(root, run_id=run_id)
    manifest, report = _read_ml_baseline_artifact(run_dir)
    assert manifest.run_id == run_id
    assert report.status is MlArtifactStatus.COMPLETED
    with pytest.raises(MlExportConflictError):
        _read_ml_baseline_artifact(root / str(uuid4()))
