"""Socket-level contract for the read-only research-experiment endpoints."""

from __future__ import annotations

import hashlib
import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import cast
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from uuid import UUID, uuid4

import duckdb
import pytest

from investment_analyst.analytics.analytical_access_models import canonical_analytical_json
from investment_analyst.application.facade import InvestmentAnalystApplication
from investment_analyst.application.market_universe import MarketAssetUniverse
from investment_analyst.frontend.local_web import (
    AaplLocalHttpServer,
    AaplLocalWebApplication,
)
from investment_analyst.research.experiment_report_service import ExperimentReportService
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


class _CatalogController:
    """Expose only the catalog seam the research application methods consume."""

    def __init__(self) -> None:
        self._universe = InvestmentAnalystApplication.create_default().list_market_assets()

    def market_assets(self) -> MarketAssetUniverse:
        return self._universe


def _sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _descriptor(path: Path) -> MlArtifactFile:
    payload = path.read_bytes()
    return MlArtifactFile(name=path.name, sha256=_sha(payload), bytes=len(payload))


def _canonical(model: object) -> bytes:
    return canonical_analytical_json(model.model_dump(mode="json"))


def _baseline_run(root: Path, *, asset_id: str = _ASSET, run_id: UUID | None = None) -> UUID:
    resolved = run_id or uuid4()
    run_dir = root / str(resolved)
    run_dir.mkdir(parents=True)
    protocol = MlBaselineProtocol(
        dataset_id=_DATASET_ID,
        dataset_content_sha256=_CONTENT,
        decision_rows=100,
        asset_id=asset_id,
        folds=(
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
        ),
        limitations=STUDY_LIMITATIONS,
    )
    (run_dir / PROTOCOL_FILE_NAME).write_bytes(_canonical(protocol))
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
    (run_dir / REPORT_FILE_NAME).write_bytes(_canonical(report))
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
    (run_dir / MANIFEST_FILE_NAME).write_bytes(_canonical(manifest))
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


def _comparison(reference: str) -> FundingReferenceComparison:
    model_brier = Decimal("0.25094407714874845")
    reference_brier = {
        "technical_logistic": Decimal("0.24940344102885015"),
        "half_probability": Decimal("0.25"),
        "training_class_prior": Decimal("0.24853491584603996"),
    }[reference]
    absolute = reference_brier - model_brier
    relative = metric_decimal(float(reference_brier - model_brier) / float(reference_brier))
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


def _funding_report(run_id: UUID) -> FundingReport:
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
        folds=(
            _fold_result(1, train_rows=1075, evaluation_rows=207),
            _fold_result(2, train_rows=1293, evaluation_rows=218),
            _fold_result(3, train_rows=1500, evaluation_rows=208),
        ),
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
    (run_dir / FUNDING_PROTOCOL_FILE_NAME).write_bytes(_canonical(protocol))
    protocol_descriptor = _descriptor(run_dir / FUNDING_PROTOCOL_FILE_NAME)
    _write_predictions(run_dir / FUNDING_PREDICTIONS_FILE_NAME)
    report = _funding_report(resolved).model_copy(
        update={"protocol_sha256": protocol_descriptor.sha256}
    )
    (run_dir / FUNDING_REPORT_FILE_NAME).write_bytes(_canonical(report))
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
    (run_dir / FUNDING_MANIFEST_FILE_NAME).write_bytes(_canonical(manifest))
    return resolved


@contextmanager
def _server(application: AaplLocalWebApplication) -> Iterator[str]:
    server = AaplLocalHttpServer(("127.0.0.1", 0), cast(AaplLocalWebApplication, application))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    try:
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _get(root: str, path: str, query: str) -> tuple[int, dict[str, object]]:
    try:
        with urlopen(f"{root}{path}?{query}", timeout=5) as response:
            return response.status, json.loads(response.read())
    except HTTPError as error:
        return error.code, json.loads(error.read())


def _post(root: str, path: str, query: str) -> tuple[int, dict[str, object]]:
    request = Request(
        f"{root}{path}?{query}",
        data=b"{}",
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read())
    except HTTPError as error:
        return error.code, json.loads(error.read())


@pytest.fixture()
def roots(tmp_path: Path) -> tuple[Path, Path]:
    baseline = tmp_path / "ml-baseline-validation"
    funding = tmp_path / "ml-funding-validation"
    baseline.mkdir()
    funding.mkdir()
    return baseline, funding


