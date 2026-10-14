"""Content-addressed remote manifest planning for sealed workspace-v2 backups."""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

from pydantic import ConfigDict, Field, field_validator, model_validator

from investment_analyst.core.models.base import ContractModel, NonEmptyStr, UTCDateTime
from investment_analyst.workspace.drive_transport import (
    DriveAttempt,
    DriveTransport,
    DriveTransportError,
)
from investment_analyst.workspace.workspace_v2_backup import (
    WorkspaceV2BackupError,
    WorkspaceV2BackupService,
    WorkspaceV2PackedStreamManifest,
    WorkspaceV2PackedVolume,
    _iter_packed_journal,
)
from investment_analyst.workspace.workspace_v2_lifecycle_models import LocalComplementManifest


class WorkspaceV2RemoteError(RuntimeError):
    """A remote manifest cannot prove a complete immutable reconstruction."""


class RemoteBackupObject(ContractModel):
    """One immutable encrypted object represented by its pre-encryption content seal."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    object_key: NonEmptyStr
    sha256: NonEmptyStr
    size_bytes: int = Field(ge=0)

    @field_validator("object_key")
    @classmethod
    def require_safe_key(cls, value: str) -> str:
        path = Path(value)
        if path.is_absolute() or ".." in path.parts or value != path.as_posix():
            raise ValueError("remote object key must be normalized and relative")
        return value

    @field_validator("sha256")
    @classmethod
    def require_digest(cls, value: str) -> str:
        if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
            raise ValueError("remote object sha256 is invalid")
        return value


class RemoteBackupManifest(ContractModel):
    """Small remote control plane; contents remain sealed backup artifacts."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: str = "workspace-v2-remote-backup-manifest-v1"
    backup_id: UUID
    source_workspace_id: UUID
    created_at: UTCDateTime
    configuration_fingerprint: NonEmptyStr
    parent_backup_id: UUID | None = None
    inventory_sha256: NonEmptyStr
    journal_sha256: NonEmptyStr
    objects: tuple[RemoteBackupObject, ...]
    complete: bool = True

    @model_validator(mode="after")
    def require_complete_unique_objects(self) -> RemoteBackupManifest:
        keys = tuple(item.object_key for item in self.objects)
        if (
            not self.complete
            or not keys
            or keys != tuple(sorted(keys))
            or len(set(keys)) != len(keys)
        ):
            raise ValueError("remote manifest objects must be complete, unique, and sorted")
        if self.parent_backup_id == self.backup_id:
            raise ValueError("remote manifest cannot parent itself")
        return self


class RemoteBackupPlan(ContractModel):
    """Exact object delta produced before any remote write."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    manifest: RemoteBackupManifest
    new_objects: tuple[RemoteBackupObject, ...]
    reused_objects: tuple[RemoteBackupObject, ...]
    sources: tuple[RemoteBackupSource, ...]

    @model_validator(mode="after")
    def require_partition(self) -> RemoteBackupPlan:
        all_keys = {item.object_key for item in self.manifest.objects}
        partition = {item.object_key for item in self.new_objects} | {
            item.object_key for item in self.reused_objects
        }
        if all_keys != partition:
            raise ValueError("remote backup object delta does not cover manifest")
        if {item.object.object_key for item in self.sources} != all_keys:
            raise ValueError("remote backup object sources do not cover manifest")
        return self


class RemoteComplementFile(ContractModel):
    """One preserved B path and its immutable remote object."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    path: NonEmptyStr
    object: RemoteBackupObject

    @field_validator("path")
    @classmethod
    def require_safe_path(cls, value: str) -> str:
        path = Path(value)
        if path.is_absolute() or ".." in path.parts or value != path.as_posix():
            raise ValueError("remote complement path must be normalized and relative")
        return value


class RemoteComplementManifest(ContractModel):
    """Sealed remote representation of the B-only operational complement."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: str = "workspace-v2-remote-complement-manifest-v1"
    operation_id: UUID
    source_workspace_id: UUID
    source_fingerprint: NonEmptyStr
    local_manifest_sha256: NonEmptyStr
    created_at: UTCDateTime
    files: tuple[RemoteComplementFile, ...]

    @model_validator(mode="after")
    def require_complete_sorted_files(self) -> RemoteComplementManifest:
        paths = tuple(item.path for item in self.files)
        if not paths or paths != tuple(sorted(paths)) or len(set(paths)) != len(paths):
            raise ValueError("remote complement files must be complete, unique, and sorted")
        if not _valid_digest(self.source_fingerprint) or not _valid_digest(
            self.local_manifest_sha256
        ):
            raise ValueError("remote complement digest is invalid")
        return self


class RemoteComplementCheckpoint(ContractModel):
    """Per-object durable resume cursor for a complement upload."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: str = "workspace-v2-remote-complement-checkpoint-v1"
    operation_id: UUID
    local_manifest_sha256: NonEmptyStr
    confirmed_object_keys: tuple[NonEmptyStr, ...]

    @model_validator(mode="after")
    def require_sorted_keys(self) -> RemoteComplementCheckpoint:
        if self.confirmed_object_keys != tuple(sorted(set(self.confirmed_object_keys))):
            raise ValueError("remote complement checkpoint keys must be unique and sorted")
        if not _valid_digest(self.local_manifest_sha256):
            raise ValueError("remote complement checkpoint digest is invalid")
        return self


