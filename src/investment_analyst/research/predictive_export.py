"""Atomic, verifiable export of one predictive research dataset."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4

import duckdb
from pydantic import ConfigDict, Field, model_validator

from investment_analyst.core.models.base import ContractModel, NonEmptyStr, UTCDateTime
from investment_analyst.research.predictive_models import (
    MANIFEST_SCHEMA_VERSION,
    PredictiveDataset,
    PredictiveDatasetManifest,
    PredictiveSample,
    predictive_dataset_content_hash,
)

ROWS_FILE_NAME = "rows.parquet"
MANIFEST_FILE_NAME = "manifest.json"
MAX_ARTIFACT_BYTES = 8 * 1024 * 1024

_TABLE_COLUMNS: tuple[tuple[str, str], ...] = (
    ("sample_key", "VARCHAR"),
    ("row_id", "VARCHAR"),
    ("decision_at", "VARCHAR"),
    ("close_return_1d", "VARCHAR"),
    ("rsi_14_fraction", "VARCHAR"),
    ("macd_12_26_9_histogram_close_ratio", "VARCHAR"),
    ("feature_state", "VARCHAR"),
    ("label_state", "VARCHAR"),
    ("label", "INTEGER"),
    ("return_5d", "VARCHAR"),
    ("entry_close", "VARCHAR"),
    ("exit_close", "VARCHAR"),
    ("entry_close_observation_id", "VARCHAR"),
    ("exit_close_observation_id", "VARCHAR"),
    ("label_available_at", "VARCHAR"),
    ("feature_evidence_json", "VARCHAR"),
    ("label_bars_json", "VARCHAR"),
    ("split", "VARCHAR"),
    ("purged", "BOOLEAN"),
    ("purge_reason", "VARCHAR"),
    ("eligible", "BOOLEAN"),
)


class PredictiveExportError(RuntimeError):
    """Base error for predictive dataset export."""


class PredictiveExportPathError(PredictiveExportError, ValueError):
    """The requested export location is unsafe or outside the explicit root."""


class PredictiveExportConflictError(PredictiveExportError):
    """An existing destination differs from the dataset and must not be overwritten."""


class PredictiveArtifactDescriptor(ContractModel):
    """Physical descriptor of the sealed Parquet artifact."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    rows_file_name: Literal["rows.parquet"] = ROWS_FILE_NAME
    rows_sha256: str
    rows_bytes: int = Field(ge=0)
    exported_at: UTCDateTime
    physical_bytes_note: NonEmptyStr = (
        "Parquet bytes identify this artifact only; the semantic dataset hash carries"
        " cross-version equality."
    )

    @model_validator(mode="after")
    def validate_descriptor(self) -> PredictiveArtifactDescriptor:
        if len(self.rows_sha256) != 64:
            raise ValueError("rows_sha256 must be a SHA-256 digest")
        return self


class PredictiveExportManifest(ContractModel):
    """Sealed manifest with the deterministic dataset and physical artifact."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["predictive-dataset-manifest-v1"] = MANIFEST_SCHEMA_VERSION
    dataset: PredictiveDatasetManifest
    artifact: PredictiveArtifactDescriptor


class PredictiveExportReceipt(ContractModel):
    """Compact export receipt without target values."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    dataset_id: UUID
    dataset_content_sha256: str
    row_count: int = Field(ge=0)
    output_dir: NonEmptyStr
    dataset_dir: NonEmptyStr
    status: Literal["exported", "reused"]
    rows_sha256: str
    rows_bytes: int = Field(ge=0)
    manifest_sha256: str

    def to_json_dict(self) -> dict[str, object]:
        """Return the deterministic JSON projection used by the CLI."""
        return self.model_dump(mode="json")


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    )


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.astimezone(UTC).isoformat()


def _decimal_text(value: object) -> str | None:
    return None if value is None else str(value)


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _sha256_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _validate_output_dir(output_dir: Path, *, workspace_root: Path) -> Path:
    if not output_dir.is_absolute():
        raise PredictiveExportPathError("output_dir must be an absolute path")
    resolved = output_dir.expanduser().resolve(strict=False)
    workspace = workspace_root.expanduser().resolve(strict=False)
    if resolved == workspace or workspace in resolved.parents:
        raise PredictiveExportPathError("output_dir must be outside the operational workspace")
    if resolved == Path(resolved.anchor):
        raise PredictiveExportPathError("output_dir must not be the filesystem root")
    return resolved


