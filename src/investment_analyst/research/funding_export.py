"""Atomic funding-complement export, verification and offline training view."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4

import duckdb
from pydantic import ConfigDict, Field

from investment_analyst.analytics.analytical_access_models import canonical_analytical_json
from investment_analyst.core.models.base import ContractModel, NonEmptyStr
from investment_analyst.research.funding_features import FundingBaseDecision
from investment_analyst.research.funding_models import (
    FUNDING_COLUMN_24H,
    FUNDING_COLUMN_168H,
    FUNDING_COLUMNS,
    FUNDING_FEATURES_FILE_NAME,
    FUNDING_ROWS_SCHEMA,
    MANIFEST_FILE_NAME,
    MAX_BUNDLE_BYTES,
    FundingArchive,
    FundingArchiveReference,
    FundingBaseDatasetReference,
    FundingBundle,
    FundingColumnSpec,
    FundingExportManifest,
    FundingFeatureManifest,
    FundingSample,
    funding_archive_reference,
)
from investment_analyst.research.historical_export import read_historical_export
from investment_analyst.research.historical_models import (
    CLOSE_RETURN_COLUMN,
    MACD_HISTOGRAM_COLUMN,
    RSI_COLUMN,
    HistoricalSample,
    SampleSplit,
)


class FundingExportError(RuntimeError):
    """Base error for funding complement export."""


class FundingExportPathError(FundingExportError, ValueError):
    """The requested export location is unsafe or inside a protected root."""


class FundingExportConflictError(FundingExportError):
    """An existing destination differs from the funding bundle."""


@dataclass(frozen=True, slots=True)
class LoadedBaseDataset:
    """Verified reference, decision metadata and original samples."""

    reference: FundingBaseDatasetReference
    decisions: tuple[FundingBaseDecision, ...]
    samples: tuple[HistoricalSample, ...]


@dataclass(frozen=True, slots=True)
class FundingTrainingFeatureRow:
    """Training features only: original reconstructed features plus funding."""

    sample_key: str
    decision_at: datetime
    close_return_1d: Decimal | None
    rsi_14_fraction: Decimal | None
    macd_12_26_9_histogram_close_ratio: Decimal | None
    funding_sum_1h_24h: Decimal | None
    funding_sum_1h_168h: Decimal | None


@dataclass(frozen=True, slots=True)
class FundingTrainingTargetRow:
    """Forward target kept separate from every feature matrix."""

    sample_key: str
    label: int | None
    return_5d: Decimal | None


@dataclass(frozen=True, slots=True)
class FundingTrainingMetadataRow:
    """Split, purge and eligibility metadata without target values."""

    sample_key: str
    decision_at: datetime
    split: str
    purged: bool
    eligibility_base: bool
    eligibility_joint: bool


@dataclass(frozen=True, slots=True)
class FundingTrainingView:
    """Verified TRAIN/VALIDATION view; holdout rows are never materialized here."""

    manifest: FundingFeatureManifest
    features: tuple[FundingTrainingFeatureRow, ...]
    targets: tuple[FundingTrainingTargetRow, ...]
    metadata: tuple[FundingTrainingMetadataRow, ...]


class FundingBundleReceipt(ContractModel):
    """Compact export receipt without feature values."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    bundle_id: UUID
    bundle_content_sha256: str
    row_count: int = Field(ge=0)
    output_dir: NonEmptyStr
    bundle_dir: NonEmptyStr
    status: Literal["exported", "reused"]
    rows_sha256: str
    rows_bytes: int = Field(ge=0)
    manifest_sha256: str

    def to_json_dict(self) -> dict[str, object]:
        """Return the deterministic JSON projection used by the CLI."""
        return self.model_dump(mode="json")


