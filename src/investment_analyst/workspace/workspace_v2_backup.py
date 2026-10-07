"""Hash-verified backup and restore for workspace format v2."""

from __future__ import annotations

import fcntl
import json
import os
import shutil
import threading
from collections.abc import Callable
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from pydantic import ConfigDict, model_validator

from investment_analyst.analytics.cazatiburones.activity_event_repository import (
    ActivityEventRepository,
)
from investment_analyst.core.models.base import ContractModel, UTCDateTime
from investment_analyst.evidence.sec_documents.repository import (
    SecDocumentRepository,
    verify_document_records,
)
from investment_analyst.storage import StorageError
from investment_analyst.storage.compact_analytical_v2 import CompactAnalyticalStore
from investment_analyst.storage.workspace_incremental_v2 import (
    WorkspaceIncrementalV2Error,
    verify_workspace_incremental_v2,
)
from investment_analyst.workspace.backup import (
    WorkspaceBackupCounts,
    WorkspaceBackupError,
    WorkspaceBackupFile,
    _copy_inventory,
    _inventory,
    _reject_symlinks,
    _sha256,
)
from investment_analyst.workspace.models import WorkspaceAccessMode, WorkspaceInspection
from investment_analyst.workspace.service import WorkspaceService

_MANIFEST_NAME = "backup_manifest.json"
_V2_DATABASE_PATH = "storage/v2/index.duckdb"
_V2_MARKER_PATH = "storage/v2/raw-v2-staging.json"


class WorkspaceV2BackupError(WorkspaceBackupError):
    """A v2 workspace snapshot cannot be proven complete or safe."""


