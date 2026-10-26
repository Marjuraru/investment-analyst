"""Opt-in physical reuse of an immutable v2 backup generation."""

from __future__ import annotations

import errno
import hashlib
import os
import shutil
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from pydantic import ConfigDict, Field

from investment_analyst.core.models.base import ContractModel, NonEmptyStr, UTCDateTime
from investment_analyst.storage.errors import StorageError
from investment_analyst.workspace.backup import (
    WorkspaceBackupError,
    WorkspaceBackupFile,
    _inventory,
    _reject_symlinks,
)
from investment_analyst.workspace.service import WorkspaceService
from investment_analyst.workspace.workspace_v2_backup import (
    WorkspaceV2BackupError,
    WorkspaceV2BackupManifest,
    _backup_id,
    _counts,
    _create_required_layout,
    _require_capacity,
    _v2_writer_guard,
    _verify_backup_directory,
    _write_manifest,
)

BACKUP_REUSE_RECEIPT_SCHEMA = "backup-reuse-receipt-v1"
BACKUP_PARENT_INSPECTION_SCHEMA = "backup-parent-inspection-v1"


class BackupReuseError(RuntimeError):
    """Raised when a backup generation cannot be reused safely."""


class BackupParentInspection(ContractModel):
    """Compact identity of the immutable parent generation."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: str = BACKUP_PARENT_INSPECTION_SCHEMA
    backup_id: UUID
    source_workspace_id: UUID
    manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    files: int = Field(ge=0)
    byte_length: int = Field(ge=0)
    created_at: UTCDateTime


class BackupReuseReceipt(ContractModel):
    """Accounting for one successor generation built from a verified parent."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: str = BACKUP_REUSE_RECEIPT_SCHEMA
    parent_backup_id: UUID
    new_backup_id: UUID
    source_workspace_id: UUID
    destination: NonEmptyStr
    parent_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    files_total: int = Field(ge=0)
    bytes_total: int = Field(ge=0)
    files_reused: int = Field(ge=0)
    bytes_reused: int = Field(ge=0)
    files_copied: int = Field(ge=0)
    bytes_copied: int = Field(ge=0)
    parent_unchanged: bool
    verified: bool
    created_at: UTCDateTime


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _manifest_path(root: Path) -> Path:
    return root / "backup_manifest.json"


def _load_parent_manifest(root: Path) -> WorkspaceV2BackupManifest:
    path = _manifest_path(root)
    if path.is_symlink() or not path.is_file():
        raise BackupReuseError("backup parent manifest is missing or unsafe")
    try:
        manifest = WorkspaceV2BackupManifest.model_validate_json(path.read_bytes())
    except (OSError, ValueError) as error:
        raise BackupReuseError("backup parent manifest is malformed") from error
    expected = _backup_id(manifest.source_workspace_id, manifest.files, manifest.counts)
    if manifest.backup_id != expected:
        raise BackupReuseError("backup parent identity does not match its inventory")
    return manifest


def inspect_backup_parent(root: Path) -> BackupParentInspection:
    """Read and validate a parent inventory without reading its payload bytes."""
    parent = root.expanduser().resolve(strict=False)
    if parent.is_symlink() or not parent.is_dir():
        raise BackupReuseError("backup parent is missing or unsafe")
    manifest_path = _manifest_path(parent)
    manifest = _load_parent_manifest(parent)
    return BackupParentInspection(
        backup_id=manifest.backup_id,
        source_workspace_id=manifest.source_workspace_id,
        manifest_sha256=_sha256(manifest_path),
        files=len(manifest.files),
        byte_length=sum(item.size_bytes for item in manifest.files),
        created_at=manifest.created_at,
    )


