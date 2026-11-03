"""Focused contract tests for research-experiment discovery and verification.

Every fixture is synthetic and written into ``tmp_path``; no operational
workspace, provider, training call or ML dependency is exercised.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import duckdb
import pytest

from investment_analyst.analytics.analytical_access_models import canonical_analytical_json
from investment_analyst.research.experiment_report_models import (
    MAX_FUNDING_CACHE_ENTRIES,
    ExperimentArtifactStatus,
    ExperimentFamily,
    ExperimentFamilyFilter,
    ExperimentListPage,
    ExperimentReportView,
)
from investment_analyst.research.experiment_report_service import (
    ExperimentCursorStaleError,
    ExperimentReportCapacityError,
    ExperimentReportConflictError,
    ExperimentReportNotFoundError,
    ExperimentReportService,
)
from investment_analyst.research.ml_funding_models import (
    ALL_SCORERS,
    CALIBRATION_BIN_COUNT,
    CONTROL_SCORER,
    FUNDING_FEATURE_COLUMNS,
    LOWER_QUANTILE,
    MAX_DEVELOPMENT_FITS,
    MODEL_SCORERS,
    TECHNICAL_FEATURE_COLUMNS,
    FundingArtifactStatus,
    FundingCalibrationBin,
    FundingCalibrationTable,
    FundingFoldBoundary,
    FundingFoldResult,
    FundingIncrementDecision,
    FundingManifest,
    FundingMetricSummary,
    FundingProtocol,
    FundingReferenceComparison,
    FundingReport,
    metric_decimal,
)
from investment_analyst.research.ml_funding_models import (
    MANIFEST_FILE_NAME as FUNDING_MANIFEST_FILE_NAME,
)
from investment_analyst.research.ml_funding_models import (
    PREDICTIONS_FILE_NAME as FUNDING_PREDICTIONS_FILE_NAME,
)
from investment_analyst.research.ml_funding_models import (
    PROTOCOL_FILE_NAME as FUNDING_PROTOCOL_FILE_NAME,
)
from investment_analyst.research.ml_funding_models import (
    REPORT_FILE_NAME as FUNDING_REPORT_FILE_NAME,
)
from investment_analyst.research.ml_funding_models import (
    STUDY_LIMITATIONS as FUNDING_STUDY_LIMITATIONS,
)
from investment_analyst.research.ml_models import (
    MANIFEST_FILE_NAME,
    PROTOCOL_FILE_NAME,
    REPORT_FILE_NAME,
    STUDY_LIMITATIONS,
    MlArtifactFile,
    MlArtifactStatus,
    MlBaselineManifest,
    MlBaselineProtocol,
    MlBaselineReport,
    MlEnvironment,
    MlFoldBoundary,
    MlTimings,
)
from investment_analyst.research.ml_report_service import MlResearchReportService

_ASSET = "crypto:btc-usd"
_OTHER_ASSET = "equity:us:aapl"
_DATASET_ID = UUID("d927c7f1-794a-571c-b729-6bb68d311115")
_BUNDLE_ID = UUID("fca740b8-8f38-5703-8006-e6a467bfb3ba")
_CONTENT = "1" * 64
_MANIFEST_DATASET = "2" * 64
_ROWS = "3" * 64
_CODE = "4" * 64
_BUNDLE_CONTENT = "5" * 64
_BUNDLE_MANIFEST = "6" * 64
_CREATED_AT = datetime(2026, 11, 1, 22, 25, 48, tzinfo=UTC)
_ENVIRONMENT = MlEnvironment(
    python_version="3.12.3",
    scikit_learn_version="1.9.1",
    numpy_version="2.5.4",
    scipy_version="1.18.1",
    threadpoolctl_version="3.7.0",
    duckdb_version="1.5.4",
)


def _sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _descriptor(path: Path) -> MlArtifactFile:
    payload = path.read_bytes()
    return MlArtifactFile(name=path.name, sha256=_sha(payload), bytes=len(payload))


def _write(path: Path, payload: bytes) -> None:
    path.write_bytes(payload)


def _canonical(model: object) -> bytes:
    return canonical_analytical_json(model.model_dump(mode="json"))


def _baseline_folds() -> tuple[MlFoldBoundary, ...]:
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


def _baseline_run(
    root: Path,
    *,
    asset_id: str = _ASSET,
    run_id: UUID | None = None,
    valid_report: bool = True,
) -> UUID:
    resolved = run_id or uuid4()
    run_dir = root / str(resolved)
    run_dir.mkdir(parents=True)
    protocol = MlBaselineProtocol(
        dataset_id=_DATASET_ID,
        dataset_content_sha256=_CONTENT,
        decision_rows=100,
        asset_id=asset_id,
        folds=_baseline_folds(),
        limitations=STUDY_LIMITATIONS,
    )
    _write(run_dir / PROTOCOL_FILE_NAME, _canonical(protocol))
    protocol_descriptor = _descriptor(run_dir / PROTOCOL_FILE_NAME)
    report = MlBaselineReport(
        run_id=resolved,
        status=MlArtifactStatus.INSUFFICIENT_EVALUATION_SAMPLE,
        dataset_id=_DATASET_ID,
        dataset_content_sha256=_CONTENT,
        dataset_manifest_sha256=_MANIFEST_DATASET,
        protocol_sha256=protocol_descriptor.sha256,
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
        timings=MlTimings(
            read_seconds=Decimal("0.1"),
            fit_seconds=Decimal("0"),
            evaluation_seconds=Decimal("0"),
            total_seconds=Decimal("0.1"),
        ),
        peak_rss_bytes=None,
    )
    payload = _canonical(report) if valid_report else b'{"not":"canonical"}'
    _write(run_dir / REPORT_FILE_NAME, payload)
    report_descriptor = _descriptor(run_dir / REPORT_FILE_NAME)
    manifest = MlBaselineManifest(
        run_id=resolved,
        status=report.status,
        dataset_id=_DATASET_ID,
        dataset_content_sha256=_CONTENT,
        dataset_manifest_sha256=_MANIFEST_DATASET,
        dataset_rows_sha256=_ROWS,
        code_material_sha256=_CODE,
        environment=_ENVIRONMENT,
        protocol=protocol_descriptor,
        selection=None,
        predictions=None,
        report=report_descriptor,
        root_bytes=protocol_descriptor.bytes + report_descriptor.bytes,
        created_at=_CREATED_AT,
    )
    _write(run_dir / MANIFEST_FILE_NAME, _canonical(manifest))
    return resolved


def _funding_folds() -> tuple[FundingFoldBoundary, ...]:
    return (
        FundingFoldBoundary(
            fold=1,
            train_end_index=100,
            validation_start_index=100,
            validation_end_index=200,
            validation_start_date=date(2021, 1, 1),
            validation_end_date=date(2021, 6, 1),
            expected_train_rows=100,
            expected_evaluation_rows=100,
        ),
        FundingFoldBoundary(
            fold=2,
            train_end_index=200,
            validation_start_index=200,
            validation_end_index=300,
            validation_start_date=date(2021, 6, 1),
            validation_end_date=date(2021, 12, 1),
            expected_train_rows=200,
            expected_evaluation_rows=100,
        ),
        FundingFoldBoundary(
            fold=3,
            train_end_index=300,
            validation_start_index=300,
            validation_end_index=400,
            validation_start_date=date(2021, 12, 1),
            validation_end_date=date(2022, 6, 1),
            expected_train_rows=300,
            expected_evaluation_rows=100,
        ),
    )


def _funding_protocol(*, asset_id: str = _ASSET) -> FundingProtocol:
    return FundingProtocol(
        dataset_id=_DATASET_ID,
        dataset_content_sha256=_CONTENT,
        bundle_id=_BUNDLE_ID,
        bundle_content_sha256=_BUNDLE_CONTENT,
        decision_rows=2186,
        asset_id=asset_id,
        technical_feature_columns=TECHNICAL_FEATURE_COLUMNS,
        funding_feature_columns=FUNDING_FEATURE_COLUMNS,
        folds=_funding_folds(),
        limitations=FUNDING_STUDY_LIMITATIONS,
    )


def _scores(rows: int) -> tuple[FundingMetricSummary, ...]:
    values = {
        "technical_logistic": Decimal("0.24940344102885015"),
        "funding_logistic": Decimal("0.25094407714874845"),
        "half_probability": Decimal("0.25"),
        "training_class_prior": Decimal("0.24853491584603996"),
    }
    return tuple(
        FundingMetricSummary(
            scorer=scorer,
            rows=rows,
            positives=rows // 2,
            negatives=rows - rows // 2,
            brier=values[scorer.value],
            log_loss=Decimal("0.69"),
        )
        for scorer in ALL_SCORERS
    )


def _comparison(reference: str, *, rows: int = 633) -> FundingReferenceComparison:
    model_brier = Decimal("0.25094407714874845")
    reference_brier = {
        "technical_logistic": Decimal("0.24940344102885015"),
        "half_probability": Decimal("0.25"),
        "training_class_prior": Decimal("0.24853491584603996"),
    }[reference]
    absolute = reference_brier - model_brier
    relative = metric_decimal(float(reference_brier - model_brier) / float(reference_brier))
    del rows
    return FundingReferenceComparison(
        reference=reference,
        model_brier=model_brier,
        reference_brier=reference_brier,
        absolute_improvement=absolute,
        relative_improvement=relative,
        mean_paired_difference=absolute,
        lower_bound=Decimal("-0.004"),
        quantile=LOWER_QUANTILE,
        improved_at_least_five_percent=False,
        lower_bound_positive=False,
        criterion_satisfied=False,
    )


def _calibration(scorer: str) -> FundingCalibrationTable:
    bins = tuple(
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
    return FundingCalibrationTable(scorer=scorer, bins=bins)


def _fold_result(fold: int, *, train_rows: int, evaluation_rows: int) -> FundingFoldResult:
    return FundingFoldResult(
        fold=fold,
        train_rows=train_rows,
        train_positives=train_rows // 2,
        train_negatives=train_rows - train_rows // 2,
        purged_excluded=0,
        ineligible_excluded=0,
        funding_unavailable_excluded=7,
        embargo_excluded=11,
        evaluation_rows=evaluation_rows,
        evaluation_positives=evaluation_rows // 2,
        evaluation_negatives=evaluation_rows - evaluation_rows // 2,
        segments=1,
        scores=_scores(evaluation_rows),
        comparisons=(
            _comparison("technical_logistic"),
            _comparison("half_probability"),
            _comparison("training_class_prior"),
        ),
        calibration=tuple(_calibration(scorer.value) for scorer in MODEL_SCORERS),
    )


def _funding_report(run_id: UUID, *, asset_id: str = _ASSET) -> FundingReport:
    folds = (
        _fold_result(1, train_rows=1075, evaluation_rows=207),
        _fold_result(2, train_rows=1293, evaluation_rows=218),
        _fold_result(3, train_rows=1500, evaluation_rows=208),
    )
    return FundingReport(
        run_id=run_id,
        status=FundingArtifactStatus.COMPLETED,
        protocol_sha256="0" * 64,
        dataset_id=_DATASET_ID,
        dataset_content_sha256=_CONTENT,
        dataset_manifest_sha256=_MANIFEST_DATASET,
        bundle_id=_BUNDLE_ID,
        bundle_content_sha256=_BUNDLE_CONTENT,
        bundle_manifest_sha256=_BUNDLE_MANIFEST,
        code_material_sha256=_CODE,
        code_material_files=("ml_funding_models.py", "ml_funding_export.py"),
        environment=_ENVIRONMENT,
        decision=FundingIncrementDecision.NO_DEMONSTRATED_INCREMENT,
        folds=folds,
        pooled_scores=_scores(633),
        pooled_comparisons=(
            _comparison(CONTROL_SCORER.value),
            _comparison("half_probability"),
            _comparison("training_class_prior"),
        ),
        calibration=tuple(_calibration(scorer.value) for scorer in MODEL_SCORERS),
        disposition_reason=None,
        prediction_rows=4,
        development_fits=MAX_DEVELOPMENT_FITS,
        timings=MlTimings(
            read_seconds=Decimal("1.865"),
            fit_seconds=Decimal("0.151"),
            evaluation_seconds=Decimal("0.118"),
            total_seconds=Decimal("4.01"),
        ),
        peak_rss_bytes=228_745_216,
    )


def _write_predictions(path: Path, rows: int = 4) -> None:
    connection = duckdb.connect(":memory:")
    try:
        connection.execute("SET threads=1")
        connection.execute(
            "CREATE TABLE sealed ("
            "sample_key VARCHAR, decision_at VARCHAR, fold INTEGER,"
            " scorer VARCHAR, label INTEGER, probability DOUBLE)"
        )
        for index in range(rows):
            connection.execute(
                "INSERT INTO sealed VALUES (?, ?, ?, ?, ?, ?)",
                [f"sample-{index}", "2021-01-01T12:00:00Z", 1, "half_probability", 1, 0.5],
            )
        target = str(path).replace("'", "''")
        connection.execute(f"COPY sealed TO '{target}' (FORMAT PARQUET)")
    finally:
        connection.close()


def _funding_run(root: Path, *, asset_id: str = _ASSET, run_id: UUID | None = None) -> UUID:
    resolved = run_id or uuid4()
    run_dir = root / str(resolved)
    run_dir.mkdir(parents=True)
    protocol = _funding_protocol(asset_id=asset_id)
    _write(run_dir / FUNDING_PROTOCOL_FILE_NAME, _canonical(protocol))
    protocol_descriptor = _descriptor(run_dir / FUNDING_PROTOCOL_FILE_NAME)
    _write_predictions(run_dir / FUNDING_PREDICTIONS_FILE_NAME)
    report = _funding_report(resolved, asset_id=asset_id).model_copy(
        update={"protocol_sha256": protocol_descriptor.sha256}
    )
    _write(run_dir / FUNDING_REPORT_FILE_NAME, _canonical(report))
    report_descriptor = _descriptor(run_dir / FUNDING_REPORT_FILE_NAME)
    predictions_descriptor = _descriptor(run_dir / FUNDING_PREDICTIONS_FILE_NAME)
    manifest = FundingManifest(
        run_id=resolved,
        dataset_id=_DATASET_ID,
        dataset_content_sha256=_CONTENT,
        dataset_manifest_sha256=_MANIFEST_DATASET,
        bundle_id=_BUNDLE_ID,
        bundle_content_sha256=_BUNDLE_CONTENT,
        bundle_manifest_sha256=_BUNDLE_MANIFEST,
        code_material_sha256=_CODE,
        code_material_files=report.code_material_files,
        environment=_ENVIRONMENT,
        protocol=protocol_descriptor,
        predictions=predictions_descriptor,
        report=report_descriptor,
        root_bytes=(
            protocol_descriptor.bytes + predictions_descriptor.bytes + report_descriptor.bytes
        ),
        created_at=_CREATED_AT,
    )
    _write(run_dir / FUNDING_MANIFEST_FILE_NAME, _canonical(manifest))
    return resolved


@pytest.fixture()
def roots(tmp_path: Path) -> tuple[Path, Path]:
    baseline = tmp_path / "ml-baseline-validation"
    funding = tmp_path / "ml-funding-validation"
    baseline.mkdir()
    funding.mkdir()
    return baseline, funding


def _service(roots: tuple[Path, Path]) -> ExperimentReportService:
    baseline, funding = roots
    return ExperimentReportService(
        baseline_reports=MlResearchReportService(baseline),
        funding_root=funding,
    )


def test_listing_returns_every_page_without_duplicates_and_keeps_a_cursor(
    roots: tuple[Path, Path],
) -> None:
    baseline, funding = roots
    baseline_ids = [_baseline_run(baseline) for _ in range(2)]
    funding_ids = [_funding_run(funding) for _ in range(3)]
    service = _service(roots)

    first: ExperimentListPage = service.list_experiments(asset_id=_ASSET, limit=2)
    assert first.schema_version == "research-experiment-list-v1"
    assert first.context.family == ExperimentFamilyFilter.ALL
    assert first.examined_candidates == 2
    assert first.next_cursor is not None
    assert len(first.items) == 2

    seen: list[tuple[str, str]] = []
    page = first
    while True:
        seen.extend((item.family.value, str(item.run_id)) for item in page.items)
        if page.next_cursor is None:
            break
        page = service.list_experiments(asset_id=_ASSET, limit=2, cursor=page.next_cursor)

    assert len(seen) == len(set(seen)) == 5
    assert {str(item) for item in baseline_ids}.issubset({key[1] for key in seen})
    assert {str(item) for item in funding_ids}.issubset({key[1] for key in seen})
    assert seen == sorted(seen)


def test_empty_page_can_still_offer_the_next_cursor(roots: tuple[Path, Path]) -> None:
    _, funding = roots
    _funding_run(
        funding, asset_id=_OTHER_ASSET, run_id=UUID("00000000-0000-4000-8000-000000000001")
    )
    _funding_run(
        funding, asset_id=_OTHER_ASSET, run_id=UUID("00000000-0000-4000-8000-000000000002")
    )
    _funding_run(funding, run_id=UUID("00000000-0000-4000-8000-000000000003"))
    _funding_run(funding, run_id=UUID("00000000-0000-4000-8000-000000000004"))
    service = _service(roots)

    first = service.list_experiments(asset_id=_ASSET, family="funding", limit=2)
    assert first.items == ()
    assert first.examined_candidates == 2
    assert first.next_cursor is not None

    page = service.list_experiments(
        asset_id=_ASSET, family="funding", limit=2, cursor=first.next_cursor
    )
    assert [str(item.run_id) for item in page.items] == [
        "00000000-0000-4000-8000-000000000003",
        "00000000-0000-4000-8000-000000000004",
    ]
    assert page.next_cursor is None


def test_listing_filters_by_asset_before_hydrating_and_hides_foreign_assets(
    roots: tuple[Path, Path],
) -> None:
    baseline, funding = roots
    _baseline_run(baseline, asset_id=_OTHER_ASSET)
    _funding_run(funding)
    service = _service(roots)

    page = service.list_experiments(asset_id=_ASSET, limit=20)

    assert {item.family for item in page.items} == {ExperimentFamily.FUNDING}
    assert page.warnings == ()
    assert _OTHER_ASSET not in canonical_analytical_json(page.to_json_dict()).decode("utf-8")


def test_listing_never_reads_the_sealed_report(roots: tuple[Path, Path]) -> None:
    baseline, _ = roots
    run_id = _baseline_run(baseline, valid_report=False)
    service = _service(roots)

    page = service.list_experiments(asset_id=_ASSET, limit=20)

    assert [item.run_id for item in page.items] == [run_id]
    with pytest.raises(ExperimentReportConflictError):
        service.report(asset_id=_ASSET, family="baseline", run_id=run_id)


def test_incomplete_funding_candidate_is_a_warning_not_a_study(roots: tuple[Path, Path]) -> None:
    _, funding = roots
    run_dir = funding / str(uuid4())
    run_dir.mkdir()
    _write(run_dir / FUNDING_PROTOCOL_FILE_NAME, _canonical(_funding_protocol()))
    service = _service(roots)

    page = service.list_experiments(asset_id=_ASSET, limit=20)

    assert page.items == ()
    assert [warning.code.value for warning in page.warnings] == ["incomplete_artifact"]
    assert page.warnings[0].run_id is not None


def test_corrupt_header_is_reported_without_paths(roots: tuple[Path, Path], tmp_path: Path) -> None:
    baseline, _ = roots
    run_id = uuid4()
    run_dir = baseline / str(run_id)
    run_dir.mkdir()
    _write(run_dir / PROTOCOL_FILE_NAME, b"{}")
    _write(run_dir / MANIFEST_FILE_NAME, b"{}")
    service = _service(roots)

    page = service.list_experiments(asset_id=_ASSET, limit=20)

    assert page.items == ()
    assert page.warnings[0].code.value == "incompatible_artifact"
    encoded = canonical_analytical_json(page.to_json_dict()).decode("utf-8")
    assert str(tmp_path) not in encoded


def test_capacity_root_symlink_and_traversal_fail_closed(
    roots: tuple[Path, Path], tmp_path: Path
) -> None:
    _, funding = roots
    for index in range(257):
        (funding / f"entry-{index}").mkdir()
    service = _service(roots)
    with pytest.raises(ExperimentReportCapacityError):
        service.list_experiments(asset_id=_ASSET, limit=20)

    for entry in funding.iterdir():
        entry.rmdir()
    real_root = tmp_path / "real-root"
    real_root.mkdir()
    linked_root = tmp_path / "linked-root"
    linked_root.symlink_to(real_root)
    linked_service = ExperimentReportService(
        baseline_reports=MlResearchReportService(tmp_path / "missing-baseline"),
        funding_root=linked_root,
    )
    with pytest.raises(ExperimentReportConflictError):
        linked_service.list_experiments(asset_id=_ASSET, limit=20)

    escape = funding / str(uuid4())
    escape.symlink_to(real_root)
    with pytest.raises(ExperimentReportConflictError):
        service.report(asset_id=_ASSET, family="funding", run_id=UUID(escape.name))


def test_absent_root_is_an_explicit_empty_state(tmp_path: Path) -> None:
    service = ExperimentReportService(
        baseline_reports=MlResearchReportService(tmp_path / "missing-baseline"),
        funding_root=tmp_path / "missing-funding",
    )

    page = service.list_experiments(asset_id=_ASSET, limit=20)

    assert page.items == ()
    assert [root.status.value for root in page.roots] == ["missing", "missing"]
    with pytest.raises(ExperimentReportNotFoundError):
        service.report(asset_id=_ASSET, family="funding", run_id=uuid4())


def test_cursor_is_bound_to_context_and_inventory(roots: tuple[Path, Path]) -> None:
    baseline, funding = roots
    for _ in range(3):
        _baseline_run(baseline)
    service = _service(roots)
    first = service.list_experiments(asset_id=_ASSET, limit=1)
    assert first.next_cursor is not None

    with pytest.raises(ExperimentCursorStaleError):
        service.list_experiments(asset_id=_ASSET, limit=2, cursor=first.next_cursor)
    with pytest.raises(ExperimentCursorStaleError):
        service.list_experiments(asset_id=_OTHER_ASSET, limit=1, cursor=first.next_cursor)

    _funding_run(funding)
    with pytest.raises(ExperimentCursorStaleError):
        service.list_experiments(asset_id=_ASSET, limit=1, cursor=first.next_cursor)
    with pytest.raises(ValueError):
        service.list_experiments(asset_id=_ASSET, limit=1, cursor="not-a-cursor")


@pytest.mark.parametrize("limit", [0, 51, -1])
def test_listing_rejects_out_of_range_limits(roots: tuple[Path, Path], limit: int) -> None:
    with pytest.raises(ValueError):
        _service(roots).list_experiments(asset_id=_ASSET, limit=limit)


def test_listing_rejects_unknown_family(roots: tuple[Path, Path]) -> None:
    with pytest.raises(ValueError):
        _service(roots).list_experiments(asset_id=_ASSET, family="other")


def test_baseline_detail_delegates_to_the_existing_projection(roots: tuple[Path, Path]) -> None:
    baseline, _ = roots
    run_id = _baseline_run(baseline)
    service = _service(roots)

    view: ExperimentReportView = service.report(asset_id=_ASSET, family="baseline", run_id=run_id)
    original = MlResearchReportService(baseline).report(asset_id=_ASSET, run_id=run_id)

    assert view.context.family is ExperimentFamily.BASELINE
    assert view.context.manifest_sha256 == original.artifact.manifest_sha256
    assert view.payload.report.to_json_dict() == original.to_json_dict()
    assert view.export_file_name() == f"baseline-{run_id}.json"


def test_funding_detail_copies_the_sealed_values_without_recomputing(
    roots: tuple[Path, Path],
) -> None:
    _, funding = roots
    run_id = _funding_run(funding)
    service = _service(roots)

    view = service.report(asset_id=_ASSET, family="funding", run_id=run_id)
    payload = view.payload

    assert view.context.family is ExperimentFamily.FUNDING
    assert view.context.run_id == run_id
    assert payload.decision is FundingIncrementDecision.NO_DEMONSTRATED_INCREMENT
    assert payload.prediction_rows == 4
    assert payload.development_fits == MAX_DEVELOPMENT_FITS
    assert payload.decision_rows == 2186
    assert payload.holdout.accesses == 0 and payload.holdout.fits == 0
    assert [item.fold for item in payload.folds] == [1, 2, 3]
    assert payload.pooled_scores[1].brier == Decimal("0.25094407714874845")
    assert payload.provenance.dataset_manifest_sha256 == _MANIFEST_DATASET
    assert payload.provenance.bundle_manifest_sha256 == _BUNDLE_MANIFEST
    assert payload.protocol.strict_pit_verified is False
    assert view.export_file_name() == f"funding-{run_id}.json"
    encoded = canonical_analytical_json(view.to_json_dict()).decode("utf-8")
    assert "0.25094407714874845" in encoded
    assert "0.25" in encoded


def test_family_and_asset_mismatch_fail_closed(roots: tuple[Path, Path]) -> None:
    baseline, funding = roots
    baseline_id = _baseline_run(baseline)
    funding_id = _funding_run(funding)
    service = _service(roots)

    with pytest.raises(ExperimentReportNotFoundError):
        service.report(asset_id=_ASSET, family="funding", run_id=baseline_id)
    with pytest.raises(ExperimentReportConflictError):
        service.report(asset_id=_OTHER_ASSET, family="funding", run_id=funding_id)
    with pytest.raises(ValueError):
        service.report(asset_id=_ASSET, family="other", run_id=funding_id)


def test_corruption_preserving_size_and_mtime_invalidates_the_cached_projection(
    roots: tuple[Path, Path],
) -> None:
    _, funding = roots
    run_id = _funding_run(funding)
    service = _service(roots)
    service.report(asset_id=_ASSET, family="funding", run_id=run_id)
    service.report(asset_id=_ASSET, family="funding", run_id=run_id)
    assert service.performance()["cache_hits"] == 1

    protocol_path = funding / str(run_id) / FUNDING_PROTOCOL_FILE_NAME
    before = protocol_path.stat()
    payload = protocol_path.read_bytes()
    tampered = payload.replace(b'"float64"', b'"float65"')
    assert len(tampered) == len(payload) and tampered != payload
    protocol_path.write_bytes(tampered)
    os.utime(protocol_path, ns=(before.st_atime_ns, before.st_mtime_ns))
    after = protocol_path.stat()
    assert (after.st_size, after.st_mtime_ns, after.st_ino) == (
        before.st_size,
        before.st_mtime_ns,
        before.st_ino,
    )

    with pytest.raises(ExperimentReportConflictError):
        service.report(asset_id=_ASSET, family="funding", run_id=run_id)
    performance = service.performance()
    assert performance["cache_hits"] == 1


def test_warm_reads_reuse_the_verified_projection_and_coalesce(
    roots: tuple[Path, Path],
) -> None:
    _, funding = roots
    run_id = _funding_run(funding)
    service = _service(roots)

    first = service.report(asset_id=_ASSET, family="funding", run_id=run_id)
    second = service.report(asset_id=_ASSET, family="funding", run_id=run_id)

    assert first is second
    performance = service.performance()
    assert performance["full_verifications"] == 1
    assert performance["cache_hits"] == 1

    def read() -> str:
        return str(service.report(asset_id=_ASSET, family="funding", run_id=run_id).context.run_id)

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: read(), range(4)))
    assert results == [str(run_id)] * 4
    assert service.performance()["full_verifications"] == 1


def test_cache_never_grows_past_its_declared_bound(roots: tuple[Path, Path]) -> None:
    _, funding = roots
    ids = [_funding_run(funding) for _ in range(MAX_FUNDING_CACHE_ENTRIES + 1)]
    service = _service(roots)
    for run_id in ids:
        service.report(asset_id=_ASSET, family="funding", run_id=run_id)

    performance = service.performance()
    assert performance["cache_entries"] == MAX_FUNDING_CACHE_ENTRIES
    assert performance["cache_bytes"] <= 2 * 1024 * 1024


def test_service_imports_without_ml_training_dependencies() -> None:
    script = "\n".join(
        (
            "import sys",
            "class _Block:",
            "    def find_spec(self, name, path=None, target=None):",
            "        if name.split('.')[0] in {'sklearn', 'numpy', 'scipy', 'threadpoolctl'}:",
            "            raise ImportError('ML dependency blocked: ' + name)",
            "        return None",
            "sys.meta_path.insert(0, _Block())",
            "from investment_analyst.research.experiment_report_service import (",
            "    ExperimentReportService,",
            ")",
            "from investment_analyst.research.experiment_report_models import (",
            "    ExperimentListPage,",
            ")",
            "print('ok', ExperimentReportService.__name__, ExperimentListPage.__name__)",
        )
    )
    completed = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        check=False,
        cwd=str(Path(__file__).resolve().parents[3]),
        env={
            "PATH": os.environ.get("PATH", ""),
            "PYTHONPATH": str(Path(__file__).resolve().parents[3] / "src"),
        },
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.startswith("ok ExperimentReportService")


def test_listing_and_detail_never_write_into_the_artifact_roots(
    roots: tuple[Path, Path],
) -> None:
    baseline, funding = roots
    baseline_id = _baseline_run(baseline)
    funding_id = _funding_run(funding)
    service = _service(roots)

    def snapshot(root: Path) -> dict[str, tuple[int, int]]:
        return {
            str(path.relative_to(root)): (path.stat().st_size, path.stat().st_mtime_ns)
            for path in sorted(root.rglob("*"))
        }

    before = (snapshot(baseline), snapshot(funding))
    service.list_experiments(asset_id=_ASSET, limit=20)
    service.report(asset_id=_ASSET, family="baseline", run_id=baseline_id)
    service.report(asset_id=_ASSET, family="funding", run_id=funding_id)

    assert (snapshot(baseline), snapshot(funding)) == before


def test_list_item_status_and_identity_are_copied_from_the_sealed_manifest(
    roots: tuple[Path, Path],
) -> None:
    baseline, funding = roots
    baseline_id = _baseline_run(baseline)
    funding_id = _funding_run(funding)
    service = _service(roots)

    page = service.list_experiments(asset_id=_ASSET, limit=20)
    by_key = {(item.family.value, str(item.run_id)): item for item in page.items}

    baseline_item = by_key[("baseline", str(baseline_id))]
    manifest = MlBaselineManifest.model_validate_json(
        (baseline / str(baseline_id) / MANIFEST_FILE_NAME).read_bytes()
    )
    assert baseline_item.artifact_status is ExperimentArtifactStatus.INSUFFICIENT_EVALUATION_SAMPLE
    assert baseline_item.protocol_sha256 == manifest.protocol.sha256
    assert baseline_item.report_sha256 == manifest.report.sha256
    assert baseline_item.artifact_bytes == manifest.root_bytes
    assert baseline_item.created_at == manifest.created_at

    funding_item = by_key[("funding", str(funding_id))]
    funding_manifest = FundingManifest.model_validate_json(
        (funding / str(funding_id) / FUNDING_MANIFEST_FILE_NAME).read_bytes()
    )
    assert funding_item.artifact_status is ExperimentArtifactStatus.COMPLETED
    assert funding_item.manifest_sha256 == _sha(
        (funding / str(funding_id) / FUNDING_MANIFEST_FILE_NAME).read_bytes()
    )
    assert funding_item.report_sha256 == funding_manifest.report.sha256
    assert funding_item.artifact_bytes == funding_manifest.root_bytes