def _application(roots: tuple[Path, Path], *, configured: bool = True) -> AaplLocalWebApplication:
    baseline, funding = roots
    reports = MlResearchReportService(baseline)
    return AaplLocalWebApplication(
        cast(object, _CatalogController()),
        None,
        ml_reports=reports,
        experiment_reports=(
            ExperimentReportService(baseline_reports=reports, funding_root=funding)
            if configured
            else None
        ),
    )


def test_listing_endpoint_pages_through_both_families(roots: tuple[Path, Path]) -> None:
    baseline, funding = roots
    baseline_id = _baseline_run(baseline)
    funding_id = _funding_run(funding)
    with _server(_application(roots)) as root:
        first_status, first = _get(
            root, "/api/v1/research/experiments", f"asset_id={_ASSET}&limit=1"
        )
        second_status, second = _get(
            root,
            "/api/v1/research/experiments",
            f"asset_id={_ASSET}&limit=1&cursor={first['next_cursor']}",
        )

    assert (first_status, second_status) == (200, 200)
    assert first["schema_version"] == "research-experiment-list-v1"
    assert first["context"] == {"asset_id": _ASSET, "family": "all", "limit": 1}
    first_items = cast(list[dict[str, object]], first["items"])
    second_items = cast(list[dict[str, object]], second["items"])
    assert len(first_items) == 1 and len(second_items) == 1
    assert {(item["family"], item["run_id"]) for item in first_items + second_items} == {
        ("baseline", str(baseline_id)),
        ("funding", str(funding_id)),
    }
    assert second["next_cursor"] is None
    assert {root_state["status"] for root_state in first["roots"]} == {"available"}


def test_report_endpoint_serves_both_families_and_preserves_decimal_text(
    roots: tuple[Path, Path],
) -> None:
    baseline, funding = roots
    baseline_id = _baseline_run(baseline)
    funding_id = _funding_run(funding)
    with _server(_application(roots)) as root:
        baseline_status, baseline_payload = _get(
            root,
            "/api/v1/research/experiment-report",
            f"asset_id={_ASSET}&family=baseline&run_id={baseline_id}",
        )
        funding_status, funding_payload = _get(
            root,
            "/api/v1/research/experiment-report",
            f"asset_id={_ASSET}&family=funding&run_id={funding_id}",
        )

    assert (baseline_status, funding_status) == (200, 200)
    assert baseline_payload["schema_version"] == "research-experiment-report-view-v1"
    assert baseline_payload["payload"]["family"] == "baseline"  # type: ignore[index]
    assert baseline_payload["payload"]["report"]["schema_version"] == (  # type: ignore[index]
        "ml-research-report-view-v1"
    )
    assert funding_payload["payload"]["family"] == "funding"  # type: ignore[index]
    assert funding_payload["payload"]["decision"] == "no_demonstrated_increment"  # type: ignore[index]
    assert funding_payload["context"]["run_id"] == str(funding_id)  # type: ignore[index]
    encoded = json.dumps(funding_payload)
    assert "0.25094407714874845" in encoded
    assert str(funding) not in encoded


def test_listing_endpoint_rejects_invalid_queries_and_methods(roots: tuple[Path, Path]) -> None:
    _baseline_run(roots[0])
    with _server(_application(roots)) as root:
        post_status, post = _post(root, "/api/v1/research/experiments", f"asset_id={_ASSET}")
        invalid = (
            "",
            f"asset_id={_ASSET}&asset_id={_ASSET}",
            f"asset_id={_ASSET}&path=/etc/passwd",
            f"asset_id={_ASSET}&limit=0",
            f"asset_id={_ASSET}&limit=51",
            f"asset_id={_ASSET}&limit=abc",
            f"asset_id={_ASSET}&family=other",
            "asset_id=equity:us:not-in-catalog",
            f"asset_id={_ASSET}&cursor=not-a-cursor",
        )
        for query in invalid:
            status, payload = _get(root, "/api/v1/research/experiments", query)
            assert status == 400, query
            assert cast(dict[str, object], payload["error"])["code"] == "invalid_request"

    assert post_status == 404
    assert cast(dict[str, object], post["error"])["code"] == "not_found"