def load_base_dataset(dataset_dir: Path) -> LoadedBaseDataset:
    """Read and verify the original reconstructed dataset through its public reader."""
    manifest = read_historical_export(dataset_dir)
    dataset = manifest.dataset
    reference = FundingBaseDatasetReference(
        dataset_id=dataset.dataset_id,
        dataset_content_sha256=dataset.dataset_content_sha256,
        feature_set_version=dataset.feature_set_version,
        experiment_spec_id=dataset.experiment_spec_id,
        target_version=dataset.target_version,
        asset_id=dataset.asset.asset_id,
        start_date=dataset.request.start_date,
        end_date=dataset.request.end_date,
        known_at=dataset.request.known_at,
        row_count=len(dataset.rows),
        base_archive_id=dataset.archive.archive_id,
        base_archive_semantic_sha256=dataset.archive.semantic_sha256,
    )
    decisions = tuple(
        FundingBaseDecision(
            sample_key=sample.sample_key,
            decision_at=sample.decision_at,
            eligible=sample.eligible,
            split=sample.split,
            purged=sample.purged,
        )
        for sample in dataset.rows
    )
    return LoadedBaseDataset(
        reference=reference,
        decisions=decisions,
        samples=dataset.rows,
    )


def _sql_type(column: FundingColumnSpec) -> str:
    return {"text": "VARCHAR", "integer": "INTEGER", "boolean": "BOOLEAN"}[column.data_type]


_TABLE_COLUMNS: tuple[tuple[str, str], ...] = tuple(
    (column.name, _sql_type(column)) for column in FUNDING_ROWS_SCHEMA
)


def _canonical_json_text(value: object) -> str:
    return canonical_analytical_json(value).decode("utf-8")


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.astimezone(UTC).isoformat()


def _decimal_text(value: Decimal | None) -> str | None:
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
        raise FundingExportPathError("output_dir must be an absolute path")
    declared = output_dir.expanduser()
    for candidate in (declared, *declared.parents):
        if candidate.is_symlink():
            raise FundingExportPathError("output_dir must not traverse a symbolic link")
    resolved = declared.resolve(strict=False)
    if resolved == Path(resolved.anchor):
        raise FundingExportPathError("output_dir must not be the filesystem root")
    for root in forbidden_roots:
        resolved_root = root.expanduser().resolve(strict=False)
        if resolved == resolved_root or resolved_root in resolved.parents:
            raise FundingExportPathError(
                f"output_dir must stay outside the protected root {resolved_root}"
            )
    if resolved.exists():
        if resolved.is_symlink() or not resolved.is_dir():
            raise FundingExportConflictError("output_dir must be a plain directory")
        _validate_output_entries(resolved)
    return resolved


def _is_archive_claim(directory: Path) -> bool:
    return (directory / "pages.jsonl").exists()


def _is_bundle_claim(directory: Path) -> bool:
    return (directory / FUNDING_FEATURES_FILE_NAME).exists()


def _validate_output_entries(root: Path) -> None:
    for child in sorted(root.iterdir(), key=lambda item: item.name):
        if child.name.startswith("."):
            continue
        if child.is_symlink() or not child.is_dir():
            raise FundingExportConflictError("output_dir contains an unexpected entry")
        if _is_archive_claim(child) or _is_bundle_claim(child):
            continue
        if (child / MANIFEST_FILE_NAME).exists():
            raise FundingExportConflictError("output_dir contains an unrecognized artifact")
        raise FundingExportConflictError("output_dir contains an unrelated entry")


def _row_values(
    sample: FundingSample,
    *,
    archive: FundingArchiveReference,
    base_dataset: FundingBaseDatasetReference,
) -> tuple[object, ...]:
    features = {item.column_name: item for item in sample.features}
    return (
        str(sample.sample_key),
        _iso(sample.decision_at),
        _decimal_text(features[FUNDING_COLUMN_24H].value),
        _decimal_text(features[FUNDING_COLUMN_168H].value),
        sample.funding_state.value,
        sample.eligibility_base,
        sample.eligibility_joint,
        sample.split.value,
        sample.purged,
        _canonical_json_text([item.model_dump(mode="json") for item in sample.features]),
        str(sample.funding_feature_hash),
        str(archive.archive_id),
        archive.semantic_sha256,
        str(base_dataset.dataset_id),
        base_dataset.dataset_content_sha256,
    )


