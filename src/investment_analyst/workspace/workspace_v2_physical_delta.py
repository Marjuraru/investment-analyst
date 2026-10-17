"""Streaming physical parent index for packed workspace-v2 backups.

The index binds each source-file range to an immutable, content-addressed
parent volume.  It deliberately writes JSONL incrementally: neither the
inventory nor the packed journal is materialized as a Python collection.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import os
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from itertools import groupby
from pathlib import Path
from typing import BinaryIO, Literal, TextIO
from uuid import UUID, uuid4

from pydantic import ConfigDict, Field, field_validator, model_validator

from investment_analyst.core.models.base import ContractModel, NonEmptyStr, UTCDateTime
from investment_analyst.workspace.drive_transport import DriveTransport
from investment_analyst.workspace.service import WorkspaceService
from investment_analyst.workspace.workspace_v2_backup import (
    WorkspaceV2BackupError,
    WorkspaceV2BackupService,
    WorkspaceV2PackedStreamManifest,
    _create_required_layout,
    _iter_packed_journal,
    _v2_writer_guard,
)
from investment_analyst.workspace.workspace_v2_backup_inventory import (
    WorkspaceV2BackupInventoryError,
    iter_inventory,
)
from investment_analyst.workspace.workspace_v2_migration_models import ReconciliationFileReference


class WorkspaceV2PhysicalDeltaError(RuntimeError):
    """The sealed physical parent index cannot be constructed or reused safely."""


class PhysicalDeltaBlock(ContractModel):
    """A bounded range in either the restored parent or a new sealed object."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    offset_bytes: int = Field(ge=0)
    length_bytes: int = Field(gt=0, le=8 * 1024 * 1024)
    sha256: NonEmptyStr
    origin: Literal["parent", "delta"]
    object_key: NonEmptyStr | None = None

    @model_validator(mode="after")
    def require_binding(self) -> PhysicalDeltaBlock:
        if not _valid_digest(self.sha256):
            raise ValueError("physical delta block digest is invalid")
        expected = f"physical-delta/objects/{self.sha256}.bin"
        if self.object_key != (expected if self.origin == "delta" else None):
            raise ValueError("physical delta block object binding differs")
        return self


class PhysicalDeltaFile(ContractModel):
    """One complete physical file, or an explicit deletion relative to the parent."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    path: NonEmptyStr
    deleted: bool = False
    size_bytes: int = Field(ge=0)
    sha256: NonEmptyStr
    blocks: tuple[PhysicalDeltaBlock, ...]

    @model_validator(mode="after")
    def require_coverage(self) -> PhysicalDeltaFile:
        PhysicalDeltaIndexEntry.require_safe_path(self.path)
        if not _valid_digest(self.sha256) or self.path in {".", ""}:
            raise ValueError("physical delta file binding is invalid")
        offset = 0
        for block in self.blocks:
            if block.offset_bytes != offset:
                raise ValueError("physical delta blocks have a gap or overlap")
            offset += block.length_bytes
        if offset != self.size_bytes or (self.deleted and (self.size_bytes or self.blocks)):
            raise ValueError("physical delta file length differs")
        if not self.size_bytes and self.sha256 != hashlib.sha256(b"").hexdigest():
            raise ValueError("physical delta empty file digest differs")
        return self


class PhysicalDeltaManifest(ContractModel):
    """Sealed final photo; the historical parent-index manifest stays readable."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["workspace-v2-physical-delta-manifest-v1"] = (
        "workspace-v2-physical-delta-manifest-v1"
    )
    kind: Literal["backup"] = "backup"
    historical_state: Literal["COMPLETE", "APPLYING"] = "COMPLETE"
    epoch: UUID
    source_root: Path
    source_workspace_id: UUID
    parent_backup_id: UUID
    parent_manifest_sha256: NonEmptyStr
    parent_index_sha256: NonEmptyStr
    files_sha256: NonEmptyStr
    file_count: int = Field(ge=0)
    deleted_count: int = Field(ge=0)
    logical_bytes: int = Field(ge=0)
    new_bytes: int = Field(ge=0)
    reused_bytes: int = Field(ge=0)

    @model_validator(mode="after")
    def require_binding(self) -> PhysicalDeltaManifest:
        if not self.source_root.is_absolute() or any(
            not _valid_digest(value)
            for value in (
                self.parent_manifest_sha256,
                self.parent_index_sha256,
                self.files_sha256,
            )
        ):
            raise ValueError("physical delta manifest binding is invalid")
        if self.new_bytes + self.reused_bytes != self.logical_bytes:
            raise ValueError("physical delta byte accounting differs")
        return self


class PhysicalDeltaTransferCheckpoint(ContractModel):
    """Bounded durable cursor; no unbounded set of confirmed objects in RAM."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["workspace-v2-physical-delta-checkpoint-v1"] = (
        "workspace-v2-physical-delta-checkpoint-v1"
    )
    kind: Literal["transfer"] = "transfer"
    manifest_sha256: NonEmptyStr
    files_sha256: NonEmptyStr
    confirmed_files: int = Field(ge=0)
    confirmed_blocks: int = Field(default=0, ge=0)


class PhysicalImmutableParentProof(ContractModel):
    """Cache authority from a verified restore, restricted to immutable files.

    Each use still checks the sealed path/hash/size and that the inode has not
    changed since that restore. A newer ctime falls back to content verification.
    This is never authority for DB, WAL, operational state or new delta blocks.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    schema_version: Literal["workspace-v2-immutable-parent-proof-v1"] = (
        "workspace-v2-immutable-parent-proof-v1"
    )
    source_root: Path
    source_workspace_id: UUID
    source_manifest_sha256: NonEmptyStr
    parent_backup_id: UUID
    parent_manifest_sha256: NonEmptyStr
    parent_index_sha256: NonEmptyStr
    restore_receipt: ReconciliationFileReference
    restored_at: UTCDateTime
    verifier_sha256: NonEmptyStr

    @model_validator(mode="after")
    def require_binding(self) -> PhysicalImmutableParentProof:
        if (
            not self.source_root.is_absolute()
            or self.restore_receipt.role != "control"
            or any(
                not _valid_digest(value)
                for value in (
                    self.source_manifest_sha256,
                    self.parent_manifest_sha256,
                    self.parent_index_sha256,
                    self.verifier_sha256,
                )
            )
        ):
            raise ValueError("immutable parent proof binding is invalid")
        return self


class PhysicalRemoteObjectRead(ContractModel):
    """A retained independent Drive read, never an upload staging object."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    schema_version: Literal["workspace-v2-independent-object-read-v1"] = (
        "workspace-v2-independent-object-read-v1"
    )
    source_kind: Literal["independent_drive_fetch"] = "independent_drive_fetch"
    object_key: NonEmptyStr
    object_sha256: NonEmptyStr
    size_bytes: int = Field(ge=0)
    configuration_sha256: NonEmptyStr
    verifier_sha256: NonEmptyStr
    verified_at: UTCDateTime

    @model_validator(mode="after")
    def require_content_address(self) -> PhysicalRemoteObjectRead:
        PhysicalDeltaIndexEntry.require_safe_path(self.object_key)
        if (
            not self.object_key.startswith("physical-delta/")
            or Path(self.object_key).stem != self.object_sha256
            or any(
                not _valid_digest(value)
                for value in (
                    self.object_sha256,
                    self.configuration_sha256,
                    self.verifier_sha256,
                )
            )
        ):
            raise ValueError("independent remote object binding is invalid")
        return self


class PhysicalDeltaPlanCheckpoint(ContractModel):
    """Commit boundary for the writer-owned JSONL; trailing bytes are unconfirmed."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["workspace-v2-physical-delta-checkpoint-v1"] = (
        "workspace-v2-physical-delta-checkpoint-v1"
    )
    kind: Literal["plan"] = "plan"
    binding_sha256: NonEmptyStr
    committed_bytes: int = Field(ge=0)
    prefix_sha256: NonEmptyStr


