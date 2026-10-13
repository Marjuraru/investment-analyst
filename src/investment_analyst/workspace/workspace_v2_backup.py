"""Hash-verified backup and restore for workspace format v2."""

from __future__ import annotations

import fcntl
import gzip
import hashlib
import json
import os
import shutil
import struct
import threading
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Protocol
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
from investment_analyst.workspace.workspace_v2_backup_inventory import (
    WorkspaceV2BackupInventoryError,
    WorkspaceV2BackupInventorySummary,
    iter_inventory,
    verify_inventory,
    write_inventory,
)

_MANIFEST_NAME = "backup_manifest.json"
_V2_DATABASE_PATH = "storage/v2/index.duckdb"
_V2_MARKER_PATH = "storage/v2/raw-v2-staging.json"
_PACKED_MANIFEST_NAME = "backup_manifest.json"
_PACKED_CHECKPOINT_NAME = "packed-backup-checkpoint.json"
_PACKED_RESTORE_CHECKPOINT_SUFFIX = ".packed-restore-checkpoint.json"
_PACKED_JOURNAL_NAME = "packed-backup-volumes.jsonl"
_PACKED_VOLUME_DIRECTORY = "volumes"
_PACKED_INVENTORY_NAME = "source-inventory.jsonl"
_PACKED_CHUNK_BYTES = 1024 * 1024
_PACKED_VOLUME_BYTES = 8 * 1024 * 1024
_MINIMUM_RESERVE_BYTES = 5_000_000_000


class WorkspaceV2BackupError(WorkspaceBackupError):
    """A v2 workspace snapshot cannot be proven complete or safe."""


class _ByteDigest(Protocol):
    def update(self, data: bytes) -> None: ...


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


class WorkspaceV2PackedFragment(ContractModel):
    """One bounded file range in an independently readable gzip volume."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    path: str
    offset_bytes: int
    length_bytes: int
    sha256: str
    stream_offset_bytes: int


class WorkspaceV2PackedVolume(ContractModel):
    """A sealed physical gzip member and its ordered bounded fragments."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    volume_id: str
    path: str
    compressed_bytes: int
    decompressed_bytes: int
    compressed_sha256: str
    decompressed_sha256: str
    fragments: tuple[WorkspaceV2PackedFragment, ...]