class WorkspaceV2BackupManifest(ContractModel):
    """Separate versioned inventory contract for format-v2 workspaces."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["workspace-v2-backup-manifest-v1"] = "workspace-v2-backup-manifest-v1"
    backup_id: UUID
    source_workspace_id: UUID
    workspace_format_version: Literal[2] = 2
    created_at: UTCDateTime
    files: tuple[WorkspaceBackupFile, ...]
    counts: WorkspaceBackupCounts

    @model_validator(mode="after")
    def validate_inventory(self) -> WorkspaceV2BackupManifest:
        paths = tuple(item.path for item in self.files)
        required = {"manifest.json", _V2_DATABASE_PATH, _V2_MARKER_PATH}
        if not paths or paths != tuple(sorted(paths)) or len(paths) != len(set(paths)):
            raise ValueError("workspace v2 backup inventory must be non-empty and sorted")
        if not required.issubset(paths):
            raise ValueError("workspace v2 backup inventory is missing required files")
        if self.backup_id != _backup_id(self.source_workspace_id, self.files, self.counts):
            raise ValueError("workspace v2 backup identity does not match its inventory")
        if self.created_at.tzinfo is None or self.created_at.utcoffset() is None:
            raise ValueError("workspace v2 backup timestamp must be timezone-aware")
        return self

    def to_json_dict(self) -> dict[str, object]:
        return self.model_dump(mode="json")


class WorkspaceV2BackupService:
    """Snapshot every non-transient workspace file under the v2 writer lock."""

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

    def create(self, source: Path, destination: Path) -> WorkspaceV2BackupManifest:
        source_path = source.expanduser()
        destination_path = destination.expanduser()
        if source_path.is_symlink() or destination_path.is_symlink():
            raise WorkspaceV2BackupError("workspace v2 backup paths cannot be symbolic links")
        source_root = source_path.resolve()
        destination_root = destination_path.resolve(strict=False)
        if destination_root.exists():
            raise WorkspaceV2BackupError("workspace v2 backup destination already exists")
        if (
            source_root == destination_root
            or source_root in destination_root.parents
            or destination_root in source_root.parents
        ):
            raise WorkspaceV2BackupError("workspace v2 backup destination must be outside source")
        temporary = destination_root.with_name(f".{destination_root.name}.{uuid4().hex}.tmp")
        try:
            _reject_symlinks(source_root)
            with _v2_writer_guard(source_root), self._writer_lock:
                inspection = self._require_v2_inspection(source_root)
                _verify_v2_workspace(
                    self._workspace_service,
                    source_root,
                    expected_counts=_counts(inspection),
                )
                files = tuple(sorted(_inventory(source_root), key=lambda item: item.path))
                manifest = WorkspaceV2BackupManifest(
                    backup_id=_backup_id(inspection.workspace_id, files, _counts(inspection)),
                    source_workspace_id=inspection.workspace_id,
                    created_at=self._now(),
                    files=files,
                    counts=_counts(inspection),
                )
                temporary.mkdir(parents=True)
                _copy_inventory(source_root, temporary, files)
                _write_manifest(temporary / _MANIFEST_NAME, manifest)
                _verify_backup_directory(temporary, manifest)
            os.replace(temporary, destination_root)
            return manifest
        except WorkspaceV2BackupError:
            raise
        except (OSError, StorageError, ValueError) as error:
            raise WorkspaceV2BackupError("workspace v2 backup could not be completed") from error
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)

    def restore(self, backup: Path, destination: Path) -> WorkspaceInspection:
        backup_path = backup.expanduser()
        destination_path = destination.expanduser()
        if backup_path.is_symlink() or destination_path.is_symlink():
            raise WorkspaceV2BackupError("workspace v2 restore paths cannot be symbolic links")
        backup_root = backup_path.resolve()
        destination_root = destination_path.resolve(strict=False)
        if destination_root.exists() and any(destination_root.iterdir()):
            raise WorkspaceV2BackupError("workspace v2 restore destination must be empty")
        if (
            backup_root == destination_root
            or backup_root in destination_root.parents
            or destination_root in backup_root.parents
        ):
            raise WorkspaceV2BackupError("workspace v2 restore destination must be outside backup")
        _reject_symlinks(backup_root)
        manifest = _load_manifest(backup_root / _MANIFEST_NAME)
        _verify_backup_directory(backup_root, manifest)
        temporary = destination_root.with_name(f".{destination_root.name}.{uuid4().hex}.tmp")
        try:
            temporary.mkdir(parents=True)
            _create_required_layout(temporary)
            _copy_inventory(backup_root, temporary, manifest.files)
            inspection = self._require_v2_inspection(temporary)
            if inspection.workspace_id != manifest.source_workspace_id:
                raise WorkspaceV2BackupError("restored workspace identity differs from backup")
            if _counts(inspection) != manifest.counts:
                raise WorkspaceV2BackupError("restored workspace counts differ from backup")
            _verify_v2_workspace(
                self._workspace_service,
                temporary,
                expected_counts=manifest.counts,
            )
            if destination_root.exists():
                destination_root.rmdir()
            os.replace(temporary, destination_root)
            return inspection.model_copy(update={"workspace_root": destination_root})
        except WorkspaceV2BackupError:
            raise
        except (OSError, StorageError, ValueError) as error:
            raise WorkspaceV2BackupError("workspace v2 restore could not be completed") from error
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)

    def _require_v2_inspection(self, root: Path) -> WorkspaceInspection:
        inspection = self._workspace_service.inspect(root)
        if inspection.status != "ready" or inspection.format_version != 2:
            raise WorkspaceV2BackupError("workspace v2 source must be ready and version 2")
        return inspection

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise WorkspaceV2BackupError("workspace v2 backup clock must be timezone-aware")
        return value.astimezone(UTC)


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
            "manifest_schema": "workspace-v2-backup-manifest-v1",
        },
        separators=(",", ":"),
        sort_keys=True,
    )
    return uuid5(NAMESPACE_URL, document)


def _write_manifest(path: Path, manifest: WorkspaceV2BackupManifest) -> None:
    payload = json.dumps(
        manifest.to_json_dict(),
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    with path.open("x", encoding="utf-8", newline="\n") as handle:
        handle.write(f"{payload}\n")
        handle.flush()
        os.fsync(handle.fileno())


def _load_manifest(path: Path) -> WorkspaceV2BackupManifest:
    if path.is_symlink() or not path.is_file():
        raise WorkspaceV2BackupError("workspace v2 backup manifest is missing or unsafe")
    try:
        return WorkspaceV2BackupManifest.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as error:
        raise WorkspaceV2BackupError("workspace v2 backup manifest is malformed") from error


def _verify_backup_directory(root: Path, manifest: WorkspaceV2BackupManifest) -> None:
    _reject_symlinks(root)
    expected = {item.path: item for item in manifest.files}
    actual = {
        item.relative_to(root).as_posix(): item
        for item in root.rglob("*")
        if item.is_file() and item.name != _MANIFEST_NAME
    }
    if set(actual) != set(expected):
        raise WorkspaceV2BackupError("workspace v2 backup inventory differs from manifest")
    for relative, entry in expected.items():
        path = actual[relative]
        if path.stat().st_size != entry.size_bytes or _sha256(path) != entry.sha256:
            raise WorkspaceV2BackupError("workspace v2 backup file hash verification failed")


def _create_required_layout(root: Path) -> None:
    for relative in (
        "exports",
        "state",
        "storage/v2",
        "storage/v2/raw",
        "data/documents",
    ):
        (root / relative).mkdir(parents=True, exist_ok=True)


@contextmanager
def _v2_writer_guard(root: Path):
    lock_paths = (
        root / "storage" / "v2" / "raw-v2-staging.lock",
        root / "state" / "aapl_daily_run.lock",
        root / "state" / "aapl_local_service.lock",
    )
    raw_lock = lock_paths[0]
    if raw_lock.is_symlink() or not raw_lock.is_file():
        raise WorkspaceV2BackupError("workspace v2 writer lock is missing or unsafe")
    descriptors: list[int] = []
    try:
        for lock_path in lock_paths:
            if lock_path.is_symlink():
                raise WorkspaceV2BackupError("workspace v2 writer lock is unsafe")
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            descriptor = os.open(
                lock_path,
                os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            descriptors.append(descriptor)
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    except BlockingIOError as error:
        raise WorkspaceV2BackupError("workspace v2 writers must close before backup") from error
    except OSError as error:
        raise WorkspaceV2BackupError("workspace v2 writer lock cannot be acquired") from error
    finally:
        for descriptor in reversed(descriptors):
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)


def _verify_v2_workspace(
    service: WorkspaceService,
    root: Path,
    *,
    expected_counts: WorkspaceBackupCounts,
) -> None:
    paths = service.resolve(root)
    storage = service.open_storage(paths, WorkspaceAccessMode.READ_ONLY)
    try:
        if (
            storage.raw_records.count() != expected_counts.raw_records
            or storage.observations.count() != expected_counts.observations
            or storage.metric_results.count() != expected_counts.metric_results
            or storage.diagnostics.count() != expected_counts.diagnostic_results
        ):
            raise WorkspaceV2BackupError("workspace v2 counts changed during backup verification")
        cursor_at: datetime | None = None
        cursor_id: UUID | None = None
        verified = 0
        sec_documents = SecDocumentRepository(storage.raw_records, storage.documents)
        while True:
            page = storage.raw_records.list_import_page(
                limit=256,
                after_received_at=cursor_at,
                after_record_id=cursor_id,
            )
            if not page:
                break
            records = storage.raw_records.get_many(page)
            if len(records) != len(page):
                raise WorkspaceV2BackupError("workspace v2 raw page did not resolve exactly")
            verify_document_records(records.values(), sec_documents)
            verified += len(records)
            last = records[page[-1]]
            cursor_at, cursor_id = last.received_at, last.record_id
        if verified != expected_counts.raw_records:
            raise WorkspaceV2BackupError("workspace v2 raw inventory is incomplete")
        observation_cursor_at: datetime | None = None
        observation_cursor_id: UUID | None = None
        verified = 0
        while True:
            page = storage.observations.list_observation_import_page(
                limit=256,
                after_available_at=observation_cursor_at,
                after_observation_id=observation_cursor_id,
            )
            if not page:
                break
            observations = storage.observations.get_many(page)
            verified += len(observations)
            last = observations[page[-1]]
            observation_cursor_at, observation_cursor_id = (
                last.available_at,
                last.observation_id,
            )
        if verified != expected_counts.observations:
            raise WorkspaceV2BackupError("workspace v2 observation inventory is incomplete")
        CompactAnalyticalStore(storage.store.connection).verify_complete()
        try:
            verify_workspace_incremental_v2(storage)
        except WorkspaceIncrementalV2Error as error:
            raise WorkspaceV2BackupError(
                "workspace v2 incremental artifacts failed verification"
            ) from error
        ActivityEventRepository(storage.paths.processed_dir, read_only=True).verify()
    finally:
        storage.close()


__all__ = ["WorkspaceV2BackupError", "WorkspaceV2BackupManifest", "WorkspaceV2BackupService"]