class BackupReuseService:
    """Create a verified successor while linking identical parent files."""

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

    def create_successor(
        self,
        source: Path,
        destination: Path,
        parent: Path,
        *,
        available_bytes: Callable[[], int] | None = None,
        reserve_bytes: int = 5_000_000_000,
        allow_copy_fallback: bool = False,
    ) -> BackupReuseReceipt:
        source_root = source.expanduser().resolve()
        destination_root = destination.expanduser().resolve(strict=False)
        parent_root = parent.expanduser().resolve()
        for root in (source_root, destination_root, parent_root):
            if root.is_symlink():
                raise BackupReuseError("backup reuse paths cannot be symbolic links")
        if not source_root.is_dir() or not parent_root.is_dir():
            raise BackupReuseError("backup reuse source or parent is missing")
        if len({source_root, destination_root, parent_root}) != 3 or any(
            first in second.parents
            for first, second in (
                (source_root, destination_root),
                (destination_root, source_root),
                (parent_root, destination_root),
                (destination_root, parent_root),
                (parent_root, source_root),
                (source_root, parent_root),
            )
        ):
            raise BackupReuseError("backup reuse roots must be disjoint")
        if destination_root.exists():
            raise BackupReuseError("backup reuse destination already exists")
        if (
            isinstance(reserve_bytes, bool)
            or not isinstance(reserve_bytes, int)
            or reserve_bytes < 0
        ):
            raise BackupReuseError("backup reuse reserve must be a non-negative integer")
        if allow_copy_fallback and available_bytes is None:
            raise BackupReuseError("copy fallback requires a host capacity reader")

        parent_manifest = _load_parent_manifest(parent_root)
        parent_manifest_sha = _sha256(_manifest_path(parent_root))
        _reject_symlinks(parent_root)
        try:
            _verify_backup_directory(parent_root, parent_manifest)
        except (WorkspaceBackupError, WorkspaceV2BackupError) as error:
            raise BackupReuseError("backup parent failed verification") from error
        parent_files = {item.path: item for item in parent_manifest.files}

        temporary = destination_root.with_name(f".{destination_root.name}.{os.getpid()}.tmp")
        if temporary.exists():
            raise BackupReuseError("backup reuse temporary destination already exists")
        reused_files = reused_bytes = copied_files = copied_bytes = 0
        try:
            with _v2_writer_guard(source_root), self._writer_lock:
                inspection = self._workspace_service.inspect(source_root)
                if inspection.format_version != 2 or inspection.status != "ready":
                    raise BackupReuseError("backup reuse source is not a ready v2 workspace")
                if inspection.workspace_id != parent_manifest.source_workspace_id:
                    raise BackupReuseError("backup reuse source identity differs from the parent")
                files = tuple(sorted(_inventory(source_root), key=lambda item: item.path))
                counts = _counts(inspection)
                manifest = WorkspaceV2BackupManifest(
                    backup_id=_backup_id(inspection.workspace_id, files, counts),
                    source_workspace_id=inspection.workspace_id,
                    created_at=self._clock(),
                    files=files,
                    counts=counts,
                )
                _create_required_layout(temporary)
                for item in files:
                    source_file = source_root / item.path
                    target_file = temporary / item.path
                    if source_file.is_symlink() or not source_file.is_file():
                        raise BackupReuseError("backup reuse source contains an unsafe file")
                    parent_item = parent_files.get(item.path)
                    target_file.parent.mkdir(parents=True, exist_ok=True)
                    if (
                        parent_item is not None
                        and parent_item.size_bytes == item.size_bytes
                        and parent_item.sha256 == item.sha256
                    ):
                        parent_file = parent_root / item.path
                        if parent_file.is_symlink() or not parent_file.is_file():
                            raise BackupReuseError("backup parent contains an unsafe file")
                        try:
                            os.link(parent_file, target_file, follow_symlinks=False)
                        except OSError as error:
                            if error.errno != errno.EXDEV:
                                raise
                            if not allow_copy_fallback:
                                raise BackupReuseError(
                                    "hardlink reuse crossed filesystems without copy admission"
                                ) from error
                            copied_bytes += self._copy_verified(
                                source_file,
                                target_file,
                                item,
                                available_bytes=available_bytes,
                                reserve_bytes=reserve_bytes,
                            )
                            copied_files += 1
                            continue
                        if (
                            target_file.is_symlink()
                            or target_file.stat().st_size != item.size_bytes
                            or _sha256(target_file) != item.sha256
                        ):
                            raise BackupReuseError("hardlinked backup file failed verification")
                        reused_files += 1
                        reused_bytes += item.size_bytes
                    else:
                        copied_bytes += self._copy_verified(
                            source_file,
                            target_file,
                            item,
                            available_bytes=available_bytes,
                            reserve_bytes=reserve_bytes,
                        )
                        copied_files += 1
                _write_manifest(temporary / "backup_manifest.json", manifest)
                _verify_backup_directory(temporary, manifest)
                if parent_manifest_sha != _sha256(_manifest_path(parent_root)):
                    raise BackupReuseError("backup parent manifest changed during reuse")
                if destination_root.exists():
                    raise BackupReuseError("backup reuse destination was created concurrently")
                os.replace(temporary, destination_root)
        except (
            BackupReuseError,
            WorkspaceBackupError,
            WorkspaceV2BackupError,
            OSError,
            StorageError,
            ValueError,
        ) as error:
            if isinstance(error, BackupReuseError):
                raise
            raise BackupReuseError("backup reuse could not be completed") from error
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)
        return BackupReuseReceipt(
            parent_backup_id=parent_manifest.backup_id,
            new_backup_id=manifest.backup_id,
            source_workspace_id=manifest.source_workspace_id,
            destination=str(destination_root),
            parent_manifest_sha256=parent_manifest_sha,
            files_total=len(files),
            bytes_total=sum(item.size_bytes for item in files),
            files_reused=reused_files,
            bytes_reused=reused_bytes,
            files_copied=copied_files,
            bytes_copied=copied_bytes,
            parent_unchanged=True,
            verified=True,
            created_at=self._clock(),
        )

    @staticmethod
    def _copy_verified(
        source: Path,
        target: Path,
        item: WorkspaceBackupFile,
        *,
        available_bytes: Callable[[], int] | None,
        reserve_bytes: int,
    ) -> int:
        if available_bytes is not None:
            try:
                _require_capacity(
                    available_bytes,
                    reserve_bytes=reserve_bytes,
                    growth_bytes=item.size_bytes,
                )
            except WorkspaceV2BackupError as error:
                raise BackupReuseError(str(error)) from error
        shutil.copyfile(source, target)
        if target.stat().st_size != item.size_bytes or _sha256(target) != item.sha256:
            raise BackupReuseError("copied backup file failed verification")
        return item.size_bytes


__all__ = [
    "BACKUP_PARENT_INSPECTION_SCHEMA",
    "BACKUP_REUSE_RECEIPT_SCHEMA",
    "BackupParentInspection",
    "BackupReuseError",
    "BackupReuseReceipt",
    "BackupReuseService",
    "inspect_backup_parent",
]