class WorkspaceV2PackedBackupManifest(ContractModel):
    """Opt-in resumable physical transport for a complete workspace-v2 backup."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["workspace-v2-backup-manifest-v2"] = "workspace-v2-backup-manifest-v2"
    transport: Literal["packed-gzip-volumes-v1"] = "packed-gzip-volumes-v1"
    backup_id: UUID
    source_workspace_id: UUID
    workspace_format_version: Literal[2] = 2
    created_at: UTCDateTime
    files: tuple[WorkspaceBackupFile, ...]
    counts: WorkspaceBackupCounts
    volumes: tuple[WorkspaceV2PackedVolume, ...]

    @model_validator(mode="after")
    def validate_packed_inventory(self) -> WorkspaceV2PackedBackupManifest:
        if not self.files or not self.volumes:
            raise ValueError("packed workspace v2 backup must contain files and volumes")
        paths = tuple(item.path for item in self.files)
        if paths != tuple(sorted(paths)):
            raise ValueError("packed workspace v2 backup files must be sorted")
        if len({item.path for item in self.files}) != len(self.files):
            raise ValueError("packed workspace v2 backup files must be unique")
        if any(not volume.fragments for volume in self.volumes):
            raise ValueError("packed workspace v2 backup volume cannot be empty")
        return self

    def to_json_dict(self) -> dict[str, object]:
        return self.model_dump(mode="json")


class WorkspaceV2PackedStreamManifest(ContractModel):
    """Small manifest binding streamable inventory and volume journal sidecars."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["workspace-v2-backup-manifest-v3"] = "workspace-v2-backup-manifest-v3"
    transport: Literal["packed-gzip-volumes-v1"] = "packed-gzip-volumes-v1"
    backup_id: UUID
    source_workspace_id: UUID
    workspace_format_version: Literal[2] = 2
    created_at: UTCDateTime
    counts: WorkspaceBackupCounts
    inventory: WorkspaceV2BackupInventorySummary
    journal_sha256: str
    volume_count: int

    @model_validator(mode="after")
    def validate_bindings(self) -> WorkspaceV2PackedStreamManifest:
        if len(self.journal_sha256) != 64 or self.volume_count <= 0:
            raise ValueError("packed stream manifest journal binding is invalid")
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
        except (
            OSError,
            StorageError,
            ValueError,
            WorkspaceV2BackupInventoryError,
        ) as error:
            raise WorkspaceV2BackupError("workspace v2 restore could not be completed") from error
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)

    def create_packed(
        self,
        source: Path,
        destination: Path,
        *,
        volume_bytes: int = _PACKED_VOLUME_BYTES,
        fragment_bytes: int = _PACKED_CHUNK_BYTES,
        available_bytes: Callable[[], int] | None = None,
        reserve_bytes: int = _MINIMUM_RESERVE_BYTES,
    ) -> WorkspaceV2PackedBackupManifest | WorkspaceV2PackedStreamManifest:
        """Resume a gzip-volume backup without recreating sealed volumes."""
        if volume_bytes <= 8 or fragment_bytes <= 0:
            raise WorkspaceV2BackupError("packed backup limits must be positive")
        source_root, destination_root = _checked_packed_roots(source, destination)
        if available_bytes is None:
            raise WorkspaceV2BackupError("packed backup requires a host capacity reader")
        capacity_reader = available_bytes
        incomplete = destination_root.with_name(f".{destination_root.name}.incomplete")
        if not incomplete.exists():
            _validate_capacity_reader(capacity_reader, reserve_bytes)
        try:
            with _v2_writer_guard(source_root), self._writer_lock:
                inspection = self._require_v2_inspection(source_root)
                inventory_path = incomplete / _PACKED_INVENTORY_NAME
                if inventory_path.exists():
                    inventory = verify_inventory(inventory_path)
                else:
                    inventory = write_inventory(source_root, inventory_path)
                    loaded_inventory = verify_inventory(inventory_path)
                    if loaded_inventory != inventory:
                        raise WorkspaceV2BackupError(
                            "packed backup inventory changed after sealing"
                        )
                inventory_digest = inventory.digest_sha256
                checkpoint = _open_packed_checkpoint(
                    incomplete,
                    source_workspace_id=inspection.workspace_id,
                    inventory_digest=inventory_digest,
                    volume_bytes=volume_bytes,
                    fragment_bytes=fragment_bytes,
                )
                if int(checkpoint["next_file_index"]) == inventory.file_count:
                    if int(checkpoint["next_offset_bytes"]) != 0:
                        raise WorkspaceV2BackupError("packed backup checkpoint cursor is invalid")
                    manifest = _stream_manifest(
                        workspace_id=inspection.workspace_id,
                        counts=_counts(inspection),
                        inventory_path=inventory_path,
                        journal_path=incomplete / _PACKED_JOURNAL_NAME,
                        created_at=self._now(),
                    )
                    _write_packed_manifest(incomplete / _PACKED_MANIFEST_NAME, manifest)
                    os.replace(incomplete, destination_root)
                    return manifest
                _write_packed_volumes_stream(
                    source_root,
                    incomplete,
                    checkpoint,
                    inventory_path=inventory_path,
                    inventory=inventory,
                    inventory_digest=inventory_digest,
                    volume_bytes=volume_bytes,
                    fragment_bytes=fragment_bytes,
                    available_bytes=capacity_reader,
                    reserve_bytes=reserve_bytes,
                )
                manifest = _stream_manifest(
                    workspace_id=inspection.workspace_id,
                    counts=_counts(inspection),
                    inventory_path=inventory_path,
                    journal_path=incomplete / _PACKED_JOURNAL_NAME,
                    created_at=self._now(),
                )
                _write_packed_manifest(incomplete / _PACKED_MANIFEST_NAME, manifest)
            os.replace(incomplete, destination_root)
            return manifest
        except WorkspaceV2BackupError:
            raise
        except (OSError, StorageError, ValueError) as error:
            raise WorkspaceV2BackupError(
                "packed workspace v2 backup could not be completed"
            ) from error

    def restore_packed(
        self,
        backup: Path,
        destination: Path,
        *,
        available_bytes: Callable[[], int] | None = None,
        reserve_bytes: int = _MINIMUM_RESERVE_BYTES,
    ) -> WorkspaceInspection:
        """Restore from verified volumes, retaining an owned partial restore on failure."""
        destination_root = destination.expanduser().resolve(strict=False)
        backup_root = backup.expanduser().resolve()
        if destination_root.is_symlink():
            raise WorkspaceV2BackupError("packed restore paths cannot be symbolic links")
        if destination_root.exists() and any(destination_root.iterdir()):
            raise WorkspaceV2BackupError("packed restore destination must be empty")
        if backup_root == destination_root or backup_root in destination_root.parents:
            raise WorkspaceV2BackupError("packed restore destination must be outside backup")
        if available_bytes is None:
            raise WorkspaceV2BackupError("packed restore requires a host capacity reader")
        capacity_reader = available_bytes
        temporary = destination_root.with_name(f".{destination_root.name}.incomplete")
        checkpoint_path = destination_root.with_name(
            f".{destination_root.name}{_PACKED_RESTORE_CHECKPOINT_SUFFIX}"
        )
        # A resume may reuse confirmed target bytes, never unverified backup
        # bytes.  ``inspect_packed`` streams the inventory and journal while
        # binding every retained volume to the sealed manifest.
        backup_root, manifest = self.inspect_packed(backup)
        manifest_digest = _sha256(backup_root / _PACKED_MANIFEST_NAME)
        if not temporary.exists() and not checkpoint_path.exists():
            _validate_capacity_reader(capacity_reader, reserve_bytes)
        try:
            if not temporary.exists():
                temporary.mkdir(parents=True)
                _create_required_layout(temporary)
            elif temporary.is_symlink() or not temporary.is_dir():
                raise WorkspaceV2BackupError("packed restore partial destination is unsafe")
            checkpoint = _open_packed_restore_checkpoint(
                checkpoint_path,
                backup_root=backup_root,
                destination_root=destination_root,
                manifest=manifest,
                manifest_sha256=manifest_digest,
            )
            if isinstance(manifest, WorkspaceV2PackedStreamManifest):
                _restore_packed_stream(
                    backup_root,
                    temporary,
                    manifest,
                    checkpoint_path=checkpoint_path,
                    checkpoint=checkpoint,
                    available_bytes=capacity_reader,
                    reserve_bytes=reserve_bytes,
                )
            else:
                _restore_packed_volumes(
                    backup_root,
                    temporary,
                    manifest,
                    checkpoint_path=checkpoint_path,
                    checkpoint=checkpoint,
                    available_bytes=capacity_reader,
                    reserve_bytes=reserve_bytes,
                )
                _verify_restored_packed_sizes(temporary, manifest.files)
            inspection = self._require_v2_inspection(temporary)
            if inspection.workspace_id != manifest.source_workspace_id:
                raise WorkspaceV2BackupError("packed restore identity differs from backup")
            if _counts(inspection) != manifest.counts:
                raise WorkspaceV2BackupError("packed restore counts differ from backup")
            _verify_v2_workspace(
                self._workspace_service,
                temporary,
                expected_counts=manifest.counts,
            )
            if destination_root.exists():
                destination_root.rmdir()
            os.replace(temporary, destination_root)
            checkpoint_path.unlink(missing_ok=True)
            return inspection.model_copy(update={"workspace_root": destination_root})
        except WorkspaceV2BackupError:
            raise
        except (OSError, StorageError, ValueError, WorkspaceV2BackupInventoryError) as error:
            raise WorkspaceV2BackupError(
                "packed workspace v2 restore could not be completed"
            ) from error

    def inspect_packed(
        self, backup: Path
    ) -> tuple[Path, WorkspaceV2PackedBackupManifest | WorkspaceV2PackedStreamManifest]:
        """Read a sealed packed backup before reuse or restore."""
        backup_root = backup.expanduser().resolve()
        if backup_root.is_symlink():
            raise WorkspaceV2BackupError("packed backup path cannot be a symbolic link")
        manifest = _load_packed_manifest(backup_root / _PACKED_MANIFEST_NAME)
        _verify_packed_backup_files(backup_root, manifest)
        return backup_root, manifest

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


