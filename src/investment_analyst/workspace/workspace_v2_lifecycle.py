"""Service-owned preservation and retirement of isolated workspace snapshots."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from collections.abc import Callable, Iterable
from contextlib import contextmanager
from datetime import UTC, datetime
from itertools import zip_longest
from pathlib import Path, PurePosixPath
from uuid import UUID, uuid4

from investment_analyst.workspace.models import WorkspaceInspection
from investment_analyst.workspace.service import WorkspaceError, WorkspaceService
from investment_analyst.workspace.workspace_v2_backup import (
    WorkspaceV2BackupError,
    WorkspaceV2PackedStreamManifest,
    _load_packed_manifest,
)
from investment_analyst.workspace.workspace_v2_backup_inventory import (
    WorkspaceV2BackupInventoryError,
    iter_inventory,
)
from investment_analyst.workspace.workspace_v2_lifecycle_models import (
    FrozenSourceCut,
    LifecycleCheckpoint,
    LifecycleContentRole,
    LifecycleFile,
    LifecycleInventory,
    LifecyclePhase,
    LifecycleReceipt,
    LocalComplementManifest,
    ProvenCopyRetirementReceipt,
)
from investment_analyst.workspace.workspace_v2_recovery import WorkspaceV2RecoveryReceipt

_COMPLEMENT_MANIFEST_NAME = "local-complement-manifest.json"
_CHUNK_BYTES = 1024 * 1024


class WorkspaceV2LifecycleError(WorkspaceError):
    """A snapshot lifecycle operation cannot safely advance."""


class WorkspaceV2LifecycleService:
    """Preserve unique state before retiring a separately proven workspace copy."""

    def __init__(
        self,
        workspace_service: WorkspaceService,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._workspace_service = workspace_service
        self._clock = clock

    def freeze_source(
        self,
        source_root: Path,
        *,
        operation_id: UUID,
        source_fingerprint: str,
        writer_closed: bool,
    ) -> FrozenSourceCut:
        """Read a workspace identity without permitting an active source writer."""
        source = _safe_existing_root(source_root, label="source")
        if not source_fingerprint.strip():
            raise WorkspaceV2LifecycleError("source fingerprint must be explicit")
        if not writer_closed:
            raise WorkspaceV2LifecycleError("source writer must be closed before lifecycle work")
        try:
            inspection = self._workspace_service.inspect(source)
            manifest = self._workspace_service.manifest(source)
        except WorkspaceError as error:
            raise WorkspaceV2LifecycleError(
                "source workspace cannot be inspected safely"
            ) from error
        if inspection.status not in {"ready", "incomplete"}:
            raise WorkspaceV2LifecycleError("source workspace status is unsupported")
        return FrozenSourceCut(
            operation_id=operation_id,
            source_root=source,
            workspace_id=manifest.workspace_id,
            workspace_format_version=manifest.format_version,
            created_at=manifest.created_at,
            source_fingerprint=source_fingerprint,
            frozen_at=self._now(),
        )

    def inventory_source(
        self,
        source: FrozenSourceCut,
        *,
        recoverable_prefixes: Iterable[str] = (),
    ) -> LifecycleInventory:
        """Classify every regular file in one metadata pass without retaining a tree."""
        root = _matching_source(source)
        recoverable = _normalised_prefixes(recoverable_prefixes)
        roles = {role: 0 for role in LifecycleContentRole}
        file_count = 0
        logical_bytes = 0
        for path in _iter_regular_files(root):
            relative = path.relative_to(root).as_posix()
            role = _classify(relative, recoverable)
            stat = path.stat()
            file_count += 1
            logical_bytes += stat.st_size
            roles[role] += 1
        return LifecycleInventory(
            frozen_source=source,
            file_count=file_count,
            logical_bytes=logical_bytes,
            roles=roles,
            unknown_file_count=roles[LifecycleContentRole.UNKNOWN],
        )

    def publish_complement(
        self,
        source: FrozenSourceCut,
        complement_root: Path,
    ) -> LocalComplementManifest:
        """Atomically retain manifest, state, and processed events outside the source."""
        source_root = _matching_source(source)
        complement = _safe_new_root(complement_root, source_root, label="complement")
        if complement.exists():
            return self.verify_complement(source, complement)
        temporary = complement.with_name(f".{complement.name}.{uuid4().hex}.tmp")
        try:
            temporary.mkdir(parents=True, mode=0o700)
            files: list[LifecycleFile] = []
            for path in _iter_complement_source_files(source_root):
                relative = path.relative_to(source_root).as_posix()
                destination = temporary / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                size, digest = _copy_and_hash(path, destination)
                files.append(
                    LifecycleFile(
                        path=relative,
                        role=LifecycleContentRole.OPERATIONAL_HISTORIC,
                        size_bytes=size,
                        sha256=digest,
                    )
                )
            manifest = LocalComplementManifest(
                source=source,
                created_at=self._now(),
                files=tuple(sorted(files, key=lambda item: item.path)),
                total_bytes=sum(item.size_bytes for item in files),
            )
            _write_new_json(temporary / _COMPLEMENT_MANIFEST_NAME, manifest.model_dump(mode="json"))
            _fsync_directory(temporary)
            os.replace(temporary, complement)
            _fsync_directory(complement.parent)
            return self.verify_complement(source, complement)
        except OSError as error:
            raise WorkspaceV2LifecycleError("local complement could not be published") from error
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)

    def reconcile_sealed_baseline(
        self,
        inventory: LifecycleInventory,
        complement: LocalComplementManifest,
        *,
        sealed_file_count: int,
        sealed_logical_bytes: int,
        post_seal_paths: Iterable[str],
        recovery_proven: bool,
    ) -> LifecycleInventory:
        """Classify a frozen baseline using its sealed receipt without rescanning it."""
        if not recovery_proven:
            raise WorkspaceV2LifecycleError("sealed baseline recovery is not proven")
        if inventory.frozen_source != complement.source:
            raise WorkspaceV2LifecycleError("baseline inventory and complement differ")
        if sealed_file_count < 0 or sealed_logical_bytes < 0:
            raise WorkspaceV2LifecycleError("sealed baseline measurements are invalid")
        paths = tuple(_normalised_prefixes(post_seal_paths))
        complement_by_path = {item.path: item for item in complement.files}
        if len(paths) != len(set(paths)) or any(path not in complement_by_path for path in paths):
            raise WorkspaceV2LifecycleError("post-seal controls are not preserved")
        expected_extra_files = len(paths)
        expected_extra_bytes = sum(complement_by_path[path].size_bytes for path in paths)
        if (
            inventory.file_count != sealed_file_count + expected_extra_files
            or inventory.logical_bytes != sealed_logical_bytes + expected_extra_bytes
        ):
            raise WorkspaceV2LifecycleError("source metadata differs from sealed baseline")
        operational = len(complement.files)
        if inventory.roles.get(LifecycleContentRole.OPERATIONAL_HISTORIC, 0) != operational:
            raise WorkspaceV2LifecycleError("operational complement classification is incomplete")
        roles = {role: 0 for role in LifecycleContentRole}
        roles[LifecycleContentRole.OPERATIONAL_HISTORIC] = operational
        roles[LifecycleContentRole.MIGRATED_RECOVERABLE] = inventory.file_count - operational
        return LifecycleInventory(
            frozen_source=inventory.frozen_source,
            file_count=inventory.file_count,
            logical_bytes=inventory.logical_bytes,
            roles=roles,
            unknown_file_count=0,
        )

    def verify_complement(
        self, source: FrozenSourceCut, complement_root: Path
    ) -> LocalComplementManifest:
        """Verify hashes and source binding before a source can be retired."""
        source_root = source.source_root.resolve(strict=False)
        if source_root.exists() and _safe_existing_root(source_root, label="source") != source_root:
            raise WorkspaceV2LifecycleError("source root is unsafe")
        complement = _safe_existing_root(complement_root, label="complement")
        if (
            complement == source_root
            or complement in source_root.parents
            or source_root in complement.parents
        ):
            raise WorkspaceV2LifecycleError("complement must be disjoint from source")
        manifest_path = complement / _COMPLEMENT_MANIFEST_NAME
        if manifest_path.is_symlink() or not manifest_path.is_file():
            raise WorkspaceV2LifecycleError("local complement manifest is missing or unsafe")
        try:
            manifest = LocalComplementManifest.model_validate_json(
                manifest_path.read_text(encoding="utf-8")
            )
        except (OSError, ValueError) as error:
            raise WorkspaceV2LifecycleError("local complement manifest is malformed") from error
        if manifest.source != source:
            raise WorkspaceV2LifecycleError("local complement binds a different source")
        found: set[str] = set()
        for item in manifest.files:
            path = complement / item.path
            if path.is_symlink() or not path.is_file():
                raise WorkspaceV2LifecycleError("local complement file is missing or unsafe")
            size, digest = _hash_file(path)
            if size != item.size_bytes or digest != item.sha256:
                raise WorkspaceV2LifecycleError("local complement hash verification failed")
            found.add(item.path)
        actual = {
            path.relative_to(complement).as_posix()
            for path in _iter_regular_files(complement)
            if path.name != _COMPLEMENT_MANIFEST_NAME
        }
        if actual != found:
            raise WorkspaceV2LifecycleError("local complement inventory differs from manifest")
        return manifest

    def write_checkpoint(
        self,
        path: Path,
        checkpoint: LifecycleCheckpoint,
    ) -> LifecycleCheckpoint:
        """Durably advance a caller-owned lifecycle checkpoint by one phase."""
        target = path.expanduser().resolve(strict=False)
        if not target.is_absolute() or target.is_symlink():
            raise WorkspaceV2LifecycleError("lifecycle checkpoint path is unsafe")
        previous = self._read_checkpoint(target)
        if previous is not None:
            if (
                previous.operation_id != checkpoint.operation_id
                or previous.source != checkpoint.source
            ):
                raise WorkspaceV2LifecycleError("lifecycle checkpoint binds different inputs")
            phases = {
                LifecyclePhase.INVENTORIED: 0,
                LifecyclePhase.COMPLEMENT_PUBLISHED: 1,
                LifecyclePhase.RETIRING: 2,
                LifecyclePhase.RETIRED: 3,
            }
            if phases[checkpoint.phase] < phases[previous.phase]:
                raise WorkspaceV2LifecycleError("lifecycle checkpoint cannot move backwards")
        _replace_json(target, checkpoint.model_dump(mode="json"))
        return checkpoint

    def retire_source(
        self,
        source: FrozenSourceCut,
        inventory: LifecycleInventory,
        complement_root: Path,
        *,
        checkpoint_path: Path,
    ) -> LifecycleReceipt:
        """Remove an exact inactive source only after all fail-closed local gates pass."""
        root = source.source_root.resolve(strict=False)
        retiring_root = _retiring_root(root, source.operation_id)
        if inventory.frozen_source != source:
            raise WorkspaceV2LifecycleError("lifecycle inventory binds a different source")
        if inventory.unknown_file_count:
            raise WorkspaceV2LifecycleError("source contains unknown durable content")
        complement = self.verify_complement(source, complement_root)
        checkpoint = self._read_checkpoint(checkpoint_path)
        if root.exists():
            if retiring_root.exists():
                raise WorkspaceV2LifecycleError("lifecycle retirement path is ambiguous")
            # The preceding metadata pass and frozen descriptor are a caller-visible proof that
            # this exact root has no writer. Resolve again before detaching it.
            if _safe_existing_root(root, label="source") != root:
                raise WorkspaceV2LifecycleError("source root changed before retirement")
            checkpoint = LifecycleCheckpoint(
                operation_id=source.operation_id,
                phase=LifecyclePhase.RETIRING,
                source=source,
                complement_sha256=_hash_file(Path(complement_root) / _COMPLEMENT_MANIFEST_NAME)[1],
                retirement_root=retiring_root,
                files_confirmed=len(complement.files),
                bytes_confirmed=complement.total_bytes,
            )
            self.write_checkpoint(checkpoint_path, checkpoint)
            os.replace(root, retiring_root)
            _fsync_directory(root.parent)
        elif not (
            checkpoint is not None
            and checkpoint.phase is LifecyclePhase.RETIRING
            and checkpoint.retirement_root == retiring_root
            and _safe_existing_root(retiring_root, label="retiring source") == retiring_root
        ):
            raise WorkspaceV2LifecycleError("source retirement cannot be resumed safely")
        try:
            shutil.rmtree(retiring_root)
        except OSError as error:
            raise WorkspaceV2LifecycleError("source retirement did not complete") from error
        terminal = LifecycleCheckpoint(
            operation_id=source.operation_id,
            phase=LifecyclePhase.RETIRED,
            source=source,
            complement_sha256=checkpoint.complement_sha256 if checkpoint is not None else None,
            files_confirmed=inventory.file_count,
            bytes_confirmed=inventory.logical_bytes,
        )
        self.write_checkpoint(checkpoint_path, terminal)
        return LifecycleReceipt(
            operation_id=source.operation_id,
            phase=LifecyclePhase.RETIRED,
            source_root=root,
            complement_root=Path(complement_root).resolve(strict=False),
            files_confirmed=inventory.file_count,
            bytes_confirmed=inventory.logical_bytes,
            completed_at=self._now(),
        )

    def retire_recovery_slot(
        self,
        receipt: WorkspaceV2RecoveryReceipt,
        *,
        operation_id: UUID,
        checkpoint_path: Path,
    ) -> LifecycleReceipt:
        """Retire a proven rehearsal restore without creating another restore."""
        restore = _safe_existing_root(receipt.restore_root, label="restore")
        backup = _safe_existing_root(receipt.backup_root, label="backup")
        try:
            manifest = _load_packed_manifest(backup / "backup_manifest.json")
        except WorkspaceV2BackupError as error:
            raise WorkspaceV2LifecycleError("recovery backup manifest is unavailable") from error
        try:
            inspection = self._workspace_service.inspect(restore)
        except WorkspaceError as error:
            raise WorkspaceV2LifecycleError("recovery restore cannot be inspected") from error
        expected_counts = (
            receipt.raw_record_count,
            receipt.observation_count,
            receipt.metric_result_count,
            receipt.diagnostic_result_count,
        )
        actual_counts = (
            inspection.raw_record_count,
            inspection.observation_count,
            inspection.metric_result_count,
            inspection.diagnostic_result_count,
        )
        if (
            inspection.status != "ready"
            or inspection.format_version != 2
            or str(inspection.workspace_id) != receipt.restored_workspace_id
            or str(manifest.source_workspace_id) != receipt.source_workspace_id
            or actual_counts != expected_counts
            or (
                manifest.counts.raw_records,
                manifest.counts.observations,
                manifest.counts.metric_results,
                manifest.counts.diagnostic_results,
            )
            != expected_counts
        ):
            raise WorkspaceV2LifecycleError("recovery slot no longer matches its sealed receipt")
        descriptor = self.freeze_source(
            restore,
            operation_id=operation_id,
            source_fingerprint=_hash_file(backup / "backup_manifest.json")[1],
            writer_closed=True,
        )
        retiring_root = _retiring_root(restore, operation_id)
        checkpoint = self._read_checkpoint(checkpoint_path)
        if restore.exists():
            if retiring_root.exists():
                raise WorkspaceV2LifecycleError("recovery retirement path is ambiguous")
            with _v2_writer_closed(restore):
                retiring = LifecycleCheckpoint(
                    operation_id=operation_id,
                    phase=LifecyclePhase.RETIRING,
                    source=descriptor,
                    retirement_root=retiring_root,
                    files_confirmed=0,
                    bytes_confirmed=0,
                )
                self.write_checkpoint(checkpoint_path, retiring)
                os.replace(restore, retiring_root)
                _fsync_directory(restore.parent)
                checkpoint = retiring
        elif not (
            checkpoint is not None
            and checkpoint.phase is LifecyclePhase.RETIRING
            and checkpoint.retirement_root == retiring_root
            and _safe_existing_root(retiring_root, label="retiring restore") == retiring_root
        ):
            raise WorkspaceV2LifecycleError("recovery retirement cannot be resumed safely")
        try:
            shutil.rmtree(retiring_root)
        except OSError as error:
            raise WorkspaceV2LifecycleError("recovery retirement did not complete") from error
        completed = LifecycleCheckpoint(
            operation_id=operation_id,
            phase=LifecyclePhase.RETIRED,
            source=descriptor,
            retirement_root=retiring_root,
            files_confirmed=0,
            bytes_confirmed=0,
        )
        self.write_checkpoint(checkpoint_path, completed)
        return LifecycleReceipt(
            operation_id=operation_id,
            phase=LifecyclePhase.RETIRED,
            source_root=restore,
            files_confirmed=0,
            bytes_confirmed=0,
            completed_at=self._now(),
        )

    def retire_proven_copy(
        self,
        source_root: Path,
        *,
        recovery: WorkspaceV2RecoveryReceipt,
        remote_manifest_sha256: str,
        operation_id: UUID,
        checkpoint_path: Path,
    ) -> ProvenCopyRetirementReceipt:
        """Retire one inactive copy after one streaming comparison to its sealed inventory."""
        source = source_root.expanduser().resolve(strict=False)
        if not source.is_absolute() or source.is_symlink():
            raise WorkspaceV2LifecycleError("proven copy source is unavailable or unsafe")
        checkpoint = self._read_checkpoint(checkpoint_path)
        if source.exists():
            source = _safe_existing_root(source, label="proven copy source")
        retained = _safe_existing_root(recovery.restore_root, label="recovery restore")
        backup = _safe_existing_root(recovery.backup_root, label="recovery backup")
        if source in (retained, backup) or retained == backup:
            raise WorkspaceV2LifecycleError("proven copy roots must be disjoint")
        if not _is_sha256(remote_manifest_sha256):
            raise WorkspaceV2LifecycleError("remote manifest digest is invalid")
        try:
            manifest = _load_packed_manifest(backup / "backup_manifest.json")
        except WorkspaceV2BackupError as error:
            raise WorkspaceV2LifecycleError("proven copy backup manifest is unavailable") from error
        if not isinstance(manifest, WorkspaceV2PackedStreamManifest):
            raise WorkspaceV2LifecycleError("proven copy requires a streaming backup manifest")
        retained_inspection = _inspect_ready_v2(
            self._workspace_service, retained, "recovery restore"
        )
        expected_counts = (
            recovery.raw_record_count,
            recovery.observation_count,
            recovery.metric_result_count,
            recovery.diagnostic_result_count,
        )
        retained_counts = _inspection_counts(retained_inspection)
        if (
            retained_inspection.workspace_id != manifest.source_workspace_id
            or recovery.source_workspace_id != str(manifest.source_workspace_id)
            or recovery.restored_workspace_id != str(retained_inspection.workspace_id)
            or retained_counts != expected_counts
        ):
            raise WorkspaceV2LifecycleError("proven copy recovery bindings differ")
        retiring_root = _retiring_root(source, operation_id)
        if source.exists():
            source_inspection = _inspect_ready_v2(
                self._workspace_service, source, "proven copy source"
            )
            if (
                source_inspection.workspace_id != manifest.source_workspace_id
                or _inspection_counts(source_inspection) != expected_counts
            ):
                raise WorkspaceV2LifecycleError("proven copy recovery bindings differ")
            confirmed_files, confirmed_bytes = _compare_source_to_inventory(
                source, backup / "source-inventory.jsonl"
            )
            if (
                confirmed_files != manifest.inventory.file_count
                or confirmed_bytes != manifest.inventory.total_bytes
            ):
                raise WorkspaceV2LifecycleError(
                    "proven copy inventory summary differs from manifest"
                )
            descriptor = self.freeze_source(
                source,
                operation_id=operation_id,
                source_fingerprint=manifest.inventory.digest_sha256,
                writer_closed=True,
            )
            if retiring_root.exists():
                raise WorkspaceV2LifecycleError("proven copy retirement path is ambiguous")
            with _v2_writer_closed(source):
                retiring = LifecycleCheckpoint(
                    operation_id=operation_id,
                    phase=LifecyclePhase.RETIRING,
                    source=descriptor,
                    retirement_root=retiring_root,
                    files_confirmed=confirmed_files,
                    bytes_confirmed=confirmed_bytes,
                )
                self.write_checkpoint(checkpoint_path, retiring)
                os.replace(source, retiring_root)
                _fsync_directory(source.parent)
                checkpoint = retiring
        else:
            if (
                checkpoint is None
                or checkpoint.operation_id != operation_id
                or checkpoint.source.source_root != source
                or checkpoint.source.source_fingerprint != manifest.inventory.digest_sha256
                or checkpoint.retirement_root != retiring_root
                or checkpoint.phase not in {LifecyclePhase.RETIRING, LifecyclePhase.RETIRED}
            ):
                raise WorkspaceV2LifecycleError("proven copy retirement cannot be resumed safely")
            descriptor = checkpoint.source
            confirmed_files = checkpoint.files_confirmed
            confirmed_bytes = checkpoint.bytes_confirmed
        if checkpoint is None or checkpoint.phase is not LifecyclePhase.RETIRED:
            if _safe_existing_root(retiring_root, label="retiring proven copy") != retiring_root:
                raise WorkspaceV2LifecycleError("proven copy retirement cannot be resumed safely")
            try:
                shutil.rmtree(retiring_root)
            except OSError as error:
                raise WorkspaceV2LifecycleError(
                    "proven copy retirement did not complete"
                ) from error
            self.write_checkpoint(
                checkpoint_path,
                LifecycleCheckpoint(
                    operation_id=operation_id,
                    phase=LifecyclePhase.RETIRED,
                    source=descriptor,
                    retirement_root=retiring_root,
                    files_confirmed=confirmed_files,
                    bytes_confirmed=confirmed_bytes,
                ),
            )
        return ProvenCopyRetirementReceipt(
            operation_id=operation_id,
            source_root=source,
            retained_root=retained,
            backup_root=backup,
            backup_id=manifest.backup_id,
            inventory_sha256=manifest.inventory.digest_sha256,
            remote_manifest_sha256=remote_manifest_sha256,
            workspace_id=manifest.source_workspace_id,
            files_confirmed=confirmed_files,
            bytes_confirmed=confirmed_bytes,
            completed_at=self._now(),
        )

    def write_proven_copy_receipt(
        self, path: Path, receipt: ProvenCopyRetirementReceipt
    ) -> ProvenCopyRetirementReceipt:
        """Persist the terminal receipt atomically outside the root being retired."""
        target = path.expanduser().resolve(strict=False)
        if not target.is_absolute() or target.is_symlink() or target.exists():
            raise WorkspaceV2LifecycleError("proven copy receipt path is unavailable or unsafe")
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.parent.is_symlink() or not target.parent.is_dir():
            raise WorkspaceV2LifecycleError("proven copy receipt parent is unavailable or unsafe")
        _write_new_json(target, receipt.model_dump(mode="json"))
        _fsync_directory(target.parent)
        return receipt

    def _read_checkpoint(self, path: Path) -> LifecycleCheckpoint | None:
        if not path.exists():
            return None
        if path.is_symlink() or not path.is_file():
            raise WorkspaceV2LifecycleError("lifecycle checkpoint is unsafe")
        try:
            return LifecycleCheckpoint.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise WorkspaceV2LifecycleError("lifecycle checkpoint is malformed") from error

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise WorkspaceV2LifecycleError("lifecycle clock must be timezone-aware")
        return value.astimezone(UTC)


def _safe_existing_root(path: Path, *, label: str) -> Path:
    candidate = path.expanduser()
    if candidate.is_symlink() or not candidate.is_dir():
        raise WorkspaceV2LifecycleError(f"{label} root is missing or unsafe")
    resolved = candidate.resolve()
    if resolved != candidate.resolve(strict=True):
        raise WorkspaceV2LifecycleError(f"{label} root is unsafe")
    return resolved


def _safe_new_root(path: Path, source: Path, *, label: str) -> Path:
    candidate = path.expanduser()
    if candidate.is_symlink():
        raise WorkspaceV2LifecycleError(f"{label} root is unsafe")
    resolved = candidate.resolve(strict=False)
    if (
        not resolved.is_absolute()
        or resolved == source
        or source in resolved.parents
        or resolved in source.parents
    ):
        raise WorkspaceV2LifecycleError(f"{label} root must be disjoint from source")
    return resolved


def _matching_source(source: FrozenSourceCut) -> Path:
    root = _safe_existing_root(source.source_root, label="source")
    return root


def _inspection_counts(inspection: WorkspaceInspection) -> tuple[int, int, int, int]:
    return (
        inspection.raw_record_count,
        inspection.observation_count,
        inspection.metric_result_count,
        inspection.diagnostic_result_count,
    )


def _inspect_ready_v2(
    workspace_service: WorkspaceService, root: Path, label: str
) -> WorkspaceInspection:
    try:
        inspection = workspace_service.inspect(root)
    except WorkspaceError as error:
        raise WorkspaceV2LifecycleError(f"{label} cannot be inspected") from error
    if inspection.status != "ready" or inspection.format_version != 2:
        raise WorkspaceV2LifecycleError(f"{label} is not a ready workspace v2")
    return inspection


def _compare_source_to_inventory(source: Path, inventory_path: Path) -> tuple[int, int]:
    """Compare paths and lengths in one streaming pass; inventory hashes stay sealed evidence."""
    sentinel = object()
    file_count = 0
    byte_count = 0
    try:
        inventory = iter_inventory(inventory_path)
        files = _iter_regular_files(source)
        for entry, path in zip_longest(inventory, files, fillvalue=sentinel):
            if entry is sentinel or path is sentinel:
                raise WorkspaceV2LifecycleError("proven copy inventory coverage differs")
            if not isinstance(entry, dict) or not isinstance(path, Path):
                raise WorkspaceV2LifecycleError("proven copy inventory coverage differs")
            relative = path.relative_to(source).as_posix()
            expected_path = entry.get("path")
            expected_size = entry.get("size_bytes")
            if relative != expected_path or path.stat().st_size != expected_size:
                raise WorkspaceV2LifecycleError("proven copy inventory coverage differs")
            file_count += 1
            byte_count += path.stat().st_size
    except WorkspaceV2BackupInventoryError as error:
        raise WorkspaceV2LifecycleError("proven copy inventory is invalid") from error
    return file_count, byte_count


def _is_sha256(value: str) -> bool:
    return len(value) == 64 and all(character in "0123456789abcdef" for character in value)


def _retiring_root(root: Path, operation_id: UUID) -> Path:
    return root.with_name(f".{root.name}.retiring-{operation_id.hex}")


def _normalised_prefixes(prefixes: Iterable[str]) -> tuple[str, ...]:
    normalised: list[str] = []
    for value in prefixes:
        path = PurePosixPath(value)
        if path.is_absolute() or ".." in path.parts or not value:
            raise WorkspaceV2LifecycleError("recoverable prefix is unsafe")
        normalised.append(path.as_posix().rstrip("/"))
    return tuple(normalised)


def _classify(relative: str, recoverable_prefixes: tuple[str, ...]) -> LifecycleContentRole:
    if relative in {"manifest.json", "backup_manifest.json"} or relative.startswith("state/"):
        return LifecycleContentRole.OPERATIONAL_HISTORIC
    if relative.startswith("storage/data/processed/") and relative.endswith(".json"):
        return LifecycleContentRole.OPERATIONAL_HISTORIC
    if relative.endswith(".lock"):
        return LifecycleContentRole.REGENERABLE
    if any(
        relative == prefix or relative.startswith(f"{prefix}/") for prefix in recoverable_prefixes
    ):
        return LifecycleContentRole.MIGRATED_RECOVERABLE
    return LifecycleContentRole.UNKNOWN


def _iter_regular_files(root: Path) -> Iterable[Path]:
    for directory, directory_names, file_names in os.walk(root, topdown=True, followlinks=False):
        current = Path(directory)
        if current.is_symlink() or any((current / name).is_symlink() for name in directory_names):
            raise WorkspaceV2LifecycleError("lifecycle roots cannot contain symbolic links")
        directory_names.sort()
        file_names.sort()
        for name in file_names:
            path = current / name
            if path.is_symlink() or not path.is_file():
                raise WorkspaceV2LifecycleError("lifecycle source contains an unsafe entry")
            yield path


def _iter_complement_source_files(root: Path) -> Iterable[Path]:
    required = (root / "manifest.json", root / "state", root / "storage" / "data" / "processed")
    for candidate in required:
        if candidate.is_symlink() or not candidate.exists():
            raise WorkspaceV2LifecycleError("required complement source is missing or unsafe")
    manifest = root / "manifest.json"
    if not manifest.is_file():
        raise WorkspaceV2LifecycleError("workspace manifest is missing")
    yield manifest
    backup_manifest = root / "backup_manifest.json"
    if backup_manifest.exists():
        if backup_manifest.is_symlink() or not backup_manifest.is_file():
            raise WorkspaceV2LifecycleError("backup control is missing or unsafe")
        yield backup_manifest
    state_root = root / "state"
    if not state_root.is_dir():
        raise WorkspaceV2LifecycleError("required complement directory is missing")
    yield from _iter_regular_files(state_root)
    processed_root = root / "storage" / "data" / "processed"
    if not processed_root.is_dir():
        raise WorkspaceV2LifecycleError("required complement directory is missing")
    for path in _iter_regular_files(processed_root):
        if path.suffix == ".json":
            yield path


def _copy_and_hash(source: Path, destination: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    total = 0
    with source.open("rb") as input_stream, destination.open("xb") as output_stream:
        while chunk := input_stream.read(_CHUNK_BYTES):
            output_stream.write(chunk)
            digest.update(chunk)
            total += len(chunk)
        output_stream.flush()
        os.fsync(output_stream.fileno())
    return total, digest.hexdigest()


def _hash_file(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    total = 0
    with path.open("rb") as stream:
        while chunk := stream.read(_CHUNK_BYTES):
            digest.update(chunk)
            total += len(chunk)
    return total, digest.hexdigest()


def _write_new_json(path: Path, document: object) -> None:
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(document, stream, separators=(",", ":"), sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())


def _replace_json(path: Path, document: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        _write_new_json(temporary, document)
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


@contextmanager
def _v2_writer_closed(root: Path):
    """Acquire existing v2 locks without creating operational files during retirement."""
    import fcntl

    paths = (
        root / "storage" / "v2" / "raw-v2-staging.lock",
        root / "state" / "aapl_daily_run.lock",
        root / "state" / "aapl_local_service.lock",
    )
    descriptors: list[int] = []
    try:
        for index, path in enumerate(paths):
            if path.is_symlink() or (index == 0 and not path.is_file()):
                raise WorkspaceV2LifecycleError("recovery writer lock is missing or unsafe")
            if not path.exists():
                continue
            descriptor = os.open(path, os.O_RDWR | getattr(os, "O_NOFOLLOW", 0))
            descriptors.append(descriptor)
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    except BlockingIOError as error:
        raise WorkspaceV2LifecycleError("recovery writer remains active") from error
    except OSError as error:
        raise WorkspaceV2LifecycleError("recovery writer lock cannot be acquired") from error
    finally:
        for descriptor in reversed(descriptors):
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)


__all__ = ["WorkspaceV2LifecycleError", "WorkspaceV2LifecycleService"]