def test_report_endpoint_rejects_invalid_queries_and_methods(roots: tuple[Path, Path]) -> None:
    _funding_run(roots[1])
    run_id = uuid4()
    with _server(_application(roots)) as root:
        post_status, post = _post(
            root,
            "/api/v1/research/experiment-report",
            f"asset_id={_ASSET}&family=funding&run_id={run_id}",
        )
        invalid = (
            f"asset_id={_ASSET}&run_id={run_id}",
            f"asset_id={_ASSET}&family=funding",
            f"asset_id={_ASSET}&family=funding&run_id=not-a-uuid",
            f"asset_id={_ASSET}&family=funding&run_id={str(run_id).upper()}",
            f"asset_id={_ASSET}&family=funding&run_id={run_id}&path=/etc/passwd",
            f"asset_id=equity:us:not-in-catalog&family=funding&run_id={run_id}",
        )
        for query in invalid:
            status, payload = _get(root, "/api/v1/research/experiment-report", query)
            assert status == 400, query
            assert cast(dict[str, object], payload["error"])["code"] == "invalid_request"

    assert post_status == 404
    assert cast(dict[str, object], post["error"])["code"] == "not_found"


def test_endpoints_map_missing_conflict_cursor_and_capacity(roots: tuple[Path, Path]) -> None:
    baseline, funding = roots
    baseline_id = _baseline_run(baseline)
    funding_id = _funding_run(funding)
    with _server(_application(roots)) as root:
        missing_status, missing = _get(
            root,
            "/api/v1/research/experiment-report",
            f"asset_id={_ASSET}&family=funding&run_id={uuid4()}",
        )
        mismatch_status, mismatch = _get(
            root,
            "/api/v1/research/experiment-report",
            f"asset_id={_OTHER_ASSET}&family=funding&run_id={funding_id}",
        )
        family_status, family = _get(
            root,
            "/api/v1/research/experiment-report",
            f"asset_id={_ASSET}&family=funding&run_id={baseline_id}",
        )
        list_status, listing = _get(
            root, "/api/v1/research/experiments", f"asset_id={_ASSET}&limit=1"
        )

    assert missing_status == 404
    assert cast(dict[str, object], missing["error"])["code"] == "research_experiment_not_found"
    assert mismatch_status == 409
    assert cast(dict[str, object], mismatch["error"])["code"] == "research_experiment_conflict"
    assert family_status == 404
    assert list_status == 200

    cursor = listing["next_cursor"]
    assert cursor is not None
    _baseline_run(baseline, run_id=UUID("00000000-0000-4000-8000-000000000009"))
    with _server(_application(roots)) as root:
        stale_status, stale = _get(
            root,
            "/api/v1/research/experiments",
            f"asset_id={_ASSET}&limit=1&cursor={cursor}",
        )
    assert stale_status == 409
    assert cast(dict[str, object], stale["error"])["code"] == "research_experiment_cursor_stale"

    crowded = funding / "crowded"
    crowded.mkdir()
    for index in range(257):
        (crowded / f"entry-{index}").mkdir()
    capacity_service = ExperimentReportService(
        baseline_reports=MlResearchReportService(baseline),
        funding_root=crowded,
    )
    application = AaplLocalWebApplication(
        cast(object, _CatalogController()),
        None,
        experiment_reports=capacity_service,
    )
    with _server(application) as root:
        capacity_status, capacity = _get(root, "/api/v1/research/experiments", f"asset_id={_ASSET}")
    assert capacity_status == 409
    assert cast(dict[str, object], capacity["error"])["code"] == "research_experiment_capacity"
    del baseline_id


def test_endpoints_report_conflict_when_not_configured(roots: tuple[Path, Path]) -> None:
    with _server(_application(roots, configured=False)) as root:
        list_status, listing = _get(root, "/api/v1/research/experiments", f"asset_id={_ASSET}")
        report_status, report = _get(
            root,
            "/api/v1/research/experiment-report",
            f"asset_id={_ASSET}&family=baseline&run_id={uuid4()}",
        )

    assert (list_status, report_status) == (409, 409)
    for payload in (listing, report):
        assert cast(dict[str, object], payload["error"])["code"] == ("research_experiment_conflict")


def test_legacy_baseline_report_route_is_unchanged(roots: tuple[Path, Path]) -> None:
    baseline_id = _baseline_run(roots[0])
    with _server(_application(roots)) as root:
        status, payload = _get(
            root,
            "/api/v1/research/ml-report",
            f"asset_id={_ASSET}&run_id={baseline_id}",
        )

    assert status == 200
    assert payload["schema_version"] == "ml-research-report-view-v1"
    assert payload["artifact"]["run_id"] == str(baseline_id)  # type: ignore[index]
    assert payload["artifact"]["status"] == "insufficient_evaluation_sample"  # type: ignore[index]