def _checked_packed_roots(source: Path, destination: Path) -> tuple[Path, Path]:
    source_path = source.expanduser()
    destination_path = destination.expanduser()
    if source_path.is_symlink() or destination_path.is_symlink():
        raise WorkspaceV2BackupError("packed backup paths cannot be symbolic links")
    source_root = source_path.resolve()
    destination_root = destination_path.resolve(strict=False)
    incomplete = destination_root.with_name(f".{destination_root.name}.incomplete")
    if destination_root.exists() or incomplete.is_symlink():
        raise WorkspaceV2BackupError("packed backup destination already exists or is unsafe")
    if (
        source_root == destination_root
        or source_root in destination_root.parents
        or destination_root in source_root.parents
    ):
        raise WorkspaceV2BackupError("packed backup destination must be outside source")
    return source_root, destination_root


def _validate_capacity_reader(available_bytes: Callable[[], int], reserve_bytes: int) -> None:
    if isinstance(reserve_bytes, bool) or reserve_bytes < 0:
        raise WorkspaceV2BackupError("packed backup capacity reserve is invalid")
    try:
        available = available_bytes()
    except (OSError, ValueError) as error:
        raise WorkspaceV2BackupError("packed backup capacity reader is unavailable") from error
    if isinstance(available, bool) or not isinstance(available, int) or available < 0:
        raise WorkspaceV2BackupError("packed backup capacity reader is invalid")


def _require_capacity(
    available_bytes: Callable[[], int], *, reserve_bytes: int, growth_bytes: int
) -> None:
    try:
        available = available_bytes()
    except (OSError, ValueError) as error:
        raise WorkspaceV2BackupError("packed backup capacity reader is unavailable") from error
    if isinstance(available, bool) or not isinstance(available, int) or available < 0:
        raise WorkspaceV2BackupError("packed backup capacity reader is invalid")
    if available < reserve_bytes + growth_bytes:
        raise WorkspaceV2BackupError("packed backup capacity reserve is insufficient")


def _atomic_json(path: Path, value: dict[str, object]) -> None:
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        temporary.unlink(missing_ok=True)


def _open_packed_checkpoint(
    root: Path,
    *,
    source_workspace_id: UUID,
    inventory_digest: str,
    volume_bytes: int,
    fragment_bytes: int,
) -> dict[str, object]:
    checkpoint_path = root / _PACKED_CHECKPOINT_NAME
    if not root.exists():
        root.mkdir(parents=True)
    if root.is_symlink() or not root.is_dir():
        raise WorkspaceV2BackupError("packed backup checkpoint is missing or unsafe")
    volume_root = root / _PACKED_VOLUME_DIRECTORY
    if not checkpoint_path.exists():
        if volume_root.exists() or (root / _PACKED_JOURNAL_NAME).exists():
            raise WorkspaceV2BackupError("packed backup checkpoint is missing or unsafe")
        volume_root.mkdir()
        _atomic_json(
            checkpoint_path,
            {
                "schema_version": "workspace-v2-packed-backup-checkpoint-v1",
                "source_workspace_id": str(source_workspace_id),
                "inventory_digest": inventory_digest,
                "volume_bytes": volume_bytes,
                "fragment_bytes": fragment_bytes,
                "next_file_index": 0,
                "next_offset_bytes": 0,
                "next_volume_ordinal": 0,
            },
        )
    if root.is_symlink() or not checkpoint_path.is_file() or checkpoint_path.is_symlink():
        raise WorkspaceV2BackupError("packed backup checkpoint is missing or unsafe")
    try:
        checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as error:
        raise WorkspaceV2BackupError("packed backup checkpoint is malformed") from error
    expected = {
        "schema_version": "workspace-v2-packed-backup-checkpoint-v1",
        "source_workspace_id": str(source_workspace_id),
        "inventory_digest": inventory_digest,
        "volume_bytes": volume_bytes,
        "fragment_bytes": fragment_bytes,
    }
    bindings_match = isinstance(checkpoint, dict) and all(
        checkpoint.get(key) == value for key, value in expected.items()
    )
    if not bindings_match:
        raise WorkspaceV2BackupError("packed backup checkpoint binds different inputs")
    for key in ("next_file_index", "next_offset_bytes", "next_volume_ordinal"):
        if isinstance(checkpoint.get(key), bool) or not isinstance(checkpoint.get(key), int):
            raise WorkspaceV2BackupError("packed backup checkpoint cursor is invalid")
    return checkpoint


def _inventory_files(path: Path) -> Iterator[WorkspaceBackupFile]:
    for entry in iter_inventory(path):
        try:
            yield WorkspaceBackupFile.model_validate(entry)
        except ValueError as error:
            raise WorkspaceV2BackupError("packed backup inventory entry is malformed") from error


