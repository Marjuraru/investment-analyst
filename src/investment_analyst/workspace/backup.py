"""Verified local workspace backup and restore without format migration."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import shutil
import threading
from collections.abc import Callable
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Literal
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from pydantic import ConfigDict, Field, field_validator, model_validator

from investment_analyst.core.models.base import ContractModel, NonEmptyStr, UTCDateTime
from investment_analyst.storage import StorageError
from investment_analyst.workspace.models import (
    WORKSPACE_FORMAT_VERSION,
    WorkspaceAccessMode,
    WorkspaceInspection,
)
from investment_analyst.workspace.service import WorkspaceError, WorkspaceService

BACKUP_MANIFEST_NAME = "backup_manifest.json"


class WorkspaceBackupError(WorkspaceError):
    """Raised when a backup or restore cannot be proven complete."""


class WorkspaceBackupFile(ContractModel):
    """One exact regular file included in a workspace backup."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    path: NonEmptyStr
    size_bytes: int = Field(ge=0)
    sha256: NonEmptyStr

    @field_validator("path")
    @classmethod
    def require_safe_relative_path(cls, value: str) -> str:
        path = PurePosixPath(value)
        if path.is_absolute() or ".." in path.parts or value != path.as_posix():
            raise ValueError("backup file path must be normalized and relative")
        if value == BACKUP_MANIFEST_NAME:
            raise ValueError("backup manifest cannot inventory itself")
        return value

    @field_validator("sha256")
    @classmethod
    def require_sha256(cls, value: str) -> str:
        if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
            raise ValueError("backup file sha256 is invalid")
        return value


class WorkspaceBackupCounts(ContractModel):
    """Analytical row counts verified before backup and after restore."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    raw_records: int = Field(ge=0)
    observations: int = Field(ge=0)
    metric_results: int = Field(ge=0)
    diagnostic_results: int = Field(ge=0)


class WorkspaceBackupManifest(ContractModel):
    """Versioned inventory used to verify a backup before activation."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["workspace-backup-manifest-v1"] = "workspace-backup-manifest-v1"
    backup_id: UUID
    source_workspace_id: UUID
    workspace_format_version: Literal[1] = WORKSPACE_FORMAT_VERSION
    created_at: UTCDateTime
    files: tuple[WorkspaceBackupFile, ...]
    counts: WorkspaceBackupCounts
    traceability_verified: Literal[True] = True

    @model_validator(mode="after")
    def validate_inventory(self) -> WorkspaceBackupManifest:
        paths = tuple(item.path for item in self.files)
        if not paths or paths != tuple(sorted(paths)) or len(paths) != len(set(paths)):
            raise ValueError("backup inventory must be non-empty, unique, and sorted")
        required = {
            "manifest.json",
            "storage/data/processed/investment_analyst.duckdb",
        }
        if not required.issubset(paths):
            raise ValueError("backup inventory is missing required workspace files")
        expected_id = _backup_id(self.source_workspace_id, self.files, self.counts)
        if self.backup_id != expected_id:
            raise ValueError("backup identity does not match its inventory")
        return self

    def to_json_dict(self) -> dict[str, object]:
        return self.model_dump(mode="json")


