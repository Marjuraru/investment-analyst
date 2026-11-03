"""Sealed, verifiable artifacts for the offline incremental-funding study.

Artifact reading depends only on core packages so that a sealed result stays
readable in an environment without the optional ML dependencies.
"""

from __future__ import annotations

import hashlib
import os
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import UUID

import duckdb

from investment_analyst.analytics.analytical_access_models import canonical_analytical_json
from investment_analyst.core.models.base import ContractModel
from investment_analyst.research.funding_export import read_funding_training_view
from investment_analyst.research.ml_export import peak_rss_bytes
from investment_analyst.research.ml_funding_models import (
    MANIFEST_FILE_NAME,
    MAX_INPUT_FILE_BYTES,
    MAX_ROOT_BYTES,
    PREDICTIONS_FILE_NAME,
    PROTOCOL_FILE_NAME,
    REPORT_FILE_NAME,
    FundingArtifactStatus,
    FundingManifest,
    FundingProtocol,
    FundingReceipt,
    FundingReport,
    FundingRunStatus,
    funding_run_identity,
)
from investment_analyst.research.ml_models import MlArtifactFile, MlEnvironment, MlTimings

if TYPE_CHECKING:
    from investment_analyst.research.ml_funding_evaluation import FundingPredictionRow

_CODE_MATERIAL_FILES = (
    "ml_funding_models.py",
    "ml_funding_evaluation.py",
    "ml_funding_export.py",
    "ml_adapter.py",
    "ml_models.py",
    "funding_export.py",
    "funding_models.py",
    "historical_export.py",
    "historical_models.py",
)
_SEALED_FILES = (
    PROTOCOL_FILE_NAME,
    PREDICTIONS_FILE_NAME,
    REPORT_FILE_NAME,
    MANIFEST_FILE_NAME,
)
_ALLOWED_NAMES = frozenset(_SEALED_FILES)
_PREDICTIONS_COLUMNS = (
    "sample_key VARCHAR",
    "decision_at VARCHAR",
    "fold INTEGER",
    "scorer VARCHAR",
    "label INTEGER",
    "probability DOUBLE",
)
_PREDICTION_NAMES = tuple(definition.split()[0] for definition in _PREDICTIONS_COLUMNS)


class FundingMlExportError(RuntimeError):
    """Base error for sealed incremental-funding artifacts."""


class FundingMlExportPathError(FundingMlExportError, ValueError):
    """A requested path is unsafe, overlapping or inside a protected root."""


class FundingMlExportConflictError(FundingMlExportError):
    """Sealed content differs from the requested run and must not be overwritten."""


class FundingMlExportCapError(FundingMlExportError):
    """A declared physical cap is exceeded."""


@dataclass(frozen=True, slots=True)
class LoadedFundingArtifact:
    """Verified sealed artifact; a non-evaluable run has no manifest."""

    manifest: FundingManifest | None
    protocol: FundingProtocol
    report: FundingReport


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
    """Digest the ordered material modules that define folds, inputs and identity."""
    digest = hashlib.sha256()
    directory = Path(__file__).resolve().parent
    for name in _CODE_MATERIAL_FILES:
        digest.update(name.encode("utf-8"))
        digest.update((directory / name).read_bytes())
    return digest.hexdigest()


def _forbidden_roots() -> tuple[Path, ...]:
    repository = Path(__file__).resolve().parents[3]
    data_root = Path("~/.local/share/investment-analyst").expanduser()
    return (
        repository,
        data_root / "workspaces",
        data_root / "runtime",
        data_root / "ops",
    )


def _validate_input_dir(path: Path, label: str) -> Path:
    if not path.is_absolute():
        raise FundingMlExportPathError(f"{label} must be an absolute path")
    if path.is_symlink():
        raise FundingMlExportPathError(f"{label} must not be a symlink")
    resolved = path.resolve()
    if not resolved.is_dir():
        raise FundingMlExportPathError(f"{label} does not exist")
    for name in (MANIFEST_FILE_NAME,):
        candidate = resolved / name
        if candidate.is_symlink() or not candidate.is_file():
            raise FundingMlExportPathError(f"{label} entry {name} is missing or is a symlink")
        if candidate.stat().st_size > MAX_INPUT_FILE_BYTES:
            raise FundingMlExportCapError(f"{label} entry {name} exceeds the input byte cap")
    return resolved