def _write_packed_volumes_stream(
    source_root: Path,
    root: Path,
    checkpoint: dict[str, object],
    *,
    inventory_path: Path,
    inventory: WorkspaceV2BackupInventorySummary,
    inventory_digest: str,
    volume_bytes: int,
    fragment_bytes: int,
    available_bytes: Callable[[], int] | None,
    reserve_bytes: int,
) -> None:
    """Write one sealed volume at a time without materializing the source inventory."""
    file_index = int(checkpoint["next_file_index"])
    offset = int(checkpoint["next_offset_bytes"])
    ordinal = int(checkpoint["next_volume_ordinal"])
    if file_index < 0 or file_index > inventory.file_count or offset < 0 or ordinal < 0:
        raise WorkspaceV2BackupError("packed backup checkpoint cursor is invalid")
    files = _inventory_files(inventory_path)
    current: WorkspaceBackupFile | None = None
    for index, item in enumerate(files):
        if index == file_index:
            current = item
            break
    if current is None:
        if file_index != inventory.file_count or offset:
            raise WorkspaceV2BackupError("packed backup checkpoint cursor is invalid")
        return
    journal = root / _PACKED_JOURNAL_NAME
    while current is not None:
        if available_bytes is None:
            raise WorkspaceV2BackupError("packed backup requires a capacity reader")
        _require_capacity(
            available_bytes,
            reserve_bytes=reserve_bytes,
            growth_bytes=volume_bytes,
        )
        volume_id = f"volume-{ordinal:08d}"
        relative_volume = f"{_PACKED_VOLUME_DIRECTORY}/{volume_id}.gz"
        temporary = root / _PACKED_VOLUME_DIRECTORY / f".{volume_id}.{uuid4().hex}.tmp"
        fragments: list[WorkspaceV2PackedFragment] = []
        stream_offset = 0
        remaining = volume_bytes
        try:
            with temporary.open("xb") as output:
                with gzip.GzipFile(filename="", mode="wb", fileobj=output, mtime=0) as compressed:
                    while current is not None and remaining > 8:
                        source_file = source_root / current.path
                        if source_file.is_symlink() or not source_file.is_file():
                            raise WorkspaceV2BackupError("packed backup source file is unsafe")
                        if offset > current.size_bytes:
                            raise WorkspaceV2BackupError("packed backup cursor exceeds source file")
                        if offset == current.size_bytes:
                            file_index += 1
                            offset = 0
                            current = next(files, None)
                            continue
                        length = min(
                            fragment_bytes,
                            remaining - 8,
                            current.size_bytes - offset,
                        )
                        with source_file.open("rb") as input_stream:
                            input_stream.seek(offset)
                            payload = input_stream.read(length)
                        if len(payload) != length:
                            raise WorkspaceV2BackupError(
                                "packed backup source file changed while reading"
                            )
                        frame = struct.pack(">Q", len(payload)) + payload
                        compressed.write(frame)
                        fragments.append(
                            WorkspaceV2PackedFragment(
                                path=current.path,
                                offset_bytes=offset,
                                length_bytes=len(payload),
                                sha256=hashlib.sha256(payload).hexdigest(),
                                stream_offset_bytes=stream_offset,
                            )
                        )
                        stream_offset += len(frame)
                        remaining -= len(frame)
                        offset += len(payload)
                        if offset == current.size_bytes:
                            file_index += 1
                            offset = 0
                            current = next(files, None)
                output.flush()
                os.fsync(output.fileno())
            if not fragments:
                raise WorkspaceV2BackupError("packed backup volume made no progress")
            final = root / relative_volume
            os.replace(temporary, final)
            volume = WorkspaceV2PackedVolume(
                volume_id=volume_id,
                path=relative_volume,
                compressed_bytes=final.stat().st_size,
                decompressed_bytes=stream_offset,
                compressed_sha256=_sha256(final),
                decompressed_sha256=_gzip_uncompressed_sha256(final),
                fragments=tuple(fragments),
            )
            _append_packed_volume(journal, volume)
            ordinal += 1
            _atomic_json(
                root / _PACKED_CHECKPOINT_NAME,
                {
                    "schema_version": "workspace-v2-packed-backup-checkpoint-v1",
                    "source_workspace_id": checkpoint["source_workspace_id"],
                    "inventory_digest": inventory_digest,
                    "volume_bytes": volume_bytes,
                    "fragment_bytes": fragment_bytes,
                    "next_file_index": file_index,
                    "next_offset_bytes": offset,
                    "next_volume_ordinal": ordinal,
                },
            )
        finally:
            temporary.unlink(missing_ok=True)


def _write_packed_volumes_legacy(
    source_root: Path,
    root: Path,
    files: tuple[WorkspaceBackupFile, ...],
    checkpoint: dict[str, object],
    *,
    inventory_digest: str,
    volume_bytes: int,
    fragment_bytes: int,
    available_bytes: Callable[[], int] | None,
    reserve_bytes: int,
) -> None:
    file_index = int(checkpoint["next_file_index"])
    offset = int(checkpoint["next_offset_bytes"])
    ordinal = int(checkpoint["next_volume_ordinal"])
    journal = root / _PACKED_JOURNAL_NAME
    while file_index < len(files):
        if available_bytes is None:
            raise WorkspaceV2BackupError("packed backup requires a capacity reader")
        _require_capacity(
            available_bytes,
            reserve_bytes=reserve_bytes,
            growth_bytes=volume_bytes,
        )
        volume_id = f"volume-{ordinal:08d}"
        relative_volume = f"{_PACKED_VOLUME_DIRECTORY}/{volume_id}.gz"
        temporary = root / _PACKED_VOLUME_DIRECTORY / f".{volume_id}.{uuid4().hex}.tmp"
        fragments: list[WorkspaceV2PackedFragment] = []
        stream_offset = 0
        remaining = volume_bytes
        try:
            with temporary.open("xb") as output:
                with gzip.GzipFile(filename="", mode="wb", fileobj=output, mtime=0) as compressed:
                    while file_index < len(files) and remaining > 8:
                        item = files[file_index]
                        source_file = source_root / item.path
                        if source_file.is_symlink() or not source_file.is_file():
                            raise WorkspaceV2BackupError("packed backup source file is unsafe")
                        if offset > item.size_bytes:
                            raise WorkspaceV2BackupError("packed backup cursor exceeds source file")
                        if offset == item.size_bytes:
                            file_index += 1
                            offset = 0
                            continue
                        length = min(fragment_bytes, remaining - 8, item.size_bytes - offset)
                        with source_file.open("rb") as input_stream:
                            input_stream.seek(offset)
                            payload = input_stream.read(length)
                        if len(payload) != length:
                            raise WorkspaceV2BackupError(
                                "packed backup source file changed while reading"
                            )
                        frame = struct.pack(">Q", len(payload)) + payload
                        compressed.write(frame)
                        fragments.append(
                            WorkspaceV2PackedFragment(
                                path=item.path,
                                offset_bytes=offset,
                                length_bytes=len(payload),
                                sha256=hashlib.sha256(payload).hexdigest(),
                                stream_offset_bytes=stream_offset,
                            )
                        )
                        stream_offset += len(frame)
                        remaining -= len(frame)
                        offset += len(payload)
                        if offset == item.size_bytes:
                            file_index += 1
                            offset = 0
                output.flush()
                os.fsync(output.fileno())
            if not fragments:
                raise WorkspaceV2BackupError("packed backup volume made no progress")
            final = root / relative_volume
            os.replace(temporary, final)
            volume = WorkspaceV2PackedVolume(
                volume_id=volume_id,
                path=relative_volume,
                compressed_bytes=final.stat().st_size,
                decompressed_bytes=stream_offset,
                compressed_sha256=_sha256(final),
                decompressed_sha256=_gzip_uncompressed_sha256(final),
                fragments=tuple(fragments),
            )
            _append_packed_volume(journal, volume)
            ordinal += 1
            _atomic_json(
                root / _PACKED_CHECKPOINT_NAME,
                {
                    "schema_version": "workspace-v2-packed-backup-checkpoint-v1",
                    "source_workspace_id": checkpoint["source_workspace_id"],
                    "inventory_digest": inventory_digest,
                    "volume_bytes": volume_bytes,
                    "fragment_bytes": fragment_bytes,
                    "next_file_index": file_index,
                    "next_offset_bytes": offset,
                    "next_volume_ordinal": ordinal,
                },
            )
        finally:
            temporary.unlink(missing_ok=True)


def _append_packed_volume(path: Path, volume: WorkspaceV2PackedVolume) -> None:
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        document = json.dumps(volume.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        handle.write(document)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())


