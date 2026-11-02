"""Sealed, verifiable artifacts for the offline ML baseline study.

Artifact reading intentionally depends only on core packages so that a sealed
report stays readable in an environment without the optional ML dependencies.
"""

from __future__ import annotations

import hashlib
import os
import time
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import UUID

import duckdb

from investment_analyst.analytics.analytical_access_models import canonical_analytical_json
from investment_analyst.core.models.base import ContractModel
from investment_analyst.research.historical_export import (
    HistoricalExportManifest,
    read_historical_dataset_view,
)
from investment_analyst.research.historical_models import (
    EXPERIMENT_SPEC_ID,
    FEATURE_SET_VERSION,
)
from investment_analyst.research.historical_models import (
    MANIFEST_FILE_NAME as DATASET_MANIFEST_FILE_NAME,
)
from investment_analyst.research.historical_models import (
    ROWS_FILE_NAME as DATASET_ROWS_FILE_NAME,
)
from investment_analyst.research.ml_models import (
    DECISION_END,
    DECISION_START,
    MANIFEST_FILE_NAME,
    MAX_DECISION_ROWS,
    MAX_INPUT_FILE_BYTES,
    MAX_ROOT_BYTES,
    PREDICTIONS_FILE_NAME,
    PROTOCOL_FILE_NAME,
    REPORT_FILE_NAME,
    SELECTION_FILE_NAME,
    MlArtifactFile,
    MlArtifactStatus,
    MlBaselineManifest,
    MlBaselineProtocol,
    MlBaselineReceipt,
    MlBaselineReport,
    MlBaselineSelection,
    MlFoldBoundary,
    MlHoldoutStatus,
    MlModelState,
    MlRunStatus,
    MlTimings,
    development_fold_boundaries,
    run_identity,
)

if TYPE_CHECKING:
    from investment_analyst.research.ml_evaluation import MlPredictionRow, MlSample

_CODE_MATERIAL_FILES = (
    "ml_adapter.py",
    "ml_evaluation.py",
    "ml_export.py",
    "ml_models.py",
)
_SEALED_FILES = (
    PROTOCOL_FILE_NAME,
    SELECTION_FILE_NAME,
    PREDICTIONS_FILE_NAME,
    REPORT_FILE_NAME,
    MANIFEST_FILE_NAME,
)
_PREDICTIONS_COLUMNS = (
    "sample_key VARCHAR",
    "decision_at VARCHAR",
    "stage VARCHAR",
    "fold INTEGER",
    "scorer VARCHAR",
    "label INTEGER",
    "probability DOUBLE",
)


class MlExportError(RuntimeError):
    """Base error for sealed ML artifacts."""


class MlExportPathError(MlExportError, ValueError):
    """A requested path is unsafe, overlapping or inside a protected root."""


class MlExportConflictError(MlExportError):
    """Sealed content differs from the requested run and must not be overwritten."""


class MlExportCapError(MlExportError):
    """A declared physical cap is exceeded."""