class WorkspaceBackupService:
    """Create and restore filesystem snapshots coordinated with one writer mutex."""

    def __init__(
        self,
        workspace_service: WorkspaceService,
        *,
        writer_lock: threading.RLock | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._workspace_service = workspace_service
        self._writer_lock = writer_lock or threading.RLock()
        self._clock = clock

    def create(self, source: Path, destination: Path) -> WorkspaceBackupManifest:
        """Publish one complete backup directory only after every hash verifies."""
        source_path = source.expanduser()
        destination_path = destination.expanduser()
        if source_path.is_symlink() or destination_path.is_symlink():
            raise WorkspaceBackupError("workspace backup paths must not be symbolic links")
        source_root = source_path.resolve()
        destination_root = destination_path.resolve(strict=False)
        if destination_root.exists():
            raise WorkspaceBackupError("backup destination already exists")
        temporary = destination_root.with_name(f".{destination_root.name}.{uuid4().hex}.tmp")
        if source_root == destination_root or source_root in destination_root.parents:
            raise WorkspaceBackupError("backup destination must be outside the source workspace")
        try:
            _reject_symlinks(source_root)
            with _workspace_process_guard(source_root), self._writer_lock:
                _reject_symlinks(source_root)
                inspection = self._workspace_service.inspect(source_root)
                if inspection.status != "ready":
                    raise WorkspaceBackupError("source workspace must be ready for backup")
                _verify_workspace_traceability(self._workspace_service, source_root)
                files = _inventory(source_root)
                counts = _counts(inspection)
                manifest = WorkspaceBackupManifest(
                    backup_id=_backup_id(inspection.workspace_id, files, counts),
                    source_workspace_id=inspection.workspace_id,
                    created_at=self._now(),
                    files=files,
                    counts=counts,
                )
                temporary.mkdir(parents=True)
                _copy_inventory(source_root, temporary, files)
                _write_manifest(temporary / BACKUP_MANIFEST_NAME, manifest)
                _verify_backup_directory(temporary, manifest)
            os.replace(temporary, destination_root)
            return manifest
        except WorkspaceBackupError:
            raise
        except (OSError, ValueError) as error:
            raise WorkspaceBackupError("workspace backup could not be completed") from error
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)

    def restore(self, backup: Path, destination: Path) -> WorkspaceInspection:
        """Verify then activate a backup only into a new or empty destination."""
        backup_path = backup.expanduser()
        destination_path = destination.expanduser()
        if backup_path.is_symlink() or destination_path.is_symlink():
            raise WorkspaceBackupError("workspace restore paths must not be symbolic links")
        backup_root = backup_path.resolve()
        destination_root = destination_path.resolve(strict=False)
        if destination_root.exists() and any(destination_root.iterdir()):
            raise WorkspaceBackupError("restore destination must be new or empty")
        if backup_root == destination_root or backup_root in destination_root.parents:
            raise WorkspaceBackupError("restore destination must be outside the backup")
        _reject_symlinks(backup_root)
        manifest = _load_manifest(backup_root / BACKUP_MANIFEST_NAME)
        _verify_backup_directory(backup_root, manifest)
        temporary = destination_root.with_name(f".{destination_root.name}.{uuid4().hex}.tmp")
        try:
            temporary.mkdir(parents=True)
            _create_required_layout(temporary)
            _copy_inventory(backup_root, temporary, manifest.files)
            inspection = self._workspace_service.inspect(temporary)
            if inspection.status != "ready":
                raise WorkspaceBackupError("restored workspace layout is incomplete")
            _verify_workspace_traceability(self._workspace_service, temporary)
            if inspection.workspace_id != manifest.source_workspace_id:
                raise WorkspaceBackupError("restored workspace identity does not match backup")
            if _counts(inspection) != manifest.counts:
                raise WorkspaceBackupError("restored analytical counts do not match backup")
            if destination_root.exists():
                destination_root.rmdir()
            os.replace(temporary, destination_root)
            return inspection.model_copy(update={"workspace_root": destination_root})
        except WorkspaceBackupError:
            raise
        except (OSError, ValueError) as error:
            raise WorkspaceBackupError("workspace restore could not be completed") from error
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise WorkspaceBackupError("backup clock must be timezone-aware")
        return value.astimezone(UTC)


def _inventory(root: Path) -> tuple[WorkspaceBackupFile, ...]:
    _reject_symlinks(root)
    files: list[WorkspaceBackupFile] = []
    for path in sorted(candidate for candidate in root.rglob("*") if candidate.is_file()):
        relative = path.relative_to(root).as_posix()
        if path.name.endswith(".lock") or ".tmp" in path.name:
            continue
        files.append(
            WorkspaceBackupFile(
                path=relative,
                size_bytes=path.stat().st_size,
                sha256=_sha256(path),
            )
        )
    return tuple(files)


def _counts(inspection: WorkspaceInspection) -> WorkspaceBackupCounts:
    return WorkspaceBackupCounts(
        raw_records=inspection.raw_record_count,
        observations=inspection.observation_count,
        metric_results=inspection.metric_result_count,
        diagnostic_results=inspection.diagnostic_result_count,
    )


def _backup_id(
    workspace_id: UUID,
    files: tuple[WorkspaceBackupFile, ...],
    counts: WorkspaceBackupCounts,
) -> UUID:
    document = json.dumps(
        {
            "workspace_id": str(workspace_id),
            "files": [item.model_dump(mode="json") for item in files],
            "counts": counts.model_dump(mode="json"),
        },
        separators=(",", ":"),
        sort_keys=True,
    )
    return uuid5(NAMESPACE_URL, document)


def _copy_inventory(
    source: Path,
    destination: Path,
    files: tuple[WorkspaceBackupFile, ...],
) -> None:
    for item in files:
        source_file = source / item.path
        target_file = destination / item.path
        if source_file.is_symlink() or not source_file.is_file():
            raise WorkspaceBackupError("backup inventory must contain only regular files")
        target_file.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source_file, target_file)
        if target_file.stat().st_size != item.size_bytes or _sha256(target_file) != item.sha256:
            raise WorkspaceBackupError("copied backup file failed verification")