def _iter_packed_journal(
    path: Path, *, digest: _ByteDigest | None = None
) -> Iterator[WorkspaceV2PackedVolume]:
    """Validate and yield the sealed journal one volume at a time."""
    if path.is_symlink() or not path.is_file():
        raise WorkspaceV2BackupError("packed backup volume journal is missing or unsafe")
    expected_ordinal = 0
    try:
        stream = path.open("rb")
    except (OSError, UnicodeError, ValueError) as error:
        raise WorkspaceV2BackupError("packed backup volume journal is malformed") from error
    with stream:
        for raw_line in stream:
            if digest is not None:
                digest.update(raw_line)
            if not raw_line.strip() or not raw_line.endswith(b"\n"):
                raise WorkspaceV2BackupError("packed backup volume journal is incomplete")
            try:
                volume = WorkspaceV2PackedVolume.model_validate_json(raw_line)
            except (UnicodeError, ValueError) as error:
                raise WorkspaceV2BackupError("packed backup volume journal is malformed") from error
            if volume.volume_id != f"volume-{expected_ordinal:08d}":
                raise WorkspaceV2BackupError("packed backup volume journal is incomplete")
            expected_ordinal += 1
            yield volume
    if expected_ordinal == 0:
        raise WorkspaceV2BackupError("packed backup volume journal is incomplete")


def _iter_packed_volumes(path: Path) -> Iterator[WorkspaceV2PackedVolume]:
    yield from _iter_packed_journal(path)


def _read_packed_volumes(path: Path) -> tuple[WorkspaceV2PackedVolume, ...]:
    """Compatibility materialization for the legacy packed manifest reader."""
    return tuple(_iter_packed_volumes(path))


def _packed_journal_binding(path: Path) -> tuple[int, str]:
    """Bind a journal's physical bytes while validating it incrementally."""
    digest = hashlib.sha256()
    count = sum(1 for _ in _iter_packed_journal(path, digest=digest))
    return count, digest.hexdigest()


def _stream_manifest(
    *,
    workspace_id: UUID,
    counts: WorkspaceBackupCounts,
    inventory_path: Path,
    journal_path: Path,
    created_at: datetime,
) -> WorkspaceV2PackedStreamManifest:
    """Build only bounded metadata after independently consuming both sidecars."""
    inventory = verify_inventory(inventory_path)
    volume_count, journal_sha256 = _packed_journal_binding(journal_path)
    backup_id = uuid5(
        NAMESPACE_URL,
        json.dumps(
            {
                "workspace_id": str(workspace_id),
                "counts": counts.model_dump(mode="json"),
                "inventory_digest": inventory.digest_sha256,
                "journal_sha256": journal_sha256,
                "manifest_schema": "workspace-v2-backup-manifest-v3",
            },
            sort_keys=True,
            separators=(",", ":"),
        ),
    )
    return WorkspaceV2PackedStreamManifest(
        backup_id=backup_id,
        source_workspace_id=workspace_id,
        created_at=created_at,
        counts=counts,
        inventory=inventory,
        journal_sha256=journal_sha256,
        volume_count=volume_count,
    )


def _packed_backup_id(
    workspace_id: UUID,
    files: tuple[WorkspaceBackupFile, ...],
    counts: WorkspaceBackupCounts,
) -> UUID:
    return uuid5(
        NAMESPACE_URL,
        json.dumps(
            {
                "workspace_id": str(workspace_id),
                "files": [item.model_dump(mode="json") for item in files],
                "counts": counts.model_dump(mode="json"),
                "manifest_schema": "workspace-v2-backup-manifest-v2",
            },
            sort_keys=True,
            separators=(",", ":"),
        ),
    )


def _write_packed_manifest(
    path: Path, manifest: WorkspaceV2PackedBackupManifest | WorkspaceV2PackedStreamManifest
) -> None:
    _atomic_json(path, manifest.to_json_dict())


def _load_packed_manifest(
    path: Path,
) -> WorkspaceV2PackedBackupManifest | WorkspaceV2PackedStreamManifest:
    if path.is_symlink() or not path.is_file():
        raise WorkspaceV2BackupError("packed backup manifest is missing or unsafe")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as error:
        raise WorkspaceV2BackupError("packed backup manifest is malformed") from error
    if not isinstance(payload, dict):
        raise WorkspaceV2BackupError("packed backup manifest is malformed")
    try:
        document = path.read_text(encoding="utf-8")
        if payload.get("schema_version") == "workspace-v2-backup-manifest-v3":
            return WorkspaceV2PackedStreamManifest.model_validate_json(document)
        return WorkspaceV2PackedBackupManifest.model_validate_json(document)
    except ValueError as error:
        raise WorkspaceV2BackupError("packed backup manifest is malformed") from error


def _gzip_uncompressed_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with gzip.open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(_PACKED_CHUNK_BYTES), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _verify_packed_backup_files(
    root: Path, manifest: WorkspaceV2PackedBackupManifest | WorkspaceV2PackedStreamManifest
) -> None:
    _reject_symlinks(root)
    if isinstance(manifest, WorkspaceV2PackedStreamManifest):
        _verify_packed_stream_sidecars(root, manifest)
        for volume in _iter_packed_volumes(root / _PACKED_JOURNAL_NAME):
            path = root / volume.path
            if (
                path.is_symlink()
                or not path.is_file()
                or path.stat().st_size != volume.compressed_bytes
                or _sha256(path) != volume.compressed_sha256
            ):
                raise WorkspaceV2BackupError("packed backup volume verification failed")
        return
    for volume in manifest.volumes:
        path = root / volume.path
        if (
            path.is_symlink()
            or not path.is_file()
            or path.stat().st_size != volume.compressed_bytes
            or _sha256(path) != volume.compressed_sha256
        ):
            raise WorkspaceV2BackupError("packed backup volume verification failed")


def _verify_packed_stream_sidecars(root: Path, manifest: WorkspaceV2PackedStreamManifest) -> None:
    _reject_symlinks(root)
    inventory = verify_inventory(root / _PACKED_INVENTORY_NAME)
    count, digest = _packed_journal_binding(root / _PACKED_JOURNAL_NAME)
    if (
        inventory != manifest.inventory
        or count != manifest.volume_count
        or digest != manifest.journal_sha256
    ):
        raise WorkspaceV2BackupError("packed backup sidecar binding differs from manifest")