def _canonical_bytes(model: ContractModel) -> bytes:
    return canonical_analytical_json(model.model_dump(mode="json"))


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _sha256_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def code_material_sha256() -> str:
    """Digest the four material ML modules that define this study."""
    digest = hashlib.sha256()
    directory = Path(__file__).resolve().parent
    for name in _CODE_MATERIAL_FILES:
        path = directory / name
        digest.update(name.encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _validate_dataset_dir(dataset_dir: Path) -> Path:
    if not dataset_dir.is_absolute():
        raise MlExportPathError("dataset directory must be an absolute path")
    if dataset_dir.is_symlink():
        raise MlExportPathError("dataset directory must not be a symlink")
    resolved = dataset_dir.resolve()
    if not resolved.is_dir():
        raise MlExportPathError("dataset directory does not exist")
    for name in (DATASET_MANIFEST_FILE_NAME, DATASET_ROWS_FILE_NAME):
        path = resolved / name
        if path.is_symlink() or not path.is_file():
            raise MlExportPathError(f"dataset entry {name} is missing or is a symlink")
        if path.stat().st_size > MAX_INPUT_FILE_BYTES:
            raise MlExportCapError(f"dataset entry {name} exceeds the input byte cap")
    return resolved


def _forbidden_roots() -> tuple[Path, ...]:
    repository = Path(__file__).resolve().parents[3]
    data_root = Path("~/.local/share/investment-analyst").expanduser()
    return (
        repository,
        data_root / "workspaces",
        data_root / "runtime",
        data_root / "ops",
    )


def _validate_output_dir(output_dir: Path, dataset_dir: Path) -> Path:
    if not output_dir.is_absolute():
        raise MlExportPathError("output directory must be an absolute path")
    if output_dir.is_symlink():
        raise MlExportPathError("output directory must not be a symlink")
    resolved = output_dir.resolve()
    for forbidden in _forbidden_roots():
        forbidden_resolved = forbidden.resolve()
        if resolved == forbidden_resolved or resolved.is_relative_to(forbidden_resolved):
            raise MlExportPathError("output directory is inside a protected root")
    if resolved.is_relative_to(dataset_dir) or dataset_dir.is_relative_to(resolved):
        raise MlExportPathError("output and dataset directories must not overlap")
    resolved.mkdir(parents=True, exist_ok=True)
    return resolved


def _verified_checkpoint(path: Path, payload: bytes) -> None:
    if path.exists():
        if path.is_symlink() or not path.is_file():
            raise MlExportConflictError(f"{path.name} is not a regular sealed file")
        if path.read_bytes() != payload:
            raise MlExportConflictError(f"{path.name} differs from the sealed content")
        return
    with path.open("xb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())


def _write_atomic(path: Path, payload: bytes) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    with temporary.open("wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _prediction_records(
    predictions: tuple[MlPredictionRow, ...],
) -> tuple[tuple[object, ...], ...]:
    rows = [
        (
            prediction.sample_key,
            prediction.decision_at.isoformat().replace("+00:00", "Z"),
            prediction.stage,
            prediction.fold,
            prediction.scorer.value,
            prediction.label,
            prediction.probability,
        )
        for prediction in predictions
    ]
    rows.sort(key=lambda row: (row[2], row[3], row[1], row[0], row[4]))
    return tuple(rows)


def _write_predictions(path: Path, predictions: tuple[MlPredictionRow, ...]) -> None:
    rows = _prediction_records(predictions)
    temporary = path.with_name(f".{path.name}.tmp")
    connection = duckdb.connect(":memory:")
    try:
        connection.execute("SET threads=1")
        connection.execute("CREATE TABLE predictions (" + ", ".join(_PREDICTIONS_COLUMNS) + ")")
        connection.executemany("INSERT INTO predictions VALUES (?, ?, ?, ?, ?, ?, ?)", rows)
        connection.execute(
            "COPY predictions TO '"
            + str(temporary).replace("'", "''")
            + "' (FORMAT PARQUET, COMPRESSION ZSTD)"
        )
    finally:
        connection.close()
    if path.exists():
        if path.is_symlink() or not path.is_file():
            raise MlExportConflictError("predictions path is not a regular file")
        if path.read_bytes() != temporary.read_bytes():
            temporary.unlink()
            raise MlExportConflictError("existing predictions differ from this run")
        temporary.unlink()
        return
    os.replace(temporary, path)


def _prediction_count(path: Path) -> int:
    connection = duckdb.connect(":memory:")
    try:
        connection.execute("SET threads=1")
        count = connection.execute("SELECT count(*) FROM read_parquet(?)", [str(path)]).fetchone()
    finally:
        connection.close()
    if count is None:
        raise MlExportConflictError("predictions parquet is unreadable")
    return int(count[0])


def peak_rss_bytes() -> int | None:
    """Measure the observed process peak; absence is UNKNOWN, never zero."""
    try:
        import resource
    except ImportError:  # pragma: no cover - Windows without resource
        return None
    usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    if usage <= 0:
        return None
    return int(usage) * 1024


def build_protocol(
    dataset: HistoricalExportManifest, samples: tuple[MlSample, ...]
) -> MlBaselineProtocol:
    """Seal the complete preregistration before any fit is executed."""
    manifest = dataset.dataset
    if manifest.asset.asset_id != "crypto:btc-usd":
        raise MlExportError("dataset asset is outside the preregistered scope")
    if manifest.experiment_spec_id != EXPERIMENT_SPEC_ID:
        raise MlExportError("dataset experiment spec is outside the preregistered scope")
    if manifest.feature_set_version != FEATURE_SET_VERSION:
        raise MlExportError("dataset feature set is outside the preregistered scope")
    if manifest.request.start_date != DECISION_START:
        raise MlExportError("dataset start date is outside the preregistered scope")
    if manifest.request.end_date != DECISION_END:
        raise MlExportError("dataset end date is outside the preregistered scope")
    if len(samples) != len(manifest.rows):
        raise MlExportError("offline projections disagree with the dataset row count")
    boundaries = development_fold_boundaries(len(samples))
    folds = tuple(
        MlFoldBoundary(
            fold=index + 1,
            train_end_index=start,
            validation_start_index=start,
            validation_end_index=end,
            validation_start_date=samples[start].decision_at.date(),
            validation_end_date=samples[end].decision_at.date(),
        )
        for index, (start, end) in enumerate(boundaries)
    )
    return MlBaselineProtocol(
        dataset_id=manifest.dataset_id,
        dataset_content_sha256=manifest.dataset_content_sha256,
        decision_rows=len(samples),
        asset_id=manifest.asset.asset_id,
        folds=folds,
    )


def _artifact_file(path: Path) -> MlArtifactFile:
    digest, size = _sha256_file(path)
    return MlArtifactFile(name=path.name, sha256=digest, bytes=size)


def _manifest_bytes(
    *,
    run_id: UUID,
    status: MlArtifactStatus,
    dataset_id: UUID,
    dataset_content_sha256: str,
    dataset_manifest_sha256: str,
    dataset_rows_sha256: str,
    code_material_sha256_value: str,
    environment: object,
    run_dir: Path,
) -> bytes:
    files = {name: run_dir / name for name in _SEALED_FILES if (run_dir / name).is_file()}
    manifest = MlBaselineManifest(
        run_id=run_id,
        status=status,
        dataset_id=dataset_id,
        dataset_content_sha256=dataset_content_sha256,
        dataset_manifest_sha256=dataset_manifest_sha256,
        dataset_rows_sha256=dataset_rows_sha256,
        code_material_sha256=code_material_sha256_value,
        environment=environment,
        protocol=_artifact_file(files[PROTOCOL_FILE_NAME]),
        selection=(
            _artifact_file(files[SELECTION_FILE_NAME]) if SELECTION_FILE_NAME in files else None
        ),
        predictions=(
            _artifact_file(files[PREDICTIONS_FILE_NAME]) if PREDICTIONS_FILE_NAME in files else None
        ),
        report=_artifact_file(files[REPORT_FILE_NAME]),
        root_bytes=sum(_artifact_file(path).bytes for path in files.values()),
        created_at=datetime.now(UTC),
    )
    return _canonical_bytes(manifest)


def _read_manifest(report_bytes: bytes) -> MlBaselineReport:
    try:
        report = MlBaselineReport.model_validate_json(report_bytes)
    except ValueError as error:
        raise MlExportConflictError("report.json is malformed") from error
    if _canonical_bytes(report) != report_bytes:
        raise MlExportConflictError("report.json is not in canonical encoding")
    return report


def _read_selection(path: Path) -> MlBaselineSelection:
    if path.is_symlink() or not path.is_file():
        raise MlExportConflictError("selection.json is missing or is a symlink")
    payload = path.read_bytes()
    try:
        selection = MlBaselineSelection.model_validate_json(payload)
    except ValueError as error:
        raise MlExportConflictError("selection.json is malformed") from error
    if _canonical_bytes(selection) != payload:
        raise MlExportConflictError("selection.json is not in canonical encoding")
    return selection


def _receipt(
    *,
    run_id: UUID,
    status: MlRunStatus,
    report: MlBaselineReport,
    manifest_bytes: bytes,
    output_dir: Path,
    run_dir: Path,
    elapsed: float,
) -> MlBaselineReceipt:
    report_bytes = (run_dir / REPORT_FILE_NAME).read_bytes()
    return MlBaselineReceipt(
        run_id=run_id,
        status=status,
        report_status=report.status,
        decision=(None if report.development is None else report.development.decision),
        holdout_status=(
            MlHoldoutStatus.NOT_EVALUATED if report.holdout is None else report.holdout.status
        ),
        dataset_id=report.dataset_id,
        dataset_content_sha256=report.dataset_content_sha256,
        output_dir=str(output_dir),
        artifact_dir=str(run_dir),
        protocol_sha256=report.protocol_sha256,
        report_sha256=_sha256_bytes(report_bytes),
        manifest_sha256=_sha256_bytes(manifest_bytes),
        prediction_rows=report.prediction_rows,
        development_fits=report.development_fits,
        holdout_fits=report.holdout_fits,
        holdout_accesses=report.holdout_accesses,
        artifact_bytes=sum(
            (run_dir / name).stat().st_size for name in _SEALED_FILES if (run_dir / name).is_file()
        ),
        elapsed_seconds=_decimal_text(elapsed),
        peak_rss_bytes=peak_rss_bytes(),
    )


def read_ml_baseline_artifact(artifact_dir: Path) -> tuple[MlBaselineManifest, MlBaselineReport]:
    """Fully verify one sealed run directory without importing ML dependencies."""
    if artifact_dir.is_symlink() or not artifact_dir.is_dir():
        raise MlExportConflictError("artifact directory is missing or is a symlink")
    manifest_path = artifact_dir / MANIFEST_FILE_NAME
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise MlExportConflictError("manifest.json is missing or is a symlink")
    manifest_bytes = manifest_path.read_bytes()
    if len(manifest_bytes) > MAX_ROOT_BYTES:
        raise MlExportCapError("artifact manifest exceeds the byte cap")
    try:
        manifest = MlBaselineManifest.model_validate_json(manifest_bytes)
    except ValueError as error:
        raise MlExportConflictError("manifest.json is malformed") from error
    if _canonical_bytes(manifest) != manifest_bytes:
        raise MlExportConflictError("manifest.json is not in canonical encoding")
    descriptors = [manifest.protocol, manifest.report]
    descriptors.extend(
        item for item in (manifest.selection, manifest.predictions) if item is not None
    )
    for descriptor in descriptors:
        path = artifact_dir / descriptor.name
        if path.is_symlink() or not path.is_file():
            raise MlExportConflictError(f"{descriptor.name} is missing or is a symlink")
        digest, size = _sha256_file(path)
        if digest != descriptor.sha256 or size != descriptor.bytes:
            raise MlExportConflictError(f"{descriptor.name} differs from its manifest")
    report_bytes = (artifact_dir / REPORT_FILE_NAME).read_bytes()
    report = _read_manifest(report_bytes)
    if (
        report.run_id != manifest.run_id
        or report.status is not manifest.status
        or report.dataset_id != manifest.dataset_id
        or report.dataset_content_sha256 != manifest.dataset_content_sha256
        or report.dataset_manifest_sha256 != manifest.dataset_manifest_sha256
        or report.protocol_sha256 != manifest.protocol.sha256
        or report.environment != manifest.environment
    ):
        raise MlExportConflictError("report and manifest disagree on the sealed run")
    if manifest.predictions is None:
        if report.predictions_sha256 is not None:
            raise MlExportConflictError("report references absent predictions")
    elif report.predictions_sha256 != manifest.predictions.sha256:
        raise MlExportConflictError("report predictions digest differs from the manifest")
    if manifest.selection is None:
        if report.selection_sha256 is not None:
            raise MlExportConflictError("report references an absent selection")
    else:
        selection = _read_selection(artifact_dir / SELECTION_FILE_NAME)
        if (
            report.selection_sha256 != manifest.selection.sha256
            or selection.run_id != manifest.run_id
            or selection.dataset_id != manifest.dataset_id
            or selection.dataset_content_sha256 != manifest.dataset_content_sha256
            or selection.protocol_sha256 != manifest.protocol.sha256
            or selection.code_material_sha256 != manifest.code_material_sha256
        ):
            raise MlExportConflictError("selection disagrees with the sealed run")
    if manifest.predictions is not None:
        rows = _prediction_count(artifact_dir / PREDICTIONS_FILE_NAME)
        if rows != report.prediction_rows:
            raise MlExportConflictError("predictions disagree with the sealed report")
    return manifest, report


def _verify_report_coherence(
    *,
    report: MlBaselineReport,
    run_id: UUID,
    protocol_sha256: str,
    dataset_manifest_sha256: str,
    dataset_content_sha256: str,
    run_dir: Path,
) -> None:
    if (
        report.run_id != run_id
        or report.protocol_sha256 != protocol_sha256
        or report.dataset_manifest_sha256 != dataset_manifest_sha256
        or report.dataset_content_sha256 != dataset_content_sha256
    ):
        raise MlExportConflictError("existing report belongs to different inputs")
    if report.predictions_sha256 is not None:
        predictions_path = run_dir / PREDICTIONS_FILE_NAME
        if predictions_path.is_symlink() or not predictions_path.is_file():
            raise MlExportConflictError("existing report references missing predictions")
        digest, size = _sha256_file(predictions_path)
        if digest != report.predictions_sha256:
            raise MlExportConflictError("existing predictions differ from the report")
        if size > MAX_ROOT_BYTES:
            raise MlExportCapError("predictions exceed the artifact cap")
    if report.selection_sha256 is not None:
        selection_path = run_dir / SELECTION_FILE_NAME
        if selection_path.is_symlink() or not selection_path.is_file():
            raise MlExportConflictError("existing report references a missing selection")
        digest, _ = _sha256_file(selection_path)
        if digest != report.selection_sha256:
            raise MlExportConflictError("existing selection differs from the report")


def evaluate_dataset(*, dataset_dir: Path, output_dir: Path) -> MlBaselineReceipt:
    """Evaluate, seal and reuse one run without opening the operational workspace."""
    from investment_analyst.research import ml_adapter, ml_evaluation

    started = time.perf_counter()
    dataset_path = _validate_dataset_dir(dataset_dir)
    output_path = _validate_output_dir(output_dir, dataset_path)
    manifest_bytes = (dataset_path / DATASET_MANIFEST_FILE_NAME).read_bytes()
    if len(manifest_bytes) > MAX_INPUT_FILE_BYTES:
        raise MlExportCapError("dataset manifest exceeds the input byte cap")
    dataset_manifest_sha256 = _sha256_bytes(manifest_bytes)
    rows_sha256, rows_bytes = _sha256_file(dataset_path / DATASET_ROWS_FILE_NAME)
    if rows_bytes > MAX_INPUT_FILE_BYTES:
        raise MlExportCapError("dataset rows exceed the input byte cap")
    read_started = time.perf_counter()
    view = read_historical_dataset_view(dataset_path)
    samples = ml_evaluation.samples_from_view(view)
    read_seconds = time.perf_counter() - read_started
    if len(samples) > MAX_DECISION_ROWS:
        raise MlExportCapError("dataset exceeds the decision row cap")
    protocol = build_protocol(view.manifest, samples)
    protocol_payload = _canonical_bytes(protocol)
    protocol_sha256 = _sha256_bytes(protocol_payload)
    environment = ml_adapter.environment_versions()
    code_sha256 = code_material_sha256()
    run_id = run_identity(
        protocol_sha256=protocol_sha256,
        dataset_id=protocol.dataset_id,
        dataset_content_sha256=protocol.dataset_content_sha256,
        code_material_sha256=code_sha256,
        environment=environment,
    )
    run_dir = output_path / str(run_id)
    if run_dir.exists() and (run_dir.is_symlink() or not run_dir.is_dir()):
        raise MlExportConflictError("run directory is not a regular directory")
    if (run_dir / MANIFEST_FILE_NAME).is_file():
        manifest, report = read_ml_baseline_artifact(run_dir)
        _verify_report_coherence(
            report=report,
            run_id=run_id,
            protocol_sha256=protocol_sha256,
            dataset_manifest_sha256=dataset_manifest_sha256,
            dataset_content_sha256=protocol.dataset_content_sha256,
            run_dir=run_dir,
        )
        return _receipt(
            run_id=run_id,
            status=MlRunStatus.REUSED,
            report=report,
            manifest_bytes=(run_dir / MANIFEST_FILE_NAME).read_bytes(),
            output_dir=output_path,
            run_dir=run_dir,
            elapsed=time.perf_counter() - started,
        )
    run_dir.mkdir(parents=True, exist_ok=True)
    _verified_checkpoint(run_dir / PROTOCOL_FILE_NAME, protocol_payload)
    report_path = run_dir / REPORT_FILE_NAME
    if report_path.is_file():
        report = _read_manifest(report_path.read_bytes())
        _verify_report_coherence(
            report=report,
            run_id=run_id,
            protocol_sha256=protocol_sha256,
            dataset_manifest_sha256=dataset_manifest_sha256,
            dataset_content_sha256=protocol.dataset_content_sha256,
            run_dir=run_dir,
        )
        manifest_payload = _manifest_bytes(
            run_id=run_id,
            status=report.status,
            dataset_id=protocol.dataset_id,
            dataset_content_sha256=protocol.dataset_content_sha256,
            dataset_manifest_sha256=dataset_manifest_sha256,
            dataset_rows_sha256=rows_sha256,
            code_material_sha256_value=code_sha256,
            environment=report.environment,
            run_dir=run_dir,
        )
        _write_atomic(run_dir / MANIFEST_FILE_NAME, manifest_payload)
        return _receipt(
            run_id=run_id,
            status=MlRunStatus.REUSED,
            report=report,
            manifest_bytes=manifest_payload,
            output_dir=output_path,
            run_dir=run_dir,
            elapsed=time.perf_counter() - started,
        )
    fit_seconds = 0.0
    evaluation_seconds = 0.0
    selection_path = run_dir / SELECTION_FILE_NAME
    sealed_selection: MlBaselineSelection | None = None
    if selection_path.exists():
        sealed_selection = _read_selection(selection_path)
        if (
            sealed_selection.run_id != run_id
            or sealed_selection.dataset_id != protocol.dataset_id
            or sealed_selection.dataset_content_sha256 != protocol.dataset_content_sha256
            or sealed_selection.protocol_sha256 != protocol_sha256
            or sealed_selection.code_material_sha256 != code_sha256
        ):
            raise MlExportConflictError("existing selection belongs to different inputs")
    try:
        development_outcome = ml_evaluation.evaluate_development(samples, protocol)
    except ml_evaluation.MlInsufficientSampleError as error:
        report = MlBaselineReport(
            run_id=run_id,
            status=MlArtifactStatus.INSUFFICIENT_EVALUATION_SAMPLE,
            dataset_id=protocol.dataset_id,
            dataset_content_sha256=protocol.dataset_content_sha256,
            dataset_manifest_sha256=dataset_manifest_sha256,
            protocol_sha256=protocol_sha256,
            selection_sha256=None,
            development=None,
            holdout=None,
            insufficiency_reason=error.reason,
            environment=environment,
            predictions_sha256=None,
            prediction_rows=0,
            holdout_accesses=0,
            development_fits=0,
            holdout_fits=0,
            timings=MlTimings(
                read_seconds=_decimal_text(read_seconds),
                fit_seconds=_decimal_text(0.0),
                evaluation_seconds=_decimal_text(0.0),
                total_seconds=_decimal_text(time.perf_counter() - started),
            ),
            peak_rss_bytes=peak_rss_bytes(),
        )
        _write_atomic(report_path, _canonical_bytes(report))
        manifest_payload = _manifest_bytes(
            run_id=run_id,
            status=report.status,
            dataset_id=protocol.dataset_id,
            dataset_content_sha256=protocol.dataset_content_sha256,
            dataset_manifest_sha256=dataset_manifest_sha256,
            dataset_rows_sha256=rows_sha256,
            code_material_sha256_value=code_sha256,
            environment=environment,
            run_dir=run_dir,
        )
        _write_atomic(run_dir / MANIFEST_FILE_NAME, manifest_payload)
        return _receipt(
            run_id=run_id,
            status=MlRunStatus.EVALUATED,
            report=report,
            manifest_bytes=manifest_payload,
            output_dir=output_path,
            run_dir=run_dir,
            elapsed=time.perf_counter() - started,
        )
    fit_seconds += development_outcome.fit_seconds
    evaluation_seconds += development_outcome.evaluation_seconds
    development = development_outcome.development
    if sealed_selection is not None:
        if _canonical_bytes(sealed_selection.development) != _canonical_bytes(development):
            raise MlExportConflictError("recomputed development differs from the sealed selection")
        development = sealed_selection.development
    selection: MlBaselineSelection | None = sealed_selection
    holdout = ml_evaluation.unopened_holdout()
    holdout_predictions: tuple[object, ...] = ()
    holdout_fits = 0
    if development.decision is MlModelState.CANDIDATE_SELECTED:
        if development.selected_model is None:
            raise MlExportConflictError("selected development result lacks a model")
        if selection is None:
            selection = MlBaselineSelection(
                run_id=run_id,
                dataset_id=protocol.dataset_id,
                dataset_content_sha256=protocol.dataset_content_sha256,
                protocol_sha256=protocol_sha256,
                code_material_sha256=code_sha256,
                development=development,
                selected_model=development.selected_model,
                sealed_at=datetime.now(UTC),
            )
            _verified_checkpoint(selection_path, _canonical_bytes(selection))
        guard = ml_evaluation.MlHoldoutGuard(selection)
        holdout_outcome = ml_evaluation.evaluate_holdout(samples, protocol, guard)
        holdout = holdout_outcome.holdout
        holdout_predictions = holdout_outcome.predictions
        holdout_fits = holdout_outcome.fits
        fit_seconds += holdout_outcome.fit_seconds
        evaluation_seconds += holdout_outcome.evaluation_seconds
        holdout_accesses = holdout_outcome.accesses
    else:
        holdout_accesses = 0
    predictions = development_outcome.predictions + holdout_predictions
    predictions_path = run_dir / PREDICTIONS_FILE_NAME
    _write_predictions(predictions_path, predictions)
    predictions_digest, predictions_bytes = _sha256_file(predictions_path)
    if predictions_bytes > MAX_ROOT_BYTES:
        raise MlExportCapError("predictions exceed the artifact cap")
    selection_digest = None
    if selection is not None:
        selection_digest, _ = _sha256_file(selection_path)
    report = MlBaselineReport(
        run_id=run_id,
        status=MlArtifactStatus.COMPLETED,
        dataset_id=protocol.dataset_id,
        dataset_content_sha256=protocol.dataset_content_sha256,
        dataset_manifest_sha256=dataset_manifest_sha256,
        protocol_sha256=protocol_sha256,
        selection_sha256=selection_digest,
        development=development,
        holdout=holdout,
        insufficiency_reason=None,
        environment=environment,
        predictions_sha256=predictions_digest,
        prediction_rows=len(predictions),
        holdout_accesses=holdout_accesses,
        development_fits=development_outcome.fits,
        holdout_fits=holdout_fits,
        timings=MlTimings(
            read_seconds=_decimal_text(read_seconds),
            fit_seconds=_decimal_text(fit_seconds),
            evaluation_seconds=_decimal_text(evaluation_seconds),
            total_seconds=_decimal_text(time.perf_counter() - started),
        ),
        peak_rss_bytes=peak_rss_bytes(),
    )
    _write_atomic(report_path, _canonical_bytes(report))
    manifest_payload = _manifest_bytes(
        run_id=run_id,
        status=report.status,
        dataset_id=protocol.dataset_id,
        dataset_content_sha256=protocol.dataset_content_sha256,
        dataset_manifest_sha256=dataset_manifest_sha256,
        dataset_rows_sha256=rows_sha256,
        code_material_sha256_value=code_sha256,
        environment=environment,
        run_dir=run_dir,
    )
    _write_atomic(run_dir / MANIFEST_FILE_NAME, manifest_payload)
    read_ml_baseline_artifact(run_dir)
    return _receipt(
        run_id=run_id,
        status=MlRunStatus.EVALUATED,
        report=report,
        manifest_bytes=manifest_payload,
        output_dir=output_path,
        run_dir=run_dir,
        elapsed=time.perf_counter() - started,
    )


def _decimal_text(value: float) -> Decimal:
    return Decimal(repr(float(value)))


__all__ = [
    "MlExportCapError",
    "MlExportConflictError",
    "MlExportError",
    "MlExportPathError",
    "build_protocol",
    "code_material_sha256",
    "evaluate_dataset",
    "peak_rss_bytes",
    "read_ml_baseline_artifact",
]