def _write_parquet(path: Path, rows: tuple[tuple[object, ...], ...]) -> None:
    connection = duckdb.connect(":memory:")
    try:
        connection.execute("SET threads=2")
        connection.execute("SET memory_limit='256MB'")
        definition = ", ".join(f"{name} {data_type}" for name, data_type in _TABLE_COLUMNS)
        connection.execute(f"CREATE TABLE funding_rows ({definition})")
        placeholders = ", ".join("?" for _ in _TABLE_COLUMNS)
        connection.executemany(
            f"INSERT INTO funding_rows VALUES ({placeholders})",
            rows,
        )
        columns = ", ".join(name for name, _ in _TABLE_COLUMNS)
        connection.execute(
            f"COPY (SELECT {columns} FROM funding_rows "
            "ORDER BY decision_at, sample_key) TO ? (FORMAT PARQUET)",
            [str(path)],
        )
    finally:
        connection.close()


def _readback_rows(path: Path) -> tuple[tuple[object, ...], ...]:
    connection = duckdb.connect(":memory:")
    try:
        connection.execute("SET threads=2")
        connection.execute("SET memory_limit='256MB'")
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
    return tuple(tuple(row) for row in sorted(rows, key=lambda item: (str(item[1]), str(item[0]))))


def _manifest_bytes(manifest: FundingExportManifest) -> bytes:
    return canonical_analytical_json(manifest.model_dump(mode="json"))


def _expected_rows(bundle: FundingBundle) -> tuple[tuple[object, ...], ...]:
    return tuple(
        _row_values(
            sample,
            archive=bundle.manifest.archive,
            base_dataset=bundle.manifest.base_dataset,
        )
        for sample in bundle.manifest.rows
    )


def read_funding_bundle(bundle_dir: Path) -> FundingBundle:
    """Read and fully verify one sealed funding-complement directory."""
    if bundle_dir.is_symlink() or not bundle_dir.is_dir():
        raise FundingExportConflictError("bundle directory is missing or is a symlink")
    manifest_path = bundle_dir / MANIFEST_FILE_NAME
    rows_path = bundle_dir / FUNDING_FEATURES_FILE_NAME
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise FundingExportConflictError("manifest.json is missing or is a symlink")
    if rows_path.is_symlink() or not rows_path.is_file():
        raise FundingExportConflictError("funding_features.parquet is missing or is a symlink")
    try:
        payload = manifest_path.read_bytes()
    except OSError as error:
        raise FundingExportConflictError("manifest.json is unreadable") from error
    try:
        manifest = FundingExportManifest.model_validate_json(payload)
    except ValueError as error:
        raise FundingExportConflictError("manifest.json is malformed") from error
    if payload != _manifest_bytes(manifest):
        raise FundingExportConflictError("manifest.json is not in canonical encoding")
    digest, size = _sha256_file(rows_path)
    if digest != manifest.artifact.rows_sha256 or size != manifest.artifact.rows_bytes:
        raise FundingExportConflictError("funding_features.parquet differs from its manifest")
    if size > MAX_BUNDLE_BYTES or len(payload) > MAX_BUNDLE_BYTES:
        raise FundingExportConflictError("sealed funding bundle exceeds the byte budget")
    expected_rows = _expected_rows(
        FundingBundle(manifest=manifest.features, rows=manifest.features.rows)
    )
    if _readback_rows(rows_path) != _expected_readback(expected_rows):
        raise FundingExportConflictError("funding_features.parquet differs from its manifest rows")
    return FundingBundle(manifest=manifest.features, rows=manifest.features.rows)


def _existing_receipt(
    bundle_dir: Path,
    *,
    bundle: FundingBundle,
    output_dir: Path,
) -> FundingBundleReceipt | None:
    if not bundle_dir.exists():
        return None
    existing = read_funding_bundle(bundle_dir)
    if (
        existing.manifest.bundle_id != bundle.manifest.bundle_id
        or existing.manifest.bundle_content_sha256 != bundle.manifest.bundle_content_sha256
        or existing.manifest.base_dataset != bundle.manifest.base_dataset
        or existing.manifest.archive != bundle.manifest.archive
        or existing.manifest.request != bundle.manifest.request
        or len(existing.rows) != len(bundle.rows)
    ):
        raise FundingExportConflictError("destination contains a different funding bundle")
    manifest_bytes = (bundle_dir / MANIFEST_FILE_NAME).read_bytes()
    artifact = FundingExportManifest.model_validate_json(manifest_bytes).artifact
    return FundingBundleReceipt(
        bundle_id=bundle.manifest.bundle_id,
        bundle_content_sha256=bundle.manifest.bundle_content_sha256,
        row_count=len(bundle.rows),
        output_dir=str(output_dir),
        bundle_dir=str(bundle_dir),
        status="reused",
        rows_sha256=artifact.rows_sha256,
        rows_bytes=artifact.rows_bytes,
        manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
    )