def _open_packed_restore_checkpoint(
    path: Path,
    *,
    backup_root: Path,
    destination_root: Path,
    manifest: WorkspaceV2PackedBackupManifest | WorkspaceV2PackedStreamManifest,
    manifest_sha256: str,
) -> dict[str, object]:
    expected = {
        "schema_version": "workspace-v2-packed-restore-checkpoint-v1",
        "backup_id": str(manifest.backup_id),
        "source_workspace_id": str(manifest.source_workspace_id),
        "backup_root": str(backup_root),
        "destination_root": str(destination_root),
        "manifest_sha256": manifest_sha256,
    }
    if not path.exists():
        checkpoint = {
            **expected,
            "next_volume_ordinal": 0,
            "next_fragment_ordinal": 0,
        }
        _atomic_json(path, checkpoint)
        return checkpoint
    if path.is_symlink() or not path.is_file():
        raise WorkspaceV2BackupError("packed restore checkpoint is missing or unsafe")
    try:
        checkpoint = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as error:
        raise WorkspaceV2BackupError("packed restore checkpoint is malformed") from error
    if not isinstance(checkpoint, dict) or any(
        checkpoint.get(key) != value for key, value in expected.items() if key != "manifest_sha256"
    ):
        raise WorkspaceV2BackupError("packed restore checkpoint binds different inputs")
    if checkpoint.get("manifest_sha256") is None:
        checkpoint = {**checkpoint, "manifest_sha256": manifest_sha256}
        _atomic_json(path, checkpoint)
    elif checkpoint.get("manifest_sha256") != manifest_sha256:
        raise WorkspaceV2BackupError("packed restore checkpoint binds different inputs")
    for key in ("next_volume_ordinal", "next_fragment_ordinal"):
        if isinstance(checkpoint.get(key), bool) or not isinstance(checkpoint.get(key), int):
            raise WorkspaceV2BackupError("packed restore checkpoint cursor is invalid")
    return checkpoint


def _write_packed_restore_checkpoint(
    path: Path,
    checkpoint: dict[str, object],
    *,
    next_volume_ordinal: int,
    next_fragment_ordinal: int,
) -> dict[str, object]:
    updated = {
        **checkpoint,
        "next_volume_ordinal": next_volume_ordinal,
        "next_fragment_ordinal": next_fragment_ordinal,
    }
    _atomic_json(path, updated)
    return updated


def _restore_prefix(
    manifest: WorkspaceV2PackedBackupManifest,
    *,
    volume_ordinal: int,
    fragment_ordinal: int,
) -> dict[str, int]:
    if volume_ordinal < 0 or volume_ordinal > len(manifest.volumes):
        raise WorkspaceV2BackupError("packed restore checkpoint volume is invalid")
    if volume_ordinal == len(manifest.volumes):
        if fragment_ordinal != 0:
            raise WorkspaceV2BackupError("packed restore checkpoint fragment is invalid")
    elif fragment_ordinal < 0 or fragment_ordinal > len(manifest.volumes[volume_ordinal].fragments):
        raise WorkspaceV2BackupError("packed restore checkpoint fragment is invalid")
    written: dict[str, int] = {}
    for index, volume in enumerate(manifest.volumes):
        limit = len(volume.fragments)
        if index == volume_ordinal:
            limit = fragment_ordinal
        elif index > volume_ordinal:
            break
        for fragment in volume.fragments[:limit]:
            if written.get(fragment.path, 0) != fragment.offset_bytes:
                raise WorkspaceV2BackupError("packed backup fragment order is invalid")
            written[fragment.path] = fragment.offset_bytes + fragment.length_bytes
    return written


def _verify_restore_prefix(
    destination: Path,
    files: tuple[WorkspaceBackupFile, ...],
    written: dict[str, int],
) -> None:
    expected = {item.path: item for item in files}
    if set(written).difference(expected):
        raise WorkspaceV2BackupError("packed restore checkpoint has unexpected file")
    for path, offset in written.items():
        target = destination / path
        if target.is_symlink() or not target.is_file() or target.stat().st_size != offset:
            raise WorkspaceV2BackupError("packed restore partial file differs from checkpoint")


def _verify_restored_packed_sizes(
    destination: Path,
    files: tuple[WorkspaceBackupFile, ...],
) -> None:
    for item in files:
        target = destination / item.path
        if item.size_bytes == 0 and not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            target.touch(exist_ok=False)
        if target.is_symlink() or not target.is_file() or target.stat().st_size != item.size_bytes:
            raise WorkspaceV2BackupError("packed restore file sizes differ from backup")


def _restore_packed_volumes(
    backup_root: Path,
    destination: Path,
    manifest: WorkspaceV2PackedBackupManifest,
    *,
    checkpoint_path: Path,
    checkpoint: dict[str, object],
    available_bytes: Callable[[], int],
    reserve_bytes: int,
) -> None:
    expected = {item.path: item for item in manifest.files}
    next_volume = int(checkpoint["next_volume_ordinal"])
    next_fragment = int(checkpoint["next_fragment_ordinal"])
    written = _restore_prefix(
        manifest,
        volume_ordinal=next_volume,
        fragment_ordinal=next_fragment,
    )
    _verify_restore_prefix(destination, manifest.files, written)
    for volume_index, volume in enumerate(manifest.volumes):
        if volume_index < next_volume:
            continue
        _require_capacity(
            available_bytes,
            reserve_bytes=reserve_bytes,
            growth_bytes=volume.decompressed_bytes,
        )
        stream_offset = 0
        with gzip.open(backup_root / volume.path, "rb") as stream:
            for fragment_index, fragment in enumerate(volume.fragments):
                length_bytes = stream.read(8)
                if (
                    len(length_bytes) != 8
                    or struct.unpack(">Q", length_bytes)[0] != fragment.length_bytes
                ):
                    raise WorkspaceV2BackupError("packed backup fragment length is invalid")
                payload = stream.read(fragment.length_bytes)
                if (
                    len(payload) != fragment.length_bytes
                    or hashlib.sha256(payload).hexdigest() != fragment.sha256
                ):
                    raise WorkspaceV2BackupError("packed backup fragment hash is invalid")
                if fragment.stream_offset_bytes != stream_offset or fragment.path not in expected:
                    raise WorkspaceV2BackupError("packed backup fragment mapping is invalid")
                if written.get(fragment.path, 0) != fragment.offset_bytes:
                    raise WorkspaceV2BackupError("packed backup fragment order is invalid")
                stream_offset += 8 + fragment.length_bytes
                if volume_index == next_volume and fragment_index < next_fragment:
                    continue
                target = destination / fragment.path
                target.parent.mkdir(parents=True, exist_ok=True)
                with target.open("ab") as output:
                    output.write(payload)
                    output.flush()
                    os.fsync(output.fileno())
                written[fragment.path] = fragment.offset_bytes + fragment.length_bytes
                if fragment_index + 1 == len(volume.fragments):
                    checkpoint = _write_packed_restore_checkpoint(
                        checkpoint_path,
                        checkpoint,
                        next_volume_ordinal=volume_index + 1,
                        next_fragment_ordinal=0,
                    )
                else:
                    checkpoint = _write_packed_restore_checkpoint(
                        checkpoint_path,
                        checkpoint,
                        next_volume_ordinal=volume_index,
                        next_fragment_ordinal=fragment_index + 1,
                    )
            if stream.read(1):
                raise WorkspaceV2BackupError("packed backup volume has undeclared payload")


