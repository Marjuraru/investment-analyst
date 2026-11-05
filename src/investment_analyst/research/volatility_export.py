"""Atomic, verifiable artifacts for the offline volatility study.

Artifact reading depends only on core packages so a sealed result stays
readable without the optional numerical dependencies.  The command never opens
the operational workspace, never contacts a provider and writes only below the
requested output root.
"""

from __future__ import annotations

import hashlib
import math
import os
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, localcontext
from pathlib import Path
from uuid import UUID

from investment_analyst.analytics.analytical_access_models import canonical_analytical_json
from investment_analyst.core.models.base import ContractModel
from investment_analyst.research.historical_archive import read_historical_archive
from investment_analyst.research.historical_models import HistoricalArchive
from investment_analyst.research.ml_models import MlEnvironment, MlTimings, metric_decimal
from investment_analyst.research.volatility_adapter import VolatilityAdapterError
from investment_analyst.research.volatility_dataset import (
    VolatilityDatasetError,
    VolatilityInsufficientSampleError,
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
from investment_analyst.research.volatility_models import (
    ALL_MODELS,
    ARCHIVE_REQUEST_END,
    ARCHIVE_REQUEST_START,
    ASSET_ID,
    DECIMAL34,
    DECISION_COUNT,
    DECISION_END,
    DECISION_START,
    DEVELOPMENT_FITS,
    EPSILON,
    FEATURE_WINDOWS,
    HYPOTHESIS,
    MANIFEST_FILE_NAME,
    MAX_CANDLES,
    MAX_INPUT_FILE_BYTES,
    MAX_PREDICTIONS_BYTES,
    MAX_RETAINED_RUNS,
    MAX_ROOT_BYTES,
    MAX_RUN_BYTES,
    PREDICTIONS_FILE_NAME,
    PREREGISTERED_FOLDS,
    PROGRESS_FILE_NAME,
    PROTOCOL_FILE_NAME,
    REFERENCE_MODELS,
    REFERENCE_WINDOW,
    REPORT_FILE_NAME,
    RESERVED_START_DATE,
    RESERVED_START_INDEX,
    SOURCE_ID,
    STUDY_LIMITATIONS,
    TARGET_HORIZON,
    VolatilityArtifactFile,
    VolatilityArtifactStatus,
    VolatilityComparison,
    VolatilityCriteria,
    VolatilityHoldoutPolicy,
    VolatilityManifest,
    VolatilityMetric,
    VolatilityModel,
    VolatilityModelParameters,
    VolatilityPredictionRecord,
    VolatilityProgress,
    VolatilityProtocol,
    VolatilityReceipt,
    VolatilityReport,
    VolatilityReportCounts,
    VolatilityResampling,
    VolatilityRunStatus,
    volatility_run_id,
)

_CODE_MATERIAL_FILES = (
    "volatility_models.py",
    "volatility_dataset.py",
    "volatility_adapter.py",
    "volatility_evaluation.py",
    "volatility_export.py",
    "historical_archive.py",
    "historical_models.py",
    "ml_adapter.py",
    "ml_models.py",
)

_MODEL_ORDER = {model: index for index, model in enumerate(ALL_MODELS)}

_SEALED_COMPLETED = frozenset(
    {PROTOCOL_FILE_NAME, REPORT_FILE_NAME, PREDICTIONS_FILE_NAME, MANIFEST_FILE_NAME}
)
_SEALED_INSUFFICIENT = frozenset({PROTOCOL_FILE_NAME, REPORT_FILE_NAME})
_SEALED_ERROR = frozenset({PROTOCOL_FILE_NAME, REPORT_FILE_NAME, PROGRESS_FILE_NAME})


class VolatilityExportError(RuntimeError):
    """Base error for the sealed volatility artifact."""


class VolatilityExportPathError(VolatilityExportError, ValueError):
    """A requested path is unsafe, overlapping or inside a protected root."""


class VolatilityExportConflictError(VolatilityExportError):
    """Sealed content differs from the requested run and must not be overwritten."""


class VolatilityExportCapError(VolatilityExportError):
    """A declared physical cap is exceeded."""


class VolatilityRetentionError(VolatilityExportError):
    """The declared two-run, sixteen-megabyte retention cap is exhausted."""


@dataclass(frozen=True, slots=True)
class LoadedVolatilityArtifact:
    """Verified sealed artifact; a non-completed run has no manifest."""

    protocol: VolatilityProtocol
    report: VolatilityReport
    manifest: VolatilityManifest | None


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


def _mean(values: list[float]) -> float:
    if not values:
        raise VolatilityExportConflictError("a sealed metric cannot cover zero rows")
    return math.fsum(values) / len(values)


def _difference(left: Decimal, right: Decimal) -> Decimal:
    with localcontext(DECIMAL34):
        return left - right


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


def _validate_input_dir(path: Path) -> Path:
    if not path.is_absolute():
        raise VolatilityExportPathError("archive directory must be an absolute path")
    declared = path.expanduser()
    if declared.is_symlink():
        raise VolatilityExportPathError("archive directory must not be a symlink")
    resolved = declared.resolve()
    if not resolved.is_dir():
        raise VolatilityExportPathError("archive directory does not exist")
    names = {entry.name for entry in resolved.iterdir()}
    if names != {MANIFEST_FILE_NAME, "candles.jsonl"}:
        raise VolatilityExportPathError("archive directory contains unexpected entries")
    for name in names:
        candidate = resolved / name
        if candidate.is_symlink() or not candidate.is_file():
            raise VolatilityExportPathError(f"archive entry {name} is missing or is a symlink")
        if candidate.stat().st_size > MAX_INPUT_FILE_BYTES:
            raise VolatilityExportCapError(f"archive entry {name} exceeds the input byte cap")
    return resolved


def _validate_output_dir(output_dir: Path, *, input_dir: Path) -> Path:
    if not output_dir.is_absolute():
        raise VolatilityExportPathError("output directory must be an absolute path")
    declared = output_dir.expanduser()
    for candidate in (declared, *declared.parents):
        if candidate.is_symlink():
            raise VolatilityExportPathError("output directory must not traverse a symbolic link")
    resolved = declared.resolve(strict=False)
    if resolved == Path(resolved.anchor):
        raise VolatilityExportPathError("output directory must not be the filesystem root")
    data_root = Path("~/.local/share/investment-analyst").expanduser().resolve(strict=False)
    research_root = data_root / "research"
    experiments_root = research_root / "volatility-baseline-research"
    if resolved == research_root or (
        research_root in resolved.parents
        and resolved != experiments_root
        and experiments_root not in resolved.parents
    ):
        raise VolatilityExportPathError(
            "output directory must not overlap another research experiment"
        )
    for forbidden in _forbidden_roots():
        root = forbidden.expanduser().resolve(strict=False)
        if resolved == root or root in resolved.parents:
            raise VolatilityExportPathError(
                f"output directory must stay outside the protected root {root}"
            )
    if resolved == input_dir or input_dir in resolved.parents or resolved in input_dir.parents:
        raise VolatilityExportPathError("output directory must not overlap the archive input")
    if resolved.exists() and (resolved.is_symlink() or not resolved.is_dir()):
        raise VolatilityExportConflictError("output directory must be a plain directory")
    resolved.mkdir(parents=True, exist_ok=True)
    return resolved


def build_protocol(archive: HistoricalArchive) -> VolatilityProtocol:
    """Seal the complete preregistration before any fit is executed."""
    manifest = archive.manifest
    if manifest.asset.asset_id != ASSET_ID:
        raise VolatilityExportError("archive asset is outside the preregistered scope")
    if manifest.asset.source_id != SOURCE_ID:
        raise VolatilityExportError("archive source is outside the preregistered scope")
    if manifest.request.start_date != ARCHIVE_REQUEST_START:
        raise VolatilityExportError("archive request start is outside the declared scope")
    if manifest.request.end_date != ARCHIVE_REQUEST_END:
        raise VolatilityExportError("archive request end is outside the declared scope")
    if len(archive.candles) > MAX_CANDLES:
        raise VolatilityExportError("archive exceeds the declared candle budget")
    return VolatilityProtocol(
        asset_id=manifest.asset.asset_id,
        source_id=manifest.asset.source_id,
        archive_id=manifest.archive_id,
        archive_semantic_sha256=manifest.semantic_sha256,
        archive_candles_sha256=manifest.candles_sha256,
        archive_retrieved_at=manifest.retrieved_at,
        archive_request_start=manifest.request.start_date,
        archive_request_end=manifest.request.end_date,
        study_cut_at=manifest.retrieved_at,
        decision_start=DECISION_START,
        decision_end=DECISION_END,
        decision_count=DECISION_COUNT,
        reserved_start_index=RESERVED_START_INDEX,
        reserved_start_date=RESERVED_START_DATE,
        folds=PREREGISTERED_FOLDS,
        feature_windows=FEATURE_WINDOWS,
        reference_window=REFERENCE_WINDOW,
        target_horizon=TARGET_HORIZON,
        epsilon=EPSILON,
        model=VolatilityModelParameters(),
        references=REFERENCE_MODELS,
        criteria=VolatilityCriteria(),
        resampling=VolatilityResampling(),
        holdout=VolatilityHoldoutPolicy(),
        hypothesis=HYPOTHESIS,
        limitations=STUDY_LIMITATIONS,
    )


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
            raise VolatilityExportConflictError(f"{path.name} is not a regular sealed file")
        if path.read_bytes() != payload:
            raise VolatilityExportConflictError(f"{path.name} differs from the sealed content")
        return
    with path.open("xb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())


def _scan_runs(output_path: Path) -> dict[UUID, Path]:
    runs: dict[UUID, Path] = {}
    for entry in sorted(output_path.iterdir(), key=lambda item: item.name):
        if entry.is_symlink() or not entry.is_dir():
            raise VolatilityExportConflictError("output directory contains an unexpected entry")
        try:
            run_id = UUID(entry.name)
        except ValueError as error:
            raise VolatilityExportConflictError(
                "output directory contains a directory that is not a run"
            ) from error
        if str(run_id) != entry.name:
            raise VolatilityExportConflictError("run directory name is not canonical")
        runs[run_id] = entry
    return runs


def _root_bytes(runs: dict[UUID, Path]) -> int:
    total = 0
    for path in runs.values():
        for entry in path.iterdir():
            if entry.is_symlink() or not entry.is_file():
                raise VolatilityExportConflictError("retained run contains an unexpected entry")
            total += entry.stat().st_size
    if total > MAX_ROOT_BYTES:
        raise VolatilityExportCapError("retained runs exceed the sixteen-megabyte root cap")
    return total


def _artifact_file(path: Path) -> VolatilityArtifactFile:
    digest, size = _sha256_file(path)
    if size > MAX_RUN_BYTES:
        raise VolatilityExportCapError(f"{path.name} exceeds the run byte cap")
    return VolatilityArtifactFile(name=path.name, sha256=digest, bytes=size)


def _manifest_bytes(
    *,
    run_id: UUID,
    protocol: VolatilityProtocol,
    report: VolatilityReport,
    run_dir: Path,
) -> bytes:
    files = {
        name: run_dir / name
        for name in (PROTOCOL_FILE_NAME, PREDICTIONS_FILE_NAME, REPORT_FILE_NAME)
        if (run_dir / name).is_file()
    }
    manifest = VolatilityManifest(
        run_id=run_id,
        archive_id=protocol.archive_id,
        archive_semantic_sha256=protocol.archive_semantic_sha256,
        archive_candles_sha256=protocol.archive_candles_sha256,
        code_material_sha256=report.code_material_sha256,
        code_material_files=report.code_material_files,
        environment=report.environment,
        criteria=protocol.criteria,
        protocol=_artifact_file(files[PROTOCOL_FILE_NAME]),
        predictions=_artifact_file(files[PREDICTIONS_FILE_NAME]),
        report=_artifact_file(files[REPORT_FILE_NAME]),
        root_bytes=sum(_artifact_file(path).bytes for path in files.values()),
        created_at=datetime.now(UTC),
    )
    return _canonical_bytes(manifest)


def _record_line(record: VolatilityPredictionRecord) -> bytes:
    return canonical_analytical_json(record.model_dump(mode="json")) + b"\n"


def _prediction_records(
    folds: tuple[VolatilityFoldOutcome, ...],
) -> tuple[VolatilityPredictionRecord, ...]:
    records = [
        VolatilityPredictionRecord(
            sample_key=prediction.sample_key,
            row_id=prediction.row_id,
            decision_at=prediction.decision_at,
            fold=prediction.fold,
            model=prediction.model,
            target=prediction.target,
            estimate=prediction.estimate,
            squared_error=prediction.squared_error,
            absolute_error=prediction.absolute_error,
        )
        for fold in folds
        for prediction in fold.predictions
    ]
    records.sort(
        key=lambda item: (
            item.fold,
            item.decision_at,
            str(item.sample_key),
            _MODEL_ORDER[item.model],
        )
    )
    return tuple(records)


def _write_predictions(path: Path, records: tuple[VolatilityPredictionRecord, ...]) -> None:
    payload = b"".join(_record_line(record) for record in records)
    if len(payload) > MAX_PREDICTIONS_BYTES:
        raise VolatilityExportCapError("predictions exceed the five-megabyte file cap")
    _write_atomic(path, payload)


def _read_predictions(path: Path) -> tuple[VolatilityPredictionRecord, ...]:
    if path.is_symlink() or not path.is_file():
        raise VolatilityExportConflictError("predictions.jsonl is missing or is a symlink")
    if path.stat().st_size > MAX_PREDICTIONS_BYTES:
        raise VolatilityExportCapError("predictions.jsonl exceeds the file cap")
    try:
        payload = path.read_bytes()
    except OSError as error:
        raise VolatilityExportConflictError("predictions.jsonl is unreadable") from error
    if not payload:
        raise VolatilityExportConflictError("predictions.jsonl is empty")
    records: list[VolatilityPredictionRecord] = []
    for line in payload.splitlines():
        if not line:
            raise VolatilityExportConflictError("predictions.jsonl contains an empty line")
        try:
            record = VolatilityPredictionRecord.model_validate_json(line)
        except ValueError as error:
            raise VolatilityExportConflictError("predictions.jsonl is malformed") from error
        if _record_line(record) != line + b"\n":
            raise VolatilityExportConflictError("predictions.jsonl is not canonical")
        records.append(record)
    keys = [
        (record.fold, record.decision_at, str(record.sample_key), _MODEL_ORDER[record.model])
        for record in records
    ]
    if keys != sorted(keys):
        raise VolatilityExportConflictError("predictions.jsonl is not in stable order")
    identities = {(record.fold, record.sample_key, record.model) for record in records}
    if len(identities) != len(records):
        raise VolatilityExportConflictError("predictions.jsonl contains duplicate rows")
    return tuple(records)


def _parse_canonical[T: ContractModel](model: type[T], payload: bytes, label: str) -> T:
    try:
        parsed = model.model_validate_json(payload)
    except ValueError as error:
        raise VolatilityExportConflictError(f"{label} is malformed") from error
    if _canonical_bytes(parsed) != payload:
        raise VolatilityExportConflictError(f"{label} is not in canonical encoding")
    return parsed


def _score_map(scores: tuple[VolatilityMetric, ...]) -> dict[VolatilityModel, VolatilityMetric]:
    mapping = {metric.model: metric for metric in scores}
    if set(mapping) != set(ALL_MODELS):
        raise VolatilityExportConflictError("sealed scores must cover every model")
    return mapping


def _verify_metric(
    stored: VolatilityMetric,
    records: tuple[VolatilityPredictionRecord, ...],
    *,
    label: str,
) -> None:
    selected = [record for record in records if record.model is stored.model]
    if len(selected) != stored.rows:
        raise VolatilityExportConflictError(f"{label} rows differ from the sealed metric")
    mse = metric_decimal(_mean([float(record.squared_error) for record in selected]))
    mae = metric_decimal(_mean([float(record.absolute_error) for record in selected]))
    if stored.mse != mse or stored.mae != mae:
        raise VolatilityExportConflictError(f"{label} loss differs from the sealed metric")


def _verify_comparisons(
    comparisons: tuple[VolatilityComparison, ...],
    scores: tuple[VolatilityMetric, ...],
    *,
    label: str,
) -> None:
    mapping = _score_map(scores)
    for comparison in comparisons:
        if comparison.model_mse != mapping[VolatilityModel.RIDGE].mse:
            raise VolatilityExportConflictError(f"{label} candidate MSE differs from scores")
        if comparison.reference_mse != mapping[comparison.reference].mse:
            raise VolatilityExportConflictError(f"{label} reference MSE differs from scores")
        if comparison.lower_bound is not None and comparison.lower_bound_positive != (
            comparison.lower_bound > 0
        ):
            raise VolatilityExportConflictError(f"{label} lower-bound flag is inconsistent")


def _verify_pooled_comparisons(
    report: VolatilityReport,
    records: tuple[VolatilityPredictionRecord, ...],
) -> None:
    ridge = [record for record in records if record.model is VolatilityModel.RIDGE]
    for comparison in report.pooled_comparisons:
        other = [record for record in records if record.model is comparison.reference]
        if len(ridge) != len(other) or not ridge:
            raise VolatilityExportConflictError("pooled comparison rows are incomplete")
        paired = _mean(
            [
                float(_difference(other_item.squared_error, ridge_item.squared_error))
                for ridge_item, other_item in zip(ridge, other, strict=True)
            ]
        )
        if comparison.mean_paired_difference != metric_decimal(paired):
            raise VolatilityExportConflictError("pooled paired difference differs from predictions")
        if comparison.lower_bound is None or comparison.lower_bound_positive != (
            comparison.lower_bound > 0
        ):
            raise VolatilityExportConflictError("pooled lower bound is inconsistent")


def _verify_completed_report(
    report: VolatilityReport,
    records: tuple[VolatilityPredictionRecord, ...],
) -> None:
    if report.prediction_rows != len(records):
        raise VolatilityExportConflictError("predictions disagree with the sealed row count")
    if report.development_fits != DEVELOPMENT_FITS:
        raise VolatilityExportConflictError("completed report requires three finalist fits")
    if len(report.folds) != 3:
        raise VolatilityExportConflictError("completed report requires three folds")
    expected_rows = 0
    for fold in report.folds:
        fold_records = tuple(record for record in records if record.fold == fold.fold)
        if len(fold_records) != fold.validation_rows * len(ALL_MODELS):
            raise VolatilityExportConflictError("fold predictions disagree with its row count")
        expected_rows += fold.validation_rows
        for metric in fold.scores:
            _verify_metric(metric, fold_records, label=f"fold {fold.fold}")
        _verify_comparisons(fold.comparisons, fold.scores, label=f"fold {fold.fold}")
    if expected_rows * len(ALL_MODELS) != len(records):
        raise VolatilityExportConflictError("fold row counts disagree with predictions")
    for metric in report.pooled_scores:
        if metric.rows != expected_rows:
            raise VolatilityExportConflictError("pooled metric rows disagree with the folds")
        _verify_metric(metric, records, label="pooled")
    _verify_comparisons(report.pooled_comparisons, report.pooled_scores, label="pooled")
    _verify_pooled_comparisons(report, records)
    expected_decision, expected_reason = decide(report.folds, report.pooled_comparisons)
    if report.decision != expected_decision or report.decision_reason != expected_reason:
        raise VolatilityExportConflictError("sealed decision differs from its evidence")


def _verify_counts(report: VolatilityReport, protocol: VolatilityProtocol) -> None:
    counts = report.counts
    if counts.planned_decisions != protocol.decision_count:
        raise VolatilityExportConflictError("sealed planned decisions differ from the protocol")
    if counts.constructed_decisions != protocol.reserved_start_index:
        raise VolatilityExportConflictError("sealed constructed decisions differ from the protocol")
    if counts.reserved_decisions_not_constructed != (
        protocol.decision_count - protocol.reserved_start_index
    ):
        raise VolatilityExportConflictError("sealed reserved decisions differ from the protocol")
    if counts.candles_read > MAX_CANDLES:
        raise VolatilityExportConflictError("sealed candle count exceeds the declared budget")
    if counts.reserved_target_constructions != 0:
        raise VolatilityExportConflictError("sealed report claims reserved target constructions")


def _verify_report_against_protocol(
    report: VolatilityReport,
    protocol: VolatilityProtocol,
    protocol_payload: bytes,
) -> None:
    if report.protocol_sha256 != _sha256_bytes(protocol_payload):
        raise VolatilityExportConflictError("report protocol digest differs from the sealed file")
    if report.protocol_id != protocol.protocol_id:
        raise VolatilityExportConflictError("report protocol id differs from the sealed protocol")
    if (
        report.archive_id != protocol.archive_id
        or report.archive_semantic_sha256 != protocol.archive_semantic_sha256
        or report.archive_candles_sha256 != protocol.archive_candles_sha256
        or report.archive_retrieved_at != protocol.archive_retrieved_at
    ):
        raise VolatilityExportConflictError("report archive identity differs from the protocol")
    if not report.code_material_files:
        raise VolatilityExportConflictError("report must name its material code files")
    if report.criteria != protocol.criteria:
        raise VolatilityExportConflictError("report criteria differ from the protocol")
    _verify_counts(report, protocol)


def read_volatility_artifact(artifact_dir: Path) -> LoadedVolatilityArtifact:
    """Fully verify one run directory without importing numerical dependencies."""
    if artifact_dir.is_symlink() or not artifact_dir.is_dir():
        raise VolatilityExportConflictError("artifact directory is missing or is a symlink")
    entries = tuple(artifact_dir.iterdir())
    names = {entry.name for entry in entries}
    if not names or not names <= (_SEALED_COMPLETED | _SEALED_ERROR | _SEALED_INSUFFICIENT):
        raise VolatilityExportConflictError("artifact directory contains unexpected entries")
    for entry in entries:
        if entry.is_symlink() or not entry.is_file():
            raise VolatilityExportConflictError("sealed entry must be a regular file")
    if PROTOCOL_FILE_NAME not in names or REPORT_FILE_NAME not in names:
        raise VolatilityExportConflictError("artifact directory is incomplete")
    protocol_payload = (artifact_dir / PROTOCOL_FILE_NAME).read_bytes()
    if len(protocol_payload) > MAX_RUN_BYTES:
        raise VolatilityExportCapError("protocol.json exceeds the run byte cap")
    protocol = _parse_canonical(VolatilityProtocol, protocol_payload, "protocol.json")
    report_payload = (artifact_dir / REPORT_FILE_NAME).read_bytes()
    if len(report_payload) > MAX_RUN_BYTES:
        raise VolatilityExportCapError("report.json exceeds the run byte cap")
    report = _parse_canonical(VolatilityReport, report_payload, "report.json")
    if artifact_dir.name != str(report.run_id):
        raise VolatilityExportConflictError("artifact directory name differs from the run id")
    _verify_report_against_protocol(report, protocol, protocol_payload)

    manifest: VolatilityManifest | None = None
    if MANIFEST_FILE_NAME in names:
        if report.status is not VolatilityArtifactStatus.COMPLETED:
            raise VolatilityExportConflictError("a non-completed run must not seal a manifest")
        manifest_payload = (artifact_dir / MANIFEST_FILE_NAME).read_bytes()
        if len(manifest_payload) > MAX_RUN_BYTES:
            raise VolatilityExportCapError("manifest.json exceeds the run byte cap")
        manifest = _parse_canonical(VolatilityManifest, manifest_payload, "manifest.json")
        descriptors = (manifest.protocol, manifest.predictions, manifest.report)
        if {descriptor.name for descriptor in descriptors} != {
            PROTOCOL_FILE_NAME,
            PREDICTIONS_FILE_NAME,
            REPORT_FILE_NAME,
        }:
            raise VolatilityExportConflictError("manifest descriptors differ from sealed files")
        for descriptor in descriptors:
            path = artifact_dir / descriptor.name
            if path.is_symlink() or not path.is_file():
                raise VolatilityExportConflictError(f"{descriptor.name} is missing or is a symlink")
            digest, size = _sha256_file(path)
            if digest != descriptor.sha256 or size != descriptor.bytes:
                raise VolatilityExportConflictError(f"{descriptor.name} differs from its manifest")
        if (
            manifest.run_id != report.run_id
            or manifest.archive_id != report.archive_id
            or manifest.archive_semantic_sha256 != report.archive_semantic_sha256
            or manifest.archive_candles_sha256 != report.archive_candles_sha256
            or manifest.code_material_sha256 != report.code_material_sha256
            or manifest.code_material_files != report.code_material_files
            or manifest.environment != report.environment
            or manifest.criteria != report.criteria
        ):
            raise VolatilityExportConflictError("manifest and report disagree on the sealed run")
        if PREDICTIONS_FILE_NAME not in names:
            raise VolatilityExportConflictError("a completed run requires sealed predictions")
        records = _read_predictions(artifact_dir / PREDICTIONS_FILE_NAME)
        _verify_completed_report(report, records)
    elif report.status is VolatilityArtifactStatus.COMPLETED:
        raise VolatilityExportConflictError("a completed run requires a sealed manifest")
    elif PREDICTIONS_FILE_NAME in names:
        raise VolatilityExportConflictError("a non-completed run must not seal predictions")

    if PROGRESS_FILE_NAME in names:
        if report.status is not VolatilityArtifactStatus.ERROR:
            raise VolatilityExportConflictError("only a failed run may retain progress")
        progress_payload = (artifact_dir / PROGRESS_FILE_NAME).read_bytes()
        if len(progress_payload) > MAX_RUN_BYTES:
            raise VolatilityExportCapError("progress.json exceeds the run byte cap")
        progress = _parse_canonical(VolatilityProgress, progress_payload, "progress.json")
        if progress.run_id != report.run_id or progress.protocol_sha256 != report.protocol_sha256:
            raise VolatilityExportConflictError("progress disagrees with the sealed report")
    return LoadedVolatilityArtifact(protocol=protocol, report=report, manifest=manifest)


def _read_progress(
    path: Path,
    *,
    run_id: UUID,
    protocol_sha256: str,
) -> VolatilityProgress | None:
    if not path.exists():
        return None
    if path.is_symlink() or not path.is_file():
        raise VolatilityExportConflictError("progress.json is missing or is a symlink")
    if path.stat().st_size > MAX_RUN_BYTES:
        raise VolatilityExportCapError("progress.json exceeds the run byte cap")
    progress = _parse_canonical(VolatilityProgress, path.read_bytes(), "progress.json")
    if progress.run_id != run_id or progress.protocol_sha256 != protocol_sha256:
        raise VolatilityExportConflictError("progress disagrees with the sealed protocol")
    return progress


def _append_progress(
    progress: VolatilityProgress | None,
    fold: VolatilityFoldOutcome,
    *,
    run_id: UUID,
    protocol_sha256: str,
) -> VolatilityProgress:
    existing = () if progress is None else progress.folds
    expected = len(existing) + 1
    if fold.result.fold != expected:
        raise VolatilityExportConflictError("progress folds must be appended in order")
    return VolatilityProgress(
        run_id=run_id,
        protocol_sha256=protocol_sha256,
        folds=(*existing, progress_from_fold(fold)),
    )


def _minimal_counts(archive: HistoricalArchive) -> VolatilityReportCounts:
    return VolatilityReportCounts(
        planned_decisions=DECISION_COUNT,
        constructed_decisions=0,
        reserved_decisions_not_constructed=DECISION_COUNT,
        feature_ready=0,
        target_ready=0,
        candles_read=len(archive.candles),
    )


def _timings(
    *,
    read_seconds: float,
    fit_seconds: float,
    evaluation_seconds: float,
    total_seconds: float,
) -> MlTimings:
    return MlTimings(
        read_seconds=Decimal(repr(float(read_seconds))),
        fit_seconds=Decimal(repr(float(fit_seconds))),
        evaluation_seconds=Decimal(repr(float(evaluation_seconds))),
        total_seconds=Decimal(repr(float(total_seconds))),
    )


def _failure_report(
    *,
    run_id: UUID,
    status: VolatilityArtifactStatus,
    reason: str,
    protocol: VolatilityProtocol,
    protocol_sha256: str,
    environment: MlEnvironment,
    code_sha256: str,
    counts: VolatilityReportCounts,
    fits_executed: int,
    resumed: bool,
    read_seconds: float,
    fit_seconds: float,
    evaluation_seconds: float,
    started: float,
) -> VolatilityReport:
    return VolatilityReport(
        run_id=run_id,
        status=status,
        protocol_sha256=protocol_sha256,
        archive_id=protocol.archive_id,
        archive_semantic_sha256=protocol.archive_semantic_sha256,
        archive_candles_sha256=protocol.archive_candles_sha256,
        archive_retrieved_at=protocol.archive_retrieved_at,
        code_material_sha256=code_sha256,
        code_material_files=_CODE_MATERIAL_FILES,
        environment=environment,
        criteria=protocol.criteria,
        decision=None,
        decision_reason=reason,
        folds=(),
        pooled_scores=(),
        pooled_comparisons=(),
        counts=counts,
        prediction_rows=0,
        development_fits=0,
        fits_executed_this_invocation=fits_executed,
        resumed_from_progress=resumed,
        timings=_timings(
            read_seconds=read_seconds,
            fit_seconds=fit_seconds,
            evaluation_seconds=evaluation_seconds,
            total_seconds=time.perf_counter() - started,
        ),
        peak_rss_bytes=peak_rss_bytes(),
    )


def _report_bytes(
    *,
    run_id: UUID,
    protocol: VolatilityProtocol,
    protocol_sha256: str,
    archive: HistoricalArchive,
    environment: MlEnvironment,
    code_sha256: str,
    counts: VolatilityReportCounts,
    folds: tuple[VolatilityFoldOutcome, ...],
    fits_executed: int,
    resumed: bool,
    read_seconds: float,
    fit_seconds: float,
    evaluation_seconds: float,
    started: float,
) -> tuple[bytes, VolatilityReport]:
    evaluation = combine_folds(folds, protocol)
    records = _prediction_records(folds)
    report = VolatilityReport(
        run_id=run_id,
        status=VolatilityArtifactStatus.COMPLETED,
        protocol_sha256=protocol_sha256,
        archive_id=protocol.archive_id,
        archive_semantic_sha256=protocol.archive_semantic_sha256,
        archive_candles_sha256=protocol.archive_candles_sha256,
        archive_retrieved_at=protocol.archive_retrieved_at,
        code_material_sha256=code_sha256,
        code_material_files=_CODE_MATERIAL_FILES,
        environment=environment,
        criteria=protocol.criteria,
        decision=evaluation.decision,
        decision_reason=evaluation.decision_reason,
        folds=tuple(fold.result for fold in folds),
        pooled_scores=evaluation.pooled_scores,
        pooled_comparisons=evaluation.pooled_comparisons,
        counts=counts,
        prediction_rows=len(records),
        development_fits=DEVELOPMENT_FITS,
        fits_executed_this_invocation=fits_executed,
        resumed_from_progress=resumed,
        timings=_timings(
            read_seconds=read_seconds,
            fit_seconds=fit_seconds + evaluation.fit_seconds,
            evaluation_seconds=evaluation_seconds + evaluation.evaluation_seconds,
            total_seconds=time.perf_counter() - started,
        ),
        peak_rss_bytes=peak_rss_bytes(),
    )
    return _canonical_bytes(report), report


def _receipt(
    *,
    run_id: UUID,
    status: VolatilityRunStatus,
    report: VolatilityReport,
    protocol_sha256: str,
    output_dir: Path,
    run_dir: Path,
    runs: dict[UUID, Path],
    fits_executed: int,
    resumed: bool,
    elapsed: float,
) -> VolatilityReceipt:
    report_payload = (run_dir / REPORT_FILE_NAME).read_bytes()
    manifest_path = run_dir / MANIFEST_FILE_NAME
    predictions_path = run_dir / PREDICTIONS_FILE_NAME
    retained = tuple(sorted(runs))
    superseded = tuple(value for value in retained if value != run_id)
    artifact_bytes = sum(entry.stat().st_size for entry in run_dir.iterdir() if entry.is_file())
    return VolatilityReceipt(
        run_id=run_id,
        status=status,
        report_status=report.status,
        decision=report.decision,
        archive_id=report.archive_id,
        archive_semantic_sha256=report.archive_semantic_sha256,
        output_dir=str(output_dir),
        artifact_dir=str(run_dir),
        protocol_sha256=protocol_sha256,
        report_sha256=_sha256_bytes(report_payload),
        manifest_sha256=(
            _sha256_bytes(manifest_path.read_bytes()) if manifest_path.is_file() else None
        ),
        predictions_sha256=(
            _sha256_bytes(predictions_path.read_bytes()) if predictions_path.is_file() else None
        ),
        prediction_rows=report.prediction_rows,
        development_fits=report.development_fits,
        fits_executed_this_invocation=fits_executed,
        resumed_from_progress=resumed,
        artifact_bytes=artifact_bytes,
        root_bytes=_root_bytes(runs),
        retained_run_ids=retained,
        final_run_id=run_id,
        superseded_run_ids=superseded,
        elapsed_seconds=Decimal(repr(float(elapsed))),
        peak_rss_bytes=peak_rss_bytes(),
    )


def evaluate_volatility_experiment(
    *,
    archive_dir: Path,
    output_dir: Path,
) -> VolatilityReceipt:
    """Evaluate, seal and reuse one run without opening the operational workspace."""
    from investment_analyst.research import volatility_adapter

    started = time.perf_counter()
    input_path = _validate_input_dir(archive_dir)
    read_started = time.perf_counter()
    archive = read_historical_archive(input_path)
    protocol = build_protocol(archive)
    output_path = _validate_output_dir(output_dir, input_dir=input_path)
    protocol_payload = _canonical_bytes(protocol)
    protocol_sha256 = _sha256_bytes(protocol_payload)
    existing = _scan_runs(output_path)
    environment = volatility_adapter.environment_versions()
    code_sha256 = code_material_sha256()
    run_id = volatility_run_id(
        protocol_sha256=protocol_sha256,
        archive_id=protocol.archive_id,
        archive_semantic_sha256=protocol.archive_semantic_sha256,
        code_material_sha256=code_sha256,
        environment=environment,
    )
    if run_id not in existing and len(existing) >= MAX_RETAINED_RUNS:
        raise VolatilityRetentionError(
            "the output root already retains two runs; archive one before a new run"
        )
    run_dir = output_path / str(run_id)
    resumed = False
    if run_dir.exists():
        if run_dir.is_symlink() or not run_dir.is_dir():
            raise VolatilityExportConflictError("run directory is not a regular directory")
        protocol_path = run_dir / PROTOCOL_FILE_NAME
        if protocol_path.is_symlink() or not protocol_path.is_file():
            raise VolatilityExportConflictError("run directory lacks its sealed protocol")
        if protocol_path.read_bytes() != protocol_payload:
            raise VolatilityExportConflictError("existing run sealed a different protocol")
        if (run_dir / REPORT_FILE_NAME).is_file():
            loaded = read_volatility_artifact(run_dir)
            if loaded.report.status is not VolatilityArtifactStatus.ERROR:
                return _receipt(
                    run_id=run_id,
                    status=VolatilityRunStatus.REUSED,
                    report=loaded.report,
                    protocol_sha256=protocol_sha256,
                    output_dir=output_path,
                    run_dir=run_dir,
                    runs=existing,
                    fits_executed=0,
                    resumed=False,
                    elapsed=time.perf_counter() - started,
                )
            resumed = (
                _read_progress(
                    run_dir / PROGRESS_FILE_NAME,
                    run_id=run_id,
                    protocol_sha256=protocol_sha256,
                )
                is not None
            )
            (run_dir / REPORT_FILE_NAME).unlink()
        else:
            resumed = (
                _read_progress(
                    run_dir / PROGRESS_FILE_NAME,
                    run_id=run_id,
                    protocol_sha256=protocol_sha256,
                )
                is not None
            )
    else:
        run_dir.mkdir(parents=False, exist_ok=False)
        _verified_checkpoint(run_dir / PROTOCOL_FILE_NAME, protocol_payload)

    runs = _scan_runs(output_path)
    _root_bytes(runs)

    try:
        dataset = build_volatility_decisions(archive, protocol)
    except VolatilityDatasetError as error:
        report = _failure_report(
            run_id=run_id,
            status=VolatilityArtifactStatus.ERROR,
            reason=str(error),
            protocol=protocol,
            protocol_sha256=protocol_sha256,
            environment=environment,
            code_sha256=code_sha256,
            counts=_minimal_counts(archive),
            fits_executed=0,
            resumed=resumed,
            read_seconds=time.perf_counter() - read_started,
            fit_seconds=0.0,
            evaluation_seconds=0.0,
            started=started,
        )
        _write_atomic(run_dir / REPORT_FILE_NAME, _canonical_bytes(report))
        read_volatility_artifact(run_dir)
        return _receipt(
            run_id=run_id,
            status=VolatilityRunStatus.EVALUATED,
            report=report,
            protocol_sha256=protocol_sha256,
            output_dir=output_path,
            run_dir=run_dir,
            runs=runs,
            fits_executed=0,
            resumed=resumed,
            elapsed=time.perf_counter() - started,
        )
    read_seconds = time.perf_counter() - read_started
    try:
        plans = plan_folds(dataset, protocol)
    except VolatilityInsufficientSampleError as error:
        (run_dir / PROGRESS_FILE_NAME).unlink(missing_ok=True)
        report = _failure_report(
            run_id=run_id,
            status=VolatilityArtifactStatus.INSUFFICIENT_SAMPLE,
            reason=error.reason,
            protocol=protocol,
            protocol_sha256=protocol_sha256,
            environment=environment,
            code_sha256=code_sha256,
            counts=dataset.counts,
            fits_executed=0,
            resumed=False,
            read_seconds=read_seconds,
            fit_seconds=0.0,
            evaluation_seconds=0.0,
            started=started,
        )
        _write_atomic(run_dir / REPORT_FILE_NAME, _canonical_bytes(report))
        read_volatility_artifact(run_dir)
        return _receipt(
            run_id=run_id,
            status=VolatilityRunStatus.EVALUATED,
            report=report,
            protocol_sha256=protocol_sha256,
            output_dir=output_path,
            run_dir=run_dir,
            runs=runs,
            fits_executed=0,
            resumed=False,
            elapsed=time.perf_counter() - started,
        )

    progress = _read_progress(
        run_dir / PROGRESS_FILE_NAME,
        run_id=run_id,
        protocol_sha256=protocol_sha256,
    )
    resumed = progress is not None
    folds: list[VolatilityFoldOutcome] = []
    fits_executed = 0
    fit_seconds = 0.0
    evaluation_seconds = 0.0
    for plan in plans:
        if progress is not None and plan.fold <= len(progress.folds):
            outcome = fold_from_progress(progress.folds[plan.fold - 1], plan)
            folds.append(outcome)
        else:
            fits_executed += 1
            try:
                outcome = evaluate_fold(plan, protocol)
            except (VolatilityAdapterError, VolatilityEvaluationError) as error:
                report = _failure_report(
                    run_id=run_id,
                    status=VolatilityArtifactStatus.ERROR,
                    reason=str(error),
                    protocol=protocol,
                    protocol_sha256=protocol_sha256,
                    environment=environment,
                    code_sha256=code_sha256,
                    counts=dataset.counts,
                    fits_executed=fits_executed,
                    resumed=resumed,
                    read_seconds=read_seconds,
                    fit_seconds=fit_seconds,
                    evaluation_seconds=evaluation_seconds,
                    started=started,
                )
                _write_atomic(run_dir / REPORT_FILE_NAME, _canonical_bytes(report))
                read_volatility_artifact(run_dir)
                return _receipt(
                    run_id=run_id,
                    status=VolatilityRunStatus.EVALUATED,
                    report=report,
                    protocol_sha256=protocol_sha256,
                    output_dir=output_path,
                    run_dir=run_dir,
                    runs=runs,
                    fits_executed=fits_executed,
                    resumed=resumed,
                    elapsed=time.perf_counter() - started,
                )
            fit_seconds += outcome.fit_seconds
            evaluation_seconds += outcome.evaluation_seconds
            folds.append(outcome)
            progress = _append_progress(
                progress,
                outcome,
                run_id=run_id,
                protocol_sha256=protocol_sha256,
            )
            _write_atomic(
                run_dir / PROGRESS_FILE_NAME,
                _canonical_bytes(progress),
            )
    if len(folds) != DEVELOPMENT_FITS:
        raise VolatilityExportError("the evaluation did not complete the three folds")
    report_payload, report = _report_bytes(
        run_id=run_id,
        protocol=protocol,
        protocol_sha256=protocol_sha256,
        archive=archive,
        environment=environment,
        code_sha256=code_sha256,
        counts=dataset.counts,
        folds=tuple(folds),
        fits_executed=fits_executed,
        resumed=resumed,
        read_seconds=read_seconds,
        fit_seconds=fit_seconds,
        evaluation_seconds=evaluation_seconds,
        started=started,
    )
    _write_predictions(run_dir / PREDICTIONS_FILE_NAME, _prediction_records(tuple(folds)))
    _write_atomic(run_dir / REPORT_FILE_NAME, report_payload)
    _write_atomic(
        run_dir / MANIFEST_FILE_NAME,
        _manifest_bytes(
            run_id=run_id,
            protocol=protocol,
            report=report,
            run_dir=run_dir,
        ),
    )
    (run_dir / PROGRESS_FILE_NAME).unlink(missing_ok=True)
    read_volatility_artifact(run_dir)
    return _receipt(
        run_id=run_id,
        status=VolatilityRunStatus.EVALUATED,
        report=report,
        protocol_sha256=protocol_sha256,
        output_dir=output_path,
        run_dir=run_dir,
        runs=runs,
        fits_executed=fits_executed,
        resumed=resumed,
        elapsed=time.perf_counter() - started,
    )


__all__ = [
    "LoadedVolatilityArtifact",
    "VolatilityExportCapError",
    "VolatilityExportConflictError",
    "VolatilityExportError",
    "VolatilityExportPathError",
    "VolatilityRetentionError",
    "build_protocol",
    "code_material_sha256",
    "evaluate_volatility_experiment",
    "peak_rss_bytes",
    "read_volatility_artifact",
]
