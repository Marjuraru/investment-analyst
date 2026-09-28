"""Verified backup and restore of an isolated raw v2 staging destination.

A staging backup is an ordered inventory of regular files with sizes and
SHA-256 digests, bound to the stable ``staging_id`` of the source staging, the
checkpoint version/digest when an import state exists, and the verified corpus
counts and ordered digest. Only a file-backed DuckDB index that lives inside
the staging destination and is consistent with the supplied writer connection
is eligible; memory or external index paths fail closed. Creation and restore
publish atomically into an empty destination after verifying every file; a
truncated manifest, a missing or corrupt file, a foreign identity or a
non-empty destination never promotes a partial restore.
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
BACKUP_MANIFEST_NAME = "raw-v2-staging-backup-manifest.json"
_IMPORT_STATE_FILENAME = "raw-v2-import-state.json"
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


class RawV2StagingBackupManifest(ContractModel):
    """Versioned inventory used to verify a staging backup before activation."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["raw-v2-staging-backup-manifest-v1"] = RAW_V2_BACKUP_MANIFEST_SCHEMA
    backup_id: UUID
    staging_id: NonEmptyStr
    staging_format: Literal["raw-v2-staging-v1"] = STAGING_FORMAT
    created_at: UTCDateTime
    files: tuple[RawV2BackupFile, ...]
    checkpoint_format: str | None = None
    checkpoint_digest: str | None = None
    counts: RawV2BackupCounts

    @model_validator(mode="after")
    def validate_inventory(self) -> RawV2StagingBackupManifest:
        paths = tuple(item.path for item in self.files)
        if not paths or paths != tuple(sorted(paths)) or len(paths) != len(set(paths)):
            raise ValueError("backup inventory must be non-empty, unique, and sorted")
        if BACKUP_MANIFEST_NAME in paths:
            raise ValueError("backup inventory must not contain its own manifest")
        expected_id = _backup_id(self.staging_id, self.files, self.counts)
        if self.backup_id != expected_id:
            raise ValueError("backup identity does not match its inventory")
        if (self.checkpoint_format is None) != (self.checkpoint_digest is None):
            raise ValueError("checkpoint version and digest travel together")
        return self

    def to_json_dict(self) -> dict[str, object]:
        return self.model_dump(mode="json")


def _backup_id(
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
                manifest = RawV2StagingBackupManifest(
                    backup_id=_backup_id(staging_id, inventory, counts),
                    staging_id=staging_id,
                    created_at=datetime.now(UTC),
                    files=inventory,
                    checkpoint_format=checkpoint_format,
                    checkpoint_digest=checkpoint_digest,
                    counts=counts,
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


__all__ = [
    "BACKUP_MANIFEST_NAME",
    "RAW_V2_BACKUP_MANIFEST_SCHEMA",
    "RawV2BackupCounts",
    "RawV2BackupError",
    "RawV2StagingBackupManifest",
    "RawV2StagingBackupService",
]