def _stream_inventory_entry_path(entry: dict[str, object]) -> tuple[str, int, str]:
    """Return a safe, validated inventory entry without retaining the inventory."""
    relative = Path(str(entry["path"]))
    if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
        raise WorkspaceV2BackupError("packed backup inventory path is unsafe")
    size = entry["size_bytes"]
    digest = entry["sha256"]
    if (
        isinstance(size, bool)
        or not isinstance(size, int)
        or size < 0
        or not isinstance(digest, str)
        or len(digest) != 64
    ):
        raise WorkspaceV2BackupError("packed backup inventory entry is malformed")
    return relative.as_posix(), size, digest


def _stream_next_data_entry(
    entries: Iterator[dict[str, object]], destination: Path
) -> tuple[str, int, str] | None:
    """Advance through empty files, then yield one non-empty expected file."""
    for entry in entries:
        path, size, digest = _stream_inventory_entry_path(entry)
        target = destination / path
        if size:
            return path, size, digest
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            if target.is_symlink() or not target.is_file() or target.stat().st_size != 0:
                raise WorkspaceV2BackupError("packed restore empty file differs from backup")
        else:
            target.touch(exist_ok=False)
    return None


def _iter_restore_regular_files(root: Path) -> Iterator[Path]:
    for parent, directories, filenames in os.walk(root, topdown=True, followlinks=False):
        directories.sort()
        filenames.sort()
        directory = Path(parent)
        if directory.is_symlink() or any((directory / name).is_symlink() for name in directories):
            raise WorkspaceV2BackupError("packed restore contains a symbolic link")
        for name in filenames:
            yield directory / name


def _restored_file_matches(expected: tuple[Path, int, str]) -> bool:
    path, size, digest = expected
    return path.stat().st_size == size and _sha256(path) == digest


def _verify_streamed_restore_inventory(
    destination: Path,
    inventory_path: Path,
    *,
    checkpoint_path: Path,
    checkpoint: dict[str, object],
) -> None:
    """Compare restored paths incrementally and retain a durable verification cursor."""
    verified = checkpoint.get("verification_file_index", 0)
    complete = checkpoint.get("verification_complete", False)
    if isinstance(verified, bool) or not isinstance(verified, int) or verified < 0:
        raise WorkspaceV2BackupError("packed restore verification checkpoint is invalid")
    if not isinstance(complete, bool):
        raise WorkspaceV2BackupError("packed restore verification checkpoint is invalid")
    expected = iter_inventory(inventory_path)
    actual_paths = _iter_restore_regular_files(destination)
    count = 0
    pending: list[tuple[Path, int, str]] = []
    with ThreadPoolExecutor(max_workers=8) as executor:
        for ordinal, entry in enumerate(expected):
            relative, size, digest = _stream_inventory_entry_path(entry)
            try:
                actual = next(actual_paths)
            except StopIteration as error:
                raise WorkspaceV2BackupError(
                    "packed restore is missing an inventory file"
                ) from error
            if (
                actual.is_symlink()
                or not actual.is_file()
                or actual.relative_to(destination).as_posix() != relative
            ):
                raise WorkspaceV2BackupError(
                    "packed restore inventory coverage differs from backup"
                )
            if ordinal >= verified:
                pending.append((actual, size, digest))
            if len(pending) == 256:
                if not all(executor.map(_restored_file_matches, pending)):
                    raise WorkspaceV2BackupError("packed restore file hash differs from backup")
                pending.clear()
            count = ordinal + 1
            if not complete and count > verified and count % 4096 == 0:
                if pending and not all(executor.map(_restored_file_matches, pending)):
                    raise WorkspaceV2BackupError("packed restore file hash differs from backup")
                pending.clear()
                checkpoint = {
                    **checkpoint,
                    "verification_file_index": count,
                    "verification_complete": False,
                }
                _atomic_json(checkpoint_path, checkpoint)
                verified = count
        if pending and not all(executor.map(_restored_file_matches, pending)):
            raise WorkspaceV2BackupError("packed restore file hash differs from backup")
    try:
        next(actual_paths)
    except StopIteration:
        if complete:
            return
        _atomic_json(
            checkpoint_path,
            {
                **checkpoint,
                "verification_file_index": count,
                "verification_complete": True,
            },
        )
        return
    raise WorkspaceV2BackupError("packed restore has an undeclared file")