def _row_values(sample: PredictiveSample) -> tuple[object, ...]:
    features = {item.column_name: item for item in sample.features}
    close_return = features["close_return_1d"].value
    rsi = features["rsi_14_fraction"].value
    histogram = features["macd_12_26_9_histogram_close_ratio"].value
    feature_payload = [item.model_dump(mode="json") for item in sample.features]
    label_payload = sample.label.model_dump(mode="json")
    return (
        str(sample.sample_key),
        str(sample.row_id),
        _iso(sample.decision_at),
        _decimal_text(close_return),
        _decimal_text(rsi),
        _decimal_text(histogram),
        sample.feature_state.value,
        sample.label_state.value,
        sample.label.label,
        _decimal_text(sample.label.return_5d),
        _decimal_text(sample.label.entry_close),
        _decimal_text(sample.label.exit_close),
        (
            str(sample.label.bars[0].observation_id)
            if sample.label.bars[0].observation_id is not None
            else None
        ),
        (
            str(sample.label.bars[-1].observation_id)
            if sample.label.bars[-1].observation_id is not None
            else None
        ),
        _iso(sample.label.label_available_at),
        _canonical_json(feature_payload),
        _canonical_json(label_payload),
        sample.split.value,
        sample.purged,
        None if sample.purge_reason is None else sample.purge_reason.value,
        sample.eligible,
    )


def _write_parquet(path: Path, rows: tuple[tuple[object, ...], ...]) -> None:
    connection = duckdb.connect(":memory:")
    try:
        connection.execute("SET threads=2")
        connection.execute("SET memory_limit='512MB'")
        definition = ", ".join(f"{name} {data_type}" for name, data_type in _TABLE_COLUMNS)
        connection.execute(f"CREATE TABLE predictive_rows ({definition})")
        placeholders = ", ".join("?" for _ in _TABLE_COLUMNS)
        connection.executemany(
            f"INSERT INTO predictive_rows VALUES ({placeholders})",
            rows,
        )
        columns = ", ".join(name for name, _ in _TABLE_COLUMNS)
        connection.execute(
            f"COPY (SELECT {columns} FROM predictive_rows "
            "ORDER BY decision_at, sample_key) TO ? (FORMAT PARQUET)",
            [str(path)],
        )
    finally:
        connection.close()


def _readback_rows(path: Path) -> tuple[tuple[object, ...], ...]:
    connection = duckdb.connect(":memory:")
    try:
        connection.execute("SET threads=2")
        connection.execute("SET memory_limit='512MB'")
        columns = ", ".join(name for name, _ in _TABLE_COLUMNS)
        rows = connection.execute(
            f"SELECT {columns} FROM read_parquet(?) ORDER BY decision_at, sample_key",
            [str(path)],
        ).fetchall()
    finally:
        connection.close()
    return tuple(tuple(row) for row in rows)


def _expected_readback(
    rows: tuple[tuple[object, ...], ...],
) -> tuple[tuple[object, ...], ...]:
    return tuple(tuple(row) for row in sorted(rows, key=lambda item: (str(item[2]), str(item[0]))))


def _manifest_bytes(manifest: PredictiveExportManifest) -> bytes:
    return _canonical_json(manifest.model_dump(mode="json")).encode("utf-8")


def read_predictive_export(dataset_dir: Path) -> PredictiveExportManifest:
    """Read and physically verify one sealed dataset directory."""
    if dataset_dir.is_symlink() or not dataset_dir.is_dir():
        raise PredictiveExportConflictError("dataset directory is missing or is a symlink")
    manifest_path = dataset_dir / MANIFEST_FILE_NAME
    rows_path = dataset_dir / ROWS_FILE_NAME
    if rows_path.is_symlink() or not rows_path.is_file():
        raise PredictiveExportConflictError("rows.parquet is missing or is a symlink")
    try:
        payload = manifest_path.read_bytes()
    except OSError as error:
        raise PredictiveExportConflictError("manifest.json is unreadable") from error
    try:
        manifest = PredictiveExportManifest.model_validate_json(payload)
    except ValueError as error:
        raise PredictiveExportConflictError("manifest.json is malformed") from error
    if payload != _manifest_bytes(manifest):
        raise PredictiveExportConflictError("manifest.json is not in canonical encoding")
    digest, size = _sha256_file(rows_path)
    if digest != manifest.artifact.rows_sha256 or size != manifest.artifact.rows_bytes:
        raise PredictiveExportConflictError("rows.parquet differs from its manifest")
    if size > MAX_ARTIFACT_BYTES or len(payload) > MAX_ARTIFACT_BYTES:
        raise PredictiveExportConflictError("sealed artifact exceeds the byte budget")
    expected_hash = predictive_dataset_content_hash(
        dataset_id=manifest.dataset.dataset_id,
        rows=manifest.dataset.rows,
    )
    if expected_hash != manifest.dataset.dataset_content_sha256:
        raise PredictiveExportConflictError("dataset semantic hash does not match its rows")
    return manifest