class PhysicalDeltaReceipt(ContractModel):
    """An independent remote read verified all new payload and controls."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["workspace-v2-physical-delta-receipt-v1"] = (
        "workspace-v2-physical-delta-receipt-v1"
    )
    kind: Literal["upload", "restore"] = "upload"
    restored_root: Path | None = None
    epoch: UUID
    historical_state: Literal["COMPLETE", "APPLYING"] = "COMPLETE"
    manifest_sha256: NonEmptyStr
    files_sha256: NonEmptyStr
    confirmed_files: int = Field(ge=0)
    parent_linked_files: int = Field(default=0, ge=0)
    parent_linked_bytes: int = Field(default=0, ge=0)
    completed_at: UTCDateTime

    @model_validator(mode="after")
    def require_recovery_root(self) -> PhysicalDeltaReceipt:
        if self.kind == "restore":
            if self.restored_root is None or not self.restored_root.is_absolute():
                raise ValueError("physical restore receipt requires its actual root")
        elif self.restored_root is not None:
            raise ValueError("upload receipt cannot claim a recovered root")
        if self.parent_linked_files > self.confirmed_files or (
            self.kind == "upload" and (self.parent_linked_files or self.parent_linked_bytes)
        ):
            raise ValueError("physical recovery reuse counters are invalid")
        return self


def _valid_digest(value: str) -> bool:
    return len(value) == 64 and all(character in "0123456789abcdef" for character in value)


class WorkspaceV2PhysicalDeltaService:
    """Plan the physical photo under writer locks and independently verify uploads.

    Only a single file's fragment metadata is retained. Payload hashing and
    copying use buffers of at most 1 MiB. This is separate from analytical
    incremental recomputation and does not modify the financial schema.
    """

    def __init__(self, workspace_service: WorkspaceService) -> None:
        self._workspace_service = workspace_service

    def prepare_immutable_parent_reuse(
        self,
        source_root: Path,
        parent_index: Path,
        restore_receipt: Path,
        destination: Path,
        *,
        parent_manifest_sha256: str,
    ) -> PhysicalImmutableParentProof:
        """Bind an existing verified restore; do not download or hash its blobs again."""
        from investment_analyst.workspace.workspace_v2_remote import RemoteBackupRestoreReceipt

        source = _safe_root(source_root)
        receipt = RemoteBackupRestoreReceipt.model_validate_json(restore_receipt.read_bytes())
        if (
            not destination.is_absolute()
            or any(path.is_symlink() for path in (destination, *destination.parents))
            or destination.is_relative_to(source)
        ):
            raise WorkspaceV2PhysicalDeltaError("immutable parent proof output is unsafe")
        parent = verify_physical_delta_index(parent_index)
        identity = self._workspace_service.manifest(source)
        if (
            identity.format_version != 2
            or identity.workspace_id != parent.source_workspace_id
            or receipt.restored_workspace_id != identity.workspace_id
            or receipt.backup_id != parent.parent_backup_id
            or receipt.remote_manifest_sha256 != parent_manifest_sha256
            or restore_receipt.is_symlink()
        ):
            raise WorkspaceV2PhysicalDeltaError("immutable parent restore binding differs")
        proof = PhysicalImmutableParentProof(
            source_root=source,
            source_workspace_id=identity.workspace_id,
            source_manifest_sha256=_sha256(self._workspace_service.resolve(source).manifest_path),
            parent_backup_id=parent.parent_backup_id,
            parent_manifest_sha256=parent_manifest_sha256,
            parent_index_sha256=_sha256(parent_index),
            restore_receipt=ReconciliationFileReference(
                path=restore_receipt,
                role="control",
                sha256=_sha256(restore_receipt),
                size_bytes=restore_receipt.stat().st_size,
            ),
            restored_at=receipt.completed_at,
            verifier_sha256=_sha256(Path(__file__)),
        )
        _write_new_json(destination, proof.model_dump(mode="json"))
        return proof

    def plan(
        self,
        source_root: Path,
        parent_index: Path,
        destination: Path,
        *,
        parent_manifest_sha256: str,
        epoch: UUID,
        block_bytes: int = 8 * 1024 * 1024,
        allow_incomplete_historical: bool = False,
        immutable_parent_proof: Path | None = None,
    ) -> PhysicalDeltaManifest:
        """Resume by verifying the exact recorded prefix, never by trusting mtime."""
        if not 1 <= block_bytes <= 8 * 1024 * 1024:
            raise WorkspaceV2PhysicalDeltaError("physical delta block size is invalid")
        source = _safe_root(source_root)
        output = _new_destination(destination)
        if output == source or output.is_relative_to(source) or source.is_relative_to(output):
            raise WorkspaceV2PhysicalDeltaError("physical delta output must be disjoint")
        parent = verify_physical_delta_index(parent_index)
        identity = self._workspace_service.manifest(source)
        if identity.format_version != 2 or identity.workspace_id != parent.source_workspace_id:
            raise WorkspaceV2PhysicalDeltaError("physical delta source identity differs")
        reuse = None
        if immutable_parent_proof is not None:
            reuse = _load_immutable_parent_proof(
                immutable_parent_proof,
                source,
                parent_index,
                parent_manifest_sha256,
                self._workspace_service,
                parent,
            )
        from investment_analyst.storage.workspace_v2_inventory import historical_transition_status

        historical_state = historical_transition_status(source)
        if historical_state != "COMPLETE" and not allow_incomplete_historical:
            raise WorkspaceV2PhysicalDeltaError(
                "incomplete historical transition is not a complete F backup"
            )
        binding = PhysicalDeltaManifest(
            historical_state=historical_state,
            epoch=epoch,
            source_root=source,
            source_workspace_id=identity.workspace_id,
            parent_backup_id=parent.parent_backup_id,
            parent_manifest_sha256=parent_manifest_sha256,
            parent_index_sha256=_sha256(parent_index),
            files_sha256=hashlib.sha256(b"").hexdigest(),
            file_count=0,
            deleted_count=0,
            logical_bytes=0,
            new_bytes=0,
            reused_bytes=0,
        )
        binding_path = output / "plan-binding.json"
        binding_document = {"manifest": binding.model_dump(mode="json"), "block_bytes": block_bytes}
        if immutable_parent_proof is not None:
            binding_document["immutable_parent_proof_sha256"] = _sha256(immutable_parent_proof)
        if binding_path.exists():
            if (
                binding_path.is_symlink()
                or json.loads(binding_path.read_bytes()) != binding_document
            ):
                raise WorkspaceV2PhysicalDeltaError("physical delta epoch binding differs")
        else:
            _write_new_json(binding_path, binding_document)
        files_path = output / "files.jsonl"
        partial = output / "files.partial.jsonl"
        plan_checkpoint = output / "plan-checkpoint.json"
        _recover_plan_cursor(partial, files_path, plan_checkpoint, _sha256(binding_path))
        prior_path = files_path if files_path.exists() else partial
        prior = _iter_delta_files(prior_path) if prior_path.exists() else iter(())
        expected_prior = next(prior, None)
        digest = hashlib.sha256()
        count = deleted = logical = changed = reused = records = 0
        with _v2_writer_guard(source), partial.open("a", encoding="utf-8", newline="\n") as stream:
            for item in _plan_delta_files(source, parent_index, output, block_bytes, reuse):
                encoded = (_compact_json(item.model_dump(mode="json")) + "\n").encode()
                digest.update(encoded)
                records += 1
                count += not item.deleted
                deleted += item.deleted
                logical += item.size_bytes
                changed += sum(b.length_bytes for b in item.blocks if b.origin == "delta")
                reused += sum(b.length_bytes for b in item.blocks if b.origin == "parent")
                if expected_prior is not None:
                    if item != expected_prior:
                        raise WorkspaceV2PhysicalDeltaError(
                            "physical delta source or cursor changed"
                        )
                    expected_prior = next(prior, None)
                elif files_path.exists():
                    raise WorkspaceV2PhysicalDeltaError("physical delta sealed source changed")
                else:
                    stream.write(encoded.decode())
                    if records % 256 == 0:
                        _commit_plan_cursor(
                            stream, plan_checkpoint, binding_path, digest.hexdigest()
                        )
            if expected_prior is not None:
                raise WorkspaceV2PhysicalDeltaError("physical delta source lost recorded files")
            if not files_path.exists():
                _commit_plan_cursor(stream, plan_checkpoint, binding_path, digest.hexdigest())
        manifest = binding.model_copy(
            update={
                "files_sha256": digest.hexdigest(),
                "file_count": count,
                "deleted_count": deleted,
                "logical_bytes": logical,
                "new_bytes": changed,
                "reused_bytes": reused,
            }
        )
        if files_path.exists():
            if _sha256(files_path) != manifest.files_sha256:
                raise WorkspaceV2PhysicalDeltaError("physical delta sealed inventory changed")
            partial.unlink(missing_ok=True)
        else:
            os.replace(partial, files_path)
            _fsync_directory(output)
        manifest_path = output / "delta-manifest.json"
        if manifest_path.exists():
            if _read_delta_manifest(manifest_path) != manifest:
                raise WorkspaceV2PhysicalDeltaError("physical delta sealed manifest changed")
        else:
            _write_new_json(manifest_path, manifest.model_dump(mode="json"))
        return manifest

    def upload(
        self,
        delta_root: Path,
        transport: DriveTransport,
        *,
        checkpoint_path: Path,
        receipt_path: Path,
        independent_cache_root: Path | None = None,
        configuration_sha256: str | None = None,
        batch_transfer: bool = False,
    ) -> PhysicalDeltaReceipt:
        """Checkpoint each file only after an independent remote hash verification."""
        root = _safe_root(delta_root)
        cache = _independent_cache(independent_cache_root, configuration_sha256, (root,))
        manifest_path = root / "delta-manifest.json"
        manifest = _read_delta_manifest(manifest_path)
        files = root / "files.jsonl"
        _validate_delta_inventory(files, manifest)
        if _sha256(files) != manifest.files_sha256:
            raise WorkspaceV2PhysicalDeltaError("physical delta files seal differs")
        manifest_digest = _sha256(manifest_path)
        checkpoint = PhysicalDeltaTransferCheckpoint(
            manifest_sha256=manifest_digest, files_sha256=manifest.files_sha256, confirmed_files=0
        )
        if checkpoint_path.exists():
            saved = PhysicalDeltaTransferCheckpoint.model_validate_json(
                checkpoint_path.read_bytes()
            )
            if saved.model_copy(update={"confirmed_files": 0, "confirmed_blocks": 0}) != checkpoint:
                raise WorkspaceV2PhysicalDeltaError("physical delta transfer binding differs")
            checkpoint = saved
        if batch_transfer:
            if cache is None or configuration_sha256 is None:
                raise WorkspaceV2PhysicalDeltaError("batch transfer requires independent reads")
            _upload_independent_batches(transport, root, files, cache, configuration_sha256)
        ordinal = 0
        for ordinal, item in enumerate(_iter_delta_files(files), 1):
            if ordinal <= checkpoint.confirmed_files:
                continue
            if checkpoint.confirmed_blocks > len(item.blocks):
                raise WorkspaceV2PhysicalDeltaError("physical delta block cursor exceeds file")
            new_blocks = False
            for block_ordinal, block in enumerate(item.blocks, 1):
                if block_ordinal <= checkpoint.confirmed_blocks:
                    continue
                if block.origin == "delta":
                    assert block.object_key is not None
                    if not batch_transfer:
                        path = _safe_file(root, block.object_key)
                        _verify_file(path, block.length_bytes, block.sha256)
                        _upload_and_verify(
                            transport,
                            path,
                            block.object_key,
                            root,
                            independent_cache_root=cache,
                            configuration_sha256=configuration_sha256,
                        )
                    new_blocks = True
                    checkpoint = checkpoint.model_copy(update={"confirmed_blocks": block_ordinal})
                    _replace_json(checkpoint_path, checkpoint.model_dump(mode="json"))
            checkpoint = checkpoint.model_copy(
                update={"confirmed_files": ordinal, "confirmed_blocks": 0}
            )
            if new_blocks or ordinal % 256 == 0:
                _replace_json(checkpoint_path, checkpoint.model_dump(mode="json"))
        if ordinal != checkpoint.confirmed_files or checkpoint.confirmed_blocks:
            raise WorkspaceV2PhysicalDeltaError("physical delta transfer cursor exceeds inventory")
        _replace_json(checkpoint_path, checkpoint.model_dump(mode="json"))
        for path, key in (
            (files, f"physical-delta/controls/{manifest.files_sha256}.jsonl"),
            (manifest_path, f"physical-delta/manifests/{manifest_digest}.json"),
        ):
            _upload_and_verify(
                transport,
                path,
                key,
                root,
                independent_cache_root=cache,
                configuration_sha256=configuration_sha256,
            )
        receipt = PhysicalDeltaReceipt(
            epoch=manifest.epoch,
            historical_state=manifest.historical_state,
            manifest_sha256=manifest_digest,
            files_sha256=manifest.files_sha256,
            confirmed_files=ordinal,
            completed_at=datetime.now(UTC),
        )
        if receipt_path.exists():
            previous = PhysicalDeltaReceipt.model_validate_json(receipt_path.read_bytes())
            if previous.model_copy(update={"completed_at": receipt.completed_at}) != receipt:
                raise WorkspaceV2PhysicalDeltaError("physical delta receipt binding differs")
            return previous
        _write_new_json(receipt_path, receipt.model_dump(mode="json"))
        return receipt

    def restore(
        self,
        delta_root: Path,
        *,
        parent_index: Path,
        parent_root: Path,
        parent_manifest_sha256: str,
        destination: Path,
        transport: DriveTransport,
        checkpoint_path: Path,
        receipt_path: Path,
        reuse_parent_immutables: bool = False,
        independent_cache_root: Path | None = None,
        configuration_sha256: str | None = None,
    ) -> PhysicalDeltaReceipt:
        """Materialize parent slices plus independently downloaded new blocks.

        Parent bytes must still match the sealed index. In particular, new
        bytes can never fall back to the candidate or local upload staging.
        The destination is a new isolated recovery slot, never the parent.
        """
        root = _safe_root(delta_root)
        parent_root = _safe_root(parent_root)
        cache = _independent_cache(
            independent_cache_root, configuration_sha256, (root, parent_root, destination)
        )
        manifest_path = root / "delta-manifest.json"
        manifest = _read_delta_manifest(manifest_path)
        trailer = verify_physical_delta_index(parent_index)
        _validate_delta_inventory(root / "files.jsonl", manifest)
        if (
            _sha256(parent_index) != manifest.parent_index_sha256
            or trailer.parent_backup_id != manifest.parent_backup_id
            or trailer.source_workspace_id != manifest.source_workspace_id
            or parent_manifest_sha256 != manifest.parent_manifest_sha256
            or _sha256(root / "files.jsonl") != manifest.files_sha256
        ):
            raise WorkspaceV2PhysicalDeltaError("physical delta recovery parent binding differs")
        for local, key in (
            (manifest_path, f"physical-delta/manifests/{_sha256(manifest_path)}.json"),
            (root / "files.jsonl", f"physical-delta/controls/{manifest.files_sha256}.jsonl"),
        ):
            downloaded_control = _fetch_verified_remote(
                transport,
                key,
                local.stat().st_size,
                _sha256(local),
                root,
                cache=cache,
                configuration_sha256=configuration_sha256,
            )
            try:
                _verify_file(downloaded_control, local.stat().st_size, _sha256(local))
            finally:
                if cache is None:
                    downloaded_control.unlink(missing_ok=True)
        if not destination.is_absolute() or any(
            p.is_symlink() for p in (destination, *destination.parents)
        ):
            raise WorkspaceV2PhysicalDeltaError("physical delta recovery destination is unsafe")
        target = destination.resolve()
        if any(
            target == p or target.is_relative_to(p) or p.is_relative_to(target)
            for p in (parent_root, root)
        ):
            raise WorkspaceV2PhysicalDeltaError("physical delta recovery roots must be disjoint")
        binding = {"manifest_sha256": _sha256(manifest_path), "destination": str(target)}
        if reuse_parent_immutables:
            binding["reuse_parent_immutables"] = True
        if checkpoint_path.exists():
            if checkpoint_path.is_symlink() or json.loads(checkpoint_path.read_bytes()) != binding:
                raise WorkspaceV2PhysicalDeltaError(
                    "physical delta recovery checkpoint binding differs"
                )
        else:
            if target.exists():
                raise WorkspaceV2PhysicalDeltaError("physical delta recovery slot already exists")
            _write_new_json(checkpoint_path, binding)
        target.mkdir(parents=True, exist_ok=True)
        _create_required_layout(target)
        parents = iter(groupby(iter_physical_delta_index(parent_index), key=lambda item: item.path))
        current = next(parents, None)
        count = linked_files = linked_bytes = 0
        with _v2_writer_guard(parent_root):
            for item in _iter_delta_files(root / "files.jsonl"):
                count += 1
                while current is not None and _path_order(current[0]) < _path_order(item.path):
                    for _ in current[1]:
                        pass
                    current = next(parents, None)
                prior = (
                    {}
                    if current is None or current[0] != item.path
                    else {entry.offset_bytes: entry for entry in current[1]}
                )
                if current is not None and current[0] == item.path:
                    current = next(parents, None)
                destination_file = _safe_file(target, item.path)
                if item.deleted:
                    if destination_file.exists():
                        raise WorkspaceV2PhysicalDeltaError(
                            "physical delta deleted file exists in recovery slot"
                        )
                    continue
                if destination_file.exists():
                    _verify_file(destination_file, item.size_bytes, item.sha256)
                    if (
                        reuse_parent_immutables
                        and _parent_immutable_file(item)
                        and os.path.samefile(destination_file, _safe_file(parent_root, item.path))
                    ):
                        linked_files += 1
                        linked_bytes += item.size_bytes
                    continue
                destination_file.parent.mkdir(parents=True, exist_ok=True)
                if reuse_parent_immutables and _parent_immutable_file(item):
                    parent_file = _safe_file(parent_root, item.path)
                    _verify_file(parent_file, item.size_bytes, item.sha256)
                    os.link(parent_file, destination_file, follow_symlinks=False)
                    _fsync_directory(destination_file.parent)
                    linked_files += 1
                    linked_bytes += item.size_bytes
                    continue
                temporary = destination_file.with_name(
                    f".{destination_file.name}.{uuid4().hex}.tmp"
                )
                try:
                    with temporary.open("xb") as output:
                        for block in item.blocks:
                            if block.origin == "parent":
                                entry = prior.get(block.offset_bytes)
                                if (
                                    entry is None
                                    or entry.length_bytes != block.length_bytes
                                    or entry.fragment_sha256 != block.sha256
                                ):
                                    raise WorkspaceV2PhysicalDeltaError(
                                        "physical delta parent slice binding differs"
                                    )
                                with _safe_file(parent_root, item.path).open("rb") as source:
                                    source.seek(block.offset_bytes)
                                    _copy_verified_range(
                                        source, output, block.length_bytes, block.sha256
                                    )
                            else:
                                assert block.object_key is not None
                                downloaded = _fetch_verified_remote(
                                    transport,
                                    block.object_key,
                                    block.length_bytes,
                                    block.sha256,
                                    root,
                                    cache=cache,
                                    configuration_sha256=configuration_sha256,
                                )
                                try:
                                    with downloaded.open("rb") as source:
                                        _copy_verified_range(
                                            source, output, block.length_bytes, block.sha256
                                        )
                                finally:
                                    if cache is None:
                                        downloaded.unlink(missing_ok=True)
                        output.flush()
                        os.fsync(output.fileno())
                    _verify_file(temporary, item.size_bytes, item.sha256)
                    os.replace(temporary, destination_file)
                    _fsync_directory(destination_file.parent)
                finally:
                    temporary.unlink(missing_ok=True)
        identity = self._workspace_service.manifest(target)
        if identity.workspace_id != manifest.source_workspace_id or identity.format_version != 2:
            raise WorkspaceV2PhysicalDeltaError(
                "physical delta recovered workspace identity differs"
            )
        receipt = PhysicalDeltaReceipt(
            kind="restore",
            restored_root=target,
            epoch=manifest.epoch,
            historical_state=manifest.historical_state,
            manifest_sha256=_sha256(manifest_path),
            files_sha256=manifest.files_sha256,
            confirmed_files=count,
            parent_linked_files=linked_files,
            parent_linked_bytes=linked_bytes,
            completed_at=datetime.now(UTC),
        )
        if receipt_path.exists():
            previous = PhysicalDeltaReceipt.model_validate_json(receipt_path.read_bytes())
            if previous.model_copy(update={"completed_at": receipt.completed_at}) != receipt:
                raise WorkspaceV2PhysicalDeltaError(
                    "physical delta recovery receipt binding differs"
                )
            return previous
        _write_new_json(receipt_path, receipt.model_dump(mode="json"))
        return receipt


def _load_immutable_parent_proof(
    path: Path,
    source: Path,
    parent_index: Path,
    parent_manifest_sha256: str,
    service: WorkspaceService,
    parent: PhysicalDeltaIndexTrailer,
) -> PhysicalImmutableParentProof:
    from investment_analyst.workspace.workspace_v2_remote import RemoteBackupRestoreReceipt

    if path.is_symlink():
        raise WorkspaceV2PhysicalDeltaError("immutable parent proof is unsafe")
    proof = PhysicalImmutableParentProof.model_validate_json(path.read_bytes())
    control = proof.restore_receipt
    _verify_file(control.path, control.size_bytes, control.sha256)
    receipt = RemoteBackupRestoreReceipt.model_validate_json(control.path.read_bytes())
    if (
        proof.source_root != source
        or proof.source_workspace_id != service.manifest(source).workspace_id
        or proof.source_manifest_sha256 != _sha256(service.resolve(source).manifest_path)
        or proof.parent_index_sha256 != _sha256(parent_index)
        or proof.parent_manifest_sha256 != parent_manifest_sha256
        or proof.parent_backup_id != parent.parent_backup_id
        or proof.source_workspace_id != parent.source_workspace_id
        or proof.parent_backup_id != receipt.backup_id
        or proof.source_workspace_id != receipt.restored_workspace_id
        or proof.parent_manifest_sha256 != receipt.remote_manifest_sha256
        or proof.restored_at != receipt.completed_at
        or proof.verifier_sha256 != _sha256(Path(__file__))
    ):
        raise WorkspaceV2PhysicalDeltaError("immutable parent proof binding differs")
    return proof


def _cached_immutable_parent_file(
    root: Path,
    relative: str,
    prior: tuple[PhysicalDeltaIndexEntry, ...],
    proof: PhysicalImmutableParentProof | None,
) -> PhysicalDeltaFile | None:
    if (
        proof is None
        or not prior
        or not relative.startswith(("storage/v2/raw/raw/sha256/", "data/documents/sha256/"))
        or Path(relative).name != prior[0].file_sha256
        or not prior[0].file_size_bytes
    ):
        return None
    stat = _safe_file(root, relative).stat()
    delta = proof.restored_at - datetime(1970, 1, 1, tzinfo=UTC)
    restored_ns = (delta.days * 86400 + delta.seconds) * 1_000_000_000 + delta.microseconds * 1000
    if stat.st_size != prior[0].file_size_bytes or stat.st_ctime_ns > restored_ns:
        return None
    return PhysicalDeltaFile(
        path=relative,
        size_bytes=prior[0].file_size_bytes,
        sha256=prior[0].file_sha256,
        blocks=tuple(
            PhysicalDeltaBlock(
                offset_bytes=entry.offset_bytes,
                length_bytes=entry.length_bytes,
                sha256=str(entry.fragment_sha256),
                origin="parent",
            )
            for entry in prior
        ),
    )


def _parent_immutable_file(item: PhysicalDeltaFile) -> bool:
    """Only content-addressed unchanged parent blobs may share an inode temporarily.

    DB, locks, journals and operational documents are always materialized independently.
    The parent writer guard remains held until the recovered photo is complete.
    """
    return (
        item.path.startswith(("storage/v2/raw/raw/sha256/", "data/documents/sha256/"))
        and Path(item.path).name == item.sha256
        and bool(item.blocks)
        and all(block.origin == "parent" for block in item.blocks)
    )


def _safe_root(value: Path) -> Path:
    if not value.is_absolute() or any(part.is_symlink() for part in (value, *value.parents)):
        raise WorkspaceV2PhysicalDeltaError("physical delta root is unsafe")
    if not value.is_dir():
        raise WorkspaceV2PhysicalDeltaError("physical delta root is unavailable")
    return value.resolve()


def _recover_plan_cursor(partial: Path, sealed: Path, checkpoint_path: Path, binding: str) -> None:
    if sealed.exists():
        return
    if partial.is_symlink() or checkpoint_path.is_symlink():
        raise WorkspaceV2PhysicalDeltaError("physical delta plan cursor is unsafe")
    if not checkpoint_path.exists():
        if partial.exists() and partial.stat().st_size:
            raise WorkspaceV2PhysicalDeltaError("physical delta plan cursor is missing")
        _replace_json(
            checkpoint_path,
            PhysicalDeltaPlanCheckpoint(
                binding_sha256=binding,
                committed_bytes=0,
                prefix_sha256=hashlib.sha256(b"").hexdigest(),
            ).model_dump(mode="json"),
        )
    saved = PhysicalDeltaPlanCheckpoint.model_validate_json(checkpoint_path.read_bytes())
    if saved.binding_sha256 != binding:
        raise WorkspaceV2PhysicalDeltaError("physical delta plan cursor binding differs")
    if not partial.exists():
        partial.touch()
    if partial.stat().st_size < saved.committed_bytes:
        raise WorkspaceV2PhysicalDeltaError("physical delta confirmed cursor is truncated")
    digest = hashlib.sha256()
    with partial.open("r+b") as stream:
        remaining = saved.committed_bytes
        while remaining:
            chunk = stream.read(min(remaining, 1024 * 1024))
            digest.update(chunk)
            remaining -= len(chunk)
        if digest.hexdigest() != saved.prefix_sha256:
            raise WorkspaceV2PhysicalDeltaError("physical delta confirmed cursor is corrupt")
        # Only this service's unconfirmed tail may be discarded.
        stream.truncate(saved.committed_bytes)
        stream.flush()
        os.fsync(stream.fileno())


def _commit_plan_cursor(stream: TextIO, checkpoint: Path, binding: Path, digest: str) -> None:
    stream.flush()
    os.fsync(stream.fileno())
    _replace_json(
        checkpoint,
        PhysicalDeltaPlanCheckpoint(
            binding_sha256=_sha256(binding),
            committed_bytes=stream.tell(),
            prefix_sha256=digest,
        ).model_dump(mode="json"),
    )


def _safe_file(root: Path, relative: str) -> Path:
    PhysicalDeltaIndexEntry.require_safe_path(relative)
    path = root / relative
    if relative == "." or any(part.is_symlink() for part in (path, *path.parents)):
        raise WorkspaceV2PhysicalDeltaError("physical delta file is unsafe")
    if not path.is_relative_to(root):
        raise WorkspaceV2PhysicalDeltaError("physical delta file escapes root")
    return path


def _verify_file(path: Path, size: int, digest: str) -> None:
    if (
        not path.is_file()
        or path.is_symlink()
        or path.stat().st_size != size
        or _sha256(path) != digest
    ):
        raise WorkspaceV2PhysicalDeltaError("physical delta file is absent or corrupt")


def _read_delta_manifest(path: Path) -> PhysicalDeltaManifest:
    if path.is_symlink() or not path.is_file():
        raise WorkspaceV2PhysicalDeltaError("physical delta manifest is unavailable")
    try:
        return PhysicalDeltaManifest.model_validate_json(path.read_bytes())
    except ValueError as error:
        raise WorkspaceV2PhysicalDeltaError("physical delta manifest is malformed") from error


def _iter_delta_files(path: Path) -> Iterator[PhysicalDeltaFile]:
    previous: tuple[tuple[str, ...], str] | None = None
    try:
        if path.is_symlink():
            raise WorkspaceV2PhysicalDeltaError("physical delta inventory is unsafe")
        with path.open("rb") as stream:
            for line in stream:
                if not line.endswith(b"\n"):
                    raise WorkspaceV2PhysicalDeltaError("physical delta file cursor is incomplete")
                item = PhysicalDeltaFile.model_validate_json(line)
                key = _path_order(item.path)
                if previous is not None and key <= previous:
                    raise WorkspaceV2PhysicalDeltaError("physical delta file ordering differs")
                previous = key
                yield item
    except (ValueError, OSError) as error:
        raise WorkspaceV2PhysicalDeltaError("physical delta file inventory is malformed") from error


def _validate_delta_inventory(path: Path, manifest: PhysicalDeltaManifest) -> None:
    files = deleted = logical = changed = reused = 0
    for item in _iter_delta_files(path):
        files += not item.deleted
        deleted += item.deleted
        logical += item.size_bytes
        changed += sum(block.length_bytes for block in item.blocks if block.origin == "delta")
        reused += sum(block.length_bytes for block in item.blocks if block.origin == "parent")
    if (files, deleted, logical, changed, reused) != (
        manifest.file_count,
        manifest.deleted_count,
        manifest.logical_bytes,
        manifest.new_bytes,
        manifest.reused_bytes,
    ) or _sha256(path) != manifest.files_sha256:
        raise WorkspaceV2PhysicalDeltaError("physical delta inventory accounting or seal differs")


def _path_order(path: str) -> tuple[tuple[str, ...], str]:
    value = Path(path)
    return value.parts[:-1], value.name


def _source_files(root: Path) -> Iterator[str]:
    for parent, directories, filenames in os.walk(root, followlinks=False):
        directories.sort()
        filenames.sort()
        directory = Path(parent)
        for name in directories:
            _safe_file(root, (directory / name).relative_to(root).as_posix())
        for name in filenames:
            relative = (directory / name).relative_to(root).as_posix()
            if not _safe_file(root, relative).is_file():
                raise WorkspaceV2PhysicalDeltaError("physical delta source is not a regular file")
            yield relative


def _plan_delta_files(
    source: Path,
    parent_index: Path,
    output: Path,
    block_bytes: int,
    immutable_parent: PhysicalImmutableParentProof | None = None,
) -> Iterator[PhysicalDeltaFile]:
    parents = iter(groupby(iter_physical_delta_index(parent_index), key=lambda item: item.path))
    current = next(parents, None)
    for relative in _source_files(source):
        while current is not None and _path_order(current[0]) < _path_order(relative):
            yield PhysicalDeltaFile(
                path=current[0],
                deleted=True,
                size_bytes=0,
                sha256=hashlib.sha256(b"").hexdigest(),
                blocks=(),
            )
            # groupby must consume the group before advancing.
            for _ in current[1]:
                pass
            current = next(parents, None)
        prior = () if current is None or current[0] != relative else tuple(current[1])
        cached = _cached_immutable_parent_file(source, relative, prior, immutable_parent)
        planned = (
            cached
            if cached is not None
            else _plan_delta_file(source, relative, prior, output, block_bytes)
        )
        if (
            immutable_parent is not None
            and relative.startswith(("storage/v2/raw/raw/sha256/", "data/documents/sha256/"))
            and _valid_digest(Path(relative).name)
            and Path(relative).name != planned.sha256
        ):
            raise WorkspaceV2PhysicalDeltaError("immutable source content-address binding differs")
        yield planned
        if current is not None and current[0] == relative:
            current = next(parents, None)
    while current is not None:
        yield PhysicalDeltaFile(
            path=current[0],
            deleted=True,
            size_bytes=0,
            sha256=hashlib.sha256(b"").hexdigest(),
            blocks=(),
        )
        for _ in current[1]:
            pass
        current = next(parents, None)


def _plan_delta_file(
    root: Path,
    relative: str,
    prior: tuple[PhysicalDeltaIndexEntry, ...],
    output: Path,
    block_bytes: int,
) -> PhysicalDeltaFile:
    path = _safe_file(root, relative)
    before = path.stat()
    known = {entry.offset_bytes: entry for entry in prior}
    blocks: list[PhysicalDeltaBlock] = []
    full_digest = hashlib.sha256()
    offset = 0
    with path.open("rb") as stream:
        while offset < before.st_size:
            previous = known.get(offset)
            length = min(
                before.st_size - offset,
                previous.length_bytes
                if previous is not None and previous.length_bytes
                else block_bytes,
                8 * 1024 * 1024,
            )
            digest = hashlib.sha256()
            remaining = length
            while remaining:
                chunk = stream.read(min(remaining, 1024 * 1024))
                if not chunk:
                    raise WorkspaceV2PhysicalDeltaError(
                        "physical delta source shortened during read"
                    )
                remaining -= len(chunk)
                digest.update(chunk)
                full_digest.update(chunk)
            sha = digest.hexdigest()
            reusable = (
                previous is not None
                and previous.length_bytes == length
                and previous.fragment_sha256 == sha
            )
            key = None if reusable else f"physical-delta/objects/{sha}.bin"
            if key is not None:
                target = _safe_file(output, key)
                if target.exists():
                    _verify_file(target, length, sha)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    temporary = target.with_name(f".{target.name}.{uuid4().hex}.tmp")
                    try:
                        stream.seek(offset)
                        remaining = length
                        with temporary.open("xb") as object_stream:
                            while remaining:
                                chunk = stream.read(min(remaining, 1024 * 1024))
                                if not chunk:
                                    raise WorkspaceV2PhysicalDeltaError(
                                        "physical delta source changed during copy"
                                    )
                                object_stream.write(chunk)
                                remaining -= len(chunk)
                            object_stream.flush()
                            os.fsync(object_stream.fileno())
                        _verify_file(temporary, length, sha)
                        os.replace(temporary, target)
                        _fsync_directory(target.parent)
                    finally:
                        temporary.unlink(missing_ok=True)
            blocks.append(
                PhysicalDeltaBlock(
                    offset_bytes=offset,
                    length_bytes=length,
                    sha256=sha,
                    origin="parent" if reusable else "delta",
                    object_key=key,
                )
            )
            offset += length
        after = path.stat()
        if stream.read(1) or (
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ) != (before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns):
            raise WorkspaceV2PhysicalDeltaError("physical delta source changed during read")
    return PhysicalDeltaFile(
        path=relative,
        size_bytes=before.st_size,
        sha256=full_digest.hexdigest(),
        blocks=tuple(blocks),
    )


def _independent_cache(
    root: Path | None, configuration_sha256: str | None, excluded: tuple[Path, ...]
) -> Path | None:
    if (root is None) != (configuration_sha256 is None):
        raise WorkspaceV2PhysicalDeltaError("independent cache configuration is incomplete")
    if root is None:
        return None
    assert configuration_sha256 is not None
    if not _valid_digest(configuration_sha256):
        raise WorkspaceV2PhysicalDeltaError("independent cache configuration digest differs")
    cache = _new_destination(root)
    if any(cache == p or cache.is_relative_to(p) or p.is_relative_to(cache) for p in excluded):
        raise WorkspaceV2PhysicalDeltaError("independent cache roots must be disjoint")
    path = cache / "cache-binding.json"
    binding = {
        "source_kind": "independent_drive_fetch",
        "configuration_sha256": configuration_sha256,
    }
    if path.exists():
        if path.is_symlink() or json.loads(path.read_bytes()) != binding:
            raise WorkspaceV2PhysicalDeltaError("independent cache namespace differs")
    else:
        _write_new_json(path, binding)
    return cache


def _fetch_verified_remote(
    transport: DriveTransport,
    key: str,
    length: int,
    digest: str,
    scratch: Path,
    *,
    cache: Path | None,
    configuration_sha256: str | None,
    fresh: bool = False,
) -> Path:
    """Retain only a verified Drive fetch; a cache miss never reads staging."""
    PhysicalDeltaIndexEntry.require_safe_path(key)
    retained = None if cache is None else _safe_file(cache, key)
    proof_path = None if retained is None else retained.with_name(retained.name + ".read.json")
    if not fresh and retained is not None and proof_path is not None:
        try:
            if proof_path.is_symlink():
                raise WorkspaceV2PhysicalDeltaError("independent read proof is unsafe")
            proof = PhysicalRemoteObjectRead.model_validate_json(proof_path.read_bytes())
            if (
                proof.object_key == key
                and proof.object_sha256 == digest
                and proof.size_bytes == length
                and proof.configuration_sha256 == configuration_sha256
                and proof.verifier_sha256 == _remote_read_verifier_sha256()
            ):
                _verify_file(retained, length, digest)
                return retained
        except (OSError, ValueError, WorkspaceV2PhysicalDeltaError):
            # Missing/corrupt retained reads are repaired exclusively from Drive.
            pass
    downloaded = (scratch if cache is None else cache) / f".remote-read-{uuid4().hex}"
    try:
        transport.fetch_sealed_object(key, downloaded)
        _verify_file(downloaded, length, digest)
        with downloaded.open("rb") as handle:
            os.fsync(handle.fileno())
        if retained is None:
            return downloaded
        assert proof_path is not None and configuration_sha256 is not None
        retained.parent.mkdir(parents=True, exist_ok=True)
        os.replace(downloaded, retained)
        _fsync_directory(retained.parent)
        proof = PhysicalRemoteObjectRead(
            object_key=key,
            object_sha256=digest,
            size_bytes=length,
            configuration_sha256=configuration_sha256,
            verifier_sha256=_remote_read_verifier_sha256(),
            verified_at=datetime.now(UTC),
        )
        _replace_json(proof_path, proof.model_dump(mode="json"))
        return retained
    except OSError as error:
        downloaded.unlink(missing_ok=True)
        raise WorkspaceV2PhysicalDeltaError("independent remote object read failed") from error
    except BaseException:
        downloaded.unlink(missing_ok=True)
        raise


def _upload_and_verify(
    transport: DriveTransport,
    source: Path,
    key: str,
    scratch: Path,
    *,
    independent_cache_root: Path | None = None,
    configuration_sha256: str | None = None,
) -> None:
    expected = _sha256(source)
    transport.copy_immutable_object(source, key)
    downloaded = _fetch_verified_remote(
        transport,
        key,
        source.stat().st_size,
        expected,
        scratch,
        cache=independent_cache_root,
        configuration_sha256=configuration_sha256,
        fresh=True,
    )
    try:
        _verify_file(downloaded, source.stat().st_size, expected)
    finally:
        if independent_cache_root is None:
            downloaded.unlink(missing_ok=True)


def _remote_read_verifier_sha256() -> str:
    """Bind retained reads to their actual verifier, not unrelated link/planning code."""
    return hashlib.sha256(
        "\n".join(
            inspect.getsource(component)
            for component in (
                PhysicalRemoteObjectRead,
                _independent_cache,
                _fetch_verified_remote,
                _remote_read_verifier_sha256,
                _verify_file,
                _safe_file,
                _valid_digest,
                _sha256,
            )
        ).encode()
    ).hexdigest()


def _verified_independent_read(
    cache: Path,
    key: str,
    size: int,
    digest: str,
    configuration_sha256: str,
) -> bool:
    retained = _safe_file(cache, key)
    proof_path = retained.with_name(retained.name + ".read.json")
    try:
        if proof_path.is_symlink():
            return False
        proof = PhysicalRemoteObjectRead.model_validate_json(proof_path.read_bytes())
        if (
            proof.object_key != key
            or proof.object_sha256 != digest
            or proof.size_bytes != size
            or proof.configuration_sha256 != configuration_sha256
            or proof.verifier_sha256 != _remote_read_verifier_sha256()
        ):
            return False
        _verify_file(retained, size, digest)
        return True
    except (OSError, ValueError, WorkspaceV2PhysicalDeltaError):
        return False


def _upload_independent_batches(
    transport: DriveTransport,
    root: Path,
    inventory: Path,
    cache: Path,
    configuration_sha256: str,
) -> None:
    """Keep only 32 block descriptors; reuse verified reads from prior attempts."""
    pending: dict[str, PhysicalDeltaBlock] = {}

    def transfer() -> None:
        keys = tuple(pending)
        for key, block in pending.items():
            _verify_file(_safe_file(root, key), block.length_bytes, block.sha256)
        transport.copy_immutable_batch(root, keys)
        downloaded_root = cache / f".drive-batch-{uuid4().hex}"
        transport.fetch_sealed_batch(keys, downloaded_root)
        # No file from this directory is trusted until its complete digest matches.
        for key, block in pending.items():
            downloaded = _safe_file(downloaded_root, key)
            _verify_file(downloaded, block.length_bytes, block.sha256)
            with downloaded.open("rb") as handle:
                os.fsync(handle.fileno())
            retained = _safe_file(cache, key)
            retained.parent.mkdir(parents=True, exist_ok=True)
            os.replace(downloaded, retained)
            _fsync_directory(retained.parent)
            proof = PhysicalRemoteObjectRead(
                object_key=key,
                object_sha256=block.sha256,
                size_bytes=block.length_bytes,
                configuration_sha256=configuration_sha256,
                verifier_sha256=_remote_read_verifier_sha256(),
                verified_at=datetime.now(UTC),
            )
            _replace_json(
                retained.with_name(retained.name + ".read.json"), proof.model_dump(mode="json")
            )
        # Only empty writer-owned directories are removed. Partial reads survive failure.
        for folder in (
            downloaded_root / "physical-delta/objects",
            downloaded_root / "physical-delta",
            downloaded_root,
        ):
            folder.rmdir()
        pending.clear()

    for item in _iter_delta_files(inventory):
        for block in item.blocks:
            if block.origin != "delta":
                continue
            assert block.object_key is not None
            key = block.object_key
            if key in pending or _verified_independent_read(
                cache, key, block.length_bytes, block.sha256, configuration_sha256
            ):
                continue
            pending[key] = block
            if len(pending) == 32:
                transfer()
    if pending:
        transfer()


def _copy_verified_range(source: BinaryIO, output: BinaryIO, length: int, digest: str) -> None:
    actual = hashlib.sha256()
    remaining = length
    while remaining:
        chunk = source.read(min(remaining, 1024 * 1024))
        if not chunk:
            raise WorkspaceV2PhysicalDeltaError("physical delta recovery range is truncated")
        actual.update(chunk)
        output.write(chunk)
        remaining -= len(chunk)
    if actual.hexdigest() != digest:
        raise WorkspaceV2PhysicalDeltaError("physical delta recovery range is corrupt")


class PhysicalDeltaIndexEntry(ContractModel):
    """One source file range, or an explicit zero-byte file, in its parent backup."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: str = "workspace-v2-physical-delta-entry-v1"
    path: NonEmptyStr
    file_sha256: NonEmptyStr
    file_size_bytes: int = Field(ge=0)
    offset_bytes: int = Field(ge=0)
    length_bytes: int = Field(ge=0)
    fragment_sha256: NonEmptyStr | None = None
    parent_volume_id: NonEmptyStr | None = None
    parent_object_key: NonEmptyStr | None = None
    parent_object_sha256: NonEmptyStr | None = None
    stream_offset_bytes: int | None = Field(default=None, ge=0)

    @field_validator("path")
    @classmethod
    def require_safe_path(cls, value: str) -> str:
        path = Path(value)
        if value == "." or path.is_absolute() or ".." in path.parts or value != path.as_posix():
            raise ValueError("physical delta path must be normalized and relative")
        return value

    @field_validator("file_sha256", "fragment_sha256", "parent_object_sha256", mode="after")
    @classmethod
    def require_digest(cls, value: str | None) -> str | None:
        if value is not None and (
            len(value) != 64 or any(character not in "0123456789abcdef" for character in value)
        ):
            raise ValueError("physical delta digest is invalid")
        return value

    @model_validator(mode="after")
    def require_consistent_range(self) -> PhysicalDeltaIndexEntry:
        parent = (
            self.fragment_sha256,
            self.parent_volume_id,
            self.parent_object_key,
            self.parent_object_sha256,
            self.stream_offset_bytes,
        )
        if self.length_bytes == 0:
            if (
                self.file_size_bytes
                or self.offset_bytes
                or any(value is not None for value in parent)
            ):
                raise ValueError("empty physical delta entry is invalid")
        elif self.offset_bytes + self.length_bytes > self.file_size_bytes or any(
            value is None for value in parent
        ):
            raise ValueError("physical delta range is invalid")
        return self