class RemoteComplementTransferReceipt(ContractModel):
    """Terminal evidence that every B complement object reached Drive."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: str = "workspace-v2-remote-complement-transfer-receipt-v1"
    operation_id: UUID
    manifest_sha256: NonEmptyStr
    objects_uploaded: int = Field(ge=0)
    bytes_uploaded: int = Field(ge=0)
    objects_reused: int = Field(ge=0)
    bytes_reused: int = Field(ge=0)
    attempts: tuple[DriveAttempt, ...]
    completed_at: UTCDateTime


class RemoteComplementVerificationReceipt(ContractModel):
    """Terminal evidence that the complement can be read back from Drive."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: str = "workspace-v2-remote-complement-verification-receipt-v1"
    operation_id: UUID
    manifest_sha256: NonEmptyStr
    files_verified: int = Field(ge=1)
    bytes_verified: int = Field(ge=0)
    attempts: tuple[DriveAttempt, ...]
    completed_at: UTCDateTime


class RemoteComplementVerificationCheckpoint(ContractModel):
    """Durable object cursor for a read-back verification interrupted between files."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: str = "workspace-v2-remote-complement-verification-checkpoint-v1"
    operation_id: UUID
    manifest_sha256: NonEmptyStr
    confirmed_object_keys: tuple[NonEmptyStr, ...]

    @model_validator(mode="after")
    def require_sorted_keys(self) -> RemoteComplementVerificationCheckpoint:
        if self.confirmed_object_keys != tuple(sorted(set(self.confirmed_object_keys))):
            raise ValueError("remote complement verification keys must be unique and sorted")
        if not _valid_digest(self.manifest_sha256):
            raise ValueError("remote complement verification manifest digest is invalid")
        return self


class RemoteBackupSource(ContractModel):
    """A bounded local source binding for one immutable remote object."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    object: RemoteBackupObject
    relative_path: NonEmptyStr

    @field_validator("relative_path")
    @classmethod
    def require_safe_relative_path(cls, value: str) -> str:
        path = Path(value)
        if path.is_absolute() or ".." in path.parts or value != path.as_posix():
            raise ValueError("remote source path must be normalized and relative")
        return value


class RemoteBackupTransferReceipt(ContractModel):
    """Durable local evidence for a completed, object-addressed remote publish."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: str = "workspace-v2-remote-transfer-receipt-v1"
    backup_id: UUID
    manifest_sha256: NonEmptyStr
    objects_uploaded: int = Field(ge=0)
    bytes_uploaded: int = Field(ge=0)
    objects_reused: int = Field(ge=0)
    bytes_reused: int = Field(ge=0)
    attempts: tuple[DriveAttempt, ...]
    completed_at: UTCDateTime


class RemoteBackupRestoreReceipt(ContractModel):
    """Evidence for an independent Drive restore that consumed sealed volumes."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: str = "workspace-v2-remote-restore-receipt-v1"
    backup_id: UUID
    remote_manifest_sha256: NonEmptyStr
    restored_workspace_id: UUID
    volume_count: int = Field(ge=1)
    downloaded_volumes: int = Field(ge=0)
    downloaded_bytes: int = Field(ge=0)
    completed_at: UTCDateTime


