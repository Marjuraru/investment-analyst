"""Atomic, verifiable export and offline consumption of one reconstructed dataset."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4

import duckdb
from pydantic import ConfigDict, Field, model_validator

from investment_analyst.analytics.analytical_access_models import canonical_analytical_json
from investment_analyst.core.models.base import ContractModel, NonEmptyStr, UTCDateTime
from investment_analyst.research.historical_models import (
    HISTORICAL_ROWS_SCHEMA,
    MANIFEST_FILE_NAME,
    MANIFEST_SCHEMA_VERSION,
    ROWS_FILE_NAME,
    HistoricalArchive,
    HistoricalArchiveReference,
    HistoricalColumnSpec,
    HistoricalDataset,
    HistoricalDatasetManifest,
    HistoricalSample,
    historical_dataset_content_hash,
)

MAX_ARTIFACT_BYTES = 8 * 1024 * 1024
_FEATURE_PROJECTION = (
    "sample_key",
    "decision_at",
    "close_return_1d",
    "rsi_14_fraction",
    "macd_12_26_9_histogram_close_ratio",
)
_TARGET_PROJECTION = ("sample_key", "label", "return_5d")
_METADATA_PROJECTION = (
    "sample_key",
    "decision_at",
    "split",
    "purged",
    "purge_reason",
    "eligible",
)


class HistoricalExportError(RuntimeError):
    """Base error for reconstructed dataset export."""


class HistoricalExportPathError(HistoricalExportError, ValueError):
    """The requested export location is unsafe or inside a protected root."""


class HistoricalExportConflictError(HistoricalExportError):
    """An existing destination differs from the dataset and must not be overwritten."""


def _sql_type(column: HistoricalColumnSpec) -> str:
    return {"text": "VARCHAR", "integer": "INTEGER", "boolean": "BOOLEAN"}[column.data_type]


_TABLE_COLUMNS: tuple[tuple[str, str], ...] = tuple(
    (column.name, _sql_type(column)) for column in HISTORICAL_ROWS_SCHEMA
)


class HistoricalExportArtifact(ContractModel):
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
    def validate_descriptor(self) -> HistoricalExportArtifact:
        if len(self.rows_sha256) != 64:
            raise ValueError("rows_sha256 must be a SHA-256 digest")
        return self


class HistoricalExportArchive(ContractModel):
    """Content-addressed reference to the single conserved archive."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    reference: NonEmptyStr
    archive_id: UUID
    semantic_sha256: str
    candles_sha256: str
    candles_bytes: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_archive(self) -> HistoricalExportArchive:
        if self.reference != f"content-addressed:sha256:{self.semantic_sha256}":
            raise ValueError("archive reference must be content-addressed")
        if len(self.semantic_sha256) != 64 or len(self.candles_sha256) != 64:
            raise ValueError("archive digests must be SHA-256 hex values")
        return self