class PhysicalDeltaIndexTrailer(ContractModel):
    """Hash-bound terminal record for the incremental physical index."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: str = "workspace-v2-physical-delta-trailer-v1"
    parent_backup_id: UUID
    source_workspace_id: UUID
    inventory_sha256: NonEmptyStr
    journal_sha256: NonEmptyStr
    volume_count: int = Field(gt=0)
    file_count: int = Field(ge=0)
    entry_count: int = Field(ge=0)
    logical_bytes: int = Field(ge=0)
    index_sha256: NonEmptyStr

    @field_validator("inventory_sha256", "journal_sha256", "index_sha256")
    @classmethod
    def require_digest(cls, value: str) -> str:
        if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
            raise ValueError("physical delta trailer digest is invalid")
        return value


class PhysicalDeltaIndexManifest(ContractModel):
    """Small immutable control record for a parent-volume physical index."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: str = "workspace-v2-physical-delta-manifest-v1"
    parent_backup_id: UUID
    source_workspace_id: UUID
    inventory_sha256: NonEmptyStr
    journal_sha256: NonEmptyStr
    index_path: NonEmptyStr
    index_sha256: NonEmptyStr
    volume_count: int = Field(gt=0)
    file_count: int = Field(ge=0)
    entry_count: int = Field(ge=0)
    logical_bytes: int = Field(ge=0)
    created_at: UTCDateTime

    @field_validator("index_path")
    @classmethod
    def require_relative_index_path(cls, value: str) -> str:
        path = Path(value)
        if path.is_absolute() or ".." in path.parts or value != path.as_posix():
            raise ValueError("physical delta index path must be normalized and relative")
        return value

    @field_validator("inventory_sha256", "journal_sha256", "index_sha256")
    @classmethod
    def require_digest(cls, value: str) -> str:
        if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
            raise ValueError("physical delta manifest digest is invalid")
        return value