def _restore_packed_stream(
    backup_root: Path,
    destination: Path,
    manifest: WorkspaceV2PackedStreamManifest,
    *,
    checkpoint_path: Path,
    checkpoint: dict[str, object],
    available_bytes: Callable[[], int],
    reserve_bytes: int,
) -> None:
    """Restore a v3 packed backup with one journal volume and inventory entry in memory."""
    next_volume = int(checkpoint["next_volume_ordinal"])
    next_fragment = int(checkpoint["next_fragment_ordinal"])
    if next_volume < 0 or next_fragment < 0:
        raise WorkspaceV2BackupError("packed restore checkpoint cursor is invalid")
    entries = iter_inventory(backup_root / _PACKED_INVENTORY_NAME)
    current = _stream_next_data_entry(entries, destination)
    current_offset = 0
    final_checkpoint = checkpoint
    seen_volumes = 0
    for volume_index, volume in enumerate(_iter_packed_volumes(backup_root / _PACKED_JOURNAL_NAME)):
        seen_volumes += 1
        if volume_index < next_volume:
            fragment_limit = len(volume.fragments)
        elif volume_index == next_volume:
            fragment_limit = next_fragment
            if fragment_limit > len(volume.fragments):
                raise WorkspaceV2BackupError("packed restore checkpoint fragment is invalid")
        else:
            fragment_limit = 0
        if fragment_limit < 0:
            raise WorkspaceV2BackupError("packed restore checkpoint fragment is invalid")
        if volume_index < next_volume:
            stream_offset = 0
            for fragment in volume.fragments:
                if fragment.stream_offset_bytes != stream_offset or current is None:
                    raise WorkspaceV2BackupError("packed backup fragment mapping is invalid")
                path, size, _ = current
                if fragment.path != path or fragment.offset_bytes != current_offset:
                    raise WorkspaceV2BackupError("packed backup fragment order is invalid")
                if fragment.length_bytes > size - current_offset:
                    raise WorkspaceV2BackupError("packed backup fragment exceeds inventory file")
                stream_offset += 8 + fragment.length_bytes
                current_offset += fragment.length_bytes
                if current_offset == size:
                    current = _stream_next_data_entry(entries, destination)
                    current_offset = 0
            if stream_offset != volume.decompressed_bytes:
                raise WorkspaceV2BackupError("packed backup volume payload differs from journal")
            continue
        if fragment_limit < len(volume.fragments):
            _require_capacity(
                available_bytes,
                reserve_bytes=reserve_bytes,
                growth_bytes=volume.decompressed_bytes,
            )
        volume_path = backup_root / volume.path
        if (
            volume_path.is_symlink()
            or not volume_path.is_file()
            or volume_path.stat().st_size != volume.compressed_bytes
            or _sha256(volume_path) != volume.compressed_sha256
        ):
            raise WorkspaceV2BackupError("packed backup volume verification failed")
        stream_offset = 0
        decompressed_digest = hashlib.sha256()
        with gzip.open(volume_path, "rb") as stream:
            for fragment_index, fragment in enumerate(volume.fragments):
                length_bytes = stream.read(8)
                if (
                    len(length_bytes) != 8
                    or struct.unpack(">Q", length_bytes)[0] != fragment.length_bytes
                ):
                    raise WorkspaceV2BackupError("packed backup fragment length is invalid")
                payload = stream.read(fragment.length_bytes)
                if (
                    len(payload) != fragment.length_bytes
                    or hashlib.sha256(payload).hexdigest() != fragment.sha256
                ):
                    raise WorkspaceV2BackupError("packed backup fragment hash is invalid")
                decompressed_digest.update(length_bytes)
                decompressed_digest.update(payload)
                if fragment.stream_offset_bytes != stream_offset or current is None:
                    raise WorkspaceV2BackupError("packed backup fragment mapping is invalid")
                path, size, _ = current
                if fragment.path != path or fragment.offset_bytes != current_offset:
                    raise WorkspaceV2BackupError("packed backup fragment order is invalid")
                if fragment.length_bytes > size - current_offset:
                    raise WorkspaceV2BackupError("packed backup fragment exceeds inventory file")
                target = destination / path
                if fragment_index < fragment_limit:
                    if (
                        target.is_symlink()
                        or not target.is_file()
                        or target.stat().st_size < current_offset + fragment.length_bytes
                    ):
                        raise WorkspaceV2BackupError(
                            "packed restore partial file differs from checkpoint"
                        )
                    with target.open("rb") as prior:
                        prior.seek(current_offset)
                        if prior.read(fragment.length_bytes) != payload:
                            raise WorkspaceV2BackupError(
                                "packed restore partial file differs from checkpoint"
                            )
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    if target.exists() and (target.is_symlink() or not target.is_file()):
                        raise WorkspaceV2BackupError(
                            "packed restore partial file differs from checkpoint"
                        )
                    if not target.exists() and current_offset:
                        raise WorkspaceV2BackupError(
                            "packed restore partial file differs from checkpoint"
                        )
                    existing_size = target.stat().st_size if target.exists() else 0
                    if existing_size > current_offset:
                        reusable = min(
                            existing_size - current_offset,
                            fragment.length_bytes,
                        )
                        with target.open("rb") as prior:
                            prior.seek(current_offset)
                            if prior.read(reusable) != payload[:reusable]:
                                raise WorkspaceV2BackupError(
                                    "packed restore partial file differs from checkpoint"
                                )
                        if existing_size >= current_offset + fragment.length_bytes:
                            reusable = fragment.length_bytes
                        else:
                            with target.open("r+b") as output:
                                output.truncate(current_offset)
                    else:
                        reusable = 0
                    if reusable != fragment.length_bytes:
                        with target.open("ab") as output:
                            output.write(payload)
                            output.flush()
                    should_checkpoint = fragment_index + 1 == len(volume.fragments)
                    if should_checkpoint:
                        os.sync()
                    if should_checkpoint and fragment_index + 1 == len(volume.fragments):
                        final_checkpoint = _write_packed_restore_checkpoint(
                            checkpoint_path,
                            final_checkpoint,
                            next_volume_ordinal=volume_index + 1,
                            next_fragment_ordinal=0,
                        )
                    elif should_checkpoint:
                        final_checkpoint = _write_packed_restore_checkpoint(
                            checkpoint_path,
                            final_checkpoint,
                            next_volume_ordinal=volume_index,
                            next_fragment_ordinal=fragment_index + 1,
                        )
                stream_offset += 8 + fragment.length_bytes
                current_offset += fragment.length_bytes
                if current_offset == size:
                    current = _stream_next_data_entry(entries, destination)
                    current_offset = 0
            if stream.read(1):
                raise WorkspaceV2BackupError("packed backup volume has undeclared payload")
        if (
            stream_offset != volume.decompressed_bytes
            or decompressed_digest.hexdigest() != volume.decompressed_sha256
        ):
            raise WorkspaceV2BackupError("packed backup volume payload differs from journal")
    if seen_volumes != manifest.volume_count or next_volume > seen_volumes:
        raise WorkspaceV2BackupError("packed restore checkpoint volume is invalid")
    if current is not None or current_offset:
        raise WorkspaceV2BackupError("packed backup fragments do not cover the inventory")
    _verify_streamed_restore_inventory(
        destination,
        backup_root / _PACKED_INVENTORY_NAME,
        checkpoint_path=checkpoint_path,
        checkpoint=final_checkpoint,
    )


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


__all__ = [
    "WorkspaceV2BackupError",
    "WorkspaceV2BackupManifest",
    "WorkspaceV2PackedBackupManifest",
    "WorkspaceV2PackedStreamManifest",
    "WorkspaceV2BackupService",
]
