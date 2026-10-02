"""Verified backup and restore of an isolated raw v2 staging destination.

A staging backup is an ordered inventory of regular files with sizes and
SHA-256 digests, bound to the stable ``staging_id`` of the source staging,
the checkpoint versions/digests of both imports when they exist, and the
verified corpus counts and ordered digest covering raw and observations.
Backups without observations keep the raw-only ``v1`` schema and read path;
backups with an observation table emit the ``v2`` schema and verify both
inventories and both checkpoints before any atomic promotion. Only a
file-backed DuckDB index that lives inside the staging destination and is
consistent with the supplied writer connection is eligible; memory or
external index paths fail closed.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from duckdb import DuckDBPyConnection
from pydantic import ConfigDict, Field, field_validator, model_validator

from investment_analyst.core.models.base import ContractModel, NonEmptyStr, UTCDateTime
from investment_analyst.storage.errors import StorageError
from investment_analyst.storage.observation_v2 import (
    ObservationV2Error,
    ensure_observation_v2_table,
    observation_to_row,
    row_to_observation,
)
from investment_analyst.storage.observation_v2_import import (
    ObservationV2ImportState,
    observation_empty_digest,
    observation_extend_digest,
)
from investment_analyst.storage.raw_v2 import (
    STAGING_FORMAT,
    RawV2Staging,
    RawV2StagingMarker,
    index_database_path,
)
from investment_analyst.storage.raw_v2_import import (
    RawV2ImportState,
    empty_digest,
    extend_digest,
)

RAW_V2_BACKUP_MANIFEST_SCHEMA = "raw-v2-staging-backup-manifest-v1"
RAW_V2_BACKUP_MANIFEST_SCHEMA_V2 = "raw-v2-staging-backup-manifest-v2"
RAW_V2_BACKUP_MANIFEST_SCHEMA_V3 = "raw-v2-staging-backup-manifest-v3"
RAW_V2_BACKUP_MANIFEST_SCHEMA_V4 = "raw-v2-staging-backup-manifest-v4"
BACKUP_MANIFEST_NAME = "raw-v2-staging-backup-manifest.json"
_IMPORT_STATE_FILENAME = "raw-v2-import-state.json"
_OBSERVATION_IMPORT_STATE_FILENAME = "observation-v2-import-state.json"
_MAX_BACKUP_PAGE = 256


class RawV2BackupError(StorageError):
    """Raised when a raw v2 staging backup or restore cannot be trusted."""


class RawV2BackupFile(ContractModel):
    """One inventoried regular file inside a staging backup."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    path: NonEmptyStr
    size_bytes: int = Field(ge=0)
    sha256: NonEmptyStr

    @field_validator("path", mode="before")
    @classmethod
    def validate_relative_path(cls, value: object) -> object:
        if not isinstance(value, str) or not value or value.startswith("/"):
            raise ValueError("backup path must be relative")
        if value.split("/") != [part for part in value.split("/") if part not in ("", ".", "..")]:
            raise ValueError("backup path must not traverse")
        return value

    @field_validator("sha256", mode="before")
    @classmethod
    def validate_digest(cls, value: object) -> object:
        if not isinstance(value, str) or len(value) != 64:
            raise ValueError("backup digest must be a SHA-256 hex string")
        return value


class RawV2BackupCounts(ContractModel):
    """Verified corpus counts bound into a staging backup."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    records: int = Field(ge=0)
    counts_by_source: Mapping[str, int]
    counts_by_schema: Mapping[str, int]
    corpus_digest: NonEmptyStr


class RawV2BackupObservationCounts(ContractModel):
    """Verified observation counts bound into a v2 staging backup."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    observations: int = Field(ge=0)
    counts_by_source: Mapping[str, int]
    counts_by_frequency: Mapping[str, int]
    corpus_digest: NonEmptyStr


class RawV2BackupMetricCounts(ContractModel):
    """Verified metric and lineage counts bound into a v3 staging backup."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    metrics: int = Field(ge=0)
    counts_by_metric_key: Mapping[str, int]
    corpus_digest: NonEmptyStr
    evidence_sets: int = Field(ge=0)
    evidence_segments: int = Field(ge=0)
    lineage_digest: NonEmptyStr


class RawV2BackupAnalysisCounts(ContractModel):
    """Verified diagnostic and analysis snapshot counts bound into a v4 staging backup."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    diagnostics: int = Field(ge=0)
    counts_by_mode: Mapping[str, int]
    diagnostic_digest: NonEmptyStr
    snapshots: int = Field(ge=0)
    counts_by_domain: Mapping[str, int]
    snapshot_digest: NonEmptyStr