def export_funding_bundle(
    bundle: FundingBundle,
    *,
    archive: FundingArchive,
    output_dir: Path,
    forbidden_roots: tuple[Path, ...],
    exported_at: datetime | None = None,
) -> FundingBundleReceipt:
    """Seal one funding complement atomically or reuse an identical artifact."""
    expected_archive = funding_archive_reference(archive)
    if bundle.manifest.archive != expected_archive:
        raise FundingExportError("the sealed funding archive does not match the bundle reference")
    resolved_output = _validate_output_dir(output_dir, forbidden_roots=forbidden_roots)
    resolved_output.mkdir(parents=True, exist_ok=True)
    bundle_dir = resolved_output / str(bundle.manifest.bundle_id)
    reused = _existing_receipt(bundle_dir, bundle=bundle, output_dir=resolved_output)
    if reused is not None:
        return reused

    timestamp = exported_at if exported_at is not None else datetime.now(UTC)
    rows = _expected_rows(bundle)
    temporary = resolved_output / f".{bundle.manifest.bundle_id}.tmp-{uuid4().hex}"
    temporary.mkdir()
    try:
        rows_path = temporary / FUNDING_FEATURES_FILE_NAME
        _write_parquet(rows_path, rows)
        if _readback_rows(rows_path) != _expected_readback(rows):
            raise FundingExportError("Parquet readback differs from the funding rows")
        digest, size = _sha256_file(rows_path)
        if size > MAX_BUNDLE_BYTES:
            raise FundingExportError("funding Parquet artifact exceeds the byte budget")
        manifest = FundingExportManifest(
            features=bundle.manifest,
            artifact={
                "rows_sha256": digest,
                "rows_bytes": size,
                "exported_at": timestamp,
            },
        )
        payload = _manifest_bytes(manifest)
        if len(payload) > MAX_BUNDLE_BYTES:
            raise FundingExportError("funding manifest artifact exceeds the byte budget")
        (temporary / MANIFEST_FILE_NAME).write_bytes(payload)
        read_funding_bundle(temporary)
        try:
            temporary.rename(bundle_dir)
        except OSError as error:
            competing = _existing_receipt(bundle_dir, bundle=bundle, output_dir=resolved_output)
            if competing is not None:
                _remove_temporary(temporary)
                return competing
            raise FundingExportConflictError(
                "funding bundle destination appeared and is not the same artifact"
            ) from error
    except BaseException:
        if temporary.exists():
            _remove_temporary(temporary)
        raise
    manifest_bytes = (bundle_dir / MANIFEST_FILE_NAME).read_bytes()
    return FundingBundleReceipt(
        bundle_id=bundle.manifest.bundle_id,
        bundle_content_sha256=bundle.manifest.bundle_content_sha256,
        row_count=len(bundle.rows),
        output_dir=str(resolved_output),
        bundle_dir=str(bundle_dir),
        status="exported",
        rows_sha256=digest,
        rows_bytes=size,
        manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
    )


def _decimals(value: object) -> Decimal | None:
    return None if value is None else Decimal(str(value))


def _instants(value: object) -> datetime:
    if not isinstance(value, str):
        raise FundingExportConflictError("decision_at must be an ISO 8601 string")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise FundingExportConflictError("decision_at must be timezone-aware")
    return parsed.astimezone(UTC)


def _base_feature(sample: HistoricalSample, column: str) -> Decimal | None:
    return next(item.value for item in sample.features if item.column_name == column)