def _validate_output_dir(output_dir: Path, inputs: tuple[Path, ...]) -> Path:
    if not output_dir.is_absolute():
        raise FundingMlExportPathError("output directory must be an absolute path")
    declared = output_dir.expanduser()
    for candidate in (declared, *declared.parents):
        if candidate.is_symlink():
            raise FundingMlExportPathError("output directory must not traverse a symbolic link")
    resolved = declared.resolve(strict=False)
    if resolved == Path(resolved.anchor):
        raise FundingMlExportPathError("output directory must not be the filesystem root")
    for forbidden in _forbidden_roots():
        root = forbidden.expanduser().resolve(strict=False)
        if resolved == root or root in resolved.parents:
            raise FundingMlExportPathError(
                f"output directory must stay outside the protected root {root}"
            )
    for source in inputs:
        if resolved == source or source in resolved.parents or resolved in source.parents:
            raise FundingMlExportPathError("output directory must not overlap an input")
    if resolved.exists() and (resolved.is_symlink() or not resolved.is_dir()):
        raise FundingMlExportConflictError("output directory must be a plain directory")
    resolved.mkdir(parents=True, exist_ok=True)
    return resolved


def _write_atomic(path: Path, payload: bytes) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    with temporary.open("wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _verified_checkpoint(path: Path, payload: bytes) -> None:
    if path.exists():
        if path.is_symlink() or not path.is_file():
            raise FundingMlExportConflictError(f"{path.name} is not a regular sealed file")
        if path.read_bytes() != payload:
            raise FundingMlExportConflictError(f"{path.name} differs from the sealed content")
        return
    with path.open("xb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())


def _prediction_records(
    predictions: tuple[FundingPredictionRow, ...],
) -> tuple[tuple[object, ...], ...]:
    rows = [
        (
            prediction.sample_key,
            prediction.decision_at.isoformat().replace("+00:00", "Z"),
            prediction.fold,
            prediction.scorer.value,
            prediction.label,
            prediction.probability,
        )
        for prediction in predictions
    ]
    rows.sort(key=lambda row: (row[2], row[1], row[0], row[3]))
    return tuple(rows)


def _write_predictions(path: Path, records: tuple[tuple[object, ...], ...]) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    connection = duckdb.connect(":memory:")
    try:
        connection.execute("SET threads=1")
        connection.execute("CREATE TABLE predictions (" + ", ".join(_PREDICTIONS_COLUMNS) + ")")
        connection.executemany(
            "INSERT INTO predictions VALUES (?, ?, ?, ?, ?, ?)",
            records,
        )
        connection.execute(
            "COPY predictions TO '"
            + str(temporary).replace("'", "''")
            + "' (FORMAT PARQUET, COMPRESSION ZSTD)"
        )
    finally:
        connection.close()
    os.replace(temporary, path)


def _readback_predictions(path: Path) -> tuple[tuple[object, ...], ...]:
    connection = duckdb.connect(":memory:")
    try:
        connection.execute("SET threads=1")
        columns = ", ".join(_PREDICTION_NAMES)
        rows = connection.execute(
            f"SELECT {columns} FROM read_parquet(?) ORDER BY fold, decision_at, sample_key, scorer",
            [str(path)],
        ).fetchall()
    finally:
        connection.close()
    return tuple(tuple(row) for row in rows)


def _prediction_count(path: Path) -> int:
    connection = duckdb.connect(":memory:")
    try:
        connection.execute("SET threads=1")
        count = connection.execute("SELECT count(*) FROM read_parquet(?)", [str(path)]).fetchone()
    finally:
        connection.close()
    if count is None:
        raise FundingMlExportConflictError("predictions parquet is unreadable")
    return int(count[0])


def _artifact_file(path: Path) -> MlArtifactFile:
    digest, size = _sha256_file(path)
    if size > MAX_ROOT_BYTES:
        raise FundingMlExportCapError(f"{path.name} exceeds the output byte cap")
    return MlArtifactFile(name=path.name, sha256=digest, bytes=size)


def _manifest_bytes(
    *,
    run_id: UUID,
    protocol: FundingProtocol,
    report: FundingReport,
    run_dir: Path,
) -> bytes:
    files = {name: run_dir / name for name in _SEALED_FILES if (run_dir / name).is_file()}
    manifest = FundingManifest(
        run_id=run_id,
        dataset_id=protocol.dataset_id,
        dataset_content_sha256=protocol.dataset_content_sha256,
        dataset_manifest_sha256=report.dataset_manifest_sha256,
        bundle_id=protocol.bundle_id,
        bundle_content_sha256=protocol.bundle_content_sha256,
        bundle_manifest_sha256=report.bundle_manifest_sha256,
        code_material_sha256=report.code_material_sha256,
        code_material_files=report.code_material_files,
        environment=report.environment,
        criteria=report.criteria,
        protocol=_artifact_file(files[PROTOCOL_FILE_NAME]),
        predictions=_artifact_file(files[PREDICTIONS_FILE_NAME]),
        report=_artifact_file(files[REPORT_FILE_NAME]),
        root_bytes=sum(_artifact_file(path).bytes for path in files.values()),
        created_at=datetime.now(UTC),
    )
    return _canonical_bytes(manifest)


def _parse_canonical[T: ContractModel](model: type[T], payload: bytes, label: str) -> T:
    try:
        parsed = model.model_validate_json(payload)
    except ValueError as error:
        raise FundingMlExportConflictError(f"{label} is malformed") from error
    if _canonical_bytes(parsed) != payload:
        raise FundingMlExportConflictError(f"{label} is not in canonical encoding")
    return parsed


def read_funding_ml_artifact(artifact_dir: Path) -> LoadedFundingArtifact:
    """Fully verify one run directory without importing ML dependencies."""
    if artifact_dir.is_symlink() or not artifact_dir.is_dir():
        raise FundingMlExportConflictError("artifact directory is missing or is a symlink")
    entries = tuple(artifact_dir.iterdir())
    names = {entry.name for entry in entries}
    if not names or not names <= _ALLOWED_NAMES:
        raise FundingMlExportConflictError("artifact directory contains unexpected entries")
    for entry in entries:
        if entry.is_symlink() or not entry.is_file():
            raise FundingMlExportConflictError("sealed entry must be a regular file")
    if PROTOCOL_FILE_NAME not in names or REPORT_FILE_NAME not in names:
        raise FundingMlExportConflictError("artifact directory is incomplete")
    protocol_payload = (artifact_dir / PROTOCOL_FILE_NAME).read_bytes()
    if len(protocol_payload) > MAX_ROOT_BYTES:
        raise FundingMlExportCapError("artifact protocol exceeds the byte cap")
    protocol = _parse_canonical(FundingProtocol, protocol_payload, "protocol.json")
    report = _parse_canonical(
        FundingReport, (artifact_dir / REPORT_FILE_NAME).read_bytes(), "report.json"
    )
    if report.protocol_sha256 != _sha256_bytes(protocol_payload):
        raise FundingMlExportConflictError("report protocol digest differs from the sealed file")
    if (
        report.dataset_id != protocol.dataset_id
        or report.dataset_content_sha256 != protocol.dataset_content_sha256
        or report.bundle_id != protocol.bundle_id
        or report.bundle_content_sha256 != protocol.bundle_content_sha256
        or report.criteria != protocol.criteria
    ):
        raise FundingMlExportConflictError("report and protocol disagree on the sealed inputs")
    manifest: FundingManifest | None = None
    if MANIFEST_FILE_NAME in names:
        manifest_payload = (artifact_dir / MANIFEST_FILE_NAME).read_bytes()
        manifest = _parse_canonical(FundingManifest, manifest_payload, "manifest.json")
        if report.status is not FundingArtifactStatus.COMPLETED:
            raise FundingMlExportConflictError("a non-evaluable run must not seal a manifest")
        descriptors = (manifest.protocol, manifest.predictions, manifest.report)
        if {descriptor.name for descriptor in descriptors} != {
            PROTOCOL_FILE_NAME,
            PREDICTIONS_FILE_NAME,
            REPORT_FILE_NAME,
        }:
            raise FundingMlExportConflictError("manifest descriptors differ from the sealed files")
        for descriptor in descriptors:
            path = artifact_dir / descriptor.name
            if path.is_symlink() or not path.is_file():
                raise FundingMlExportConflictError(f"{descriptor.name} is missing or is a symlink")
            digest, size = _sha256_file(path)
            if digest != descriptor.sha256 or size != descriptor.bytes:
                raise FundingMlExportConflictError(f"{descriptor.name} differs from its manifest")
        if (
            manifest.run_id != report.run_id
            or manifest.dataset_id != report.dataset_id
            or manifest.dataset_content_sha256 != report.dataset_content_sha256
            or manifest.dataset_manifest_sha256 != report.dataset_manifest_sha256
            or manifest.bundle_id != report.bundle_id
            or manifest.bundle_content_sha256 != report.bundle_content_sha256
            or manifest.bundle_manifest_sha256 != report.bundle_manifest_sha256
            or manifest.code_material_sha256 != report.code_material_sha256
            or manifest.code_material_files != report.code_material_files
            or manifest.environment != report.environment
            or manifest.criteria != report.criteria
        ):
            raise FundingMlExportConflictError("manifest and report disagree on the sealed run")
        if PREDICTIONS_FILE_NAME not in names:
            raise FundingMlExportConflictError("a completed run requires sealed predictions")
        rows = _prediction_count(artifact_dir / PREDICTIONS_FILE_NAME)
        if rows != report.prediction_rows:
            raise FundingMlExportConflictError("predictions disagree with the sealed report")
    elif PREDICTIONS_FILE_NAME in names:
        raise FundingMlExportConflictError("a non-evaluable run must not seal predictions")
    if manifest is None and report.status is FundingArtifactStatus.COMPLETED:
        raise FundingMlExportConflictError("a completed run requires a sealed manifest")
    return LoadedFundingArtifact(manifest=manifest, protocol=protocol, report=report)


def _receipt(
    *,
    run_id: UUID,
    status: FundingRunStatus,
    report: FundingReport,
    protocol_sha256: str,
    output_dir: Path,
    run_dir: Path,
    elapsed: float,
) -> FundingReceipt:
    manifest_path = run_dir / MANIFEST_FILE_NAME
    return FundingReceipt(
        run_id=run_id,
        status=status,
        report_status=report.status,
        decision=report.decision,
        holdout_status=report.holdout.status,
        dataset_id=report.dataset_id,
        dataset_content_sha256=report.dataset_content_sha256,
        bundle_id=report.bundle_id,
        bundle_content_sha256=report.bundle_content_sha256,
        output_dir=str(output_dir),
        artifact_dir=str(run_dir),
        protocol_sha256=protocol_sha256,
        report_sha256=_sha256_bytes((run_dir / REPORT_FILE_NAME).read_bytes()),
        manifest_sha256=(
            _sha256_bytes(manifest_path.read_bytes()) if manifest_path.is_file() else None
        ),
        prediction_rows=report.prediction_rows,
        development_fits=report.development_fits,
        artifact_bytes=sum(
            (run_dir / name).stat().st_size for name in _SEALED_FILES if (run_dir / name).is_file()
        ),
        elapsed_seconds=Decimal(repr(float(elapsed))),
        peak_rss_bytes=peak_rss_bytes(),
    )


def _failure_report(
    *,
    run_id: UUID,
    protocol: FundingProtocol,
    protocol_sha256: str,
    dataset_manifest_sha256: str,
    bundle_manifest_sha256: str,
    code_material_sha256_value: str,
    environment: MlEnvironment,
    status: FundingArtifactStatus,
    reason: str,
    read_seconds: float,
    started: float,
) -> FundingReport:
    return FundingReport(
        run_id=run_id,
        status=status,
        protocol_sha256=protocol_sha256,
        dataset_id=protocol.dataset_id,
        dataset_content_sha256=protocol.dataset_content_sha256,
        dataset_manifest_sha256=dataset_manifest_sha256,
        bundle_id=protocol.bundle_id,
        bundle_content_sha256=protocol.bundle_content_sha256,
        bundle_manifest_sha256=bundle_manifest_sha256,
        code_material_sha256=code_material_sha256_value,
        code_material_files=_CODE_MATERIAL_FILES,
        environment=environment,
        criteria=protocol.criteria,
        decision=None,
        folds=(),
        pooled_scores=(),
        pooled_comparisons=(),
        calibration=(),
        disposition_reason=reason,
        prediction_rows=0,
        development_fits=0,
        timings=MlTimings(
            read_seconds=Decimal(repr(float(read_seconds))),
            fit_seconds=Decimal(0),
            evaluation_seconds=Decimal(0),
            total_seconds=Decimal(repr(float(time.perf_counter() - started))),
        ),
        peak_rss_bytes=peak_rss_bytes(),
    )


def evaluate_funding_experiment(
    *,
    dataset_dir: Path,
    bundle_dir: Path,
    output_dir: Path,
) -> FundingReceipt:
    """Evaluate, seal and reuse one run without opening the operational workspace."""
    from investment_analyst.research import ml_adapter, ml_funding_evaluation
    from investment_analyst.research.ml_adapter import MlAdapterError

    started = time.perf_counter()
    dataset_path = _validate_input_dir(dataset_dir, "dataset directory")
    bundle_path = _validate_input_dir(bundle_dir, "funding bundle directory")
    output_path = _validate_output_dir(output_dir, (dataset_path, bundle_path))
    dataset_manifest_sha256 = _sha256_bytes((dataset_path / MANIFEST_FILE_NAME).read_bytes())
    bundle_manifest_sha256 = _sha256_bytes((bundle_path / MANIFEST_FILE_NAME).read_bytes())
    read_started = time.perf_counter()
    view = read_funding_training_view(bundle_path, dataset_path)
    protocol = ml_funding_evaluation.build_funding_protocol(view)
    samples = ml_funding_evaluation.funding_samples_from_view(view)
    read_seconds = time.perf_counter() - read_started
    protocol_payload = _canonical_bytes(protocol)
    protocol_sha256 = _sha256_bytes(protocol_payload)
    environment = ml_adapter.environment_versions()
    code_sha256 = code_material_sha256()
    run_id = funding_run_identity(
        protocol_sha256=protocol_sha256,
        dataset_id=protocol.dataset_id,
        dataset_content_sha256=protocol.dataset_content_sha256,
        bundle_id=protocol.bundle_id,
        bundle_content_sha256=protocol.bundle_content_sha256,
        code_material_sha256=code_sha256,
        environment=environment,
    )
    run_dir = output_path / str(run_id)
    if run_dir.exists():
        if run_dir.is_symlink() or not run_dir.is_dir():
            raise FundingMlExportConflictError("run directory is not a regular directory")
        entries = {entry.name for entry in run_dir.iterdir()}
        if not entries <= _ALLOWED_NAMES:
            raise FundingMlExportConflictError("run directory contains an unrelated entry")
        if MANIFEST_FILE_NAME in entries or REPORT_FILE_NAME in entries:
            loaded = read_funding_ml_artifact(run_dir)
            if _canonical_bytes(loaded.protocol) != protocol_payload:
                raise FundingMlExportConflictError("existing run sealed a different protocol")
            return _receipt(
                run_id=run_id,
                status=FundingRunStatus.REUSED,
                report=loaded.report,
                protocol_sha256=protocol_sha256,
                output_dir=output_path,
                run_dir=run_dir,
                elapsed=time.perf_counter() - started,
            )
    run_dir.mkdir(parents=True, exist_ok=True)
    _verified_checkpoint(run_dir / PROTOCOL_FILE_NAME, protocol_payload)
    try:
        outcome = ml_funding_evaluation.evaluate_funding_development(samples, protocol)
    except ml_funding_evaluation.FundingInsufficientSampleError as error:
        report = _failure_report(
            run_id=run_id,
            protocol=protocol,
            protocol_sha256=protocol_sha256,
            dataset_manifest_sha256=dataset_manifest_sha256,
            bundle_manifest_sha256=bundle_manifest_sha256,
            code_material_sha256_value=code_sha256,
            environment=environment,
            status=FundingArtifactStatus.INSUFFICIENT_SAMPLE,
            reason=error.reason,
            read_seconds=read_seconds,
            started=started,
        )
        _write_atomic(run_dir / REPORT_FILE_NAME, _canonical_bytes(report))
        read_funding_ml_artifact(run_dir)
        return _receipt(
            run_id=run_id,
            status=FundingRunStatus.EVALUATED,
            report=report,
            protocol_sha256=protocol_sha256,
            output_dir=output_path,
            run_dir=run_dir,
            elapsed=time.perf_counter() - started,
        )
    except (ml_funding_evaluation.FundingEvaluationError, MlAdapterError) as error:
        report = _failure_report(
            run_id=run_id,
            protocol=protocol,
            protocol_sha256=protocol_sha256,
            dataset_manifest_sha256=dataset_manifest_sha256,
            bundle_manifest_sha256=bundle_manifest_sha256,
            code_material_sha256_value=code_sha256,
            environment=environment,
            status=FundingArtifactStatus.ERROR,
            reason=str(error),
            read_seconds=read_seconds,
            started=started,
        )
        _write_atomic(run_dir / REPORT_FILE_NAME, _canonical_bytes(report))
        read_funding_ml_artifact(run_dir)
        return _receipt(
            run_id=run_id,
            status=FundingRunStatus.EVALUATED,
            report=report,
            protocol_sha256=protocol_sha256,
            output_dir=output_path,
            run_dir=run_dir,
            elapsed=time.perf_counter() - started,
        )
    fit_seconds = outcome.fit_seconds
    evaluation_seconds = outcome.evaluation_seconds
    records = _prediction_records(outcome.predictions)
    _write_predictions(run_dir / PREDICTIONS_FILE_NAME, records)
    if _readback_predictions(run_dir / PREDICTIONS_FILE_NAME) != records:
        raise FundingMlExportError("Parquet readback differs from the funding predictions")
    report = FundingReport(
        run_id=run_id,
        status=FundingArtifactStatus.COMPLETED,
        protocol_sha256=protocol_sha256,
        dataset_id=protocol.dataset_id,
        dataset_content_sha256=protocol.dataset_content_sha256,
        dataset_manifest_sha256=dataset_manifest_sha256,
        bundle_id=protocol.bundle_id,
        bundle_content_sha256=protocol.bundle_content_sha256,
        bundle_manifest_sha256=bundle_manifest_sha256,
        code_material_sha256=code_sha256,
        code_material_files=_CODE_MATERIAL_FILES,
        environment=environment,
        criteria=protocol.criteria,
        decision=outcome.decision,
        folds=outcome.folds,
        pooled_scores=outcome.pooled_scores,
        pooled_comparisons=outcome.pooled_comparisons,
        calibration=outcome.calibration,
        disposition_reason=None,
        prediction_rows=len(records),
        development_fits=outcome.fits,
        timings=MlTimings(
            read_seconds=Decimal(repr(float(read_seconds))),
            fit_seconds=Decimal(repr(float(fit_seconds))),
            evaluation_seconds=Decimal(repr(float(evaluation_seconds))),
            total_seconds=Decimal(repr(float(time.perf_counter() - started))),
        ),
        peak_rss_bytes=peak_rss_bytes(),
    )
    _write_atomic(run_dir / REPORT_FILE_NAME, _canonical_bytes(report))
    manifest_payload = _manifest_bytes(
        run_id=run_id,
        protocol=protocol,
        report=report,
        run_dir=run_dir,
    )
    _write_atomic(run_dir / MANIFEST_FILE_NAME, manifest_payload)
    read_funding_ml_artifact(run_dir)
    return _receipt(
        run_id=run_id,
        status=FundingRunStatus.EVALUATED,
        report=report,
        protocol_sha256=protocol_sha256,
        output_dir=output_path,
        run_dir=run_dir,
        elapsed=time.perf_counter() - started,
    )


__all__ = [
    "FundingMlExportCapError",
    "FundingMlExportConflictError",
    "FundingMlExportError",
    "FundingMlExportPathError",
    "LoadedFundingArtifact",
    "code_material_sha256",
    "evaluate_funding_experiment",
    "read_funding_ml_artifact",
]