class RawV2StagingBackupManifest(ContractModel):
    """Versioned inventory used to verify a staging backup before activation.

    Schema ``v1`` is raw-only and stays byte-compatible with prior backups.
    Schema ``v2`` additionally binds the typed observation inventory, its
    content digest and the observation import checkpoint when it exists.
    Schema ``v3`` additionally binds the typed metric inventory with its
    shared lineage digest; v1 and v2 backups stay readable and restorable.
    Schema ``v4`` additionally binds the typed diagnostic and snapshot
    inventory with their verified links and digests.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[
        "raw-v2-staging-backup-manifest-v1",
        "raw-v2-staging-backup-manifest-v2",
        "raw-v2-staging-backup-manifest-v3",
        "raw-v2-staging-backup-manifest-v4",
    ] = RAW_V2_BACKUP_MANIFEST_SCHEMA
    backup_id: UUID
    staging_id: NonEmptyStr
    staging_format: Literal["raw-v2-staging-v1"] = STAGING_FORMAT
    created_at: UTCDateTime
    files: tuple[RawV2BackupFile, ...]
    checkpoint_format: str | None = None
    checkpoint_digest: str | None = None
    counts: RawV2BackupCounts
    observation_checkpoint_format: str | None = None
    observation_checkpoint_digest: str | None = None
    observation_counts: RawV2BackupObservationCounts | None = None
    metric_counts: RawV2BackupMetricCounts | None = None
    analysis_counts: RawV2BackupAnalysisCounts | None = None

    @model_validator(mode="after")
    def validate_inventory(self) -> RawV2StagingBackupManifest:
        paths = tuple(item.path for item in self.files)
        if not paths or paths != tuple(sorted(paths)) or len(paths) != len(set(paths)):
            raise ValueError("backup inventory must be non-empty, unique, and sorted")
        if BACKUP_MANIFEST_NAME in paths:
            raise ValueError("backup inventory must not contain its own manifest")
        if self.schema_version == RAW_V2_BACKUP_MANIFEST_SCHEMA_V4:
            expected_id = _backup_id(
                self.staging_id,
                self.files,
                self.counts,
                self.observation_counts,
                self.schema_version,
                self.metric_counts,
                self.analysis_counts,
            )
        elif self.schema_version == RAW_V2_BACKUP_MANIFEST_SCHEMA_V3:
            expected_id = _backup_id(
                self.staging_id,
                self.files,
                self.counts,
                self.observation_counts,
                self.schema_version,
                self.metric_counts,
            )
        elif self.schema_version == RAW_V2_BACKUP_MANIFEST_SCHEMA_V2:
            expected_id = _backup_id(
                self.staging_id,
                self.files,
                self.counts,
                self.observation_counts,
                self.schema_version,
            )
        else:
            expected_id = _legacy_backup_id(self.staging_id, self.files, self.counts)
        if self.backup_id != expected_id:
            raise ValueError("backup identity does not match its inventory")
        if (self.checkpoint_format is None) != (self.checkpoint_digest is None):
            raise ValueError("checkpoint version and digest travel together")
        if (self.observation_checkpoint_format is None) != (
            self.observation_checkpoint_digest is None
        ):
            raise ValueError("observation checkpoint version and digest travel together")
        if self.schema_version == RAW_V2_BACKUP_MANIFEST_SCHEMA_V4:
            if self.observation_counts is None:
                raise ValueError("v4 manifest requires observation counts")
            if self.metric_counts is None:
                raise ValueError("v4 manifest requires metric counts")
            if self.analysis_counts is None:
                raise ValueError("v4 manifest requires analysis counts")
        elif self.schema_version == RAW_V2_BACKUP_MANIFEST_SCHEMA_V3:
            if self.observation_counts is None:
                raise ValueError("v3 manifest requires observation counts")
            if self.metric_counts is None:
                raise ValueError("v3 manifest requires metric counts")
            if self.analysis_counts is not None:
                raise ValueError("v3 manifest must not carry analysis counts")
        elif self.schema_version == RAW_V2_BACKUP_MANIFEST_SCHEMA_V2:
            if self.observation_counts is None:
                raise ValueError("v2 manifest requires observation counts")
            if self.metric_counts is not None:
                raise ValueError("v2 manifest must not carry metric counts")
            if self.analysis_counts is not None:
                raise ValueError("v2 manifest must not carry analysis counts")
        else:
            if self.observation_counts is not None:
                raise ValueError("v1 manifest must not carry observation counts")
            if self.metric_counts is not None:
                raise ValueError("v1 manifest must not carry metric counts")
            if self.analysis_counts is not None:
                raise ValueError("v1 manifest must not carry analysis counts")
            if self.observation_checkpoint_format is not None:
                raise ValueError("v1 manifest must not carry observation checkpoint")
        return self

    def to_json_dict(self) -> dict[str, object]:
        return self.model_dump(mode="json")


def _backup_id(
    staging_id: str,
    files: tuple[RawV2BackupFile, ...],
    counts: RawV2BackupCounts,
    observation_counts: RawV2BackupObservationCounts | None = None,
    schema_version: str = RAW_V2_BACKUP_MANIFEST_SCHEMA,
    metric_counts: RawV2BackupMetricCounts | None = None,
    analysis_counts: RawV2BackupAnalysisCounts | None = None,
) -> UUID:
    payload: dict[str, object] = {
        "staging_id": staging_id,
        "schema_version": schema_version,
        "files": [item.model_dump(mode="json") for item in files],
        "counts": counts.model_dump(mode="json"),
        "observation_counts": (
            observation_counts.model_dump(mode="json") if observation_counts else None
        ),
        "metric_counts": (metric_counts.model_dump(mode="json") if metric_counts else None),
    }
    if schema_version == RAW_V2_BACKUP_MANIFEST_SCHEMA_V4:
        payload["analysis_counts"] = (
            analysis_counts.model_dump(mode="json") if analysis_counts else None
        )
    document = json.dumps(
        payload,
        separators=(",", ":"),
        sort_keys=True,
    )
    return uuid5(NAMESPACE_URL, document)


def _legacy_backup_id(
    staging_id: str,
    files: tuple[RawV2BackupFile, ...],
    counts: RawV2BackupCounts,
) -> UUID:
    document = json.dumps(
        {
            "staging_id": staging_id,
            "files": [item.model_dump(mode="json") for item in files],
            "counts": counts.model_dump(mode="json"),
        },
        separators=(",", ":"),
        sort_keys=True,
    )
    return uuid5(NAMESPACE_URL, document)


def _sha256_streaming(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _observation_row_digest(row: tuple[object, ...]) -> str:
    document = json.dumps(
        [None if value is None else str(value) for value in row],
        separators=(",", ":"),
        sort_keys=False,
    )
    return hashlib.sha256(document.encode("utf-8")).hexdigest()


def _sha256_row(row: tuple[object, ...]) -> str:
    return _observation_row_digest(row)


def _reject_symlinks(root: Path) -> None:
    for candidate in [root, *root.rglob("*")]:
        if candidate.is_symlink():
            raise RawV2BackupError("staging backup paths must not use symbolic links")


def _index_snapshot_path(destination: Path) -> Path:
    candidate = index_database_path(destination)
    if candidate is None:
        raise RawV2BackupError("staging index must be a single file inside the destination")
    return candidate


def _require_snapshot_consistent(
    connection: DuckDBPyConnection,
    index_path: Path,
    *,
    allow_checkpoint: bool = True,
) -> None:
    listed = {str(row[2]) for row in connection.execute("PRAGMA database_list").fetchall()}
    if str(index_path) not in listed:
        raise RawV2BackupError("staging index is not the connected database file")
    if allow_checkpoint:
        connection.execute("CHECKPOINT")
    if any(
        sibling.suffix == ".wal" and sibling.stem == index_path.name
        for sibling in index_path.parent.iterdir()
    ):
        raise RawV2BackupError("staging index has a pending write-ahead log")
    names = {
        str(row[0])
        for row in connection.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'raw_v2_index'"
        ).fetchall()
    }
    if "document_json" in names:
        raise RawV2BackupError("staging index is incompatible with raw v2")
    count = connection.execute("SELECT count(*) FROM raw_v2_index").fetchone()
    if count is None:
        raise RawV2BackupError("staging index cannot be inspected")


class RawV2StagingBackupService:
    """Create and restore verified snapshots of one raw v2 staging destination."""

    def create(
        self,
        staging: RawV2Staging,
        connection: DuckDBPyConnection,
        backup_root: Path,
    ) -> RawV2StagingBackupManifest:
        """Publish one complete backup only after every file verifies.

        The caller must close its writer connection before calling so the
        DuckDB index file is a consistent file-backed snapshot; the staging
        object is closed (releasing its in-process writer lock) for the
        duration of the copy and reopened afterwards when it was open.
        """
        destination = staging.destination
        if not destination.is_absolute() or destination.is_symlink():
            raise RawV2BackupError("staging destination must be absolute and not a symlink")
        backup_target = backup_root.expanduser()
        if backup_target.is_symlink():
            raise RawV2BackupError("backup destination must not be a symbolic link")
        staging_root = destination.resolve()
        backup_resolved = backup_target.resolve(strict=False)
        if backup_resolved.exists():
            raise RawV2BackupError("backup destination already exists")
        if staging_root == backup_resolved or staging_root in backup_resolved.parents:
            raise RawV2BackupError("backup destination must be outside the staging")
        if backup_resolved in staging_root.parents:
            raise RawV2BackupError("backup destination must be outside the staging")
        staging_was_open = staging.is_open
        try:
            staging.close()
            _reject_symlinks(staging_root)
            index_path = _index_snapshot_path(destination)
            _require_snapshot_consistent(connection, index_path)
            staging_id = staging.staging_id
            if staging_id is None:
                raise RawV2BackupError("staging marker must carry a stable identity")
            temporary = backup_resolved.with_name(f".{backup_resolved.name}.{uuid4().hex}.tmp")
            try:
                temporary.mkdir(parents=True)
                inventory = self._inventory(staging_root)
                counts = self._count_corpus(staging, connection)
                checkpoint_format, checkpoint_digest = self._checkpoint_binding(staging_root)
                observation_counts = self._count_observations(staging, connection)
                (observation_format, observation_digest) = self._observation_checkpoint_binding(
                    staging_root
                )
                if observation_counts is None:
                    manifest = RawV2StagingBackupManifest(
                        backup_id=_legacy_backup_id(staging_id, inventory, counts),
                        staging_id=staging_id,
                        created_at=datetime.now(UTC),
                        files=inventory,
                        checkpoint_format=checkpoint_format,
                        checkpoint_digest=checkpoint_digest,
                        counts=counts,
                    )
                else:
                    metric_counts = self._count_metrics(staging, connection)
                    if metric_counts is None:
                        manifest = RawV2StagingBackupManifest(
                            schema_version=RAW_V2_BACKUP_MANIFEST_SCHEMA_V2,
                            backup_id=_backup_id(
                                staging_id,
                                inventory,
                                counts,
                                observation_counts,
                                RAW_V2_BACKUP_MANIFEST_SCHEMA_V2,
                            ),
                            staging_id=staging_id,
                            created_at=datetime.now(UTC),
                            files=inventory,
                            checkpoint_format=checkpoint_format,
                            checkpoint_digest=checkpoint_digest,
                            counts=counts,
                            observation_checkpoint_format=observation_format,
                            observation_checkpoint_digest=observation_digest,
                            observation_counts=observation_counts,
                        )
                    else:
                        analysis_counts = self._count_analysis(staging, connection)
                        if analysis_counts is None:
                            manifest = RawV2StagingBackupManifest(
                                schema_version=RAW_V2_BACKUP_MANIFEST_SCHEMA_V3,
                                backup_id=_backup_id(
                                    staging_id,
                                    inventory,
                                    counts,
                                    observation_counts,
                                    RAW_V2_BACKUP_MANIFEST_SCHEMA_V3,
                                    metric_counts,
                                ),
                                staging_id=staging_id,
                                created_at=datetime.now(UTC),
                                files=inventory,
                                checkpoint_format=checkpoint_format,
                                checkpoint_digest=checkpoint_digest,
                                counts=counts,
                                observation_checkpoint_format=observation_format,
                                observation_checkpoint_digest=observation_digest,
                                observation_counts=observation_counts,
                                metric_counts=metric_counts,
                            )
                        else:
                            manifest = RawV2StagingBackupManifest(
                                schema_version=RAW_V2_BACKUP_MANIFEST_SCHEMA_V4,
                                backup_id=_backup_id(
                                    staging_id,
                                    inventory,
                                    counts,
                                    observation_counts,
                                    RAW_V2_BACKUP_MANIFEST_SCHEMA_V4,
                                    metric_counts,
                                    analysis_counts,
                                ),
                                staging_id=staging_id,
                                created_at=datetime.now(UTC),
                                files=inventory,
                                checkpoint_format=checkpoint_format,
                                checkpoint_digest=checkpoint_digest,
                                counts=counts,
                                observation_checkpoint_format=observation_format,
                                observation_checkpoint_digest=observation_digest,
                                observation_counts=observation_counts,
                                metric_counts=metric_counts,
                                analysis_counts=analysis_counts,
                            )
                for item in inventory:
                    source_file = staging_root / item.path
                    target_file = temporary / item.path
                    target_file.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(source_file, target_file)
                    if (
                        target_file.stat().st_size != item.size_bytes
                        or _sha256_streaming(target_file) != item.sha256
                    ):
                        raise RawV2BackupError("copied backup file failed verification")
                (temporary / BACKUP_MANIFEST_NAME).write_text(
                    manifest.model_dump_json() + "\n", encoding="utf-8"
                )
                self._verify_backup_directory(temporary, manifest)
                os.replace(temporary, backup_resolved)
                return manifest
            except (RawV2BackupError, ValueError):
                raise
            except OSError as error:
                raise RawV2BackupError("staging backup could not be completed") from error
            finally:
                if temporary.exists():
                    shutil.rmtree(temporary)
        finally:
            if staging_was_open:
                staging.open()

    def restore(self, backup: Path, destination: Path) -> RawV2StagingBackupManifest:
        """Verify then activate a backup only into a new or empty destination."""
        backup_path = backup.expanduser()
        destination_path = destination.expanduser()
        if backup_path.is_symlink() or destination_path.is_symlink():
            raise RawV2BackupError("restore paths must not be symbolic links")
        backup_root = backup_path.resolve()
        destination_root = destination_path.resolve(strict=False)
        if backup_root == destination_root or backup_root in destination_root.parents:
            raise RawV2BackupError("restore destination must be outside the backup")
        if destination_root in backup_root.parents:
            raise RawV2BackupError("restore destination must be outside the backup")
        if destination_root.exists() and any(destination_root.iterdir()):
            raise RawV2BackupError("restore destination must be new or empty")
        _reject_symlinks(backup_root)
        manifest = self._load_manifest(backup_root / BACKUP_MANIFEST_NAME)
        self._verify_backup_directory(backup_root, manifest)
        temporary = destination_root.with_name(f".{destination_root.name}.{uuid4().hex}.tmp")
        try:
            temporary.mkdir(parents=True)
            for item in manifest.files:
                source_file = backup_root / item.path
                target_file = temporary / item.path
                target_file.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source_file, target_file)
            _reject_symlinks(temporary)
            self._verify_backup_directory(temporary, manifest)
            self._verify_restored_staging(temporary, manifest)
            if destination_root.exists():
                destination_root.rmdir()
            os.replace(temporary, destination_root)
            return manifest
        except (RawV2BackupError, ValueError):
            raise
        except OSError as error:
            raise RawV2BackupError("staging restore could not be completed") from error
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)

    def _inventory(self, staging_root: Path) -> tuple[RawV2BackupFile, ...]:
        entries: list[RawV2BackupFile] = []
        for path in sorted(
            (candidate for candidate in staging_root.rglob("*") if candidate.is_file()),
            key=lambda candidate: candidate.relative_to(staging_root).as_posix(),
        ):
            if path.is_symlink():
                raise RawV2BackupError("staging backup paths must not use symbolic links")
            if path.name == BACKUP_MANIFEST_NAME or ".tmp" in path.name:
                raise RawV2BackupError("staging contains unexpected backup artifacts")
            relative = path.relative_to(staging_root).as_posix()
            if relative == "raw-v2-staging.lock":
                continue
            entries.append(
                RawV2BackupFile(
                    path=relative,
                    size_bytes=path.stat().st_size,
                    sha256=_sha256_streaming(path),
                )
            )
        if not entries:
            raise RawV2BackupError("staging backup inventory is empty")
        required = {"raw-v2-staging.json"}
        if not required.issubset({item.path for item in entries}):
            raise RawV2BackupError("staging backup inventory is missing its marker")
        index_names = [item.path for item in entries if item.path.endswith(".duckdb")]
        if len(index_names) != 1:
            raise RawV2BackupError("staging backup must contain exactly one index file")
        return tuple(entries)

    def _count_corpus(
        self, staging: RawV2Staging, connection: DuckDBPyConnection
    ) -> RawV2BackupCounts:
        from investment_analyst.storage.serialization import canonical_json_bytes, sha256_hex

        reader = RawV2Staging(staging.destination, connection, read_only=True)
        reader.open()
        try:
            digest = empty_digest()
            counts_by_source: dict[str, int] = {}
            counts_by_schema: dict[str, int] = {}
            records = 0
            cursor_at = None
            cursor_id = None
            while True:
                page = reader.list_inventory_page(
                    limit=_MAX_BACKUP_PAGE,
                    after_received_at=cursor_at,
                    after_record_id=cursor_id,
                )
                if not page:
                    break
                hydrated = reader.get_many(page)
                for record_id in page:
                    record = hydrated[record_id]
                    digest = extend_digest(digest, sha256_hex(canonical_json_bytes(record)))
                    counts_by_source[record.source.source_id] = (
                        counts_by_source.get(record.source.source_id, 0) + 1
                    )
                    counts_by_schema[record.schema_version] = (
                        counts_by_schema.get(record.schema_version, 0) + 1
                    )
                records += len(page)
                last = hydrated[page[-1]]
                cursor_at, cursor_id = last.received_at, last.record_id
        finally:
            reader.close()
        return RawV2BackupCounts(
            records=records,
            counts_by_source=counts_by_source,
            counts_by_schema=counts_by_schema,
            corpus_digest=digest,
        )

    def _checkpoint_binding(self, staging_root: Path) -> tuple[str | None, str | None]:
        state_path = staging_root / _IMPORT_STATE_FILENAME
        if not state_path.exists():
            return None, None
        if state_path.is_symlink() or not state_path.is_file():
            raise RawV2BackupError("import state must be a regular file")
        try:
            state = RawV2ImportState.model_validate_json(state_path.read_text(encoding="utf-8"))
        except ValueError as error:
            raise RawV2BackupError("import state is truncated or incompatible") from error
        return state.format, _sha256_streaming(state_path)

    def _observation_checkpoint_binding(self, staging_root: Path) -> tuple[str | None, str | None]:
        state_path = staging_root / _OBSERVATION_IMPORT_STATE_FILENAME
        if not state_path.exists():
            return None, None
        if state_path.is_symlink() or not state_path.is_file():
            raise RawV2BackupError("observation import state must be a regular file")
        try:
            state = ObservationV2ImportState.model_validate_json(
                state_path.read_text(encoding="utf-8")
            )
        except ValueError as error:
            raise RawV2BackupError("observation import state is truncated") from error
        return state.format, _sha256_streaming(state_path)

    def _count_observations(
        self, staging: RawV2Staging, connection: DuckDBPyConnection
    ) -> RawV2BackupObservationCounts | None:
        reader = RawV2Staging(staging.destination, connection, read_only=True)
        try:
            reader.open()
        except (ObservationV2Error, Exception) as error:
            if "observation v2 index table is missing" in str(error):
                return None
            raise
        try:
            ensure_observation_v2_table(connection, create=False)
        except ObservationV2Error:
            reader.close()
            return None
        try:
            digest = observation_empty_digest()
            counts_by_source: dict[str, int] = {}
            counts_by_frequency: dict[str, int] = {}
            observations = 0
            cursor_at = None
            cursor_id = None
            while True:
                page = reader.list_observation_inventory_page(
                    limit=_MAX_BACKUP_PAGE,
                    after_available_at=cursor_at,
                    after_observation_id=cursor_id,
                )
                if not page:
                    break
                hydrated = reader.get_observations(page)
                for observation_id in page:
                    observation = hydrated[observation_id]
                    canonical_row = tuple(observation_to_row(observation))
                    digest = observation_extend_digest(
                        digest, _observation_row_digest(canonical_row)
                    )
                    counts_by_source[observation.source.source_id] = (
                        counts_by_source.get(observation.source.source_id, 0) + 1
                    )
                    counts_by_frequency[observation.frequency.value] = (
                        counts_by_frequency.get(observation.frequency.value, 0) + 1
                    )
                    expected = observation_to_row(observation)
                    if observation_to_row(hydrated[observation_id]) != expected:
                        raise RawV2BackupError("observation projection diverged")
                observations += len(page)
                last = hydrated[page[-1]]
                cursor_at, cursor_id = last.available_at, last.observation_id
            if observations == 0:
                try:
                    state_path = staging.destination / _OBSERVATION_IMPORT_STATE_FILENAME
                    has_state = state_path.is_file() and not state_path.is_symlink()
                except OSError:
                    has_state = False
                if not has_state:
                    return None
            return RawV2BackupObservationCounts(
                observations=observations,
                counts_by_source=counts_by_source,
                counts_by_frequency=counts_by_frequency,
                corpus_digest=digest,
            )
        finally:
            reader.close()

    def _count_metrics(
        self, staging: RawV2Staging, connection: DuckDBPyConnection
    ) -> RawV2BackupMetricCounts | None:
        from investment_analyst.storage.evidence_set_v2 import EvidenceSetV2Error
        from investment_analyst.storage.metric_v2 import (
            METRIC_V2_METRIC_LINKS_TABLE,
            METRIC_V2_OBSERVATION_LINKS_TABLE,
            METRIC_V2_TABLE,
            MetricV2Error,
            metric_v2_table_exists,
        )

        reader = RawV2Staging(staging.destination, connection, read_only=True)
        try:
            reader.open()
        except (MetricV2Error, EvidenceSetV2Error, Exception) as error:
            if "metric v2 index table is missing" in str(error):
                return None
            raise
        if not metric_v2_table_exists(connection):
            reader.close()
            return None
        try:
            digest = empty_digest()
            counts_by_key: dict[str, int] = {}
            metrics = 0
            cursor_at = None
            cursor_id = None
            while True:
                page = reader.list_metric_inventory_page(
                    limit=_MAX_BACKUP_PAGE,
                    after_available_at=cursor_at,
                    after_result_id=cursor_id,
                )
                if not page:
                    break
                hydrated = reader.get_metrics(page)
                for result_id in page:
                    result = hydrated[result_id]
                    canonical = (
                        f"{result.result_id}|{result.asset_id}|{result.metric_key}|"
                        f"{result.value}|{result.unit}|{result.as_of.isoformat()}|"
                        f"{result.available_at.isoformat()}|{result.algorithm_version}|"
                        f"{result.quality.value}"
                    )
                    digest = extend_digest(digest, hashlib.sha256(canonical.encode()).hexdigest())
                    counts_by_key[result.metric_key] = counts_by_key.get(result.metric_key, 0) + 1
                metrics += len(page)
                last = hydrated[page[-1]]
                cursor_at, cursor_id = last.available_at, last.result_id
            if metrics == 0:
                return None
            segments = connection.execute("SELECT count(*) FROM evidence_segments_v2").fetchone()
            sets = connection.execute("SELECT count(*) FROM evidence_sets_v2").fetchone()
            lineage_rows = connection.execute(
                f"SELECT evidence_set_id, result_id FROM {METRIC_V2_TABLE} "
                "WHERE evidence_set_id IS NOT NULL ORDER BY evidence_set_id"
            ).fetchall()
            lineage_digest = empty_digest()
            for evidence_set_id, result_id in lineage_rows:
                lineage_digest = extend_digest(
                    lineage_digest,
                    hashlib.sha256(f"{evidence_set_id}:{result_id}".encode()).hexdigest(),
                )
            link_obs = connection.execute(
                f"SELECT count(*) FROM {METRIC_V2_OBSERVATION_LINKS_TABLE}"
            ).fetchone()
            link_met = connection.execute(
                f"SELECT count(*) FROM {METRIC_V2_METRIC_LINKS_TABLE}"
            ).fetchone()
            if (link_obs is not None and int(link_obs[0]) == 0) or (
                link_met is not None and metrics > 0 and int(link_met[0]) == 0
            ):
                pass
            return RawV2BackupMetricCounts(
                metrics=metrics,
                counts_by_metric_key=counts_by_key,
                corpus_digest=digest,
                evidence_sets=int(sets[0]) if sets else 0,
                evidence_segments=int(segments[0]) if segments else 0,
                lineage_digest=lineage_digest,
            )
        except (MetricV2Error, EvidenceSetV2Error):
            raise
        except Exception as error:
            if "metric v2 index table is missing" in str(error) or "does not exist" in str(error):
                return None
            raise
        finally:
            reader.close()

    def _count_analysis(
        self, staging: RawV2Staging, connection: DuckDBPyConnection
    ) -> RawV2BackupAnalysisCounts | None:
        from investment_analyst.storage.analysis_snapshot_v2 import (
            ANALYSIS_SNAPSHOT_V2_TABLE,
            AnalysisSnapshotV2Error,
            AnalysisSnapshotV2Store,
            analysis_snapshot_v2_tables_exist,
        )
        from investment_analyst.storage.diagnostic_v2 import (
            DIAGNOSTIC_V2_TABLE,
            DiagnosticV2Error,
            DiagnosticV2Store,
            diagnostic_v2_tables_exist,
        )

        del staging
        if not diagnostic_v2_tables_exist(connection) or not analysis_snapshot_v2_tables_exist(
            connection
        ):
            return None

        try:
            diag_store = DiagnosticV2Store(connection)
            snap_store = AnalysisSnapshotV2Store(connection)

            diag_digest = empty_digest()
            counts_by_mode: dict[str, int] = {}
            total_diags = 0
            cursor_at: str | None = None
            cursor_id: str | None = None

            while True:
                clauses: list[str] = []
                params: list[object] = []
                if cursor_at is not None and cursor_id is not None:
                    clauses.append("(available_at, diagnostic_id) > (?, ?)")
                    params.extend([cursor_at, cursor_id])
                where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
                rows = connection.execute(
                    f"SELECT diagnostic_id, available_at FROM {DIAGNOSTIC_V2_TABLE}{where} "
                    f"ORDER BY available_at, diagnostic_id LIMIT {_MAX_BACKUP_PAGE}",
                    params,
                ).fetchall()
                if not rows:
                    break
                page_ids = [UUID(str(r[0])) for r in rows]
                hydrated = diag_store.get_diagnostics(page_ids)
                for did in page_ids:
                    diag = hydrated[did]
                    mode_str = diag.mode.value if hasattr(diag.mode, "value") else str(diag.mode)
                    verdict_str = (
                        diag.verdict.value if hasattr(diag.verdict, "value") else str(diag.verdict)
                    )
                    qual_str = (
                        diag.quality.value if hasattr(diag.quality, "value") else str(diag.quality)
                    )
                    canonical = (
                        f"{diag.diagnostic_id}|{diag.asset_id}|{mode_str}|{verdict_str}|"
                        f"{diag.final_score}|{diag.confidence}|{diag.as_of.isoformat()}|"
                        f"{diag.available_at.isoformat()}|{diag.computed_at.isoformat()}|"
                        f"{diag.algorithm_version}|{qual_str}|{len(diag.components)}|{len(diag.evidence)}"
                    )
                    diag_digest = extend_digest(
                        diag_digest, hashlib.sha256(canonical.encode("utf-8")).hexdigest()
                    )
                    counts_by_mode[mode_str] = counts_by_mode.get(mode_str, 0) + 1
                total_diags += len(rows)
                cursor_at = str(rows[-1][1])
                cursor_id = str(rows[-1][0])

            snap_digest = empty_digest()
            counts_by_domain: dict[str, int] = {}
            total_snaps = 0
            cursor_at = None
            cursor_id = None

            while True:
                clauses = []
                params = []
                if cursor_at is not None and cursor_id is not None:
                    clauses.append("(known_at, snapshot_id) > (?, ?)")
                    params.extend([cursor_at, cursor_id])
                where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
                rows = connection.execute(
                    f"SELECT snapshot_id, known_at FROM {ANALYSIS_SNAPSHOT_V2_TABLE}{where} "
                    f"ORDER BY known_at, snapshot_id LIMIT {_MAX_BACKUP_PAGE}",
                    params,
                ).fetchall()
                if not rows:
                    break
                page_ids = [UUID(str(r[0])) for r in rows]
                hydrated = snap_store.get_snapshots(page_ids)
                for sid in page_ids:
                    snap = hydrated[sid]
                    canonical = (
                        f"{snap.snapshot_id}|{snap.asset_id}|{snap.domain}|{snap.known_at.isoformat()}|"
                        f"{snap.policy_version}|{snap.evidence_set_digest}|{len(snap.metric_ids)}|{len(snap.diagnostic_ids)}"
                    )
                    snap_digest = extend_digest(
                        snap_digest, hashlib.sha256(canonical.encode("utf-8")).hexdigest()
                    )
                    counts_by_domain[snap.domain] = counts_by_domain.get(snap.domain, 0) + 1
                total_snaps += len(rows)
                cursor_at = str(rows[-1][1])
                cursor_id = str(rows[-1][0])

            if total_diags == 0 and total_snaps == 0:
                return None

            return RawV2BackupAnalysisCounts(
                diagnostics=total_diags,
                counts_by_mode=counts_by_mode,
                diagnostic_digest=diag_digest,
                snapshots=total_snaps,
                counts_by_domain=counts_by_domain,
                snapshot_digest=snap_digest,
            )
        except (DiagnosticV2Error, AnalysisSnapshotV2Error):
            raise
        except Exception as error:
            if "table is missing" in str(error) or "does not exist" in str(error):
                return None
            raise

    def _verify_backup_directory(self, root: Path, manifest: RawV2StagingBackupManifest) -> None:
        _reject_symlinks(root)
        expected = {item.path: item for item in manifest.files}
        actual = {
            path.relative_to(root).as_posix(): path
            for path in root.rglob("*")
            if path.is_file() and path.name != BACKUP_MANIFEST_NAME
        }
        if set(actual) != set(expected):
            raise RawV2BackupError("backup file inventory does not match manifest")
        for relative, item in expected.items():
            path = actual[relative]
            if path.stat().st_size != item.size_bytes or _sha256_streaming(path) != item.sha256:
                raise RawV2BackupError("backup file hash verification failed")

    def _load_manifest(self, path: Path) -> RawV2StagingBackupManifest:
        if path.is_symlink() or not path.is_file():
            raise RawV2BackupError("backup manifest is missing or not a regular file")
        try:
            text = path.read_text(encoding="utf-8")
            if ".." in text or text.strip().startswith("/"):
                raise RawV2BackupError("backup manifest is incompatible")
            return RawV2StagingBackupManifest.model_validate_json(text)
        except ValueError as error:
            raise RawV2BackupError("backup manifest is truncated or incompatible") from error

    def _verify_restored_staging(self, root: Path, manifest: RawV2StagingBackupManifest) -> None:
        import duckdb

        marker_path = root / "raw-v2-staging.json"
        try:
            marker = RawV2StagingMarker.model_validate_json(marker_path.read_text(encoding="utf-8"))
        except ValueError as error:
            raise RawV2BackupError("restored staging marker is incompatible") from error
        if marker.format != STAGING_FORMAT or marker.staging_id != manifest.staging_id:
            raise RawV2BackupError("restored staging identity does not match backup")
        index_names = [item.path for item in manifest.files if item.path.endswith(".duckdb")]
        if len(index_names) != 1:
            raise RawV2BackupError("backup manifest must reference exactly one index file")
        index_path = root / index_names[0]
        if index_path.is_symlink() or not index_path.is_file():
            raise RawV2BackupError("restored staging index is missing")
        connection = duckdb.connect(str(index_path), read_only=True)
        try:
            rows = connection.execute(
                "SELECT record_id, relative_path, checksum_sha256, asset_id, source_id, "
                "event_time, available_at, received_at, schema_version, "
                "projected_manager_cik, projected_report_id "
                "FROM raw_v2_index ORDER BY received_at, record_id LIMIT 1"
            ).fetchall()
            del rows
        except Exception as error:
            raise RawV2BackupError("restored staging index is incompatible") from error
        finally:
            connection.close()
        state_path = root / _IMPORT_STATE_FILENAME
        if state_path.exists():
            if state_path.is_symlink() or not state_path.is_file():
                raise RawV2BackupError("restored import state is not a regular file")
            try:
                state = RawV2ImportState.model_validate_json(state_path.read_text(encoding="utf-8"))
            except ValueError as error:
                raise RawV2BackupError("restored import state is incompatible") from error
            if manifest.checkpoint_format != state.format:
                raise RawV2BackupError("restored checkpoint does not match backup")
            if manifest.checkpoint_digest != _sha256_streaming(state_path):
                raise RawV2BackupError("restored checkpoint does not match backup")
            if state.staging_id is not None and state.staging_id != manifest.staging_id:
                raise RawV2BackupError("restored checkpoint belongs to another staging")
            if state.format == "raw-v2-import-state-v1" and state.staging_id is not None:
                raise RawV2BackupError("restored checkpoint mixes portable and legacy bindings")
        observation_state_path = root / _OBSERVATION_IMPORT_STATE_FILENAME
        if manifest.schema_version == RAW_V2_BACKUP_MANIFEST_SCHEMA_V4:
            if (
                manifest.observation_counts is None
                or manifest.metric_counts is None
                or manifest.analysis_counts is None
            ):
                raise RawV2BackupError("restored v4 manifest is missing analysis counts")
            if not observation_state_path.is_file() or observation_state_path.is_symlink():
                if manifest.observation_checkpoint_format is not None:
                    raise RawV2BackupError("restored observation checkpoint is missing")
            else:
                try:
                    observation_state = ObservationV2ImportState.model_validate_json(
                        observation_state_path.read_text(encoding="utf-8")
                    )
                except ValueError as error:
                    raise RawV2BackupError("restored observation state is incompatible") from error
                if manifest.observation_checkpoint_format != observation_state.format:
                    raise RawV2BackupError("restored observation checkpoint mismatches backup")
                if manifest.observation_checkpoint_digest != _sha256_streaming(
                    observation_state_path
                ):
                    raise RawV2BackupError("restored observation checkpoint mismatches backup")
            self._verify_restored_observations(root, index_path, manifest)
            self._verify_restored_metrics(root, index_path, manifest)
            self._verify_restored_analysis(root, index_path, manifest)
        elif manifest.schema_version == RAW_V2_BACKUP_MANIFEST_SCHEMA_V3:
            if manifest.observation_counts is None or manifest.metric_counts is None:
                raise RawV2BackupError("restored v3 manifest is missing metric counts")
            if not observation_state_path.is_file() or observation_state_path.is_symlink():
                if manifest.observation_checkpoint_format is not None:
                    raise RawV2BackupError("restored observation checkpoint is missing")
            else:
                try:
                    observation_state = ObservationV2ImportState.model_validate_json(
                        observation_state_path.read_text(encoding="utf-8")
                    )
                except ValueError as error:
                    raise RawV2BackupError("restored observation state is incompatible") from error
                if manifest.observation_checkpoint_format != observation_state.format:
                    raise RawV2BackupError("restored observation checkpoint mismatches backup")
                if manifest.observation_checkpoint_digest != _sha256_streaming(
                    observation_state_path
                ):
                    raise RawV2BackupError("restored observation checkpoint mismatches backup")
            self._verify_restored_observations(root, index_path, manifest)
            self._verify_restored_metrics(root, index_path, manifest)
        elif manifest.schema_version == RAW_V2_BACKUP_MANIFEST_SCHEMA_V2:
            if manifest.observation_counts is None:
                raise RawV2BackupError("restored v2 manifest is missing observation counts")
            if not observation_state_path.is_file() or observation_state_path.is_symlink():
                if manifest.observation_checkpoint_format is not None:
                    raise RawV2BackupError("restored observation checkpoint is missing")
            else:
                try:
                    observation_state = ObservationV2ImportState.model_validate_json(
                        observation_state_path.read_text(encoding="utf-8")
                    )
                except ValueError as error:
                    raise RawV2BackupError("restored observation state is incompatible") from error
                if manifest.observation_checkpoint_format != observation_state.format:
                    raise RawV2BackupError("restored observation checkpoint mismatches backup")
                if manifest.observation_checkpoint_digest != _sha256_streaming(
                    observation_state_path
                ):
                    raise RawV2BackupError("restored observation checkpoint mismatches backup")
            self._verify_restored_observations(root, index_path, manifest)
        elif observation_state_path.exists():
            raise RawV2BackupError("restored v1 backup must not carry observation state")

    def _verify_restored_observations(
        self, root: Path, index_path: Path, manifest: RawV2StagingBackupManifest
    ) -> None:
        import duckdb

        expected = manifest.observation_counts
        if expected is None:
            raise RawV2BackupError("restored v2 manifest is missing observation counts")
        connection = duckdb.connect(str(index_path), read_only=True)
        try:
            try:
                names = {
                    str(row[0])
                    for row in connection.execute(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_name = 'normalized_observations_v2'"
                    ).fetchall()
                }
            except Exception as error:
                raise RawV2BackupError("restored observation table is missing") from error
            if not names or "document_json" in names:
                raise RawV2BackupError("restored observation table is incompatible")
            digest = observation_empty_digest()
            counts_by_source: dict[str, int] = {}
            counts_by_frequency: dict[str, int] = {}
            verified = 0
            cursor_at = None
            cursor_id = None
            from uuid import UUID as _UUID

            while True:
                clauses: list[str] = []
                parameters: list[object] = []
                if cursor_at is not None and cursor_id is not None:
                    clauses.append("(available_at, observation_id) > (?, ?)")
                    parameters.extend([cursor_at, str(cursor_id)])
                where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
                rows = connection.execute(
                    "SELECT observation_id, raw_record_id, source_id, available_at "
                    "FROM normalized_observations_v2"
                    f"{where} ORDER BY available_at, observation_id LIMIT {_MAX_BACKUP_PAGE}",
                    parameters,
                ).fetchall()
                if not rows:
                    break
                full = connection.execute(
                    "SELECT observation_id, raw_record_id, asset_id, field_name, value_text, "
                    "unit, frequency, observed_at, period_start, period_end, available_at, "
                    "normalized_at, source_id, source_record_key, source_retrieved_at, "
                    "source_raw_uri, source_checksum_sha256, quality, transformation_version "
                    "FROM normalized_observations_v2 WHERE observation_id IN ("
                    + ", ".join("?" for _ in rows)
                    + ")",
                    [str(row[0]) for row in rows],
                ).fetchall()
                indexed = {str(row[0]): row for row in full}
                ordered_ids = [str(row[0]) for row in rows]
                if sorted(indexed) != sorted(ordered_ids):
                    raise RawV2BackupError("restored observation page is incomplete")
                for key in ordered_ids:
                    row = indexed[key]
                    try:
                        row_to_observation(tuple(row))
                    except ObservationV2Error as error:
                        raise RawV2BackupError("restored observation row is corrupt") from error
                    raw_rows = connection.execute(
                        "SELECT source_id FROM raw_v2_index WHERE record_id = ?",
                        [str(row[1])],
                    ).fetchall()
                    if not raw_rows or str(raw_rows[0][0]) != str(row[12]):
                        raise RawV2BackupError("restored observation raw link is foreign")
                    digest = observation_extend_digest(digest, _observation_row_digest(tuple(row)))
                    counts_by_source[str(row[12])] = counts_by_source.get(str(row[12]), 0) + 1
                    counts_by_frequency[str(row[6])] = counts_by_frequency.get(str(row[6]), 0) + 1
                verified += len(rows)
                cursor_at, cursor_id = str(rows[-1][3]), _UUID(str(rows[-1][0]))
            if verified != expected.observations:
                raise RawV2BackupError("restored observation count mismatches manifest")
            if dict(counts_by_source) != dict(expected.counts_by_source):
                raise RawV2BackupError("restored observation sources mismatch manifest")
            if dict(counts_by_frequency) != dict(expected.counts_by_frequency):
                raise RawV2BackupError("restored observation frequencies mismatch manifest")
            if digest != expected.corpus_digest:
                raise RawV2BackupError("restored observation digest mismatches manifest")
        finally:
            connection.close()

    def _verify_restored_metrics(
        self, root: Path, index_path: Path, manifest: RawV2StagingBackupManifest
    ) -> None:
        from uuid import UUID as _UUID

        import duckdb

        from investment_analyst.storage.evidence_set_v2 import (
            EvidenceSetV2Error,
            row_to_evidence_set,
            row_to_segment,
        )
        from investment_analyst.storage.metric_v2 import MetricV2Error, row_to_metric

        del root
        expected = manifest.metric_counts
        if expected is None:
            raise RawV2BackupError("restored v3 manifest is missing metric counts")
        connection = duckdb.connect(str(index_path), read_only=True)
        try:
            try:
                names = {
                    str(row[0])
                    for row in connection.execute(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_name = 'metric_results_v2'"
                    ).fetchall()
                }
            except Exception as error:
                raise RawV2BackupError("restored metric table is missing") from error
            if not names or "document_json" in names:
                raise RawV2BackupError("restored metric table is incompatible")
            digest = empty_digest()
            counts_by_key: dict[str, int] = {}
            verified = 0
            cursor_at = None
            cursor_id = None
            while True:
                clauses: list[str] = []
                parameters: list[object] = []
                if cursor_at is not None and cursor_id is not None:
                    clauses.append("(available_at, result_id) > (?, ?)")
                    parameters.extend([cursor_at, str(cursor_id)])
                where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
                rows = connection.execute(
                    "SELECT result_id, asset_id, metric_key, value_text, unit, as_of, "
                    "available_at, computed_at, parameters_json, evidence_set_id, "
                    "algorithm_version, quality FROM metric_results_v2"
                    f"{where} ORDER BY available_at, result_id LIMIT {_MAX_BACKUP_PAGE}",
                    parameters,
                ).fetchall()
                if not rows:
                    break
                for row in rows:
                    result_id = _UUID(str(row[0]))
                    obs_link_rows = connection.execute(
                        "SELECT position, observation_id FROM metric_v2_observation_links "
                        "WHERE result_id = ? ORDER BY position",
                        [str(result_id)],
                    ).fetchall()
                    obs_positions = [int(r[0]) for r in obs_link_rows]
                    if obs_positions != list(range(len(obs_positions))):
                        raise RawV2BackupError(
                            "restored metric observation links have non-contiguous positions"
                        )
                    obs_links = [r[1] for r in obs_link_rows]

                    met_link_rows = connection.execute(
                        "SELECT position, input_result_id FROM metric_v2_metric_links "
                        "WHERE result_id = ? ORDER BY position",
                        [str(result_id)],
                    ).fetchall()
                    met_positions = [int(r[0]) for r in met_link_rows]
                    if met_positions != list(range(len(met_positions))):
                        raise RawV2BackupError(
                            "restored metric dependency links have non-contiguous positions"
                        )
                    met_links = [r[1] for r in met_link_rows]

                    try:
                        result = row_to_metric(
                            tuple(row),
                            observation_ids=[_UUID(str(item)) for item in obs_links],
                            metric_ids=[_UUID(str(item)) for item in met_links],
                        )
                    except MetricV2Error as error:
                        raise RawV2BackupError("restored metric row is corrupt") from error

                    from investment_analyst.storage.metric_v2 import (
                        recalculate_metric_result_id,
                    )

                    if recalculate_metric_result_id(result) != result.result_id:
                        raise RawV2BackupError(
                            "restored metric identity does not match its semantic preimage"
                        )

                    for observation_id in result.input_observation_ids:
                        obs_rows = connection.execute(
                            "SELECT asset_id, available_at FROM normalized_observations_v2 "
                            "WHERE observation_id = ?",
                            [str(observation_id)],
                        ).fetchall()
                        if not obs_rows:
                            raise RawV2BackupError("restored metric link is missing observation")
                        if str(obs_rows[0][0]) != result.asset_id:
                            raise RawV2BackupError("restored metric link is foreign")
                        obs_available = datetime.fromisoformat(str(obs_rows[0][1]))
                        if obs_available.tzinfo is None:
                            raise RawV2BackupError("restored metric link is future")
                        if obs_available.astimezone(UTC) > result.available_at:
                            raise RawV2BackupError("restored metric link is future")
                    for input_id in result.input_metric_result_ids:
                        met_rows = connection.execute(
                            "SELECT asset_id FROM metric_results_v2 WHERE result_id = ?",
                            [str(input_id)],
                        ).fetchall()
                        if not met_rows or str(met_rows[0][0]) != result.asset_id:
                            raise RawV2BackupError("restored metric dependency is foreign")
                    reference = result.parameters.get("evidence_set_id")
                    if reference is not None:
                        set_rows = connection.execute(
                            "SELECT evidence_set_id, asset_id, source_id, field_name, "
                            "input_count, head_offset, inline_observation_ids_json, "
                            "inline_available_at, first_observed_at, first_observation_id, "
                            "last_observed_at, last_observation_id, available_at, "
                            "canonical_hash FROM evidence_sets_v2 WHERE evidence_set_id = ?",
                            [str(reference)],
                        ).fetchall()
                        if not set_rows:
                            raise RawV2BackupError("restored metric lineage is missing")
                        member_rows = connection.execute(
                            "SELECT position, segment_id FROM evidence_set_v2_members "
                            "WHERE evidence_set_id = ? ORDER BY position",
                            [str(reference)],
                        ).fetchall()
                        mem_positions = [int(r[0]) for r in member_rows]
                        if mem_positions != list(range(len(mem_positions))):
                            raise RawV2BackupError(
                                "restored evidence set members have non-contiguous positions"
                            )
                        members = [r[1] for r in member_rows]
                        try:
                            stored_set = row_to_evidence_set(
                                tuple(set_rows[0]),
                                segment_ids=[_UUID(str(item)) for item in members],
                            )
                        except EvidenceSetV2Error as error:
                            raise RawV2BackupError("restored metric lineage is corrupt") from error
                        for segment_id in stored_set.segment_ids:
                            seg_rows = connection.execute(
                                "SELECT segment_id, asset_id, source_id, field_name, day, "
                                "observation_ids_json, available_at, canonical_hash "
                                "FROM evidence_segments_v2 WHERE segment_id = ?",
                                [str(segment_id)],
                            ).fetchall()
                            if not seg_rows:
                                raise RawV2BackupError("restored lineage segment is missing")
                            try:
                                row_to_segment(tuple(seg_rows[0]))
                            except EvidenceSetV2Error as error:
                                raise RawV2BackupError(
                                    "restored lineage segment is corrupt"
                                ) from error
                        if stored_set.available_at > result.available_at:
                            raise RawV2BackupError("restored metric lineage is future")
                    canonical = (
                        f"{result.result_id}|{result.asset_id}|{result.metric_key}|"
                        f"{result.value}|{result.unit}|{result.as_of.isoformat()}|"
                        f"{result.available_at.isoformat()}|{result.algorithm_version}|"
                        f"{result.quality.value}"
                    )
                    digest = extend_digest(
                        digest, hashlib.sha256(canonical.encode("utf-8")).hexdigest()
                    )
                    counts_by_key[result.metric_key] = counts_by_key.get(result.metric_key, 0) + 1
                verified += len(rows)
                cursor_at, cursor_id = str(rows[-1][6]), _UUID(str(rows[-1][0]))
            if verified != expected.metrics:
                raise RawV2BackupError("restored metric count mismatches manifest")
            if dict(counts_by_key) != dict(expected.counts_by_metric_key):
                raise RawV2BackupError("restored metric keys mismatch manifest")
            if digest != expected.corpus_digest:
                raise RawV2BackupError("restored metric digest mismatches manifest")
            segments = connection.execute("SELECT count(*) FROM evidence_segments_v2").fetchone()
            sets = connection.execute("SELECT count(*) FROM evidence_sets_v2").fetchone()
            if int(segments[0]) != expected.evidence_segments:
                raise RawV2BackupError("restored lineage segments mismatch manifest")
            if int(sets[0]) != expected.evidence_sets:
                raise RawV2BackupError("restored lineage sets mismatch manifest")
        finally:
            connection.close()

    def _verify_restored_analysis(
        self, root: Path, index_path: Path, manifest: RawV2StagingBackupManifest
    ) -> None:
        import duckdb

        from investment_analyst.storage.analysis_snapshot_v2 import (
            ANALYSIS_SNAPSHOT_V2_TABLE,
            AnalysisSnapshotV2Store,
            analysis_snapshot_v2_tables_exist,
        )
        from investment_analyst.storage.diagnostic_v2 import (
            DIAGNOSTIC_V2_TABLE,
            DiagnosticV2Store,
            diagnostic_v2_tables_exist,
        )

        del root
        expected = manifest.analysis_counts
        if expected is None:
            raise RawV2BackupError("restored v4 manifest is missing analysis counts")

        connection = duckdb.connect(str(index_path), read_only=True)
        try:
            if not diagnostic_v2_tables_exist(connection) or not analysis_snapshot_v2_tables_exist(
                connection
            ):
                raise RawV2BackupError("restored diagnostic or snapshot tables are missing")

            diag_store = DiagnosticV2Store(connection)
            snap_store = AnalysisSnapshotV2Store(connection)

            # 1. Verify diagnostics
            diag_digest = empty_digest()
            counts_by_mode: dict[str, int] = {}
            verified_diags = 0
            cursor_at: str | None = None
            cursor_id: str | None = None

            while True:
                clauses: list[str] = []
                params: list[object] = []
                if cursor_at is not None and cursor_id is not None:
                    clauses.append("(available_at, diagnostic_id) > (?, ?)")
                    params.extend([cursor_at, cursor_id])
                where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
                rows = connection.execute(
                    f"SELECT diagnostic_id, available_at FROM {DIAGNOSTIC_V2_TABLE}{where} "
                    f"ORDER BY available_at, diagnostic_id LIMIT {_MAX_BACKUP_PAGE}",
                    params,
                ).fetchall()
                if not rows:
                    break
                page_ids = [UUID(str(r[0])) for r in rows]
                try:
                    hydrated = diag_store.get_diagnostics(page_ids)
                except Exception as error:
                    raise RawV2BackupError(
                        f"restored diagnostic verification failed: {error}"
                    ) from error

                for did in page_ids:
                    diag = hydrated[did]
                    mode_str = diag.mode.value if hasattr(diag.mode, "value") else str(diag.mode)
                    verdict_str = (
                        diag.verdict.value if hasattr(diag.verdict, "value") else str(diag.verdict)
                    )
                    qual_str = (
                        diag.quality.value if hasattr(diag.quality, "value") else str(diag.quality)
                    )
                    canonical = (
                        f"{diag.diagnostic_id}|{diag.asset_id}|{mode_str}|{verdict_str}|"
                        f"{diag.final_score}|{diag.confidence}|{diag.as_of.isoformat()}|"
                        f"{diag.available_at.isoformat()}|{diag.computed_at.isoformat()}|"
                        f"{diag.algorithm_version}|{qual_str}|{len(diag.components)}|{len(diag.evidence)}"
                    )
                    diag_digest = extend_digest(
                        diag_digest, hashlib.sha256(canonical.encode("utf-8")).hexdigest()
                    )
                    counts_by_mode[mode_str] = counts_by_mode.get(mode_str, 0) + 1
                verified_diags += len(rows)
                cursor_at = str(rows[-1][1])
                cursor_id = str(rows[-1][0])

            if verified_diags != expected.diagnostics:
                raise RawV2BackupError("restored diagnostic count mismatches manifest")
            if dict(counts_by_mode) != dict(expected.counts_by_mode):
                raise RawV2BackupError("restored diagnostic modes mismatch manifest")
            if diag_digest != expected.diagnostic_digest:
                raise RawV2BackupError("restored diagnostic digest mismatches manifest")

            # 2. Verify snapshots
            snap_digest = empty_digest()
            counts_by_domain: dict[str, int] = {}
            verified_snaps = 0
            cursor_at = None
            cursor_id = None

            while True:
                clauses = []
                params = []
                if cursor_at is not None and cursor_id is not None:
                    clauses.append("(known_at, snapshot_id) > (?, ?)")
                    params.extend([cursor_at, cursor_id])
                where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
                rows = connection.execute(
                    f"SELECT snapshot_id, known_at FROM {ANALYSIS_SNAPSHOT_V2_TABLE}{where} "
                    f"ORDER BY known_at, snapshot_id LIMIT {_MAX_BACKUP_PAGE}",
                    params,
                ).fetchall()
                if not rows:
                    break
                page_ids = [UUID(str(r[0])) for r in rows]
                try:
                    hydrated = snap_store.get_snapshots(page_ids)
                except Exception as error:
                    raise RawV2BackupError(
                        f"restored snapshot verification failed: {error}"
                    ) from error
                for sid in page_ids:
                    snap = hydrated[sid]
                    canonical = (
                        f"{snap.snapshot_id}|{snap.asset_id}|{snap.domain}|{snap.known_at.isoformat()}|"
                        f"{snap.policy_version}|{snap.evidence_set_digest}|{len(snap.metric_ids)}|{len(snap.diagnostic_ids)}"
                    )
                    snap_digest = extend_digest(
                        snap_digest, hashlib.sha256(canonical.encode("utf-8")).hexdigest()
                    )
                    counts_by_domain[snap.domain] = counts_by_domain.get(snap.domain, 0) + 1
                verified_snaps += len(rows)
                cursor_at = str(rows[-1][1])
                cursor_id = str(rows[-1][0])

            if verified_snaps != expected.snapshots:
                raise RawV2BackupError("restored snapshot count mismatches manifest")
            if dict(counts_by_domain) != dict(expected.counts_by_domain):
                raise RawV2BackupError("restored snapshot domains mismatch manifest")
            if snap_digest != expected.snapshot_digest:
                raise RawV2BackupError("restored snapshot digest mismatches manifest")
        finally:
            connection.close()


__all__ = [
    "BACKUP_MANIFEST_NAME",
    "RAW_V2_BACKUP_MANIFEST_SCHEMA",
    "RAW_V2_BACKUP_MANIFEST_SCHEMA_V2",
    "RAW_V2_BACKUP_MANIFEST_SCHEMA_V3",
    "RAW_V2_BACKUP_MANIFEST_SCHEMA_V4",
    "RawV2BackupAnalysisCounts",
    "RawV2BackupCounts",
    "RawV2BackupError",
    "RawV2BackupMetricCounts",
    "RawV2StagingBackupManifest",
    "RawV2StagingBackupService",
]