def _create_required_layout(root: Path) -> None:
    """Recreate only empty directories required by workspace/storage format v1."""
    for relative in (
        "exports",
        "state",
        "storage/data/exports",
        "storage/data/processed",
        "storage/data/raw",
    ):
        (root / relative).mkdir(parents=True, exist_ok=True)


def _verify_backup_directory(root: Path, manifest: WorkspaceBackupManifest) -> None:
    _reject_symlinks(root)
    expected = {item.path: item for item in manifest.files}
    actual = {
        path.relative_to(root).as_posix(): path
        for path in root.rglob("*")
        if path.is_file() and path.name != BACKUP_MANIFEST_NAME
    }
    if set(actual) != set(expected):
        raise WorkspaceBackupError("backup file inventory does not match manifest")
    for relative, item in expected.items():
        path = actual[relative]
        if path.stat().st_size != item.size_bytes or _sha256(path) != item.sha256:
            raise WorkspaceBackupError("backup file hash verification failed")


def _load_manifest(path: Path) -> WorkspaceBackupManifest:
    if path.is_symlink() or not path.is_file():
        raise WorkspaceBackupError("backup manifest must be a regular file")
    try:
        return WorkspaceBackupManifest.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as error:
        raise WorkspaceBackupError("backup manifest is malformed or unavailable") from error


def _write_manifest(path: Path, manifest: WorkspaceBackupManifest) -> None:
    document = json.dumps(
        manifest.to_json_dict(),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(f"{document}\n")
        stream.flush()
        os.fsync(stream.fileno())


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, "rb", closefd=True) as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _reject_symlinks(root: Path) -> None:
    try:
        if root.is_symlink() or any(path.is_symlink() for path in root.rglob("*")):
            raise WorkspaceBackupError("workspace backups must not contain symbolic links")
    except OSError as error:
        raise WorkspaceBackupError("workspace backup tree could not be inspected") from error


@contextmanager
def _workspace_process_guard(root: Path):
    """Exclude the resident service while copying its writer-owned workspace."""
    paths = (
        root / "state" / "aapl_daily_run.lock",
        root / "state" / "aapl_local_service.lock",
    )
    descriptors: list[int] = []
    try:
        for path in paths:
            descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
            descriptors.append(descriptor)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise WorkspaceBackupError(
                    "workspace writers must be stopped before creating a backup"
                ) from error
        yield
    except OSError as error:
        raise WorkspaceBackupError("workspace backup lock could not be acquired") from error
    finally:
        for descriptor in reversed(descriptors):
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)


def _verify_workspace_traceability(service: WorkspaceService, root: Path) -> None:
    """Read and connect every persisted evidence layer before backup activation."""
    paths = service.resolve(root)
    try:
        storage = service.open_storage(paths, WorkspaceAccessMode.READ_ONLY)
        try:
            raw_records = storage.raw_records.list()
            observations = storage.observations.list()
            metrics = storage.metric_results.list()
            diagnostics = storage.diagnostics.list()
        finally:
            storage.close()
    except (OSError, StorageError, WorkspaceError, ValueError) as error:
        raise WorkspaceBackupError("workspace traceability could not be verified") from error
    raw_ids = {item.record_id for item in raw_records}
    observation_ids = {item.observation_id for item in observations}
    metric_ids = {item.result_id for item in metrics}
    if any(item.raw_record_id not in raw_ids for item in observations):
        raise WorkspaceBackupError("workspace contains an observation without its raw record")
    if any(
        input_id not in observation_ids
        for metric in metrics
        for input_id in metric.input_observation_ids
    ):
        raise WorkspaceBackupError("workspace contains a metric without its observations")
    if any(
        input_id not in metric_ids
        for metric in metrics
        for input_id in metric.input_metric_result_ids
    ):
        raise WorkspaceBackupError("workspace contains a metric without its derived metrics")
    diagnostic_metric_ids = {
        metric_id
        for diagnostic in diagnostics
        for component in diagnostic.components
        for metric_id in component.metric_result_ids
    } | {
        evidence.metric_result_id for diagnostic in diagnostics for evidence in diagnostic.evidence
    }
    if not diagnostic_metric_ids.issubset(metric_ids):
        raise WorkspaceBackupError("workspace contains a diagnostic without its metrics")


__all__ = [
    "BACKUP_MANIFEST_NAME",
    "WorkspaceBackupError",
    "WorkspaceBackupFile",
    "WorkspaceBackupManifest",
    "WorkspaceBackupService",
]
