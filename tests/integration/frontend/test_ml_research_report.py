"""Socket-level contract for the read-only local ML research report endpoint."""

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

import pytest

from investment_analyst.analytics.analytical_access_models import canonical_analytical_json
from investment_analyst.application.facade import InvestmentAnalystApplication
from investment_analyst.application.market_universe import MarketAssetUniverse
from investment_analyst.frontend.local_web import (
    AaplLocalHttpServer,
    AaplLocalWebApplication,
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
_DATASET_ID = UUID("d927c7f1-794a-571c-b729-6bb68d311115")
_CONTENT = "1" * 64
_MANIFEST_DATASET = "2" * 64
_ROWS = "3" * 64
_CODE = "4" * 64
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
    """Expose only the catalog seam the ML report application method consumes."""

    def __init__(self) -> None:
        self._universe = InvestmentAnalystApplication.create_default().list_market_assets()

    def market_assets(self) -> MarketAssetUniverse:
        return self._universe


def _sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _descriptor(path: Path) -> MlArtifactFile:
    payload = path.read_bytes()
    return MlArtifactFile(name=path.name, sha256=_sha(payload), bytes=len(payload))


def _minimal_artifact(root: Path) -> UUID:
    run_id = uuid4()
    run_dir = root / str(run_id)
    run_dir.mkdir(parents=True)
    protocol = MlBaselineProtocol(
        dataset_id=_DATASET_ID,
        dataset_content_sha256=_CONTENT,
        decision_rows=100,
        asset_id=_ASSET,
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
    protocol_payload = canonical_analytical_json(protocol.model_dump(mode="json"))
    (run_dir / PROTOCOL_FILE_NAME).write_bytes(protocol_payload)
    protocol_descriptor = _descriptor(run_dir / PROTOCOL_FILE_NAME)
    report = MlBaselineReport(
        run_id=run_id,
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
    (run_dir / REPORT_FILE_NAME).write_bytes(
        canonical_analytical_json(report.model_dump(mode="json"))
    )
    report_descriptor = _descriptor(run_dir / REPORT_FILE_NAME)
    manifest = MlBaselineManifest(
        run_id=run_id,
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
    (run_dir / MANIFEST_FILE_NAME).write_bytes(
        canonical_analytical_json(manifest.model_dump(mode="json"))
    )
    return run_id


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


def _get(root: str, query: str) -> tuple[int, dict[str, object]]:
    try:
        with urlopen(f"{root}/api/v1/research/ml-report?{query}", timeout=5) as response:
            return response.status, json.loads(response.read())
    except HTTPError as error:
        return error.code, json.loads(error.read())


def _post(root: str, query: str) -> tuple[int, dict[str, object]]:
    request = Request(
        f"{root}/api/v1/research/ml-report?{query}",
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
def ml_root(tmp_path: Path) -> Path:
    return tmp_path / "ml-results"


def test_report_endpoint_returns_the_versioned_projection(ml_root: Path) -> None:
    run_id = _minimal_artifact(ml_root)
    application = AaplLocalWebApplication(
        cast(object, _CatalogController()),
        None,
        ml_reports=MlResearchReportService(ml_root),
    )

    with _server(application) as root:
        status, payload = _get(root, f"asset_id={_ASSET}&run_id={run_id}")

    assert status == 200
    assert payload["schema_version"] == "ml-research-report-view-v1"
    assert payload["provenance"]["asset_id"] == _ASSET  # type: ignore[index]
    assert payload["artifact"]["run_id"] == str(run_id)  # type: ignore[index]
    assert payload["artifact"]["status"] == "insufficient_evaluation_sample"  # type: ignore[index]
    assert payload["development"] is None
    assert payload["holdout"] is None
    assert payload["insufficiency_reason"] == "fixture sample below the preregistered floors"
    assert isinstance(payload["timings"]["read_seconds"], str)  # type: ignore[index]
    assert str(ml_root) not in json.dumps(payload)


def test_report_endpoint_rejects_invalid_queries_without_filesystem_parameters(
    ml_root: Path,
) -> None:
    run_id = _minimal_artifact(ml_root)
    application = AaplLocalWebApplication(
        cast(object, _CatalogController()),
        None,
        ml_reports=MlResearchReportService(ml_root),
    )
    invalid = (
        f"run_id={run_id}",
        f"asset_id={_ASSET}",
        f"asset_id={_ASSET}&asset_id={_ASSET}&run_id={run_id}",
        f"asset_id={_ASSET}&run_id={run_id}&path=/etc/passwd",
        f"asset_id={_ASSET}&run_id=not-a-uuid",
        f"asset_id={_ASSET}&run_id={str(run_id).upper()}",
        f"asset_id=equity:us:not-in-catalog&run_id={run_id}",
    )
    with _server(application) as root:
        for query in invalid:
            status, payload = _get(root, query)
            assert status == 400, query
            assert payload["error"]["code"] == "invalid_request"  # type: ignore[index]
            assert str(ml_root) not in json.dumps(payload)


def test_report_endpoint_maps_missing_run_asset_mismatch_and_method(ml_root: Path) -> None:
    run_id = _minimal_artifact(ml_root)
    application = AaplLocalWebApplication(
        cast(object, _CatalogController()),
        None,
        ml_reports=MlResearchReportService(ml_root),
    )
    with _server(application) as root:
        missing_status, missing = _get(root, f"asset_id={_ASSET}&run_id={uuid4()}")
        mismatch_status, mismatch = _get(root, f"asset_id=equity:us:aapl&run_id={run_id}")
        post_status, post = _post(root, f"asset_id={_ASSET}&run_id={run_id}")

    assert missing_status == 404
    assert missing["error"]["code"] == "ml_report_not_found"  # type: ignore[index]
    assert mismatch_status == 409
    assert mismatch["error"]["code"] == "ml_report_conflict"  # type: ignore[index]
    assert post_status == 404
    assert post["error"]["code"] == "not_found"  # type: ignore[index]
    for payload in (missing, mismatch, post):
        assert str(ml_root) not in json.dumps(payload)


def test_report_endpoint_conflict_when_service_is_not_configured() -> None:
    application = AaplLocalWebApplication(cast(object, _CatalogController()), None)

    with _server(application) as root:
        status, payload = _get(root, f"asset_id={_ASSET}&run_id={uuid4()}")

    assert status == 409
    assert payload["error"]["code"] == "ml_report_conflict"  # type: ignore[index]


def test_report_endpoint_reuses_the_bounded_projection_on_reopening(ml_root: Path) -> None:
    run_id = _minimal_artifact(ml_root)
    service = MlResearchReportService(ml_root)
    application = AaplLocalWebApplication(
        cast(object, _CatalogController()),
        None,
        ml_reports=service,
    )

    with _server(application) as root:
        first_status, first = _get(root, f"asset_id={_ASSET}&run_id={run_id}")
        second_status, second = _get(root, f"asset_id={_ASSET}&run_id={run_id}")

    assert (first_status, second_status) == (200, 200)
    assert first == second
    performance = service.performance()
    assert performance["full_verifications"] == 1
    assert performance["cache_hits"] == 1