class PhysicalDeltaIndexCheckpoint(ContractModel):
    """Durable cursor for a resumable JSONL physical index derivation."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: str = "workspace-v2-physical-delta-checkpoint-v1"
    phase: Literal["writing", "sealed"]
    parent_backup_id: UUID
    source_workspace_id: UUID
    inventory_sha256: NonEmptyStr
    journal_sha256: NonEmptyStr
    entry_count: int = Field(ge=0)
    partial_index_sha256: NonEmptyStr

    @field_validator("inventory_sha256", "journal_sha256", "partial_index_sha256")
    @classmethod
    def require_digest(cls, value: str) -> str:
        if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
            raise ValueError("physical delta checkpoint digest is invalid")
        return value


class WorkspaceV2PhysicalDeltaIndexService:
    """Derive and verify one resumable physical index without global collections."""

    def __init__(
        self,
        backup_service: WorkspaceV2BackupService,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        checkpoint_interval: int = 256,
    ) -> None:
        if checkpoint_interval <= 0:
            raise ValueError("physical delta checkpoint interval must be positive")
        self._backup_service = backup_service
        self._clock = clock
        self._checkpoint_interval = checkpoint_interval

    def build(self, backup_root: Path, destination: Path) -> PhysicalDeltaIndexManifest:
        """Seal an index, resuming only an exact validated partial prefix."""
        root, backup_manifest = self._inspect(backup_root)
        destination_root = _new_destination(destination)
        index_path = destination_root / "physical-delta-index.jsonl"
        manifest_path = destination_root / "physical-delta-manifest.json"
        checkpoint_path = destination_root / "physical-delta-checkpoint.json"
        if index_path.exists():
            trailer = verify_physical_delta_index(index_path)
            if (
                trailer.parent_backup_id != backup_manifest.backup_id
                or trailer.source_workspace_id != backup_manifest.source_workspace_id
                or trailer.inventory_sha256 != backup_manifest.inventory.digest_sha256
                or trailer.journal_sha256 != backup_manifest.journal_sha256
                or trailer.volume_count != backup_manifest.volume_count
            ):
                raise WorkspaceV2PhysicalDeltaError("physical delta sealed parent binding differs")
            _replace_json(
                checkpoint_path,
                _checkpoint_from_trailer(trailer, phase="sealed").model_dump(mode="json"),
            )
            if manifest_path.exists():
                try:
                    persisted = PhysicalDeltaIndexManifest.model_validate_json(
                        manifest_path.read_text(encoding="utf-8")
                    )
                except (OSError, ValueError) as error:
                    raise WorkspaceV2PhysicalDeltaError(
                        "physical delta manifest is malformed"
                    ) from error
                expected = _manifest_from_trailer(
                    trailer,
                    index_path=index_path.name,
                    created_at=persisted.created_at,
                )
                if persisted != expected:
                    raise WorkspaceV2PhysicalDeltaError("physical delta manifest binding differs")
                return persisted
            manifest = _manifest_from_trailer(
                trailer, index_path=index_path.name, created_at=self._now()
            )
            _write_new_json(manifest_path, manifest.model_dump(mode="json"))
            return manifest
        temporary = _partial_index_path(index_path)
        if temporary.exists() and (temporary.is_symlink() or not temporary.is_file()):
            raise WorkspaceV2PhysicalDeltaError("physical delta partial index is unsafe")
        prefix_count, prefix_digest = self._verify_partial_prefix(temporary, root, backup_manifest)
        expected_checkpoint = _writing_checkpoint(
            backup_manifest,
            entry_count=prefix_count,
            partial_index_sha256=prefix_digest.hexdigest(),
        )
        checkpoint = _read_checkpoint(checkpoint_path)
        if checkpoint is not None and checkpoint != expected_checkpoint:
            raise WorkspaceV2PhysicalDeltaError("physical delta checkpoint binding differs")
        _replace_json(checkpoint_path, expected_checkpoint.model_dump(mode="json"))
        try:
            with temporary.open("a", encoding="utf-8", newline="\n") as stream:
                trailer = self._write_records(
                    stream,
                    root,
                    backup_manifest,
                    skip_count=prefix_count,
                    digest=prefix_digest,
                    checkpoint_path=checkpoint_path,
                )
                stream.write(_compact_json(trailer.model_dump(mode="json")) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, index_path)
            _fsync_directory(destination_root)
        finally:
            if index_path.exists():
                temporary.unlink(missing_ok=True)
        if verify_physical_delta_index(index_path) != trailer:
            raise WorkspaceV2PhysicalDeltaError("physical delta index changed after sealing")
        _replace_json(
            checkpoint_path,
            _checkpoint_from_trailer(trailer, phase="sealed").model_dump(mode="json"),
        )
        manifest = _manifest_from_trailer(
            trailer, index_path=index_path.name, created_at=self._now()
        )
        _write_new_json(manifest_path, manifest.model_dump(mode="json"))
        return manifest

    def _inspect(self, backup_root: Path) -> tuple[Path, WorkspaceV2PackedStreamManifest]:
        try:
            root, manifest = self._backup_service.inspect_packed_for_restore(backup_root)
        except WorkspaceV2BackupError as error:
            raise WorkspaceV2PhysicalDeltaError(
                "physical delta parent backup is not reusable"
            ) from error
        if not isinstance(manifest, WorkspaceV2PackedStreamManifest):
            raise WorkspaceV2PhysicalDeltaError("physical delta requires streamable packed backup")
        _verify_parent_volumes(root, manifest)
        return root, manifest

    def _verify_partial_prefix(
        self,
        partial: Path,
        root: Path,
        manifest: WorkspaceV2PackedStreamManifest,
    ) -> tuple[int, hashlib._Hash]:
        digest = hashlib.sha256()
        if not partial.exists():
            return 0, digest
        count = 0
        expected = _iter_entries(root, manifest)
        try:
            with partial.open("rb") as stream:
                for raw_line in stream:
                    if not raw_line.endswith(b"\n"):
                        raise WorkspaceV2PhysicalDeltaError(
                            "physical delta partial index is incomplete"
                        )
                    try:
                        actual = PhysicalDeltaIndexEntry.model_validate_json(raw_line)
                        wanted = next(expected)
                    except (StopIteration, ValueError) as error:
                        raise WorkspaceV2PhysicalDeltaError(
                            "physical delta partial index binding differs"
                        ) from error
                    if actual != wanted:
                        raise WorkspaceV2PhysicalDeltaError(
                            "physical delta partial index binding differs"
                        )
                    digest.update(raw_line)
                    count += 1
        except OSError as error:
            raise WorkspaceV2PhysicalDeltaError(
                "physical delta partial index is unavailable"
            ) from error
        return count, digest

    def _write_records(
        self,
        stream: object,
        root: Path,
        manifest: WorkspaceV2PackedStreamManifest,
        *,
        skip_count: int,
        digest: hashlib._Hash,
        checkpoint_path: Path,
    ) -> PhysicalDeltaIndexTrailer:
        file_count = 0
        entry_count = 0
        logical_bytes = 0
        for entry in _iter_entries(root, manifest):
            entry_count += 1
            if entry.offset_bytes == 0:
                file_count += 1
                logical_bytes += entry.file_size_bytes
            encoded = (_compact_json(entry.model_dump(mode="json")) + "\n").encode("utf-8")
            if entry_count > skip_count:
                stream.write(encoded.decode("utf-8"))
                digest.update(encoded)
                if entry_count % self._checkpoint_interval == 0:
                    stream.flush()
                    os.fsync(stream.fileno())
                    _replace_json(
                        checkpoint_path,
                        _writing_checkpoint(
                            manifest,
                            entry_count=entry_count,
                            partial_index_sha256=digest.hexdigest(),
                        ).model_dump(mode="json"),
                    )
        if entry_count < skip_count:
            raise WorkspaceV2PhysicalDeltaError("physical delta partial index exceeds parent")
        return PhysicalDeltaIndexTrailer(
            parent_backup_id=manifest.backup_id,
            source_workspace_id=manifest.source_workspace_id,
            inventory_sha256=manifest.inventory.digest_sha256,
            journal_sha256=manifest.journal_sha256,
            volume_count=manifest.volume_count,
            file_count=file_count,
            entry_count=entry_count,
            logical_bytes=logical_bytes,
            index_sha256=digest.hexdigest(),
        )

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise WorkspaceV2PhysicalDeltaError("physical delta clock must be timezone-aware")
        return value.astimezone(UTC)


def _iter_entries(
    root: Path, manifest: WorkspaceV2PackedStreamManifest
) -> Iterator[PhysicalDeltaIndexEntry]:
    """Join one inventory entry and one packed fragment at a time."""
    entries = iter_inventory(root / "source-inventory.jsonl")
    current = _next_inventory_entry(entries, previous_path=None)
    offset = 0
    last_path: str | None = None
    journal_digest = hashlib.sha256()
    volume_count = 0
    try:
        for volume in _iter_packed_journal(
            root / "packed-backup-volumes.jsonl", digest=journal_digest
        ):
            volume_count += 1
            for fragment in volume.fragments:
                while current is not None and current[1] == 0:
                    path, _, file_digest = current
                    yield PhysicalDeltaIndexEntry(
                        path=path,
                        file_sha256=file_digest,
                        file_size_bytes=0,
                        offset_bytes=0,
                        length_bytes=0,
                    )
                    last_path = path
                    current = _next_inventory_entry(entries, previous_path=last_path)
                if current is None:
                    raise WorkspaceV2PhysicalDeltaError("packed journal exceeds parent inventory")
                path, size, file_digest = current
                if (
                    fragment.path != path
                    or fragment.offset_bytes != offset
                    or fragment.length_bytes > size - offset
                    or fragment.stream_offset_bytes < 0
                ):
                    raise WorkspaceV2PhysicalDeltaError(
                        "packed journal mapping differs from inventory"
                    )
                yield PhysicalDeltaIndexEntry(
                    path=path,
                    file_sha256=file_digest,
                    file_size_bytes=size,
                    offset_bytes=offset,
                    length_bytes=fragment.length_bytes,
                    fragment_sha256=fragment.sha256,
                    parent_volume_id=volume.volume_id,
                    parent_object_key=f"volumes/{volume.compressed_sha256}.gz",
                    parent_object_sha256=volume.compressed_sha256,
                    stream_offset_bytes=fragment.stream_offset_bytes,
                )
                offset += fragment.length_bytes
                if offset == size:
                    last_path = path
                    current = _next_inventory_entry(entries, previous_path=last_path)
                    offset = 0
    except (WorkspaceV2BackupError, WorkspaceV2BackupInventoryError) as error:
        raise WorkspaceV2PhysicalDeltaError(
            "physical delta parent sidecars are malformed"
        ) from error
    while current is not None and current[1] == 0:
        path, _, file_digest = current
        yield PhysicalDeltaIndexEntry(
            path=path,
            file_sha256=file_digest,
            file_size_bytes=0,
            offset_bytes=0,
            length_bytes=0,
        )
        last_path = path
        current = _next_inventory_entry(entries, previous_path=last_path)
    if current is not None or offset:
        raise WorkspaceV2PhysicalDeltaError("packed journal does not cover parent inventory")
    if (
        volume_count != manifest.volume_count
        or journal_digest.hexdigest() != manifest.journal_sha256
    ):
        raise WorkspaceV2PhysicalDeltaError("packed journal binding differs from parent manifest")


def _next_inventory_entry(
    entries: Iterator[dict[str, object]], *, previous_path: str | None
) -> tuple[str, int, str] | None:
    try:
        entry = next(entries)
    except StopIteration:
        return None
    path = entry["path"]
    size = entry["size_bytes"]
    digest = entry["sha256"]
    if (
        not isinstance(path, str)
        or not isinstance(size, int)
        or isinstance(size, bool)
        or size < 0
        or not isinstance(digest, str)
        or len(digest) != 64
        or (previous_path is not None and _path_order(path) <= _path_order(previous_path))
    ):
        raise WorkspaceV2PhysicalDeltaError("parent inventory ordering is invalid")
    return path, size, digest


def _verify_parent_volumes(root: Path, manifest: WorkspaceV2PackedStreamManifest) -> None:
    """Read each parent volume once before declaring it reusable."""
    count = 0
    journal_digest = hashlib.sha256()
    try:
        for volume in _iter_packed_journal(
            root / "packed-backup-volumes.jsonl", digest=journal_digest
        ):
            candidate = root / volume.path
            if (
                candidate.is_symlink()
                or not candidate.is_file()
                or candidate.stat().st_size != volume.compressed_bytes
                or _sha256(candidate) != volume.compressed_sha256
            ):
                raise WorkspaceV2PhysicalDeltaError(
                    "physical delta parent volume is absent or corrupt"
                )
            count += 1
    except WorkspaceV2BackupError as error:
        raise WorkspaceV2PhysicalDeltaError("physical delta parent journal is malformed") from error
    if count != manifest.volume_count or journal_digest.hexdigest() != manifest.journal_sha256:
        raise WorkspaceV2PhysicalDeltaError("physical delta parent journal binding differs")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_physical_delta_index(path: Path) -> PhysicalDeltaIndexTrailer:
    """Validate the index in one pass without retaining its records."""
    if not path.is_absolute() or any(part.is_symlink() for part in (path, *path.parents)):
        raise WorkspaceV2PhysicalDeltaError("physical delta index is unavailable or unsafe")
    target = path.expanduser().resolve(strict=False)
    if not target.is_absolute() or target.is_symlink() or not target.is_file():
        raise WorkspaceV2PhysicalDeltaError("physical delta index is unavailable or unsafe")
    digest = hashlib.sha256()
    count = 0
    files = 0
    bytes_total = 0
    previous_path: str | None = None
    previous_offset = 0
    previous_size = 0
    try:
        with target.open("rb") as stream:
            for raw_line in stream:
                if not raw_line.endswith(b"\n"):
                    raise WorkspaceV2PhysicalDeltaError("physical delta index is incomplete")
                document = json.loads(raw_line)
                if isinstance(document, dict) and document.get("schema_version") == (
                    "workspace-v2-physical-delta-trailer-v1"
                ):
                    trailer = PhysicalDeltaIndexTrailer.model_validate_json(raw_line)
                    if stream.read(1):
                        raise WorkspaceV2PhysicalDeltaError(
                            "physical delta trailer is not terminal"
                        )
                    if (
                        trailer.index_sha256 != digest.hexdigest()
                        or trailer.entry_count != count
                        or trailer.file_count != files
                        or trailer.logical_bytes != bytes_total
                    ):
                        raise WorkspaceV2PhysicalDeltaError(
                            "physical delta trailer does not bind entries"
                        )
                    if previous_path is not None and previous_offset != previous_size:
                        raise WorkspaceV2PhysicalDeltaError(
                            "physical delta entries do not cover a complete file"
                        )
                    return trailer
                entry = PhysicalDeltaIndexEntry.model_validate_json(raw_line)
                if entry.path != previous_path:
                    if previous_path is not None and _path_order(entry.path) <= _path_order(
                        previous_path
                    ):
                        raise WorkspaceV2PhysicalDeltaError(
                            "physical delta path reappears after completion"
                        )
                    if previous_path is not None and previous_offset != previous_size:
                        raise WorkspaceV2PhysicalDeltaError(
                            "physical delta entries do not cover a complete file"
                        )
                    previous_path = entry.path
                    previous_offset = 0
                    previous_size = entry.file_size_bytes
                    files += 1
                    bytes_total += entry.file_size_bytes
                elif entry.file_size_bytes != previous_size:
                    raise WorkspaceV2PhysicalDeltaError("physical delta file size binding differs")
                if entry.offset_bytes != previous_offset:
                    raise WorkspaceV2PhysicalDeltaError(
                        "physical delta entries have a gap or overlap"
                    )
                previous_offset += entry.length_bytes
                if previous_offset > entry.file_size_bytes:
                    raise WorkspaceV2PhysicalDeltaError("physical delta entry exceeds its file")
                digest.update(raw_line)
                count += 1
    except (OSError, UnicodeError, ValueError) as error:
        raise WorkspaceV2PhysicalDeltaError("physical delta index is malformed") from error
    raise WorkspaceV2PhysicalDeltaError("physical delta index is missing its trailer")


def iter_physical_delta_index(path: Path) -> Iterator[PhysicalDeltaIndexEntry]:
    """Yield index entries incrementally, validating the sealed trailer on exhaustion."""
    if not path.is_absolute() or any(part.is_symlink() for part in (path, *path.parents)):
        raise WorkspaceV2PhysicalDeltaError("physical delta index is unavailable or unsafe")
    target = path.expanduser().resolve(strict=False)
    if not target.is_absolute() or target.is_symlink() or not target.is_file():
        raise WorkspaceV2PhysicalDeltaError("physical delta index is unavailable or unsafe")
    digest = hashlib.sha256()
    try:
        with target.open("rb") as stream:
            for raw_line in stream:
                if not raw_line.endswith(b"\n"):
                    raise WorkspaceV2PhysicalDeltaError("physical delta index is incomplete")
                document = json.loads(raw_line)
                if isinstance(document, dict) and document.get("schema_version") == (
                    "workspace-v2-physical-delta-trailer-v1"
                ):
                    trailer = PhysicalDeltaIndexTrailer.model_validate_json(raw_line)
                    if stream.read(1) or trailer.index_sha256 != digest.hexdigest():
                        raise WorkspaceV2PhysicalDeltaError(
                            "physical delta trailer does not bind entries"
                        )
                    return
                entry = PhysicalDeltaIndexEntry.model_validate_json(raw_line)
                digest.update(raw_line)
                yield entry
    except (OSError, UnicodeError, ValueError) as error:
        raise WorkspaceV2PhysicalDeltaError("physical delta index is malformed") from error
    raise WorkspaceV2PhysicalDeltaError("physical delta index is missing its trailer")


def _new_destination(path: Path) -> Path:
    if not path.is_absolute() or any(part.is_symlink() for part in (path, *path.parents)):
        raise WorkspaceV2PhysicalDeltaError("physical delta destination is unsafe")
    target = path.expanduser().resolve(strict=False)
    if not target.is_absolute() or target.is_symlink():
        raise WorkspaceV2PhysicalDeltaError("physical delta destination is unsafe")
    target.mkdir(parents=True, exist_ok=True)
    if target.is_symlink() or not target.is_dir():
        raise WorkspaceV2PhysicalDeltaError("physical delta destination is unsafe")
    return target


def _partial_index_path(index_path: Path) -> Path:
    candidates = tuple(
        item
        for item in index_path.parent.glob(f".{index_path.name}.*.tmp")
        if item.is_file() and not item.is_symlink()
    )
    if len(candidates) > 1:
        raise WorkspaceV2PhysicalDeltaError("physical delta has ambiguous partial progress")
    return (
        candidates[0]
        if candidates
        else index_path.with_name(f".{index_path.name}.{uuid4().hex}.tmp")
    )


def _manifest_from_trailer(
    trailer: PhysicalDeltaIndexTrailer, *, index_path: str, created_at: datetime
) -> PhysicalDeltaIndexManifest:
    return PhysicalDeltaIndexManifest(
        parent_backup_id=trailer.parent_backup_id,
        source_workspace_id=trailer.source_workspace_id,
        inventory_sha256=trailer.inventory_sha256,
        journal_sha256=trailer.journal_sha256,
        index_path=index_path,
        index_sha256=trailer.index_sha256,
        volume_count=trailer.volume_count,
        file_count=trailer.file_count,
        entry_count=trailer.entry_count,
        logical_bytes=trailer.logical_bytes,
        created_at=created_at,
    )


def _checkpoint_from_trailer(
    trailer: PhysicalDeltaIndexTrailer, *, phase: Literal["writing", "sealed"]
) -> PhysicalDeltaIndexCheckpoint:
    return PhysicalDeltaIndexCheckpoint(
        phase=phase,
        parent_backup_id=trailer.parent_backup_id,
        source_workspace_id=trailer.source_workspace_id,
        inventory_sha256=trailer.inventory_sha256,
        journal_sha256=trailer.journal_sha256,
        entry_count=trailer.entry_count,
        partial_index_sha256=trailer.index_sha256,
    )


def _writing_checkpoint(
    manifest: WorkspaceV2PackedStreamManifest,
    *,
    entry_count: int,
    partial_index_sha256: str,
) -> PhysicalDeltaIndexCheckpoint:
    return PhysicalDeltaIndexCheckpoint(
        phase="writing",
        parent_backup_id=manifest.backup_id,
        source_workspace_id=manifest.source_workspace_id,
        inventory_sha256=manifest.inventory.digest_sha256,
        journal_sha256=manifest.journal_sha256,
        entry_count=entry_count,
        partial_index_sha256=partial_index_sha256,
    )


def _compact_json(document: object) -> str:
    return json.dumps(document, sort_keys=True, separators=(",", ":"))


def _write_new_json(path: Path, document: object) -> None:
    if path.exists() or path.is_symlink():
        raise WorkspaceV2PhysicalDeltaError("physical delta manifest path is unavailable")
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(_compact_json(document))
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    _fsync_directory(path.parent)


def _replace_json(path: Path, document: object) -> None:
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(_compact_json(document))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _read_checkpoint(path: Path) -> PhysicalDeltaIndexCheckpoint | None:
    if not path.exists():
        return None
    if path.is_symlink() or not path.is_file():
        raise WorkspaceV2PhysicalDeltaError("physical delta checkpoint is unavailable or unsafe")
    try:
        return PhysicalDeltaIndexCheckpoint.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise WorkspaceV2PhysicalDeltaError("physical delta checkpoint is malformed") from error


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


__all__ = [
    "PhysicalDeltaBlock",
    "PhysicalDeltaFile",
    "PhysicalDeltaManifest",
    "PhysicalDeltaPlanCheckpoint",
    "PhysicalDeltaReceipt",
    "PhysicalDeltaTransferCheckpoint",
    "PhysicalDeltaIndexEntry",
    "PhysicalDeltaIndexCheckpoint",
    "PhysicalDeltaIndexManifest",
    "PhysicalDeltaIndexTrailer",
    "WorkspaceV2PhysicalDeltaError",
    "WorkspaceV2PhysicalDeltaIndexService",
    "WorkspaceV2PhysicalDeltaService",
    "iter_physical_delta_index",
    "verify_physical_delta_index",
]


def _verify_full_recovery_coverage(
    root: Path, inventory: Path, manifest: PhysicalDeltaManifest, connection: object
) -> dict[str, int | str]:
    """Check complete paths and raw-index membership without hydrating payloads.

    Content integrity comes from the sealed physical restore. This additional
    check detects missing/extra paths and unmatched raw index entries; it is
    separate from a compatible reader's bounded model sample.
    """
    from duckdb import DuckDBPyConnection

    from investment_analyst.storage.resource_policy import execute_bounded

    if not isinstance(connection, DuckDBPyConnection):
        raise WorkspaceV2PhysicalDeltaError("raw coverage requires its admitted native connection")
    databases = execute_bounded(connection, "PRAGMA database_list").fetchall()
    if {Path(str(row[2])).resolve() for row in databases if row[2]} != {
        _safe_file(root, "storage/v2/index.duckdb").resolve()
    }:
        raise WorkspaceV2PhysicalDeltaError("raw coverage native database binding differs")
    _validate_delta_inventory(inventory, manifest)
    actual = iter(_source_files(_safe_root(root)))
    files = total = raw_files = 0
    pending: list[tuple[str, str]] = []
    table = "_physical_raw_coverage_" + uuid4().hex
    execute_bounded(connection, f"CREATE TEMP TABLE {table}(path VARCHAR,checksum VARCHAR)")
    try:
        for item in _iter_delta_files(inventory):
            if item.deleted:
                continue
            if (
                next(actual, None) != item.path
                or _safe_file(root, item.path).stat().st_size != item.size_bytes
            ):
                raise WorkspaceV2PhysicalDeltaError("recovery physical path coverage differs")
            files += 1
            total += item.size_bytes
            if item.path.startswith("storage/v2/raw/raw/sha256/"):
                if Path(item.path).name != item.sha256:
                    raise WorkspaceV2PhysicalDeltaError("recovery raw file address differs")
                pending.append((item.path.removeprefix("storage/v2/raw/"), item.sha256))
                raw_files += 1
                if len(pending) == 256:
                    connection.executemany(f"INSERT INTO {table} VALUES (?,?)", pending)
                    pending.clear()
        if pending:
            connection.executemany(f"INSERT INTO {table} VALUES (?,?)", pending)
        if next(actual, None) is not None or (files, total) != (
            manifest.file_count,
            manifest.logical_bytes,
        ):
            raise WorkspaceV2PhysicalDeltaError("recovery physical inventory coverage differs")
        mismatch = execute_bounded(
            connection,
            f"SELECT count(*) FROM raw_v2_index r LEFT JOIN {table} f "
            "ON r.relative_path=f.path WHERE f.path IS NULL "
            "OR r.checksum_sha256<>f.checksum OR r.relative_path<>"
            "('raw/sha256/'||substr(r.checksum_sha256,1,2)||'/'||"
            "substr(r.checksum_sha256,3,2)||'/'||r.checksum_sha256)",
        ).fetchone()
        orphan = execute_bounded(
            connection,
            f"SELECT count(*) FROM {table} f WHERE NOT EXISTS "
            "(SELECT 1 FROM raw_v2_index r WHERE r.relative_path=f.path "
            "AND r.checksum_sha256=f.checksum)",
        ).fetchone()
        if mismatch is None or orphan is None or mismatch[0] or orphan[0]:
            raise WorkspaceV2PhysicalDeltaError("recovery complete raw membership differs")
        digest = hashlib.sha256(b"").hexdigest()
        count = 0
        cursor = execute_bounded(
            connection,
            "SELECT checksum_sha256 FROM raw_v2_index "
            "ORDER BY CAST(received_at AS TIMESTAMPTZ),record_id",
        )
        while rows := cursor.fetchmany(256):
            for row in rows:
                checksum = str(row[0])
                if not _valid_digest(checksum):
                    raise WorkspaceV2PhysicalDeltaError("recovery raw index digest is invalid")
                digest = hashlib.sha256(f"{digest}:{checksum}".encode()).hexdigest()
                count += 1
        if count != raw_files:
            raise WorkspaceV2PhysicalDeltaError("recovery raw membership cardinality differs")
        return {
            "physical_files": files,
            "physical_bytes": total,
            "raw_files": raw_files,
            "raw_indexed": count,
            "raw_corpus_digest": digest,
        }
    finally:
        execute_bounded(connection, f"DROP TABLE temp.main.{table}")


class ImmutableLinkIdentity(ContractModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")
    device: int = Field(ge=0)
    inode: int = Field(ge=0)
    size: int = Field(ge=0)
    mtime_ns: int = Field(ge=0)
    ctime_ns: int = Field(ge=0)
    nlink: int = Field(ge=1)

    @classmethod
    def observe(cls, path: Path) -> ImmutableLinkIdentity:
        if path.is_symlink() or not path.is_file():
            raise WorkspaceV2PhysicalDeltaError("immutable link member is unsafe")
        metadata = path.stat()
        return cls(
            device=metadata.st_dev,
            inode=metadata.st_ino,
            size=metadata.st_size,
            mtime_ns=metadata.st_mtime_ns,
            ctime_ns=metadata.st_ctime_ns,
            nlink=metadata.st_nlink,
        )


class ImmutableLinkHeader(ContractModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")
    schema_version: Literal["workspace-v2-immutable-link-journal-v1"] = (
        "workspace-v2-immutable-link-journal-v1"
    )
    operation_id: UUID
    action: Literal["LINK", "UNLINK"]
    retained_root: Path
    retained_workspace_id: UUID
    slot: Path
    inventory: ReconciliationFileReference
    parent_manifest: ReconciliationFileReference
    parent_index: ReconciliationFileReference
    parent_proof: ReconciliationFileReference
    previous_journal: ReconciliationFileReference | None = None
    verifier_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    policy: Literal["controlled-immutable-links-v1"] = "controlled-immutable-links-v1"
    byte_budget: int = Field(gt=0)
    created_at: UTCDateTime

    @model_validator(mode="after")
    def disjoint_roots(self) -> ImmutableLinkHeader:
        if (
            not self.slot.is_absolute()
            or not self.retained_root.is_absolute()
            or self.slot == self.retained_root
            or self.slot.is_relative_to(self.retained_root)
            or self.retained_root.is_relative_to(self.slot)
        ):
            raise ValueError("immutable link roots must be absolute and disjoint")
        return self


class ImmutableLinkEntry(ContractModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")
    ordinal: int = Field(ge=0)
    inventory_ordinal: int = Field(ge=0)
    action: Literal["LINK", "UNLINK"]
    phase: Literal["BASELINE", "INTENT", "CONFIRMED"]
    before: ImmutableLinkIdentity | None = None
    after: ImmutableLinkIdentity | None = None
    recorded_at: UTCDateTime

    @model_validator(mode="after")
    def explicit_preimage(self) -> ImmutableLinkEntry:
        if self.phase == "BASELINE":
            if self.before is not None or self.after is None:
                raise ValueError("adopted link has no invented historical preimage")
        elif self.before is None or ((self.phase == "CONFIRMED") != (self.after is not None)):
            raise ValueError("link intent/confirmation identity is incomplete")
        return self


class ImmutableLinkTrailer(ContractModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")
    page: int = Field(ge=0)
    entries: int = Field(ge=1, le=256)
    intent_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    confirmed_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    previous_trailer_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    confirmed_bytes: int = Field(ge=0)
    generation: UUID


class ImmutableLinkJournal:
    """This backup/restore's bounded link witness, not a general filesystem cache."""

    def __init__(self, root: Path, header: ImmutableLinkHeader) -> None:
        if not root.is_absolute() or any(p.is_symlink() for p in (root, *root.parents)):
            raise WorkspaceV2PhysicalDeltaError("link journal path is unsafe")
        if any(root == p or root.is_relative_to(p) for p in (header.retained_root, header.slot)):
            raise WorkspaceV2PhysicalDeltaError("link journal must survive slot retirement")
        self.root, self.header = root, header
        self._ordinal = 0
        self._inventory = iter(enumerate(_iter_delta_files(header.inventory.path)))
        self._current_item: tuple[int, PhysicalDeltaFile] | None = None
        self._budget_used = 0
        self._previous: str | None = None
        self._page = 0
        self._prior: Iterator[ImmutableLinkEntry] | None = None

    def open(self) -> ImmutableLinkJournal:
        from investment_analyst.storage.workspace_v2_inventory import WorkspaceV2ProgressReconciler

        if self.header.verifier_sha256 != _immutable_link_verifier_sha256():
            raise WorkspaceV2PhysicalDeltaError("immutable link verifier policy changed")
        for reference in (
            self.header.inventory,
            self.header.parent_manifest,
            self.header.parent_index,
            self.header.parent_proof,
        ):
            WorkspaceV2ProgressReconciler._verify(reference)
        self.root.mkdir(parents=True, exist_ok=True)
        document = self.header.model_dump_json().encode() + b"\n"
        self._seal(self.root / "header.json", document)
        self._budget_used = sum(p.stat().st_size for p in self.root.iterdir() if p.is_file())
        if self._budget_used > self.header.byte_budget:
            raise WorkspaceV2PhysicalDeltaError("immutable link journal budget exceeded")
        while (self.root / f"page-{self._page:08d}.trailer.json").exists():
            trailer, entries = self._read_page(self.root, self._page, self._previous)
            if trailer.generation != self.header.operation_id:
                raise WorkspaceV2PhysicalDeltaError("immutable link journal generation differs")
            for entry in entries:
                if entry.ordinal != self._ordinal:
                    raise WorkspaceV2PhysicalDeltaError("immutable link journal ordinal differs")
                for current in self._inventory:
                    self._current_item = current
                    if current[0] >= entry.inventory_ordinal:
                        break
                if self._current_item is None or self._current_item[0] != entry.inventory_ordinal:
                    raise WorkspaceV2PhysicalDeltaError(
                        "immutable link journal inventory is missing"
                    )
                self._ordinal += 1
            self._previous = _sha256(self.root / f"page-{self._page:08d}.trailer.json")
            self._page += 1
        if self.header.previous_journal is not None:
            WorkspaceV2ProgressReconciler._verify(self.header.previous_journal)
            self._prior = self._prior_witnesses(self.header.previous_journal)
            for _ in range(self._ordinal):
                if next(self._prior, None) is None:
                    raise WorkspaceV2PhysicalDeltaError("prior link witness prefix is incomplete")
        return self

    @property
    def confirmed_inventory_ordinal(self) -> int:
        return -1 if self._current_item is None else self._current_item[0]

    @staticmethod
    def _read_page(
        root: Path,
        page: int,
        previous: str | None,
    ) -> tuple[ImmutableLinkTrailer, tuple[ImmutableLinkEntry, ...]]:
        trailer_path = _safe_file(root, f"page-{page:08d}.trailer.json")
        trailer = ImmutableLinkTrailer.model_validate_json(trailer_path.read_bytes())
        intent_path = _safe_file(root, f"page-{page:08d}.intents.jsonl")
        confirmed_path = _safe_file(root, f"page-{page:08d}.confirmed.jsonl")
        if (
            trailer.page != page
            or trailer.previous_trailer_sha256 != previous
            or _sha256(intent_path) != trailer.intent_sha256
            or _sha256(confirmed_path) != trailer.confirmed_sha256
            or confirmed_path.stat().st_size != trailer.confirmed_bytes
        ):
            raise WorkspaceV2PhysicalDeltaError("immutable link page seal differs")
        with confirmed_path.open("rb") as stream:
            entries: list[ImmutableLinkEntry] = []
            for line in stream:
                if len(entries) == 256 or len(line) > 1 << 20:
                    raise WorkspaceV2PhysicalDeltaError("immutable link page exceeds bounds")
                entries.append(ImmutableLinkEntry.model_validate_json(line))
        if len(entries) != trailer.entries or any(e.after is None for e in entries):
            raise WorkspaceV2PhysicalDeltaError("immutable link page coverage differs")
        return trailer, tuple(entries)

    def _prior_witnesses(
        self, reference: ReconciliationFileReference
    ) -> Iterator[ImmutableLinkEntry]:
        root = reference.path.parent
        header = ImmutableLinkHeader.model_validate_json((root / "header.json").read_bytes())
        if (
            header.retained_root != self.header.retained_root
            or header.slot != self.header.slot
            or header.retained_workspace_id != self.header.retained_workspace_id
            or header.inventory != self.header.inventory
            or header.verifier_sha256 != self.header.verifier_sha256
            or header.policy != self.header.policy
        ):
            raise WorkspaceV2PhysicalDeltaError("prior immutable link journal binding differs")
        previous = None
        page = 0
        while True:
            _, entries = self._read_page(root, page, previous)
            yield from entries
            path = root / f"page-{page:08d}.trailer.json"
            previous = _sha256(path)
            if path == reference.path:
                return
            page += 1

    def _seal(self, path: Path, document: bytes) -> None:
        from investment_analyst.storage.workspace_v2_inventory import WorkspaceV2ProgressReconciler

        existed = path.exists()
        if self._budget_used + (0 if existed else len(document)) > self.header.byte_budget:
            raise WorkspaceV2PhysicalDeltaError("immutable link journal budget exceeded")
        WorkspaceV2ProgressReconciler._write_once(path, document)
        if not existed:
            self._budget_used += len(document)

    def _member(self, ordinal: int, item: PhysicalDeltaFile) -> tuple[Path, Path]:
        if self._current_item is not None and ordinal <= self._current_item[0]:
            raise WorkspaceV2PhysicalDeltaError("link journal inventory order differs")
        for current in self._inventory:
            if current[0] >= ordinal:
                self._current_item = current
                break
        if self._current_item != (ordinal, item):
            raise WorkspaceV2PhysicalDeltaError("link journal sealed member differs")
        if (
            not (
                item.path.startswith("storage/v2/raw/raw/sha256/")
                or item.path.startswith("data/documents/sha256/")
            )
            or Path(item.path).name != item.sha256
            or item.deleted
        ):
            raise WorkspaceV2PhysicalDeltaError("mutable or non-addressed file cannot be linked")
        return _safe_file(self.header.retained_root, item.path), _safe_file(
            self.header.slot, item.path
        )

    @staticmethod
    def _same_content(before: ImmutableLinkIdentity, after: ImmutableLinkIdentity) -> bool:
        return (before.device, before.inode, before.size, before.mtime_ns) == (
            after.device,
            after.inode,
            after.size,
            after.mtime_ns,
        )

    def apply_page(
        self,
        members: tuple[tuple[int, PhysicalDeltaFile], ...],
        *,
        adopt_existing: bool = False,
    ) -> ImmutableLinkTrailer:
        """Seal intents before filesystem work; a crash tail is rehashed, never truncated."""
        if not 1 <= len(members) <= 256:
            raise WorkspaceV2PhysicalDeltaError("link journal requires one bounded ordered page")
        if adopt_existing and self.header.action != "LINK":
            raise WorkspaceV2PhysicalDeltaError("adoption describes existing links only")
        prepared: list[tuple[Path, Path, PhysicalDeltaFile, ImmutableLinkEntry]] = []
        intent_path = self.root / f"page-{self._page:08d}.intents.jsonl"
        pending: list[ImmutableLinkEntry] = []
        if intent_path.exists():
            with intent_path.open("rb") as stream:
                for line in stream:
                    if len(pending) == 256 or len(line) > 1 << 20:
                        raise WorkspaceV2PhysicalDeltaError(
                            "pending immutable link page exceeds bounds"
                        )
                    pending.append(ImmutableLinkEntry.model_validate_json(line))
            if len(pending) != len(members):
                raise WorkspaceV2PhysicalDeltaError(
                    "pending immutable link page membership differs"
                )
        for offset, (ordinal, item) in enumerate(members):
            retained, slot = self._member(ordinal, item)
            before = ImmutableLinkIdentity.observe(retained)
            if before.size != item.size_bytes:
                raise WorkspaceV2PhysicalDeltaError("immutable link member length differs")
            witness = next(self._prior, None) if self._prior is not None else None
            if self._prior is not None and witness is None:
                raise WorkspaceV2PhysicalDeltaError("prior immutable link witness is missing")
            if witness is not None and not pending:
                if witness.inventory_ordinal != ordinal or before != witness.after:
                    raise WorkspaceV2PhysicalDeltaError("immutable link witness metadata changed")
            else:
                # Prior links have no historical preimage. Establish current content
                # once before trusting any future metadata transition.
                _verify_file(retained, item.size_bytes, item.sha256)
                if ImmutableLinkIdentity.observe(retained) != before:
                    raise WorkspaceV2PhysicalDeltaError(
                        "immutable link changed during verification"
                    )
            entry = ImmutableLinkEntry(
                ordinal=self._ordinal + offset,
                inventory_ordinal=ordinal,
                action=self.header.action,
                phase="BASELINE" if adopt_existing else "INTENT",
                before=None if adopt_existing else before,
                after=before if adopt_existing else None,
                recorded_at=datetime.now(UTC),
            )
            if pending:
                entry = pending[offset]
                if (
                    entry.ordinal != self._ordinal + offset
                    or entry.inventory_ordinal != ordinal
                    or entry.action != self.header.action
                    or entry.phase != ("BASELINE" if adopt_existing else "INTENT")
                ):
                    raise WorkspaceV2PhysicalDeltaError("pending immutable link intent differs")
            prepared.append((retained, slot, item, entry))
        intent = b"".join(e.model_dump_json().encode() + b"\n" for *_, e in prepared)
        self._seal(intent_path, intent)
        confirmed: list[ImmutableLinkEntry] = []
        for retained, slot, item, entry in prepared:
            before = entry.after if adopt_existing else entry.before
            assert before is not None
            observed = ImmutableLinkIdentity.observe(retained)
            expected_links = before.nlink + (
                0 if adopt_existing else 1 if self.header.action == "LINK" else -1
            )
            recovered = False
            if observed != before:
                slot_matches = (
                    ImmutableLinkIdentity.observe(slot) == observed if slot.exists() else False
                )
                if not (
                    pending
                    and not adopt_existing
                    and self._same_content(before, observed)
                    and observed.nlink == expected_links
                    and (
                        (self.header.action == "LINK" and slot_matches)
                        or (self.header.action == "UNLINK" and not slot.exists())
                    )
                ):
                    raise WorkspaceV2PhysicalDeltaError("immutable link preimage changed")
                _verify_file(retained, item.size_bytes, item.sha256)
                recovered = True
            if not recovered and (adopt_existing or self.header.action == "UNLINK"):
                linked = ImmutableLinkIdentity.observe(slot)
                if linked != before:
                    raise WorkspaceV2PhysicalDeltaError("slot does not share the witnessed inode")
            if not adopt_existing and not recovered:
                if self.header.action == "LINK":
                    slot.parent.mkdir(parents=True, exist_ok=True)
                    os.link(retained, slot, follow_symlinks=False)
                else:
                    slot.unlink()
                descriptor = os.open(slot.parent, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
            after = ImmutableLinkIdentity.observe(retained)
            if not self._same_content(before, after) or after.nlink != expected_links:
                raise WorkspaceV2PhysicalDeltaError("immutable link transition differs")
            confirmed.append(
                ImmutableLinkEntry(
                    ordinal=entry.ordinal,
                    inventory_ordinal=entry.inventory_ordinal,
                    action=entry.action,
                    phase="BASELINE" if adopt_existing else "CONFIRMED",
                    before=None if adopt_existing else before,
                    after=after,
                    recorded_at=datetime.now(UTC),
                )
            )
        document = b"".join(e.model_dump_json().encode() + b"\n" for e in confirmed)
        path = self.root / f"page-{self._page:08d}.confirmed.jsonl"
        if path.exists():
            previous_document = path.read_bytes()
            previous_entries = tuple(
                ImmutableLinkEntry.model_validate_json(line)
                for line in previous_document.splitlines()
            )
            if len(previous_entries) != len(confirmed) or any(
                previous.model_copy(update={"recorded_at": actual.recorded_at}) != actual
                for previous, actual in zip(previous_entries, confirmed, strict=True)
            ):
                raise WorkspaceV2PhysicalDeltaError("uncommitted link confirmations differ")
            document = previous_document
        self._seal(path, document)
        trailer = ImmutableLinkTrailer(
            page=self._page,
            entries=len(confirmed),
            intent_sha256=_sha256(intent_path),
            confirmed_sha256=_sha256(path),
            previous_trailer_sha256=self._previous,
            confirmed_bytes=len(document),
            generation=self.header.operation_id,
        )
        trailer_path = self.root / f"page-{self._page:08d}.trailer.json"
        self._seal(trailer_path, trailer.model_dump_json().encode() + b"\n")
        self._previous = _sha256(trailer_path)
        self._ordinal += len(confirmed)
        self._page += 1
        return trailer


def _immutable_link_verifier_sha256() -> str:
    """Bind this narrow witness policy without invalidating independent Drive reads."""
    digest = hashlib.sha256()
    for value in (
        ImmutableLinkIdentity,
        ImmutableLinkHeader,
        ImmutableLinkEntry,
        ImmutableLinkTrailer,
        ImmutableLinkJournal,
        _verify_file,
        _safe_file,
        _immutable_link_verifier_sha256,
    ):
        digest.update(inspect.getsource(value).encode())
    return digest.hexdigest()