def _existing_receipt(
    dataset_dir: Path,
    *,
    dataset: PredictiveDataset,
    output_dir: Path,
) -> PredictiveExportReceipt | None:
    if not dataset_dir.exists():
        return None
    manifest = read_predictive_export(dataset_dir)
    if (
        manifest.dataset.dataset_id != dataset.dataset_id
        or manifest.dataset.dataset_content_sha256 != dataset.dataset_content_sha256
        or len(manifest.dataset.rows) != len(dataset.samples)
    ):
        raise PredictiveExportConflictError("destination contains a different predictive dataset")
    manifest_bytes = (dataset_dir / MANIFEST_FILE_NAME).read_bytes()
    return PredictiveExportReceipt(
        dataset_id=dataset.dataset_id,
        dataset_content_sha256=dataset.dataset_content_sha256,
        row_count=len(dataset.samples),
        output_dir=str(output_dir),
        dataset_dir=str(dataset_dir),
        status="reused",
        rows_sha256=manifest.artifact.rows_sha256,
        rows_bytes=manifest.artifact.rows_bytes,
        manifest_sha256=_sha256_bytes(manifest_bytes),
    )


def export_predictive_dataset(
    dataset: PredictiveDataset,
    *,
    output_dir: Path,
    workspace_root: Path,
    exported_at: datetime | None = None,
) -> PredictiveExportReceipt:
    """Seal one dataset atomically under output_dir or reuse an identical artifact."""
    resolved_output = _validate_output_dir(output_dir, workspace_root=workspace_root)
    resolved_output.mkdir(parents=True, exist_ok=True)
    dataset_dir = resolved_output / str(dataset.dataset_id)
    reused = _existing_receipt(dataset_dir, dataset=dataset, output_dir=resolved_output)
    if reused is not None:
        return reused

    timestamp = exported_at if exported_at is not None else datetime.now(UTC)
    rows = tuple(_row_values(sample) for sample in dataset.samples)
    temporary = resolved_output / f".{dataset.dataset_id}.tmp-{uuid4().hex}"
    temporary.mkdir()
    try:
        rows_path = temporary / ROWS_FILE_NAME
        _write_parquet(rows_path, rows)
        readback = _readback_rows(rows_path)
        if readback != _expected_readback(rows):
            raise PredictiveExportError("Parquet readback differs from the dataset rows")
        digest, size = _sha256_file(rows_path)
        if size > MAX_ARTIFACT_BYTES:
            raise PredictiveExportError("Parquet artifact exceeds the byte budget")
        manifest = PredictiveExportManifest(
            dataset=dataset.manifest,
            artifact=PredictiveArtifactDescriptor(
                rows_sha256=digest,
                rows_bytes=size,
                exported_at=timestamp,
            ),
        )
        payload = _manifest_bytes(manifest)
        if len(payload) > MAX_ARTIFACT_BYTES:
            raise PredictiveExportError("manifest artifact exceeds the byte budget")
        (temporary / MANIFEST_FILE_NAME).write_bytes(payload)
        read_predictive_export(temporary)
        try:
            temporary.rename(dataset_dir)
        except OSError as error:
            competing = _existing_receipt(
                dataset_dir,
                dataset=dataset,
                output_dir=resolved_output,
            )
            if competing is not None:
                _remove_temporary(temporary)
                return competing
            raise PredictiveExportConflictError(
                "dataset destination appeared and is not the same artifact"
            ) from error
    except BaseException:
        if temporary.exists():
            _remove_temporary(temporary)
        raise
    manifest_bytes = (dataset_dir / MANIFEST_FILE_NAME).read_bytes()
    return PredictiveExportReceipt(
        dataset_id=dataset.dataset_id,
        dataset_content_sha256=dataset.dataset_content_sha256,
        row_count=len(dataset.samples),
        output_dir=str(resolved_output),
        dataset_dir=str(dataset_dir),
        status="exported",
        rows_sha256=digest,
        rows_bytes=size,
        manifest_sha256=_sha256_bytes(manifest_bytes),
    )


def _remove_temporary(temporary: Path) -> None:
    for child in temporary.iterdir():
        if child.is_file() and not child.is_symlink():
            child.unlink()
    temporary.rmdir()


__all__ = [
    "MAX_ARTIFACT_BYTES",
    "MANIFEST_FILE_NAME",
    "ROWS_FILE_NAME",
    "PredictiveExportConflictError",
    "PredictiveExportError",
    "PredictiveExportManifest",
    "PredictiveExportPathError",
    "PredictiveExportReceipt",
    "export_predictive_dataset",
    "read_predictive_export",
]