class RemoteBackupManifestReconciliationReceipt(ContractModel):
    """Binds a legacy transfer receipt to the manifest actually readable in Drive."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: str = "workspace-v2-remote-manifest-reconciliation-receipt-v1"
    backup_id: UUID
    transfer_receipt_sha256: NonEmptyStr
    transfer_manifest_sha256: NonEmptyStr
    observed_manifest_sha256: NonEmptyStr
    object_count: int = Field(ge=1)
    completed_at: UTCDateTime


class WorkspaceV2RemoteService:
    """Create and atomically publish remote object plans from sealed local volumes."""

    def __init__(
        self,
        backup_service: WorkspaceV2BackupService,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._backup_service = backup_service
        self._clock = clock

    def plan_base(
        self,
        backup_root: Path,
        *,
        configuration_fingerprint: str,
        parent: RemoteBackupManifest | None = None,
    ) -> RemoteBackupPlan:
        """Stream the sealed journal and produce a content-addressed object delta."""
        if not _valid_digest(configuration_fingerprint):
            raise WorkspaceV2RemoteError("remote configuration fingerprint is invalid")
        try:
            root, manifest = self._backup_service.inspect_packed_for_restore(backup_root)
        except WorkspaceV2BackupError as error:
            raise WorkspaceV2RemoteError("local packed backup is not reusable") from error
        if not isinstance(manifest, WorkspaceV2PackedStreamManifest):
            raise WorkspaceV2RemoteError("remote transfer requires a streamable packed backup")
        control_sources = tuple(_control_objects(root))
        object_map = {item.object.object_key: item.object for item in control_sources}
        source_map = {item.object.object_key: item for item in control_sources}
        journal_path = root / "packed-backup-volumes.jsonl"
        journal_digest = hashlib.sha256()
        try:
            journal_size = journal_path.stat().st_size
        except OSError as error:
            raise WorkspaceV2RemoteError("sealed volume journal is unavailable") from error
        try:
            for volume in _iter_packed_journal(journal_path, digest=journal_digest):
                object_item = RemoteBackupObject(
                    object_key=f"volumes/{volume.compressed_sha256}.gz",
                    sha256=volume.compressed_sha256,
                    size_bytes=volume.compressed_bytes,
                )
                prior = object_map.setdefault(object_item.object_key, object_item)
                if prior != object_item:
                    raise WorkspaceV2RemoteError("content-addressed volume binding conflicts")
                source_map.setdefault(
                    object_item.object_key,
                    RemoteBackupSource(object=object_item, relative_path=volume.path),
                )
        except WorkspaceV2BackupError as error:
            raise WorkspaceV2RemoteError("sealed volume journal is malformed") from error
        if journal_digest.hexdigest() != manifest.journal_sha256:
            raise WorkspaceV2RemoteError("sealed volume journal binding differs from manifest")
        object_map["controls/packed-backup-volumes.jsonl"] = RemoteBackupObject(
            object_key="controls/packed-backup-volumes.jsonl",
            sha256=journal_digest.hexdigest(),
            size_bytes=journal_size,
        )
        source_map["controls/packed-backup-volumes.jsonl"] = RemoteBackupSource(
            object=object_map["controls/packed-backup-volumes.jsonl"],
            relative_path="packed-backup-volumes.jsonl",
        )
        ordered = tuple(sorted(object_map.values(), key=lambda item: item.object_key))
        remote = RemoteBackupManifest(
            backup_id=manifest.backup_id,
            source_workspace_id=manifest.source_workspace_id,
            created_at=self._now(),
            configuration_fingerprint=configuration_fingerprint,
            parent_backup_id=(
                None
                if parent is None or parent.backup_id == manifest.backup_id
                else parent.backup_id
            ),
            inventory_sha256=manifest.inventory.digest_sha256,
            journal_sha256=manifest.journal_sha256,
            objects=ordered,
        )
        previous = {} if parent is None else {item.object_key: item for item in parent.objects}
        reused = tuple(item for item in ordered if previous.get(item.object_key) == item)
        new = tuple(item for item in ordered if previous.get(item.object_key) != item)
        return RemoteBackupPlan(
            manifest=remote,
            new_objects=new,
            reused_objects=reused,
            sources=tuple(sorted(source_map.values(), key=lambda item: item.object.object_key)),
        )

    def upload_base(
        self,
        plan: RemoteBackupPlan,
        backup_root: Path,
        transport: DriveTransport,
        receipt_path: Path,
    ) -> RemoteBackupTransferReceipt:
        """Upload only the delta, then publish the immutable remote manifest last."""
        try:
            root, manifest = self._backup_service.inspect_packed_for_restore(backup_root)
        except WorkspaceV2BackupError as error:
            raise WorkspaceV2RemoteError("local packed backup is not reusable") from error
        if not isinstance(manifest, WorkspaceV2PackedStreamManifest) or (
            manifest.backup_id != plan.manifest.backup_id
            or manifest.source_workspace_id != plan.manifest.source_workspace_id
        ):
            raise WorkspaceV2RemoteError("remote plan no longer binds the local packed backup")
        receipt_target = receipt_path.expanduser().resolve(strict=False)
        if (
            receipt_target.exists()
            or receipt_target.is_symlink()
            or not receipt_target.is_absolute()
        ):
            raise WorkspaceV2RemoteError(
                "remote transfer receipt destination is unavailable or unsafe"
            )
        sources = {item.object.object_key: item for item in plan.sources}
        attempts: list[DriveAttempt] = []
        for object_item in plan.new_objects:
            source = sources[object_item.object_key]
            local_path = _checked_source_path(root, source, object_item)
            try:
                attempts.extend(transport.copy_sealed_object(local_path, object_item.object_key))
            except DriveTransportError as error:
                raise WorkspaceV2RemoteError("remote object upload was not confirmed") from error
        manifest_path = receipt_target.with_name(f".{receipt_target.name}.{uuid4().hex}.manifest")
        try:
            self.publish_manifest(plan, manifest_path)
            manifest_digest = _sha256(manifest_path)
            try:
                attempts.extend(
                    transport.copy_sealed_object(
                        manifest_path,
                        f"manifests/{plan.manifest.backup_id}.json",
                    )
                )
            except DriveTransportError as error:
                raise WorkspaceV2RemoteError("remote manifest upload was not confirmed") from error
            receipt = RemoteBackupTransferReceipt(
                backup_id=plan.manifest.backup_id,
                manifest_sha256=manifest_digest,
                objects_uploaded=len(plan.new_objects),
                bytes_uploaded=sum(item.size_bytes for item in plan.new_objects),
                objects_reused=len(plan.reused_objects),
                bytes_reused=sum(item.size_bytes for item in plan.reused_objects),
                attempts=tuple(attempts),
                completed_at=self._now(),
            )
            _publish_json(receipt_target, receipt.model_dump(mode="json"))
            return receipt
        finally:
            manifest_path.unlink(missing_ok=True)

    def upload_complement(
        self,
        complement_root: Path,
        *,
        transport: DriveTransport,
        checkpoint_path: Path,
        receipt_path: Path,
    ) -> RemoteComplementTransferReceipt:
        """Upload the preserved B-only files with a durable object cursor.

        A success is checkpointed before advancing.  A resumed invocation trusts
        only its own sealed checkpoint and never rebuilds or rewrites B.
        """
        root = complement_root.expanduser().resolve()
        control = root / "local-complement-manifest.json"
        if root.is_symlink() or not root.is_dir() or control.is_symlink() or not control.is_file():
            raise WorkspaceV2RemoteError("local complement is unavailable or unsafe")
        try:
            local = LocalComplementManifest.model_validate_json(control.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError) as error:
            raise WorkspaceV2RemoteError("local complement manifest is malformed") from error
        local_digest = _sha256(control)
        files = tuple(
            RemoteComplementFile(
                path=item.path,
                object=RemoteBackupObject(
                    object_key=f"complement-objects/{item.sha256}",
                    sha256=item.sha256,
                    size_bytes=item.size_bytes,
                ),
            )
            for item in local.files
        )
        manifest = RemoteComplementManifest(
            operation_id=local.source.operation_id,
            source_workspace_id=local.source.workspace_id,
            source_fingerprint=local.source.source_fingerprint,
            local_manifest_sha256=local_digest,
            created_at=self._now(),
            files=files,
        )
        objects = {item.object.object_key: item.object for item in files}
        sources: dict[str, Path] = {}
        for item in files:
            path = _checked_complement_path(root, item)
            sources.setdefault(item.object.object_key, path)
        checkpoint_target = checkpoint_path.expanduser().resolve(strict=False)
        receipt_target = receipt_path.expanduser().resolve(strict=False)
        if (
            not checkpoint_target.is_absolute()
            or checkpoint_target.is_symlink()
            or not receipt_target.is_absolute()
            or receipt_target.exists()
            or receipt_target.is_symlink()
        ):
            raise WorkspaceV2RemoteError("remote complement output is unavailable or unsafe")
        confirmed = _read_complement_checkpoint(checkpoint_target, manifest)
        reused = set(confirmed)
        uploaded: set[str] = set()
        attempts: list[DriveAttempt] = []
        for key in sorted(objects):
            if key in confirmed:
                continue
            try:
                attempts.extend(transport.copy_sealed_object(sources[key], key))
            except DriveTransportError as error:
                raise WorkspaceV2RemoteError(
                    "remote complement upload was not confirmed"
                ) from error
            confirmed.add(key)
            uploaded.add(key)
            _replace_json(
                checkpoint_target,
                RemoteComplementCheckpoint(
                    operation_id=manifest.operation_id,
                    local_manifest_sha256=manifest.local_manifest_sha256,
                    confirmed_object_keys=tuple(sorted(confirmed)),
                ).model_dump(mode="json"),
            )
        manifest_path = receipt_target.with_name(f".{receipt_target.name}.{uuid4().hex}.manifest")
        try:
            _publish_json(manifest_path, manifest.model_dump(mode="json"))
            digest = _sha256(manifest_path)
            try:
                attempts.extend(
                    transport.copy_sealed_object(
                        manifest_path, f"complement-manifests/{manifest.operation_id}.json"
                    )
                )
            except DriveTransportError as error:
                raise WorkspaceV2RemoteError(
                    "remote complement manifest upload was not confirmed"
                ) from error
            receipt = RemoteComplementTransferReceipt(
                operation_id=manifest.operation_id,
                manifest_sha256=digest,
                objects_uploaded=len(uploaded),
                bytes_uploaded=sum(
                    item.size_bytes for key, item in objects.items() if key in uploaded
                ),
                objects_reused=len(reused),
                bytes_reused=sum(item.size_bytes for key, item in objects.items() if key in reused),
                attempts=tuple(attempts),
                completed_at=self._now(),
            )
            _publish_json(receipt_target, receipt.model_dump(mode="json"))
            checkpoint_target.unlink(missing_ok=True)
            return receipt
        finally:
            manifest_path.unlink(missing_ok=True)

    def restore_from_remote(
        self,
        transfer: RemoteBackupTransferReceipt,
        *,
        configuration_fingerprint: str,
        transport: DriveTransport,
        staging_root: Path,
        destination: Path,
        available_bytes: Callable[[], int],
        reserve_bytes: int,
        receipt_path: Path,
        expected_manifest_sha256: str | None = None,
    ) -> RemoteBackupRestoreReceipt:
        """Restore a v3 backup from Drive with one downloaded volume at a time.

        ``staging_root`` contains only remote controls and the current cache
        file.  It never points at the local backup, so a successful restore is
        independent from the source volume tree.
        """
        if not _valid_digest(configuration_fingerprint):
            raise WorkspaceV2RemoteError("remote configuration fingerprint is invalid")
        if reserve_bytes < 0:
            raise WorkspaceV2RemoteError("remote restore reserve is invalid")
        staging = _prepare_staging_root(staging_root)
        target_receipt = receipt_path.expanduser().resolve(strict=False)
        if (
            not target_receipt.is_absolute()
            or target_receipt.exists()
            or target_receipt.is_symlink()
        ):
            raise WorkspaceV2RemoteError("remote restore receipt destination is unavailable")
        manifest_sha256 = expected_manifest_sha256 or transfer.manifest_sha256
        if not _valid_digest(manifest_sha256):
            raise WorkspaceV2RemoteError("remote manifest digest is invalid")
        remote_manifest_path = staging / "remote-backup-manifest.json"
        _fetch_bound_object(
            transport,
            f"manifests/{transfer.backup_id}.json",
            remote_manifest_path,
            expected_sha256=manifest_sha256,
            expected_size=None,
        )
        try:
            remote_manifest = RemoteBackupManifest.model_validate_json(
                remote_manifest_path.read_text(encoding="utf-8")
            )
        except (OSError, UnicodeError, ValueError) as error:
            raise WorkspaceV2RemoteError("remote manifest is malformed") from error
        if (
            remote_manifest.backup_id != transfer.backup_id
            or remote_manifest.configuration_fingerprint != configuration_fingerprint
        ):
            raise WorkspaceV2RemoteError("remote manifest binding differs from transfer receipt")
        controls = {item.object_key: item for item in remote_manifest.objects}
        for remote_key, local_name in (
            ("controls/backup_manifest.json", "backup_manifest.json"),
            ("controls/source-inventory.jsonl", "source-inventory.jsonl"),
            ("controls/packed-backup-volumes.jsonl", "packed-backup-volumes.jsonl"),
        ):
            item = controls.get(remote_key)
            if item is None:
                raise WorkspaceV2RemoteError("remote manifest is missing a restore control")
            _fetch_bound_object(
                transport,
                remote_key,
                staging / local_name,
                expected_sha256=item.sha256,
                expected_size=item.size_bytes,
            )
        try:
            _, packed_manifest = self._backup_service.inspect_packed_for_restore(staging)
        except WorkspaceV2BackupError as error:
            raise WorkspaceV2RemoteError(
                "remote controls do not bind a restorable backup"
            ) from error
        if not isinstance(packed_manifest, WorkspaceV2PackedStreamManifest):
            raise WorkspaceV2RemoteError("remote restore requires a streamable packed backup")
        if (
            packed_manifest.backup_id != remote_manifest.backup_id
            or packed_manifest.source_workspace_id != remote_manifest.source_workspace_id
            or packed_manifest.journal_sha256 != remote_manifest.journal_sha256
            or packed_manifest.inventory.digest_sha256 != remote_manifest.inventory_sha256
        ):
            raise WorkspaceV2RemoteError("remote controls differ from the remote manifest")
        downloaded_volumes = 0
        downloaded_bytes = 0

        def prepare_volume(volume: WorkspaceV2PackedVolume, local_path: Path) -> None:
            nonlocal downloaded_volumes, downloaded_bytes
            key = f"volumes/{volume.compressed_sha256}.gz"
            item = controls.get(key)
            if (
                item is None
                or item.sha256 != volume.compressed_sha256
                or item.size_bytes != volume.compressed_bytes
            ):
                raise WorkspaceV2RemoteError("remote volume binding differs from journal")
            _require_remote_capacity(
                available_bytes,
                reserve_bytes=reserve_bytes,
                growth_bytes=volume.compressed_bytes + volume.decompressed_bytes,
            )
            _fetch_bound_object(
                transport,
                key,
                local_path,
                expected_sha256=None,
                expected_size=volume.compressed_bytes,
            )
            downloaded_volumes += 1
            downloaded_bytes += volume.compressed_bytes

        def release_volume(_volume: WorkspaceV2PackedVolume, local_path: Path) -> None:
            if local_path.is_symlink() or not local_path.is_file():
                raise WorkspaceV2RemoteError("remote restore cache is unavailable or unsafe")
            local_path.unlink()
            _fsync_directory(local_path.parent)

        try:
            restored = self._backup_service.restore_packed(
                staging,
                destination,
                available_bytes=available_bytes,
                reserve_bytes=reserve_bytes,
                prepare_stream_volume=prepare_volume,
                release_stream_volume=release_volume,
                reuse_checkpointed_stream_volumes=True,
            )
        except WorkspaceV2BackupError as error:
            raise WorkspaceV2RemoteError(f"remote restore did not complete: {error}") from error
        result = RemoteBackupRestoreReceipt(
            backup_id=remote_manifest.backup_id,
            remote_manifest_sha256=manifest_sha256,
            restored_workspace_id=restored.workspace_id,
            volume_count=packed_manifest.volume_count,
            downloaded_volumes=downloaded_volumes,
            downloaded_bytes=downloaded_bytes,
            completed_at=self._now(),
        )
        _publish_json(target_receipt, result.model_dump(mode="json"))
        return result

    def verify_complement_from_remote(
        self,
        transfer: RemoteComplementTransferReceipt,
        *,
        transport: DriveTransport,
        staging_root: Path,
        checkpoint_path: Path,
        receipt_path: Path,
    ) -> RemoteComplementVerificationReceipt:
        """Read every preserved complement object once through a bounded cache."""
        staging = _prepare_staging_root(staging_root) / "complement-verification"
        staging.mkdir(exist_ok=True)
        target = receipt_path.expanduser().resolve(strict=False)
        checkpoint_target = checkpoint_path.expanduser().resolve(strict=False)
        if (
            not target.is_absolute()
            or target.exists()
            or target.is_symlink()
            or not checkpoint_target.is_absolute()
            or checkpoint_target.is_symlink()
        ):
            raise WorkspaceV2RemoteError("remote complement receipt destination is unavailable")
        manifest_path = staging / "manifest.json"
        _fetch_bound_object(
            transport,
            f"complement-manifests/{transfer.operation_id}.json",
            manifest_path,
            expected_sha256=transfer.manifest_sha256,
            expected_size=None,
        )
        try:
            manifest = RemoteComplementManifest.model_validate_json(
                manifest_path.read_text(encoding="utf-8")
            )
        except (OSError, UnicodeError, ValueError) as error:
            raise WorkspaceV2RemoteError("remote complement manifest is malformed") from error
        if manifest.operation_id != transfer.operation_id:
            raise WorkspaceV2RemoteError("remote complement manifest binding differs")
        confirmed = _read_complement_verification_checkpoint(checkpoint_target, transfer, manifest)
        attempts: list[DriveAttempt] = []
        cache = staging / "object.bin"
        for item in manifest.files:
            key = item.object.object_key
            if key in confirmed:
                continue
            cache.unlink(missing_ok=True)
            try:
                attempts.extend(transport.fetch_sealed_object(key, cache))
            except DriveTransportError as error:
                raise WorkspaceV2RemoteError(
                    "remote complement download was not confirmed"
                ) from error
            if (
                cache.stat().st_size != item.object.size_bytes
                or _sha256(cache) != item.object.sha256
            ):
                raise WorkspaceV2RemoteError("remote complement object binding differs")
            confirmed.add(key)
            _replace_json(
                checkpoint_target,
                RemoteComplementVerificationCheckpoint(
                    operation_id=manifest.operation_id,
                    manifest_sha256=transfer.manifest_sha256,
                    confirmed_object_keys=tuple(sorted(confirmed)),
                ).model_dump(mode="json"),
            )
            cache.unlink()
            _fsync_directory(cache.parent)
        result = RemoteComplementVerificationReceipt(
            operation_id=manifest.operation_id,
            manifest_sha256=transfer.manifest_sha256,
            files_verified=len(manifest.files),
            bytes_verified=sum(item.object.size_bytes for item in manifest.files),
            attempts=tuple(attempts),
            completed_at=self._now(),
        )
        _publish_json(target, result.model_dump(mode="json"))
        checkpoint_target.unlink(missing_ok=True)
        return result

    def reconcile_remote_manifest(
        self,
        transfer: RemoteBackupTransferReceipt,
        *,
        transfer_receipt_path: Path,
        configuration_fingerprint: str,
        transport: DriveTransport,
        staging_root: Path,
        receipt_path: Path,
    ) -> RemoteBackupManifestReconciliationReceipt:
        """Record an observed legacy manifest before an independent restore.

        This is deliberately not a rewrite of a prior transfer receipt.  It
        preserves the prior claim and binds the actual remote control object
        that a restore will subsequently verify end to end.
        """
        if not _valid_digest(configuration_fingerprint):
            raise WorkspaceV2RemoteError("remote configuration fingerprint is invalid")
        source_receipt = transfer_receipt_path.expanduser().resolve()
        if source_receipt.is_symlink() or not source_receipt.is_file():
            raise WorkspaceV2RemoteError("remote transfer receipt is unavailable or unsafe")
        staging = _prepare_staging_root(staging_root)
        target = staging / "remote-backup-manifest.json"
        if target.exists():
            if target.is_symlink() or not target.is_file():
                raise WorkspaceV2RemoteError(
                    "remote restore staging object is unavailable or unsafe"
                )
            target.unlink()
        try:
            transport.fetch_sealed_object(f"manifests/{transfer.backup_id}.json", target)
        except DriveTransportError as error:
            raise WorkspaceV2RemoteError("remote manifest download was not confirmed") from error
        observed_sha256 = _sha256(target)
        try:
            manifest = RemoteBackupManifest.model_validate_json(target.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError) as error:
            raise WorkspaceV2RemoteError("remote manifest is malformed") from error
        required_controls = {
            "controls/backup_manifest.json",
            "controls/source-inventory.jsonl",
            "controls/packed-backup-volumes.jsonl",
        }
        if (
            manifest.backup_id != transfer.backup_id
            or manifest.configuration_fingerprint != configuration_fingerprint
            or not required_controls.issubset({item.object_key for item in manifest.objects})
        ):
            raise WorkspaceV2RemoteError("observed remote manifest does not bind the transfer")
        result = RemoteBackupManifestReconciliationReceipt(
            backup_id=transfer.backup_id,
            transfer_receipt_sha256=_sha256(source_receipt),
            transfer_manifest_sha256=transfer.manifest_sha256,
            observed_manifest_sha256=observed_sha256,
            object_count=len(manifest.objects),
            completed_at=self._now(),
        )
        _publish_json(receipt_path, result.model_dump(mode="json"))
        return result

    def publish_manifest(self, plan: RemoteBackupPlan, destination: Path) -> RemoteBackupManifest:
        """Atomically publish only a fully planned manifest after object confirmation."""
        target = destination.expanduser().resolve(strict=False)
        if target.exists() or target.is_symlink() or not target.is_absolute():
            raise WorkspaceV2RemoteError("remote manifest destination is unavailable or unsafe")
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{target.name}.{uuid4().hex}.tmp")
        try:
            with temporary.open("x", encoding="utf-8", newline="\n") as stream:
                json.dump(
                    plan.manifest.model_dump(mode="json"),
                    stream,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
            _fsync_directory(target.parent)
        except OSError as error:
            raise WorkspaceV2RemoteError("remote manifest could not be published") from error
        finally:
            temporary.unlink(missing_ok=True)
        return plan.manifest

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise WorkspaceV2RemoteError("remote manifest clock must be timezone-aware")
        return value.astimezone(UTC)


def _control_objects(root: Path) -> Iterable[RemoteBackupSource]:
    for name in ("backup_manifest.json", "source-inventory.jsonl"):
        path = root / name
        if path.is_symlink() or not path.is_file():
            raise WorkspaceV2RemoteError("sealed remote control is missing or unsafe")
        digest = hashlib.sha256()
        size = 0
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
                size += len(chunk)
        object_item = RemoteBackupObject(
            object_key=f"controls/{name}", sha256=digest.hexdigest(), size_bytes=size
        )
        yield RemoteBackupSource(object=object_item, relative_path=name)


def _checked_source_path(
    root: Path, source: RemoteBackupSource, object_item: RemoteBackupObject
) -> Path:
    path = root / source.relative_path
    if (
        source.object != object_item
        or path.is_symlink()
        or not path.is_file()
        or path.stat().st_size != object_item.size_bytes
    ):
        raise WorkspaceV2RemoteError("local remote object binding is unavailable or unsafe")
    return path


def _checked_complement_path(root: Path, item: RemoteComplementFile) -> Path:
    path = (root / item.path).resolve(strict=False)
    if (
        not path.is_relative_to(root)
        or path.is_symlink()
        or not path.is_file()
        or path.stat().st_size != item.object.size_bytes
        or _sha256(path) != item.object.sha256
    ):
        raise WorkspaceV2RemoteError("local complement binding is unavailable or unsafe")
    return path


def _read_complement_checkpoint(
    checkpoint_path: Path, manifest: RemoteComplementManifest
) -> set[str]:
    if not checkpoint_path.exists():
        return set()
    if checkpoint_path.is_symlink() or not checkpoint_path.is_file():
        raise WorkspaceV2RemoteError("remote complement checkpoint is unavailable or unsafe")
    try:
        checkpoint = RemoteComplementCheckpoint.model_validate_json(
            checkpoint_path.read_text(encoding="utf-8")
        )
    except (OSError, UnicodeError, ValueError) as error:
        raise WorkspaceV2RemoteError("remote complement checkpoint is malformed") from error
    valid_keys = {item.object.object_key for item in manifest.files}
    if (
        checkpoint.operation_id != manifest.operation_id
        or checkpoint.local_manifest_sha256 != manifest.local_manifest_sha256
        or not set(checkpoint.confirmed_object_keys).issubset(valid_keys)
    ):
        raise WorkspaceV2RemoteError("remote complement checkpoint binding differs")
    return set(checkpoint.confirmed_object_keys)


def _read_complement_verification_checkpoint(
    checkpoint_path: Path,
    transfer: RemoteComplementTransferReceipt,
    manifest: RemoteComplementManifest,
) -> set[str]:
    if not checkpoint_path.exists():
        return set()
    if checkpoint_path.is_symlink() or not checkpoint_path.is_file():
        raise WorkspaceV2RemoteError("remote complement verification checkpoint is unavailable")
    try:
        checkpoint = RemoteComplementVerificationCheckpoint.model_validate_json(
            checkpoint_path.read_text(encoding="utf-8")
        )
    except (OSError, UnicodeError, ValueError) as error:
        raise WorkspaceV2RemoteError(
            "remote complement verification checkpoint is malformed"
        ) from error
    allowed = {item.object.object_key for item in manifest.files}
    if (
        checkpoint.operation_id != manifest.operation_id
        or checkpoint.manifest_sha256 != transfer.manifest_sha256
        or not set(checkpoint.confirmed_object_keys).issubset(allowed)
    ):
        raise WorkspaceV2RemoteError("remote complement verification checkpoint binding differs")
    return set(checkpoint.confirmed_object_keys)


def _prepare_staging_root(value: Path) -> Path:
    root = value.expanduser().resolve(strict=False)
    if not root.is_absolute() or root.is_symlink():
        raise WorkspaceV2RemoteError("remote restore staging root is unavailable or unsafe")
    if root.exists() and not root.is_dir():
        raise WorkspaceV2RemoteError("remote restore staging root is unavailable or unsafe")
    root.mkdir(parents=True, exist_ok=True)
    volumes = root / "volumes"
    if volumes.is_symlink() or (volumes.exists() and not volumes.is_dir()):
        raise WorkspaceV2RemoteError("remote restore volume cache is unavailable or unsafe")
    volumes.mkdir(exist_ok=True)
    return root


def _fetch_bound_object(
    transport: DriveTransport,
    remote_key: str,
    destination: Path,
    *,
    expected_sha256: str | None,
    expected_size: int | None,
) -> None:
    """Reuse only a local staging object whose declared seal already matches."""
    if expected_sha256 is not None and not _valid_digest(expected_sha256):
        raise WorkspaceV2RemoteError("remote object digest is invalid")
    if expected_size is not None and expected_size < 0:
        raise WorkspaceV2RemoteError("remote object size is invalid")
    target = destination.expanduser().resolve(strict=False)
    if target.is_symlink() or not target.is_absolute() or target.parent.is_symlink():
        raise WorkspaceV2RemoteError("remote restore staging object is unavailable or unsafe")
    if target.exists():
        if not target.is_file():
            raise WorkspaceV2RemoteError("remote restore staging object is unavailable or unsafe")
        size_matches = expected_size is None or target.stat().st_size == expected_size
        digest_matches = expected_sha256 is None or _sha256(target) == expected_sha256
        if size_matches and digest_matches:
            return
        target.unlink()
    try:
        transport.fetch_sealed_object(remote_key, target)
    except DriveTransportError as error:
        raise WorkspaceV2RemoteError("remote object download was not confirmed") from error
    if (
        target.is_symlink()
        or not target.is_file()
        or (expected_size is not None and target.stat().st_size != expected_size)
        or (expected_sha256 is not None and _sha256(target) != expected_sha256)
    ):
        target.unlink(missing_ok=True)
        raise WorkspaceV2RemoteError("remote object binding differs after download")


def _require_remote_capacity(
    available_bytes: Callable[[], int], *, reserve_bytes: int, growth_bytes: int
) -> None:
    try:
        available = available_bytes()
    except (OSError, ValueError) as error:
        raise WorkspaceV2RemoteError("remote restore capacity reader is unavailable") from error
    if (
        isinstance(available, bool)
        or not isinstance(available, int)
        or available < 0
        or available < reserve_bytes + growth_bytes
    ):
        raise WorkspaceV2RemoteError("remote restore capacity reserve is insufficient")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _publish_json(destination: Path, document: dict[str, object]) -> None:
    target = destination.expanduser().resolve(strict=False)
    if target.exists() or target.is_symlink() or not target.is_absolute():
        raise WorkspaceV2RemoteError("remote transfer receipt destination is unavailable or unsafe")
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(document, stream, sort_keys=True, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
        _fsync_directory(target.parent)
    except OSError as error:
        raise WorkspaceV2RemoteError("remote transfer receipt could not be published") from error
    finally:
        temporary.unlink(missing_ok=True)


def _replace_json(destination: Path, document: dict[str, object]) -> None:
    """Atomically replace an owned checkpoint after a confirmed unit."""
    target = destination.expanduser().resolve(strict=False)
    if not target.is_absolute() or target.is_symlink():
        raise WorkspaceV2RemoteError("remote complement checkpoint is unavailable or unsafe")
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(document, stream, sort_keys=True, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
        _fsync_directory(target.parent)
    except OSError as error:
        raise WorkspaceV2RemoteError(
            "remote complement checkpoint could not be published"
        ) from error
    finally:
        temporary.unlink(missing_ok=True)


def _valid_digest(value: str) -> bool:
    return len(value) == 64 and all(character in "0123456789abcdef" for character in value)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


__all__ = [
    "RemoteBackupManifest",
    "RemoteBackupManifestReconciliationReceipt",
    "RemoteBackupObject",
    "RemoteBackupPlan",
    "RemoteBackupRestoreReceipt",
    "RemoteBackupSource",
    "RemoteBackupTransferReceipt",
    "RemoteComplementCheckpoint",
    "RemoteComplementFile",
    "RemoteComplementManifest",
    "RemoteComplementTransferReceipt",
    "RemoteComplementVerificationCheckpoint",
    "RemoteComplementVerificationReceipt",
    "WorkspaceV2RemoteError",
    "WorkspaceV2RemoteService",
]