class HistoricalExportManifest(ContractModel):
    """Sealed manifest with the deterministic dataset, archive and artifact."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["research-historical-manifest-v1"] = MANIFEST_SCHEMA_VERSION
    dataset: HistoricalDatasetManifest
    archive: HistoricalExportArchive
    artifact: HistoricalExportArtifact

    @model_validator(mode="after")
    def validate_manifest(self) -> HistoricalExportManifest:
        if self.archive.archive_id != self.dataset.archive.archive_id:
            raise ValueError("export archive must match the dataset reference")
        if self.archive.semantic_sha256 != self.dataset.archive.semantic_sha256:
            raise ValueError("export archive digest must match the dataset reference")
        if self.archive.candles_sha256 != self.dataset.archive.candles_sha256:
            raise ValueError("export archive physical digest must match the dataset reference")
        return self


class HistoricalExportReceipt(ContractModel):
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


@dataclass(frozen=True, slots=True)
class HistoricalFeatureRow:
    """Only the three causal features for one decision."""

    sample_key: str
    decision_at: datetime
    close_return_1d: Decimal | None
    rsi_14_fraction: Decimal | None
    macd_12_26_9_histogram_close_ratio: Decimal | None


@dataclass(frozen=True, slots=True)
class HistoricalTargetRow:
    """Only the forward target for one decision."""

    sample_key: str
    label: int | None
    return_5d: Decimal | None


@dataclass(frozen=True, slots=True)
class HistoricalMetadataRow:
    """Split and eligibility metadata, never a feature or a target."""

    sample_key: str
    decision_at: datetime
    split: str
    purged: bool
    purge_reason: str | None
    eligible: bool


@dataclass(frozen=True, slots=True)
class HistoricalOfflineView:
    """Verified offline view with features, targets and metadata explicitly separated."""

    manifest: HistoricalExportManifest
    features: tuple[HistoricalFeatureRow, ...]
    targets: tuple[HistoricalTargetRow, ...]
    metadata: tuple[HistoricalMetadataRow, ...]


def _canonical_json_text(value: object) -> str:
    return canonical_analytical_json(value).decode("utf-8")


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.astimezone(UTC).isoformat()


def _decimal_text(value: object) -> str | None:
    return None if value is None else str(value)


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


def _validate_output_dir(output_dir: Path, *, forbidden_roots: tuple[Path, ...]) -> Path:
    if not output_dir.is_absolute():
        raise HistoricalExportPathError("output_dir must be an absolute path")
    declared = output_dir.expanduser()
    for candidate in (declared, *declared.parents):
        if candidate.is_symlink():
            raise HistoricalExportPathError("output_dir must not traverse a symbolic link")
    resolved = declared.resolve(strict=False)
    if resolved == Path(resolved.anchor):
        raise HistoricalExportPathError("output_dir must not be the filesystem root")
    for root in forbidden_roots:
        resolved_root = root.expanduser().resolve(strict=False)
        if resolved == resolved_root or resolved_root in resolved.parents:
            raise HistoricalExportPathError(
                f"output_dir must stay outside the protected root {resolved_root}"
            )
    return resolved


def _row_values(
    sample: HistoricalSample, *, archive: HistoricalArchiveReference
) -> tuple[object, ...]:
    features = {item.column_name: item for item in sample.features}
    close_return = features["close_return_1d"].value
    rsi = features["rsi_14_fraction"].value
    histogram = features["macd_12_26_9_histogram_close_ratio"].value
    segment_seed = features["rsi_14_fraction"].segment_seed_start
    warmup = max(item.warmup_bars for item in sample.features)
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
        _iso(sample.label.evidence_available_at),
        _canonical_json_text([item.model_dump(mode="json") for item in sample.features]),
        _canonical_json_text(sample.label.model_dump(mode="json")),
        sample.split.value,
        sample.purged,
        None if sample.purge_reason is None else sample.purge_reason.value,
        sample.eligible,
        str(archive.archive_id),
        archive.semantic_sha256,
        None if segment_seed is None else segment_seed.isoformat(),
        warmup,
    )


def _write_parquet(path: Path, rows: tuple[tuple[object, ...], ...]) -> None:
    connection = duckdb.connect(":memory:")
    try:
        connection.execute("SET threads=2")
        connection.execute("SET memory_limit='512MB'")
        definition = ", ".join(f"{name} {data_type}" for name, data_type in _TABLE_COLUMNS)
        connection.execute(f"CREATE TABLE historical_rows ({definition})")
        placeholders = ", ".join("?" for _ in _TABLE_COLUMNS)
        connection.executemany(
            f"INSERT INTO historical_rows VALUES ({placeholders})",
            rows,
        )
        columns = ", ".join(name for name, _ in _TABLE_COLUMNS)
        connection.execute(
            f"COPY (SELECT {columns} FROM historical_rows "
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


def _manifest_bytes(manifest: HistoricalExportManifest) -> bytes:
    return canonical_analytical_json(manifest.model_dump(mode="json"))


def _archive_descriptor(archive: HistoricalArchive) -> HistoricalExportArchive:
    return HistoricalExportArchive(
        reference=f"content-addressed:sha256:{archive.manifest.semantic_sha256}",
        archive_id=archive.archive_id,
        semantic_sha256=archive.manifest.semantic_sha256,
        candles_sha256=archive.manifest.candles_sha256,
        candles_bytes=archive.manifest.candles_bytes,
    )


def read_historical_export(dataset_dir: Path) -> HistoricalExportManifest:
    """Read and fully verify one sealed dataset directory."""
    if dataset_dir.is_symlink() or not dataset_dir.is_dir():
        raise HistoricalExportConflictError("dataset directory is missing or is a symlink")
    manifest_path = dataset_dir / MANIFEST_FILE_NAME
    rows_path = dataset_dir / ROWS_FILE_NAME
    if rows_path.is_symlink() or not rows_path.is_file():
        raise HistoricalExportConflictError("rows.parquet is missing or is a symlink")
    try:
        payload = manifest_path.read_bytes()
    except OSError as error:
        raise HistoricalExportConflictError("manifest.json is unreadable") from error
    try:
        manifest = HistoricalExportManifest.model_validate_json(payload)
    except ValueError as error:
        raise HistoricalExportConflictError("manifest.json is malformed") from error
    if payload != _manifest_bytes(manifest):
        raise HistoricalExportConflictError("manifest.json is not in canonical encoding")
    digest, size = _sha256_file(rows_path)
    if digest != manifest.artifact.rows_sha256 or size != manifest.artifact.rows_bytes:
        raise HistoricalExportConflictError("rows.parquet differs from its manifest")
    if size > MAX_ARTIFACT_BYTES or len(payload) > MAX_ARTIFACT_BYTES:
        raise HistoricalExportConflictError("sealed artifact exceeds the byte budget")
    expected_hash = historical_dataset_content_hash(
        dataset_id=manifest.dataset.dataset_id,
        rows=manifest.dataset.rows,
    )
    if expected_hash != manifest.dataset.dataset_content_sha256:
        raise HistoricalExportConflictError("dataset semantic hash does not match its rows")
    expected_rows = tuple(
        _row_values(sample, archive=manifest.dataset.archive) for sample in manifest.dataset.rows
    )
    if _readback_rows(rows_path) != _expected_readback(expected_rows):
        raise HistoricalExportConflictError("rows.parquet content differs from its manifest")
    return manifest


def _existing_receipt(
    dataset_dir: Path,
    *,
    dataset: HistoricalDataset,
    output_dir: Path,
) -> HistoricalExportReceipt | None:
    if not dataset_dir.exists():
        return None
    manifest = read_historical_export(dataset_dir)
    if (
        manifest.dataset.dataset_id != dataset.dataset_id
        or manifest.dataset.dataset_content_sha256 != dataset.dataset_content_sha256
        or len(manifest.dataset.rows) != len(dataset.samples)
    ):
        raise HistoricalExportConflictError("destination contains a different dataset")
    manifest_bytes = (dataset_dir / MANIFEST_FILE_NAME).read_bytes()
    return HistoricalExportReceipt(
        dataset_id=dataset.dataset_id,
        dataset_content_sha256=dataset.dataset_content_sha256,
        row_count=len(dataset.samples),
        output_dir=str(output_dir),
        dataset_dir=str(dataset_dir),
        status="reused",
        rows_sha256=manifest.artifact.rows_sha256,
        rows_bytes=manifest.artifact.rows_bytes,
        manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
    )


def export_historical_dataset(
    dataset: HistoricalDataset,
    *,
    archive: HistoricalArchive,
    output_dir: Path,
    forbidden_roots: tuple[Path, ...],
    exported_at: datetime | None = None,
) -> HistoricalExportReceipt:
    """Seal one dataset atomically under output_dir or reuse an identical artifact."""
    if archive.archive_id != dataset.manifest.archive.archive_id:
        raise HistoricalExportError("the sealed archive does not match the dataset reference")
    resolved_output = _validate_output_dir(output_dir, forbidden_roots=forbidden_roots)
    resolved_output.mkdir(parents=True, exist_ok=True)
    dataset_dir = resolved_output / str(dataset.dataset_id)
    reused = _existing_receipt(dataset_dir, dataset=dataset, output_dir=resolved_output)
    if reused is not None:
        return reused

    timestamp = exported_at if exported_at is not None else datetime.now(UTC)
    rows = tuple(
        _row_values(sample, archive=dataset.manifest.archive) for sample in dataset.samples
    )
    temporary = resolved_output / f".{dataset.dataset_id}.tmp-{uuid4().hex}"
    temporary.mkdir()
    try:
        rows_path = temporary / ROWS_FILE_NAME
        _write_parquet(rows_path, rows)
        if _readback_rows(rows_path) != _expected_readback(rows):
            raise HistoricalExportError("Parquet readback differs from the dataset rows")
        digest, size = _sha256_file(rows_path)
        if size > MAX_ARTIFACT_BYTES:
            raise HistoricalExportError("Parquet artifact exceeds the byte budget")
        manifest = HistoricalExportManifest(
            dataset=dataset.manifest,
            archive=_archive_descriptor(archive),
            artifact=HistoricalExportArtifact(
                rows_sha256=digest,
                rows_bytes=size,
                exported_at=timestamp,
            ),
        )
        payload = _manifest_bytes(manifest)
        if len(payload) > MAX_ARTIFACT_BYTES:
            raise HistoricalExportError("manifest artifact exceeds the byte budget")
        (temporary / MANIFEST_FILE_NAME).write_bytes(payload)
        read_historical_export(temporary)
        try:
            temporary.rename(dataset_dir)
        except OSError as error:
            competing = _existing_receipt(dataset_dir, dataset=dataset, output_dir=resolved_output)
            if competing is not None:
                _remove_temporary(temporary)
                return competing
            raise HistoricalExportConflictError(
                "dataset destination appeared and is not the same artifact"
            ) from error
    except BaseException:
        if temporary.exists():
            _remove_temporary(temporary)
        raise
    manifest_bytes = (dataset_dir / MANIFEST_FILE_NAME).read_bytes()
    return HistoricalExportReceipt(
        dataset_id=dataset.dataset_id,
        dataset_content_sha256=dataset.dataset_content_sha256,
        row_count=len(dataset.samples),
        output_dir=str(resolved_output),
        dataset_dir=str(dataset_dir),
        status="exported",
        rows_sha256=digest,
        rows_bytes=size,
        manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
    )


def _decimals(value: object) -> Decimal | None:
    return None if value is None else Decimal(str(value))


def _instants(value: object) -> datetime:
    if not isinstance(value, str):
        raise HistoricalExportConflictError("decision_at must be an ISO 8601 string")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise HistoricalExportConflictError("decision_at must be timezone-aware")
    return parsed.astimezone(UTC)


def read_historical_dataset_view(dataset_dir: Path) -> HistoricalOfflineView:
    """Load an offline view selecting only the declared feature, target or metadata columns."""
    manifest = read_historical_export(dataset_dir)
    rows_path = dataset_dir / ROWS_FILE_NAME
    connection = duckdb.connect(":memory:")
    try:
        connection.execute("SET threads=2")
        connection.execute("SET memory_limit='512MB'")
        feature_rows = connection.execute(
            "SELECT " + ", ".join(_FEATURE_PROJECTION) + " FROM read_parquet(?)"
            " ORDER BY decision_at, sample_key",
            [str(rows_path)],
        ).fetchall()
        target_rows = connection.execute(
            "SELECT " + ", ".join(_TARGET_PROJECTION) + " FROM read_parquet(?) ORDER BY sample_key",
            [str(rows_path)],
        ).fetchall()
        metadata_rows = connection.execute(
            "SELECT " + ", ".join(_METADATA_PROJECTION) + " FROM read_parquet(?)"
            " ORDER BY decision_at, sample_key",
            [str(rows_path)],
        ).fetchall()
    finally:
        connection.close()
    features = tuple(
        HistoricalFeatureRow(
            sample_key=str(row[0]),
            decision_at=_instants(row[1]),
            close_return_1d=_decimals(row[2]),
            rsi_14_fraction=_decimals(row[3]),
            macd_12_26_9_histogram_close_ratio=_decimals(row[4]),
        )
        for row in feature_rows
    )
    targets = tuple(
        HistoricalTargetRow(
            sample_key=str(row[0]),
            label=None if row[1] is None else int(row[1]),
            return_5d=_decimals(row[2]),
        )
        for row in target_rows
    )
    metadata = tuple(
        HistoricalMetadataRow(
            sample_key=str(row[0]),
            decision_at=_instants(row[1]),
            split=str(row[2]),
            purged=bool(row[3]),
            purge_reason=None if row[4] is None else str(row[4]),
            eligible=bool(row[5]),
        )
        for row in metadata_rows
    )
    if not (len(features) == len(targets) == len(metadata) == len(manifest.dataset.rows)):
        raise HistoricalExportConflictError("offline projections disagree on the row count")
    return HistoricalOfflineView(
        manifest=manifest,
        features=features,
        targets=targets,
        metadata=metadata,
    )


def _remove_temporary(temporary: Path) -> None:
    for child in temporary.iterdir():
        if child.is_file() and not child.is_symlink():
            child.unlink()
    temporary.rmdir()


__all__ = [
    "MAX_ARTIFACT_BYTES",
    "HistoricalExportArchive",
    "HistoricalExportArtifact",
    "HistoricalExportConflictError",
    "HistoricalExportError",
    "HistoricalExportManifest",
    "HistoricalExportPathError",
    "HistoricalExportReceipt",
    "HistoricalFeatureRow",
    "HistoricalMetadataRow",
    "HistoricalOfflineView",
    "HistoricalTargetRow",
    "export_historical_dataset",
    "read_historical_dataset_view",
    "read_historical_export",
]