def _validate_base_alignment(
    bundle: FundingBundle,
    loaded: LoadedBaseDataset,
) -> dict[UUID, HistoricalSample]:
    if loaded.reference != bundle.manifest.base_dataset:
        raise FundingExportConflictError("base dataset reference differs from the funding bundle")
    if loaded.reference.asset_id != bundle.manifest.funding_asset.asset_id:
        raise FundingExportConflictError("base dataset asset differs from the funding asset")
    if len(loaded.samples) != len(bundle.rows):
        raise FundingExportConflictError("base dataset and funding bundle row counts differ")
    samples_by_key = {sample.sample_key: sample for sample in loaded.samples}
    if len(samples_by_key) != len(loaded.samples):
        raise FundingExportConflictError("base dataset repeats a sample identity")
    for funding, base in zip(bundle.rows, loaded.decisions, strict=True):
        if funding.sample_key != base.sample_key or funding.decision_at != base.decision_at:
            raise FundingExportConflictError("funding rows do not align with the base dataset")
        if (
            funding.eligibility_base != base.eligible
            or funding.split is not base.split
            or funding.purged != base.purged
        ):
            raise FundingExportConflictError("funding metadata contradicts the base dataset")
    return samples_by_key


def read_funding_training_view(
    bundle_dir: Path,
    base_dataset_dir: Path,
) -> FundingTrainingView:
    """Return only TRAIN/VALIDATION features and targets, joined by sample_key.

    The full bundle is verified first, including holdout availability, but holdout
    rows are never materialized into these training collections.
    """
    bundle = read_funding_bundle(bundle_dir)
    loaded = load_base_dataset(base_dataset_dir)
    samples_by_key = _validate_base_alignment(bundle, loaded)
    features: list[FundingTrainingFeatureRow] = []
    targets: list[FundingTrainingTargetRow] = []
    metadata: list[FundingTrainingMetadataRow] = []
    for row in bundle.rows:
        if row.split not in {SampleSplit.TRAIN, SampleSplit.VALIDATION}:
            continue
        base = samples_by_key[row.sample_key]
        funding = {item.column_name: item for item in row.features}
        if set(funding) != set(FUNDING_COLUMNS):
            raise FundingExportConflictError("funding rows lack the declared features")
        features.append(
            FundingTrainingFeatureRow(
                sample_key=str(row.sample_key),
                decision_at=row.decision_at,
                close_return_1d=_base_feature(base, CLOSE_RETURN_COLUMN),
                rsi_14_fraction=_base_feature(base, RSI_COLUMN),
                macd_12_26_9_histogram_close_ratio=_base_feature(base, MACD_HISTOGRAM_COLUMN),
                funding_sum_1h_24h=funding[FUNDING_COLUMN_24H].value,
                funding_sum_1h_168h=funding[FUNDING_COLUMN_168H].value,
            )
        )
        targets.append(
            FundingTrainingTargetRow(
                sample_key=str(row.sample_key),
                label=base.label.label,
                return_5d=base.label.return_5d,
            )
        )
        metadata.append(
            FundingTrainingMetadataRow(
                sample_key=str(row.sample_key),
                decision_at=row.decision_at,
                split=row.split.value,
                purged=row.purged,
                eligibility_base=row.eligibility_base,
                eligibility_joint=row.eligibility_joint,
            )
        )
    expected = sum(
        item.requested
        for item in bundle.manifest.splits
        if item.split in {SampleSplit.TRAIN, SampleSplit.VALIDATION}
    )
    if not (len(features) == len(targets) == len(metadata) == expected):
        raise FundingExportConflictError("training view projections disagree on the row count")
    return FundingTrainingView(
        manifest=bundle.manifest,
        features=tuple(features),
        targets=tuple(targets),
        metadata=tuple(metadata),
    )


def _remove_temporary(temporary: Path) -> None:
    for child in temporary.iterdir():
        if child.is_file() and not child.is_symlink():
            child.unlink()
    temporary.rmdir()


__all__ = [
    "FundingBundleReceipt",
    "FundingExportConflictError",
    "FundingExportError",
    "FundingExportPathError",
    "FundingTrainingFeatureRow",
    "FundingTrainingMetadataRow",
    "FundingTrainingTargetRow",
    "FundingTrainingView",
    "LoadedBaseDataset",
    "export_funding_bundle",
    "load_base_dataset",
    "read_funding_bundle",
    "read_funding_training_view",
]
