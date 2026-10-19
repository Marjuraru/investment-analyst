"""Service-owned preservation and retirement of isolated workspace snapshots."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shutil
from collections.abc import Callable, Iterable, Iterator
from contextlib import ExitStack, contextmanager
from datetime import UTC, datetime
from itertools import tee, zip_longest
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Literal
from uuid import UUID, uuid4

from investment_analyst.workspace.models import WorkspaceAccessMode, WorkspaceInspection
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
    BackupDirectoryRetirementProof,
    FrozenSourceCut,
    LifecycleCheckpoint,
    LifecycleContentRole,
    LifecycleFile,
    LifecycleInventory,
    LifecyclePhase,
    LifecycleReceipt,
    LocalComplementManifest,
    OwnedPayloadRetirementInput,
    OwnedPayloadRetirementReceipt,
    OwnedPayloadRole,
    PreparatorySlotArtifact,
    PreparatorySlotRetirementReceipt,
    ProvenCopyRetirementReceipt,
    SealedDirectoryRetirementCheckpoint,
    SealedDirectoryRetirementReceipt,
    WorkspaceV2RetirementProof,
)
from investment_analyst.workspace.workspace_v2_recovery import WorkspaceV2RecoveryReceipt

if TYPE_CHECKING:
    from investment_analyst.workspace.workspace_v2_migration_models import (
        ReconciliationFileReference,
    )
    from investment_analyst.workspace.workspace_v2_physical_delta import (
        ImmutableLinkJournal,
        PhysicalDeltaReceipt,
    )

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

    def retire_orphaned_duckdb_spill(
        self, root: Path, *, operation_id: UUID, receipt_path: Path
    ) -> tuple[int, int, int]:
        """Retire only disposable engine spill, preserving DB, WAL and all state.

        Spill has no recoverable domain content. Record its metadata before any
        unlink, under the existing writer locks and an exclusive database lock. Never
        open the database here: opening may replay its WAL before capacity exists.
        """
        root = _safe_existing_root(root, label="spill workspace")
        manifest = self._workspace_service.manifest(root)
        if manifest.format_version != 2:
            raise WorkspaceV2LifecycleError("spill retirement requires workspace v2")
        parent = root / "storage" / "v2"
        spill = parent / "index.duckdb.tmp"
        if any(path.is_symlink() for path in (root / "storage", parent, spill)):
            raise WorkspaceV2LifecycleError("spill directory is unsafe")
        if not spill.is_dir() or not (parent / "index.duckdb").is_file():
            raise WorkspaceV2LifecycleError("spill directory or database is missing")
        if receipt_path.is_symlink() or not receipt_path.is_absolute():
            raise WorkspaceV2LifecycleError("spill receipt is unsafe")
        receipt = receipt_path.resolve()
        if receipt.is_relative_to(root):
            raise WorkspaceV2LifecycleError("spill evidence must survive outside workspace")
        inventory = receipt.with_name(receipt.name + ".inventory.json")
        if receipt.exists() or inventory.exists() or inventory.is_symlink():
            raise WorkspaceV2LifecycleError("spill retirement evidence already exists")
        with _v2_writer_closed(root), _closed_duckdb_file(parent / "index.duckdb"):
            entries: list[tuple[Path, os.stat_result]] = []
            for path in spill.iterdir():
                if (
                    len(entries) >= 4096
                    or path.is_symlink()
                    or not path.is_file()
                    or re.fullmatch(
                        r"duckdb_temp_storage_(?:DEFAULT|S[0-9]+K)-[0-9]+\.tmp", path.name
                    )
                    is None
                ):
                    raise WorkspaceV2LifecycleError("spill contains an unknown or unsafe entry")
                entries.append((path, path.stat()))
            logical = sum(stat.st_size for _, stat in entries)
            allocated = sum(stat.st_blocks * 512 for _, stat in entries)
            document = {
                "operation_id": str(operation_id),
                "workspace_id": str(manifest.workspace_id),
                "workspace_root": str(root),
                "spill_root": str(spill),
                "classification": "regenerable_duckdb_spill",
                "files": [
                    {
                        "name": path.name,
                        "bytes": stat.st_size,
                        "allocated_bytes": stat.st_blocks * 512,
                        "inode": stat.st_ino,
                        "mtime_ns": stat.st_mtime_ns,
                    }
                    for path, stat in entries
                ],
                "logical_bytes": logical,
                "allocated_bytes": allocated,
                "recorded_at": self._now().isoformat(),
            }
            receipt.parent.mkdir(parents=True, exist_ok=True)
            _write_new_json(inventory, document)
            _fsync_directory(inventory.parent)
            for path, before in entries:
                current = path.stat()
                if (current.st_ino, current.st_size, current.st_mtime_ns) != (
                    before.st_ino,
                    before.st_size,
                    before.st_mtime_ns,
                ):
                    raise WorkspaceV2LifecycleError("spill changed during retirement")
                path.unlink()
            spill.rmdir()
            _fsync_directory(parent)
            _write_new_json(
                receipt,
                {
                    "operation_id": str(operation_id),
                    "workspace_id": str(manifest.workspace_id),
                    "inventory_sha256": hashlib.sha256(inventory.read_bytes()).hexdigest(),
                    "files_retired": len(entries),
                    "logical_bytes": logical,
                    "allocated_bytes": allocated,
                    "completed_at": self._now().isoformat(),
                },
            )
            _fsync_directory(receipt.parent)
        return len(entries), logical, allocated

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

    def inventory_current_v1(
        self, source: FrozenSourceCut, complement_root: Path
    ) -> LifecycleInventory:
        """Classify metadata against indexed raw paths and the preserved complement.

        This does not claim a new payload audit. The cutover caller must separately
        prove complete F equivalence/recovery and an unchanged frozen source.
        Unindexed raw files, exports, extra SQL tables and other files stay unknown.
        """
        root = _matching_source(source)
        identity = self._workspace_service.manifest(root)
        if (
            source.workspace_format_version != 1
            or identity.format_version != 1
            or identity.workspace_id != source.workspace_id
            or identity.created_at != source.created_at
        ):
            raise WorkspaceV2LifecycleError("source metadata inventory identity differs")
        preserved = self.verify_complement(source, complement_root)
        complement = {item.path: item for item in preserved.files}
        roles = {role: 0 for role in LifecycleContentRole}
        count = total = 0
        with self._workspace_service.open_storage(
            self._workspace_service.resolve(root), WorkspaceAccessMode.READ_ONLY
        ) as storage:
            tables = {
                str(row[0])
                for row in storage.store.connection.execute(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema='main' AND table_type='BASE TABLE'"
                ).fetchall()
            }
            if tables != {
                "storage_metadata",
                "assets",
                "source_definitions",
                "raw_record_index",
                "normalized_observations",
                "metric_definitions",
                "metric_results",
                "diagnostic_results",
            }:
                raise WorkspaceV2LifecycleError("source database contains unrepresented tables")
            cursor = storage.store.connection.cursor()
            try:
                cursor.execute("SELECT relative_path FROM raw_record_index ORDER BY relative_path")

                def indexed_paths() -> Iterable[str]:
                    while rows := cursor.fetchmany(256):
                        for row in rows:
                            relative = str(row[0])
                            path = Path(relative)
                            if (
                                path.is_absolute()
                                or ".." in path.parts
                                or path.as_posix() != relative
                            ):
                                raise WorkspaceV2LifecycleError("source raw index path is unsafe")
                            yield relative

                indexed = iter(indexed_paths())
                expected = next(indexed, None)
                for path in _iter_regular_files(root):
                    relative = path.relative_to(root).as_posix()
                    size = path.stat().st_size
                    role = LifecycleContentRole.UNKNOWN
                    if relative in complement:
                        preserved_file = complement[relative]
                        if _hash_file(path) != (preserved_file.size_bytes, preserved_file.sha256):
                            raise WorkspaceV2LifecycleError(
                                "source complement changed since preservation"
                            )
                        role = LifecycleContentRole.OPERATIONAL_HISTORIC
                    elif path == storage.paths.database_path:
                        role = LifecycleContentRole.MIGRATED_RECOVERABLE
                    elif path.is_relative_to(storage.paths.raw_dir):
                        raw_relative = path.relative_to(storage.paths.raw_dir).as_posix()
                        if expected is not None and raw_relative == expected:
                            role = LifecycleContentRole.MIGRATED_RECOVERABLE
                            expected = next(indexed, None)
                    elif path.is_relative_to(storage.paths.documents_dir / "sha256"):
                        if _is_sha256(path.name):
                            role = LifecycleContentRole.MIGRATED_RECOVERABLE
                    elif relative in {"state/aapl_daily_run.lock", "state/aapl_local_service.lock"}:
                        role = LifecycleContentRole.REGENERABLE
                    roles[role] += 1
                    count += 1
                    total += size
                if expected is not None:
                    raise WorkspaceV2LifecycleError("source indexed raw file is absent")
            finally:
                cursor.close()
        return LifecycleInventory(
            frozen_source=source,
            file_count=count,
            logical_bytes=total,
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
        complement_digest = _hash_file(Path(complement_root) / _COMPLEMENT_MANIFEST_NAME)[1]
        if checkpoint is not None and (
            checkpoint.source != source
            or checkpoint.operation_id != source.operation_id
            or (
                checkpoint.complement_sha256 != complement_digest
                and not (
                    checkpoint.complement_sha256 is None
                    and checkpoint.phase
                    in {LifecyclePhase.INVENTORIED, LifecyclePhase.COMPLEMENT_PUBLISHED}
                )
            )
        ):
            raise WorkspaceV2LifecycleError("source retirement checkpoint bindings differ")
        if root.exists():
            if retiring_root.exists() or (
                checkpoint is not None and checkpoint.phase == LifecyclePhase.RETIRED
            ):
                raise WorkspaceV2LifecycleError("lifecycle retirement path is ambiguous")
            # The preceding metadata pass and frozen descriptor are a caller-visible proof that
            # this exact root has no writer. Resolve again before detaching it.
            if _safe_existing_root(root, label="source") != root:
                raise WorkspaceV2LifecycleError("source root changed before retirement")
            checkpoint = LifecycleCheckpoint(
                operation_id=source.operation_id,
                phase=LifecyclePhase.RETIRING,
                source=source,
                complement_sha256=complement_digest,
                retirement_root=retiring_root,
                files_confirmed=len(complement.files),
                bytes_confirmed=complement.total_bytes,
            )
            self.write_checkpoint(checkpoint_path, checkpoint)
            os.replace(root, retiring_root)
            _fsync_directory(root.parent)
        elif not (
            checkpoint is not None
            and (
                (
                    checkpoint.phase is LifecyclePhase.RETIRING
                    and checkpoint.retirement_root == retiring_root
                )
                or (
                    checkpoint.phase is LifecyclePhase.RETIRED
                    and checkpoint.files_confirmed == inventory.file_count
                    and checkpoint.bytes_confirmed == inventory.logical_bytes
                )
            )
        ):
            raise WorkspaceV2LifecycleError("source retirement cannot be resumed safely")
        if retiring_root.exists():
            if checkpoint is not None and checkpoint.phase is LifecyclePhase.RETIRED:
                raise WorkspaceV2LifecycleError("retired source unexpectedly exists")
            _safe_existing_root(retiring_root, label="retiring source")
            try:
                shutil.rmtree(retiring_root)
                _fsync_directory(root.parent)
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
        if checkpoint != terminal:
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

    def retire_preparatory_slot(
        self,
        proposal: PreparatorySlotRetirementReceipt,
        *,
        admission_path: Path,
        checkpoint_path: Path,
        proof_path: Path,
        unlink_root: Path,
        journal_byte_budget: int,
    ) -> PreparatorySlotRetirementReceipt:
        """Retire remotely covered APPLYING W/W2, retaining its certified superset."""
        from investment_analyst.application.local_release import LocalReleaseService
        from investment_analyst.storage.resource_policy import (
            MaintenanceResourceReceipt,
            require_maintenance_admission,
        )
        from investment_analyst.storage.serialization import canonical_json_bytes
        from investment_analyst.storage.workspace_v2_inventory import (
            GenerationContentProof,
            WorkspaceV2GenerationLedger,
            WorkspaceV2ProgressReconciler,
            _reconciliation_lock,
            reconciliation_file_reference,
        )
        from investment_analyst.workspace.drive_transport import DriveTransportConfig
        from investment_analyst.workspace.workspace_v2_acceptance import CompatibleRecoveryReadback
        from investment_analyst.workspace.workspace_v2_migration_models import (
            ProgressReconciliation,
        )
        from investment_analyst.workspace.workspace_v2_physical_delta import (
            ImmutableLinkHeader,
            ImmutableLinkJournal,
            PhysicalDeltaReceipt,
            _apply_controlled_link_page,
            _immutable_link_verifier_sha256,
            _iter_delta_files,
            _read_delta_manifest,
        )
        from investment_analyst.workspace.workspace_v2_remote import RemoteBackupRestoreReceipt

        admission = MaintenanceResourceReceipt.model_validate_json(admission_path.read_bytes())
        execution = require_maintenance_admission(admission)
        root, retained = (
            proposal.source_root,
            _safe_existing_root(proposal.retained_root, label="retained W2"),
        )
        for output in (checkpoint_path, proof_path, unlink_root):
            if (
                not output.is_absolute()
                or output.is_relative_to(root)
                or output.is_relative_to(retained)
                or any(p.is_symlink() for p in (output, *output.parents))
            ):
                raise WorkspaceV2LifecycleError("preparatory controls must survive retirement")
        if (
            admission.budget.operation_id != proposal.operation_id
            or retained / "storage/v2/index.duckdb"
            not in {a.database for a in admission.budget.assignments}
            or journal_byte_budget
            > admission.budget.next_unit_growth_bytes + admission.budget.cache_and_control_bytes
        ):
            raise WorkspaceV2LifecycleError("preparatory retirement resource binding differs")
        controls = {item.kind: item for item in proposal.evidence}
        if proposal.phase != "retiring" or proposal.unlink_witness is not None:
            raise WorkspaceV2LifecycleError("preparatory retirement requires a fresh proposal")
        for item in proposal.evidence:
            if (
                any(p.is_symlink() for p in (item.path, *item.path.parents))
                or not item.path.is_file()
                or _hash_file(item.path) != (item.size_bytes, item.sha256)
            ):
                raise WorkspaceV2LifecycleError("preparatory retirement evidence changed")
        prepared_path = proof_path.with_name(proof_path.name + ".prepared.json")
        checkpoint = self._read_checkpoint(checkpoint_path)
        retiring = _retiring_root(root, proposal.operation_id)
        if proof_path.exists():
            terminal = PreparatorySlotRetirementReceipt.model_validate_json(proof_path.read_bytes())
            prepared = PreparatorySlotRetirementReceipt.model_validate_json(
                prepared_path.read_bytes()
            )
            if (
                checkpoint is None
                or checkpoint.phase != LifecyclePhase.RETIRED
                or terminal.phase != "retired"
                or root.exists()
                or retiring.exists()
                or proposal.model_copy(update={"verified_at": prepared.verified_at}) != prepared
                or terminal.model_copy(
                    update={"phase": "retiring", "unlink_witness": None, "completed_at": None}
                )
                != prepared
                or terminal.unlink_witness is None
                or _hash_file(terminal.unlink_witness.path)
                != (terminal.unlink_witness.size_bytes, terminal.unlink_witness.sha256)
            ):
                raise WorkspaceV2LifecycleError("preparatory terminal retirement differs")
            return terminal
        manifest = _read_delta_manifest(controls["delta_manifest"].path)
        restore = PhysicalDeltaReceipt.model_validate_json(
            controls["restore_receipt"].path.read_bytes()
        )
        upload = PhysicalDeltaReceipt.model_validate_json(
            controls["remote_upload"].path.read_bytes()
        )
        parent = RemoteBackupRestoreReceipt.model_validate_json(
            controls["remote_parent_restore"].path.read_bytes()
        )
        readback = CompatibleRecoveryReadback.model_validate_json(
            controls["compatible_readback"].path.read_bytes()
        )
        coverage = json.loads(controls["complete_coverage"].path.read_bytes())
        config = DriveTransportConfig.model_validate_json(
            controls["remote_locator"].path.read_bytes()
        )
        ledger = WorkspaceV2GenerationLedger(self._workspace_service, retained)
        resolved = ledger.resolve()
        if resolved is None:
            raise WorkspaceV2LifecycleError("preparatory retirement requires certified W2")
        head, generation = resolved
        if generation.core_proof is None or generation.inventory is None:
            raise WorkspaceV2LifecycleError("preparatory retirement requires corrective W2 proofs")
        content = GenerationContentProof.model_validate_json(ledger._read(generation.core_proof))
        w = ProgressReconciliation.model_validate_json(ledger._read(generation.verified_w))
        same_generation = manifest.epoch == generation.generation_id
        photo = generation.inventory if same_generation else w.inventory
        photo_epoch = generation.generation_id if same_generation else w.operation_id
        if photo is None:
            raise WorkspaceV2LifecycleError("preparatory photo inventory is missing")
        if same_generation:
            image = next(
                (
                    item
                    for item in _iter_delta_files(controls["files_inventory"].path)
                    if item.path == "storage/v2/index.duckdb"
                ),
                None,
            )
            if (
                image is None
                or image.sha256 != generation.database.sha256
                or image.size_bytes != generation.database.size_bytes
            ):
                raise WorkspaceV2LifecycleError("preparatory W2 database binding differs")
        if (
            generation.phase != "BASELINE_VERIFIED"
            or generation.reserved_successor is not None
            or head.certificate.sha256 != controls["retained_certificate"].sha256
            or head.certificate.path != controls["retained_certificate"].path
            or generation.generation_id != proposal.retained_generation_id
            or generation.database != content.database
            or content.baseline_certificate != generation.verified_w
            or w.phase != "BASELINE_VERIFIED"
            or w.inventory is None
            or manifest.historical_state != "APPLYING"
            or restore.historical_state != "APPLYING"
            or restore.kind != "restore"
            or upload.kind != "upload"
            or upload.historical_state != "APPLYING"
            or restore.epoch != manifest.epoch
            or upload.epoch != manifest.epoch
            or proposal.recovery_epoch != manifest.epoch
            or photo_epoch != manifest.epoch
            or restore.restored_root != root
            or manifest.source_root != retained
            or manifest.source_workspace_id != proposal.workspace_id
            or generation.destination_workspace_id != proposal.workspace_id
            or restore.manifest_sha256 != controls["delta_manifest"].sha256
            or upload.manifest_sha256 != restore.manifest_sha256
            or upload.files_sha256 != manifest.files_sha256
            or restore.files_sha256 != manifest.files_sha256
            or controls["files_inventory"].sha256 != manifest.files_sha256
            or controls["remote_parent_manifest"].sha256 != manifest.parent_manifest_sha256
            or controls["remote_parent_index"].sha256 != manifest.parent_index_sha256
            or parent.backup_id != manifest.parent_backup_id
            or parent.remote_manifest_sha256 != manifest.parent_manifest_sha256
            or parent.restored_workspace_id != proposal.workspace_id
            or upload.confirmed_files != manifest.file_count + manifest.deleted_count
            or restore.confirmed_files != upload.confirmed_files
            or readback.operation_id != manifest.epoch
            or readback.restored_root != root
            or readback.workspace_id != proposal.workspace_id
            or readback.manifest_sha256 != restore.manifest_sha256
            or readback.recovery_receipt_sha256
            != hashlib.sha256(restore.model_dump_json().encode()).hexdigest()
            or readback.reader_sha != "dbbdfd021b0fee20be31b2d2a845874b0e861067"
            or (
                readback.raw_count,
                readback.observation_count,
                readback.metric_count,
                readback.diagnostic_count,
            )
            != (
                photo.raw_count,
                photo.observation_count,
                photo.metric_count,
                photo.diagnostic_count,
            )
            or coverage.get("action") != "recovery-readback"
            or coverage.get("operation_id") != str(photo_epoch)
            or coverage.get("result", {}).get("readback") != readback.model_dump(mode="json")
            or coverage.get("result", {}).get("coverage", {}).get("physical_files")
            != manifest.file_count
            or coverage.get("result", {}).get("coverage", {}).get("physical_bytes")
            != manifest.logical_bytes
            or coverage.get("result", {}).get("coverage", {}).get("raw_indexed")
            != readback.raw_count
            or hashlib.sha256(config.model_dump_json().encode()).hexdigest()
            != proposal.configuration_sha256
            or (proposal.files_confirmed, proposal.bytes_confirmed)
            != (manifest.file_count, manifest.logical_bytes)
        ):
            raise WorkspaceV2LifecycleError("preparatory retirement proof bindings differ")
        expected_reader = (
            LocalReleaseService().paths.releases
            / readback.reader_sha
            / "src/investment_analyst/storage/compact_analytical_v2.py"
        ).resolve()
        if readback.reader_module_path != expected_reader or not expected_reader.is_file():
            raise WorkspaceV2LifecycleError("preparatory retirement requires integrated reader 44")
        for binding in (
            LocalReleaseService().load_workspace_bindings().current,
            LocalReleaseService().load_workspace_bindings().previous,
        ):
            if binding is not None and binding.workspace_root in (root, retained):
                raise WorkspaceV2LifecycleError(
                    "preparatory workspace has an active runtime binding"
                )
        link_ref = reconciliation_file_reference(controls["link_witness"].path, "control")
        link_header = ImmutableLinkHeader.model_validate_json(
            (link_ref.path.parent / "header.json").read_bytes()
        )
        if (
            link_header.retained_root != retained
            or link_header.slot != root
            or (
                link_header.inventory.sha256 != manifest.files_sha256
                and not (same_generation and link_ref in restore.immutable_link_journals)
            )
            or (same_generation and link_header.operation_id != manifest.epoch)
            or link_header.retained_workspace_id != proposal.workspace_id
        ):
            raise WorkspaceV2LifecycleError("preparatory retirement link witness differs")
        if root.exists():
            root = _safe_existing_root(root, label="preparatory slot")
            if (root / "storage/v2/index.duckdb").samefile(retained / "storage/v2/index.duckdb"):
                raise WorkspaceV2LifecycleError("mutable database must never be linked")
            _require_no_directory_users(root)
            with (
                _reconciliation_lock(retained / "state"),
                _v2_writer_closed(root),
                _v2_writer_closed(retained),
                _closed_duckdb_file(root / "storage/v2/index.duckdb"),
                _closed_duckdb_file(retained / "storage/v2/index.duckdb"),
            ):
                if any((p / "storage/v2/index.duckdb.wal").exists() for p in (root, retained)):
                    raise WorkspaceV2LifecycleError("preparatory retirement requires closed images")
                if ledger.resolve()[0].certificate != head.certificate:
                    raise WorkspaceV2LifecycleError("retained W2 changed during retirement")
                WorkspaceV2ProgressReconciler._verify(generation.database)
                intent_path = proof_path.with_name(proof_path.name + ".intent.json")
                if not intent_path.exists():
                    proposal = proposal.model_copy(update={"verified_at": self._now()})
                    WorkspaceV2ProgressReconciler._write_once(
                        intent_path, canonical_json_bytes(proposal) + b"\n"
                    )
                else:
                    old = PreparatorySlotRetirementReceipt.model_validate_json(
                        intent_path.read_bytes()
                    )
                    if proposal.model_copy(update={"verified_at": old.verified_at}) != old:
                        raise WorkspaceV2LifecycleError("preparatory retirement proposal changed")
                    proposal = old
                unheader = link_header.model_copy(
                    update={
                        "operation_id": proposal.operation_id,
                        "action": "UNLINK",
                        "previous_journal": link_ref if restore.parent_linked_files else None,
                        "byte_budget": journal_byte_budget,
                        "created_at": proposal.verified_at,
                        "verifier_sha256": _immutable_link_verifier_sha256(),
                    }
                )
                journal = ImmutableLinkJournal(unlink_root, unheader).open()
                if prepared_path.exists():
                    if (
                        PreparatorySlotRetirementReceipt.model_validate_json(
                            prepared_path.read_bytes()
                        )
                        != proposal
                    ):
                        raise WorkspaceV2LifecycleError("prepared retirement changed before resume")
                    _resume_preparatory_unlink_tail(journal)
                count, total = _verify_preparatory_slot_remaining(
                    root, controls["files_inventory"].path, link_ref, journal
                )
                if (count, total) != (manifest.file_count, manifest.logical_bytes):
                    raise WorkspaceV2LifecycleError("preparatory slot remaining coverage differs")
                WorkspaceV2ProgressReconciler._write_once(
                    prepared_path, canonical_json_bytes(proposal) + b"\n"
                )
                if restore.parent_linked_files:
                    assert journal._prior is not None
                    prior, journal._prior = tee(journal._prior)
                else:
                    prior = iter(())
                entry = next(prior, None)
                pending = []
                for ordinal, item in enumerate(_iter_delta_files(link_header.inventory.path)):
                    if entry is None:
                        break
                    if ordinal != entry.inventory_ordinal:
                        continue
                    if ordinal > journal.confirmed_inventory_ordinal:
                        pending.append((ordinal, item))
                        if len(pending) == 256:
                            _apply_controlled_link_page(journal, tuple(pending))
                            pending.clear()
                            execution.record_measurement(
                                "preparatory_unlinked_files", journal._ordinal
                            )
                    entry = next(prior, None)
                if pending:
                    _apply_controlled_link_page(journal, tuple(pending))
                if entry is not None or journal._ordinal != restore.parent_linked_files:
                    raise WorkspaceV2LifecycleError("preparatory unlink coverage differs")
                unlink_ref = reconciliation_file_reference(
                    unlink_root
                    / (
                        f"page-{journal._page - 1:08d}.trailer.json"
                        if journal._page
                        else "header.json"
                    ),
                    "control",
                )
                unlink_artifact = PreparatorySlotArtifact(
                    kind="link_witness",
                    path=unlink_ref.path,
                    sha256=unlink_ref.sha256,
                    size_bytes=unlink_ref.size_bytes,
                )
                WorkspaceV2ProgressReconciler._write_once(
                    proof_path.with_name(proof_path.name + ".unlink.json"),
                    canonical_json_bytes(unlink_artifact) + b"\n",
                )
                descriptor = FrozenSourceCut(
                    operation_id=proposal.operation_id,
                    source_root=root,
                    workspace_id=proposal.workspace_id,
                    workspace_format_version=2,
                    created_at=self._workspace_service.manifest(root).created_at,
                    source_fingerprint=manifest.files_sha256,
                    frozen_at=proposal.verified_at,
                )
                checkpoint = LifecycleCheckpoint(
                    operation_id=proposal.operation_id,
                    phase=LifecyclePhase.RETIRING,
                    source=descriptor,
                    retirement_root=retiring,
                    files_confirmed=proposal.files_confirmed,
                    bytes_confirmed=proposal.bytes_confirmed,
                )
                self.write_checkpoint(checkpoint_path, checkpoint)
                if retiring.exists():
                    raise WorkspaceV2LifecycleError("preparatory retiring root already exists")
                os.replace(root, retiring)
                _fsync_directory(root.parent)
        if checkpoint is None or checkpoint.phase not in {
            LifecyclePhase.RETIRING,
            LifecyclePhase.RETIRED,
        }:
            raise WorkspaceV2LifecycleError("preparatory retirement has no valid checkpoint")
        if (
            checkpoint.operation_id != proposal.operation_id
            or checkpoint.source.source_root != root
            or checkpoint.retirement_root != retiring
        ):
            raise WorkspaceV2LifecycleError("preparatory retirement checkpoint differs")
        if retiring.exists():
            _safe_existing_root(retiring, label="retiring preparatory slot")
            _require_no_directory_users(retiring)
            old = PreparatorySlotRetirementReceipt.model_validate_json(prepared_path.read_bytes())
            unheader = link_header.model_copy(
                update={
                    "operation_id": old.operation_id,
                    "action": "UNLINK",
                    "previous_journal": link_ref if restore.parent_linked_files else None,
                    "byte_budget": journal_byte_budget,
                    "created_at": old.verified_at,
                    "verifier_sha256": _immutable_link_verifier_sha256(),
                }
            )
            with _reconciliation_lock(retained / "state"), ExitStack() as locks:
                if (retiring / "storage/v2/raw-v2-staging.lock").exists():
                    locks.enter_context(_v2_writer_closed(retiring))
                if (retiring / "storage/v2/index.duckdb").exists():
                    locks.enter_context(_closed_duckdb_file(retiring / "storage/v2/index.duckdb"))
                journal = ImmutableLinkJournal(unlink_root, unheader).open()
                if _verify_preparatory_slot_remaining(
                    retiring,
                    controls["files_inventory"].path,
                    link_ref,
                    journal,
                    allow_removed=True,
                ) != (manifest.file_count, manifest.logical_bytes):
                    raise WorkspaceV2LifecycleError("detached preparatory contents changed")
                shutil.rmtree(retiring)
                _fsync_directory(root.parent)
        proposal = PreparatorySlotRetirementReceipt.model_validate_json(prepared_path.read_bytes())
        unlink_artifact = PreparatorySlotArtifact.model_validate_json(
            proof_path.with_name(proof_path.name + ".unlink.json").read_bytes()
        )
        terminal = proposal.model_copy(
            update={
                "phase": "retired",
                "unlink_witness": unlink_artifact,
                "completed_at": self._now(),
            }
        )
        self.write_checkpoint(
            checkpoint_path, checkpoint.model_copy(update={"phase": LifecyclePhase.RETIRED})
        )
        WorkspaceV2ProgressReconciler._write_once(
            proof_path, canonical_json_bytes(terminal) + b"\n"
        )
        return terminal

    def retire_delta_recovery_slot(
        self,
        receipt: PhysicalDeltaReceipt,
        *,
        delta_root: Path,
        operation_id: UUID,
        checkpoint_path: Path,
        proof_path: Path,
    ) -> LifecycleReceipt:
        """Retire the exact independently recovered slot, preserving its small proof."""
        from investment_analyst.storage.serialization import canonical_json_bytes
        from investment_analyst.storage.workspace_v2_inventory import historical_transition_status
        from investment_analyst.workspace.workspace_v2_physical_delta import (
            PhysicalDeltaReceipt,
            _iter_delta_files,
            _read_delta_manifest,
            _source_files,
        )

        receipt = PhysicalDeltaReceipt.model_validate(receipt.model_dump())
        if (
            receipt.kind != "restore"
            or receipt.restored_root is None
            or receipt.historical_state != "COMPLETE"
        ):
            raise WorkspaceV2LifecycleError("retirement requires a terminal root-bound restore")
        root = receipt.restored_root
        if root.is_symlink() or any(p.is_symlink() for p in root.parents):
            raise WorkspaceV2LifecycleError("recovery retirement root is unsafe")
        controls = _safe_existing_root(delta_root, label="physical delta controls")
        manifest_path = controls / "delta-manifest.json"
        manifest = _read_delta_manifest(manifest_path)
        inventory = controls / "files.jsonl"
        if (
            manifest.historical_state != "COMPLETE"
            or receipt.epoch != manifest.epoch
            or receipt.manifest_sha256 != _hash_file(manifest_path)[1]
            or receipt.files_sha256 != manifest.files_sha256
            or _hash_file(inventory)[1] != manifest.files_sha256
            or receipt.confirmed_files != manifest.file_count + manifest.deleted_count
        ):
            raise WorkspaceV2LifecycleError("recovery retirement manifest binding differs")
        retained = _safe_existing_root(manifest.source_root, label="retained physical source")
        if (
            root in (retained, controls)
            or root in retained.parents
            or retained in root.parents
            or root in controls.parents
            or controls in root.parents
        ):
            raise WorkspaceV2LifecycleError("recovery retirement roots must be disjoint")
        for output in (checkpoint_path, proof_path):
            if (
                not output.is_absolute()
                or output.is_relative_to(root)
                or output.is_relative_to(retained)
                or any(path.is_symlink() for path in (output, *output.parents))
            ):
                raise WorkspaceV2LifecycleError("retirement controls must be outside workspaces")
        if checkpoint_path == proof_path:
            raise WorkspaceV2LifecycleError("retirement controls must be distinct")
        receipt_digest = hashlib.sha256(canonical_json_bytes(receipt)).hexdigest()
        retiring = _retiring_root(root, operation_id)
        checkpoint = self._read_checkpoint(checkpoint_path)
        proof: WorkspaceV2RetirementProof | None = None
        if proof_path.exists():
            if proof_path.is_symlink():
                raise WorkspaceV2LifecycleError("recovery retirement proof is unsafe")
            proof = WorkspaceV2RetirementProof.model_validate_json(proof_path.read_bytes())
            if (
                proof.operation_id != operation_id
                or proof.source_root != root
                or proof.retained_root != retained
                or proof.recovery_receipt_sha256 != receipt_digest
                or proof.recovery_manifest_sha256 != receipt.manifest_sha256
                or proof.inventory_sha256 != manifest.files_sha256
                or proof.recovery_epoch != manifest.epoch
                or proof.source_workspace_id != manifest.source_workspace_id
            ):
                raise WorkspaceV2LifecycleError("recovery retirement proof binding differs")
        if root.exists():
            if retiring.exists() or (
                checkpoint is not None
                and (
                    proof is None
                    or checkpoint.phase != LifecyclePhase.RETIRING
                    or checkpoint.operation_id != operation_id
                    or checkpoint.source.source_root != root
                    or checkpoint.retirement_root != retiring
                    or checkpoint.source.source_fingerprint != manifest.files_sha256
                )
            ):
                raise WorkspaceV2LifecycleError("recovery retirement paths are ambiguous")
            retained_inspection = _inspect_ready_v2(
                self._workspace_service, retained, "retained source"
            )
            with _v2_writer_closed(root):
                inspection = _inspect_ready_v2(self._workspace_service, root, "recovered slot")
                if (
                    inspection.workspace_id != manifest.source_workspace_id
                    or retained_inspection.workspace_id != inspection.workspace_id
                    or _inspection_counts(inspection) != _inspection_counts(retained_inspection)
                    or historical_transition_status(root) != "COMPLETE"
                ):
                    raise WorkspaceV2LifecycleError("recovery retirement workspace binding differs")
                actual = iter(_source_files(root))
                count = total = 0
                for item in _iter_delta_files(inventory):
                    if item.deleted:
                        continue
                    if next(actual, None) != item.path:
                        raise WorkspaceV2LifecycleError(
                            "recovery retirement contains unique or absent files"
                        )
                    size, digest = _hash_file(root / item.path)
                    if (size, digest) != (item.size_bytes, item.sha256):
                        raise WorkspaceV2LifecycleError("recovery retirement content differs")
                    count += 1
                    total += size
                if next(actual, None) is not None or (count, total) != (
                    manifest.file_count,
                    manifest.logical_bytes,
                ):
                    raise WorkspaceV2LifecycleError("recovery retirement coverage differs")
                verified = WorkspaceV2RetirementProof(
                    operation_id=operation_id,
                    role="physical_delta_recovery_slot",
                    source_root=root,
                    source_workspace_id=inspection.workspace_id,
                    retained_root=retained,
                    recovery_epoch=manifest.epoch,
                    recovery_manifest_sha256=receipt.manifest_sha256,
                    recovery_receipt_sha256=receipt_digest,
                    inventory_sha256=manifest.files_sha256,
                    files_confirmed=count,
                    bytes_confirmed=total,
                    verified_at=self._now(),
                )
                if (
                    proof is not None
                    and proof.model_copy(update={"verified_at": verified.verified_at}) != verified
                ):
                    raise WorkspaceV2LifecycleError("recovery retirement proof changed")
                if proof is None:
                    _replace_json(proof_path, verified.model_dump(mode="json"))
                    proof = verified
                identity = self._workspace_service.manifest(root)
                source = FrozenSourceCut(
                    operation_id=operation_id,
                    source_root=root,
                    workspace_id=identity.workspace_id,
                    workspace_format_version=2,
                    created_at=identity.created_at,
                    source_fingerprint=manifest.files_sha256,
                    frozen_at=proof.verified_at,
                )
                checkpoint = LifecycleCheckpoint(
                    operation_id=operation_id,
                    phase=LifecyclePhase.RETIRING,
                    source=source,
                    retirement_root=retiring,
                    files_confirmed=count,
                    bytes_confirmed=total,
                )
                self.write_checkpoint(checkpoint_path, checkpoint)
                try:
                    os.replace(root, retiring)
                    _fsync_directory(root.parent)
                except OSError as error:
                    raise WorkspaceV2LifecycleError(
                        "recovery retirement rename did not complete"
                    ) from error
        elif (
            checkpoint is None
            or proof is None
            or checkpoint.operation_id != operation_id
            or checkpoint.source.source_root != root
            or checkpoint.retirement_root != retiring
            or checkpoint.source.source_fingerprint != manifest.files_sha256
            or checkpoint.files_confirmed != proof.files_confirmed
            or checkpoint.bytes_confirmed != proof.bytes_confirmed
        ):
            raise WorkspaceV2LifecycleError("recovery retirement cannot resume")
        assert checkpoint is not None and proof is not None
        if checkpoint.phase == LifecyclePhase.RETIRING:
            if retiring.exists():
                _safe_existing_root(retiring, label="retiring recovered slot")
                try:
                    shutil.rmtree(retiring)
                except OSError as error:
                    raise WorkspaceV2LifecycleError(
                        "recovery retirement did not complete"
                    ) from error
                _fsync_directory(root.parent)
            checkpoint = checkpoint.model_copy(update={"phase": LifecyclePhase.RETIRED})
            self.write_checkpoint(checkpoint_path, checkpoint)
        elif checkpoint.phase != LifecyclePhase.RETIRED or retiring.exists():
            raise WorkspaceV2LifecycleError("recovery retirement terminal state differs")
        return LifecycleReceipt(
            operation_id=operation_id,
            phase=LifecyclePhase.RETIRED,
            source_root=root,
            files_confirmed=proof.files_confirmed,
            bytes_confirmed=proof.bytes_confirmed,
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

    def retire_packed_backup(
        self,
        root: Path,
        *,
        retained_controls: Path,
        parent_index: Path,
        remote_manifest_path: Path,
        recovery_receipt_path: Path,
        operation_id: UUID,
        inventory_path: Path,
        proof_path: Path,
        checkpoint_path: Path,
        role: Literal["packed_backup", "restore_staging"] = "packed_backup",
    ) -> SealedDirectoryRetirementReceipt:
        """Retire packed bytes only after independently restored parent and index survive."""
        from investment_analyst.workspace.workspace_v2_backup import _iter_packed_journal
        from investment_analyst.workspace.workspace_v2_physical_delta import (
            verify_physical_delta_index,
        )
        from investment_analyst.workspace.workspace_v2_remote import (
            RemoteBackupManifest,
            RemoteBackupRestoreReceipt,
        )

        if role not in {"packed_backup", "restore_staging"}:
            raise WorkspaceV2LifecycleError("backup retirement role is invalid")
        for path in (
            root,
            retained_controls,
            parent_index,
            remote_manifest_path,
            recovery_receipt_path,
            inventory_path,
            proof_path,
            checkpoint_path,
        ):
            if not path.is_absolute() or any(p.is_symlink() for p in (path, *path.parents)):
                raise WorkspaceV2LifecycleError("packed retirement path is unsafe")
        for path in (
            retained_controls,
            parent_index,
            remote_manifest_path,
            recovery_receipt_path,
            inventory_path,
            proof_path,
            checkpoint_path,
        ):
            if path.is_relative_to(root) or root.is_relative_to(path):
                raise WorkspaceV2LifecycleError("packed retirement dependencies overlap root")
        index = verify_physical_delta_index(parent_index)
        remote = RemoteBackupManifest.model_validate_json(remote_manifest_path.read_bytes())
        recovery = RemoteBackupRestoreReceipt.model_validate_json(
            recovery_receipt_path.read_bytes()
        )
        remote_digest = _hash_file(remote_manifest_path)[1]
        recovery_digest = _hash_file(recovery_receipt_path)[1]
        if (
            remote.backup_id != index.parent_backup_id
            or recovery.backup_id != remote.backup_id
            or remote.source_workspace_id != index.source_workspace_id
            or recovery.restored_workspace_id != index.source_workspace_id
            or recovery.remote_manifest_sha256 != remote_digest
            or recovery.volume_count != index.volume_count
            or remote.inventory_sha256 != index.inventory_sha256
            or remote.journal_sha256 != index.journal_sha256
        ):
            raise WorkspaceV2LifecycleError("packed retirement recovery parent differs")
        retiring = _retiring_root(root, operation_id)
        checkpoint = self._read_sealed_directory_checkpoint(checkpoint_path)
        if checkpoint is not None and (
            checkpoint.operation_id != operation_id
            or checkpoint.source_root != root
            or checkpoint.retiring_root != retiring
            or checkpoint.remote_manifest_sha256 != remote_digest
        ):
            raise WorkspaceV2LifecycleError("packed retirement checkpoint differs")
        if root.exists():
            if retiring.exists():
                raise WorkspaceV2LifecycleError("packed retirement path is ambiguous")
            _require_no_directory_users(root)
            packed = WorkspaceV2PackedStreamManifest.model_validate_json(
                (root / "backup_manifest.json").read_bytes()
            )
            if (
                packed.backup_id != remote.backup_id
                or packed.inventory.digest_sha256 != index.inventory_sha256
                or packed.journal_sha256 != index.journal_sha256
                or packed.volume_count != index.volume_count
            ):
                raise WorkspaceV2LifecycleError("packed retirement manifest differs")
            expected: dict[str, tuple[int, str]] = {}
            objects = {item.object_key: item for item in remote.objects}
            for name in (
                "backup_manifest.json",
                "source-inventory.jsonl",
                "packed-backup-volumes.jsonl",
            ):
                size, digest = _hash_file(root / name)
                if (role == "packed_backup" or name == "backup_manifest.json") and (
                    _hash_file(retained_controls / name) != (size, digest)
                ):
                    raise WorkspaceV2LifecycleError(
                        "packed retirement control preservation differs"
                    )
                matches = [
                    item
                    for item in remote.objects
                    if item.object_key in {f"controls/{name}", f"controls-v2/{digest}/{name}"}
                ]
                if len(matches) != 1 or (matches[0].size_bytes, matches[0].sha256) != (
                    size,
                    digest,
                ):
                    raise WorkspaceV2LifecycleError("packed retirement remote control differs")
                expected[name] = (size, digest)
            volume_count = 0
            for volume in _iter_packed_journal(root / "packed-backup-volumes.jsonl"):
                item = objects.get(f"volumes/{volume.compressed_sha256}.gz")
                if item is None or (item.size_bytes, item.sha256) != (
                    volume.compressed_bytes,
                    volume.compressed_sha256,
                ):
                    raise WorkspaceV2LifecycleError("packed retirement remote volume differs")
                if role == "packed_backup" or (root / volume.path).exists():
                    expected[volume.path] = (volume.compressed_bytes, volume.compressed_sha256)
                volume_count += 1
            if volume_count != index.volume_count:
                raise WorkspaceV2LifecycleError("packed retirement volume count differs")
            own = root / (
                "packed-backup-checkpoint.json"
                if role == "packed_backup"
                else "remote-backup-manifest.json"
            )
            own_data = own.read_bytes()
            own_state = json.loads(own_data)
            if role == "packed_backup" and (
                own_state.get("source_workspace_id") != str(index.source_workspace_id)
                or own_state.get("inventory_digest") != index.inventory_sha256
                or own_state.get("next_volume_ordinal") != index.volume_count
                or own_state.get("next_file_index") != index.file_count
                or own_state.get("next_offset_bytes") != 0
            ):
                raise WorkspaceV2LifecycleError("packed retirement checkpoint is incomplete")
            if role == "restore_staging" and hashlib.sha256(own_data).hexdigest() != remote_digest:
                raise WorkspaceV2LifecycleError(
                    "restore staging control differs from independent parent"
                )
            preserved = (
                proof_path.with_name(proof_path.stem + ".packed-backup-checkpoint.json")
                if role == "packed_backup"
                else retained_controls / own.name
            )
            if preserved.is_symlink():
                raise WorkspaceV2LifecycleError("preserved backup control is unsafe")
            if preserved.exists():
                if preserved.read_bytes() != own_data:
                    raise WorkspaceV2LifecycleError(
                        "packed retirement preserved checkpoint differs"
                    )
            else:
                _write_new_bytes(preserved, own_data)
            expected[own.name] = (len(own_data), hashlib.sha256(own_data).hexdigest())
            actual = sorted(path.relative_to(root).as_posix() for path in _iter_regular_files(root))
            if actual != sorted(expected):
                raise WorkspaceV2LifecycleError(
                    "packed retirement contains unique or absent content"
                )
            digest = hashlib.sha256()
            total = 0
            partial = inventory_path.with_name(inventory_path.name + ".partial")
            if partial.is_symlink() or (partial.exists() and not partial.is_file()):
                raise WorkspaceV2LifecycleError("packed retirement partial inventory is unsafe")
            with partial.open("w", encoding="utf-8", newline="\n") as stream:
                for name in actual:
                    size, checksum = _hash_file(root / name)
                    if (size, checksum) != expected[name]:
                        raise WorkspaceV2LifecycleError("packed retirement file content differs")
                    encoded = (
                        json.dumps(
                            {"path": name, "size_bytes": size, "sha256": checksum},
                            sort_keys=True,
                            separators=(",", ":"),
                        )
                        + "\n"
                    ).encode()
                    digest.update(encoded)
                    stream.write(encoded.decode())
                    total += size
                stream.flush()
                os.fsync(stream.fileno())
            if inventory_path.exists() and _hash_file(inventory_path)[1] != digest.hexdigest():
                raise WorkspaceV2LifecycleError("packed retirement inventory changed")
            os.replace(partial, inventory_path)
            _fsync_directory(inventory_path.parent)
            proof = BackupDirectoryRetirementProof(
                operation_id=operation_id,
                role=role,
                source_root=root,
                retained_controls=retained_controls,
                parent_index=parent_index,
                parent_index_sha256=_hash_file(parent_index)[1],
                remote_manifest_sha256=remote_digest,
                recovery_receipt_sha256=recovery_digest,
                inventory_sha256=digest.hexdigest(),
                files_confirmed=len(actual),
                bytes_confirmed=total,
                verified_at=self._now(),
            )
            if proof_path.exists():
                previous = BackupDirectoryRetirementProof.model_validate_json(
                    proof_path.read_bytes()
                )
                if previous.model_copy(update={"verified_at": proof.verified_at}) != proof:
                    raise WorkspaceV2LifecycleError("packed retirement proof changed")
            else:
                _write_new_json(proof_path, proof.model_dump(mode="json"))
            checkpoint = SealedDirectoryRetirementCheckpoint(
                operation_id=operation_id,
                phase="retiring",
                source_root=root,
                retiring_root=retiring,
                inventory_sha256=proof.inventory_sha256,
                remote_manifest_sha256=remote_digest,
                files_confirmed=proof.files_confirmed,
                bytes_confirmed=total,
            )
            self._write_sealed_directory_checkpoint(checkpoint_path, checkpoint)
            _require_no_directory_users(root)
            os.replace(root, retiring)
            _fsync_directory(root.parent)
        if checkpoint is None or not proof_path.is_file() or not inventory_path.is_file():
            raise WorkspaceV2LifecycleError("packed retirement cannot resume without proof")
        proof = BackupDirectoryRetirementProof.model_validate_json(proof_path.read_bytes())
        if (
            proof.source_root != root
            or proof.role != role
            or proof.operation_id != operation_id
            or proof.remote_manifest_sha256 != remote_digest
            or proof.recovery_receipt_sha256 != recovery_digest
            or proof.parent_index_sha256 != _hash_file(parent_index)[1]
            or proof.inventory_sha256 != _hash_file(inventory_path)[1]
            or checkpoint.inventory_sha256 != proof.inventory_sha256
        ):
            raise WorkspaceV2LifecycleError("packed retirement resume proof differs")
        if checkpoint.phase == "retiring":
            if retiring.exists():
                _require_no_directory_users(retiring)
                shutil.rmtree(retiring)
                _fsync_directory(root.parent)
            checkpoint = checkpoint.model_copy(update={"phase": "retired"})
            self._write_sealed_directory_checkpoint(checkpoint_path, checkpoint)
        elif retiring.exists():
            raise WorkspaceV2LifecycleError("packed retirement terminal path differs")
        return SealedDirectoryRetirementReceipt(checkpoint=checkpoint, completed_at=self._now())

    def preserve_small_backup_controls(self, root: Path, destination: Path) -> tuple[Path, Path]:
        """Keep small original controls byte-exact, leaving large JSONL in a single copy."""
        source = _safe_existing_root(root, label="backup controls")
        target = _safe_new_root(destination, source, label="preserved backup controls")
        if any(path.is_symlink() for path in (*source.parents, *target.parents)):
            raise WorkspaceV2LifecycleError("backup control ancestor is unsafe")
        _require_no_directory_users(source)
        target.mkdir(parents=True, exist_ok=True)
        outputs: list[Path] = []
        for name in ("backup_manifest.json", "remote-backup-manifest.json"):
            original = source / name
            output = target / name
            if (
                original.is_symlink()
                or not original.is_file()
                or original.stat().st_size > 1024 * 1024
            ):
                raise WorkspaceV2LifecycleError("small backup control is unsafe or exceeds bound")
            expected = _hash_file(original)
            if output.exists():
                if output.is_symlink() or _hash_file(output) != expected:
                    raise WorkspaceV2LifecycleError("preserved backup control differs")
            else:
                temporary = output.with_name(f".{name}.{uuid4().hex}.tmp")
                try:
                    if _copy_and_hash(original, temporary) != expected:
                        raise WorkspaceV2LifecycleError(
                            "backup control changed during preservation"
                        )
                    os.replace(temporary, output)
                    _fsync_directory(target)
                finally:
                    temporary.unlink(missing_ok=True)
            outputs.append(output)
        return outputs[0], outputs[1]

    def retire_owned_payload(
        self, request: OwnedPayloadRetirementInput
    ) -> OwnedPayloadRetirementReceipt:
        """Retire only exact matrix payloads, with proofs retained outside the root.

        A sealed-directory cursor owns detach/removal. The owned receipt binds that
        cursor to the original request and a streaming inventory, including across
        a crash after detach or halfway through removal.
        """
        from investment_analyst.workspace.workspace_v2_lifecycle_models import (
            PreparatorySlotArtifact,
        )

        root, controls = request.source_root, request.retained_controls_root
        _owned_payload_scope(request)
        for path in (root, controls, *[ref.path for ref in request.evidence]):
            if any(p.is_symlink() for p in (path, *path.parents)):
                raise WorkspaceV2LifecycleError("owned payload path is unsafe")
        controls.mkdir(parents=True, exist_ok=True)
        stem = str(request.operation_id)
        receipt_path = controls / f"{stem}.json"
        inventory_path = controls / f"{stem}.inventory.jsonl"
        checkpoint_path = controls / f"{stem}.checkpoint.json"
        for path in (
            receipt_path,
            inventory_path,
            checkpoint_path,
            controls / "owned-payload-retirement.lock",
        ):
            if path.is_symlink():
                raise WorkspaceV2LifecycleError("owned payload control is unsafe")
        with (controls / "owned-payload-retirement.lock").open("a+b") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            existing = None
            if receipt_path.exists():
                existing = OwnedPayloadRetirementReceipt.model_validate_json(
                    receipt_path.read_bytes()
                )
                if existing.request != request or _hash_file(existing.inventory.path) != (
                    existing.inventory.size_bytes,
                    existing.inventory.sha256,
                ):
                    raise WorkspaceV2LifecycleError("owned payload retirement binding differs")
            checkpoint = self._read_sealed_directory_checkpoint(checkpoint_path)
            retiring = _retiring_root(root, request.operation_id)
            if existing is not None and existing.completed_at is not None:
                if root.exists() or retiring.exists() or checkpoint != existing.checkpoint:
                    raise WorkspaceV2LifecycleError("owned payload terminal state differs")
                return existing
            _require_no_directory_users(root)
            _require_no_directory_users(retiring)
            if existing is None:
                if not root.is_dir() or retiring.exists() or checkpoint is not None:
                    raise WorkspaceV2LifecycleError("owned payload source is unavailable")
                expected = _owned_payload_proofs(request)
                started = self._now()
                count = logical = allocated = 0
                # An incomplete inventory is not a deletion cursor. Recreate only
                # this operation's inventory while the original root is untouched.
                temporary = inventory_path.with_suffix(".pending")
                if temporary.is_symlink():
                    raise WorkspaceV2LifecycleError("owned payload inventory is ambiguous")
                with temporary.open("w", encoding="utf-8") as stream:
                    for parent, dirs, files in os.walk(root, followlinks=False):
                        for name in sorted(dirs + files):
                            path = Path(parent) / name
                            if path.is_symlink():
                                raise WorkspaceV2LifecycleError("owned payload contains a symlink")
                            if path.is_dir():
                                continue
                            if not path.is_file():
                                raise WorkspaceV2LifecycleError(
                                    "owned payload contains unknown data"
                                )
                            relative = path.relative_to(root).as_posix()
                            _owned_payload_member(request, relative, expected)
                            stat = path.stat()
                            size, digest = _hash_file(path)
                            if (
                                request.role == OwnedPayloadRole.DELTA_STAGING_OBJECTS
                                and (size, digest) != expected[relative]
                            ):
                                raise WorkspaceV2LifecycleError("owned payload content differs")
                            stream.write(
                                json.dumps(
                                    {
                                        "path": relative,
                                        "size_bytes": size,
                                        "allocated_bytes": stat.st_blocks * 512,
                                        "sha256": digest,
                                        "device": stat.st_dev,
                                        "inode": stat.st_ino,
                                        "mtime_ns": stat.st_mtime_ns,
                                        "ctime_ns": stat.st_ctime_ns,
                                        "links": stat.st_nlink,
                                    },
                                    sort_keys=True,
                                    separators=(",", ":"),
                                )
                                + "\n"
                            )
                            count += 1
                            logical += size
                            allocated += stat.st_blocks * 512
                    stream.flush()
                    os.fsync(stream.fileno())
                if inventory_path.exists() and _hash_file(inventory_path) != _hash_file(temporary):
                    raise WorkspaceV2LifecycleError("owned payload uncommitted inventory differs")
                os.replace(temporary, inventory_path)
                size, digest = _hash_file(inventory_path)
                checkpoint = SealedDirectoryRetirementCheckpoint(
                    operation_id=request.operation_id,
                    phase="retiring",
                    source_root=root,
                    retiring_root=retiring,
                    inventory_sha256=digest,
                    remote_manifest_sha256=hashlib.sha256(
                        request.model_dump_json().encode()
                    ).hexdigest(),
                    files_confirmed=count,
                    bytes_confirmed=logical,
                )
                existing = OwnedPayloadRetirementReceipt(
                    request=request,
                    inventory=PreparatorySlotArtifact(
                        kind="files_inventory", path=inventory_path, size_bytes=size, sha256=digest
                    ),
                    checkpoint=checkpoint,
                    allocated_bytes=allocated,
                    started_at=started,
                )
                _replace_json(receipt_path, existing.model_dump(mode="json"))
            if checkpoint is None:
                checkpoint = existing.checkpoint
                self._write_sealed_directory_checkpoint(checkpoint_path, checkpoint)
            if checkpoint.model_copy(update={"phase": "retiring"}) != existing.checkpoint:
                raise WorkspaceV2LifecycleError("owned payload cursor differs")
            if root.exists():
                if retiring.exists():
                    raise WorkspaceV2LifecycleError("owned payload detach is ambiguous")
                _require_no_directory_users(root)
                _verify_owned_payload_inventory(root, inventory_path, allow_missing=False)
                os.replace(root, retiring)
                _fsync_directory(root.parent)
            if retiring.exists():
                _require_no_directory_users(retiring)
                _verify_owned_payload_inventory(retiring, inventory_path, allow_missing=True)
                shutil.rmtree(retiring)
                _fsync_directory(root.parent)
            checkpoint = checkpoint.model_copy(update={"phase": "retired"})
            self._write_sealed_directory_checkpoint(checkpoint_path, checkpoint)
            terminal = existing.model_copy(
                update={
                    "checkpoint": checkpoint,
                    "completed_at": self._now(),
                }
            )
            _replace_json(receipt_path, terminal.model_dump(mode="json"))
            return terminal

    def retire_sealed_directory(
        self,
        root: Path,
        *,
        complement: LocalComplementManifest,
        remote_manifest_sha256: str,
        operation_id: UUID,
        checkpoint_path: Path,
    ) -> SealedDirectoryRetirementReceipt:
        """Retire a verified non-workspace complement without inventing workspace identity."""
        source = root.expanduser().resolve(strict=False)
        if not source.is_absolute() or source.is_symlink():
            raise WorkspaceV2LifecycleError("sealed directory root is unavailable or unsafe")
        if not _is_sha256(remote_manifest_sha256):
            raise WorkspaceV2LifecycleError("remote manifest digest is invalid")
        checkpoint = self._read_sealed_directory_checkpoint(checkpoint_path)
        retiring = _retiring_root(source, operation_id)
        if source.exists():
            source = _safe_existing_root(source, label="sealed directory root")
            verified = self.verify_complement(complement.source, source)
            if verified != complement:
                raise WorkspaceV2LifecycleError("sealed directory complement changed")
            inventory_sha256 = _hash_file(source / _COMPLEMENT_MANIFEST_NAME)[1]
            cursor = SealedDirectoryRetirementCheckpoint(
                operation_id=operation_id,
                phase="retiring",
                source_root=source,
                retiring_root=retiring,
                inventory_sha256=inventory_sha256,
                remote_manifest_sha256=remote_manifest_sha256,
                files_confirmed=len(complement.files),
                bytes_confirmed=complement.total_bytes,
            )
            self._write_sealed_directory_checkpoint(checkpoint_path, cursor)
            if retiring.exists():
                raise WorkspaceV2LifecycleError("sealed directory retirement path is ambiguous")
            os.replace(source, retiring)
            _fsync_directory(source.parent)
        else:
            if (
                checkpoint is None
                or checkpoint.operation_id != operation_id
                or checkpoint.source_root != source
                or checkpoint.retiring_root != retiring
                or checkpoint.remote_manifest_sha256 != remote_manifest_sha256
            ):
                raise WorkspaceV2LifecycleError(
                    "sealed directory retirement cannot be resumed safely"
                )
            cursor = checkpoint
        if cursor.phase == "retiring":
            if _safe_existing_root(retiring, label="retiring sealed directory") != retiring:
                raise WorkspaceV2LifecycleError(
                    "sealed directory retirement cannot be resumed safely"
                )
            try:
                shutil.rmtree(retiring)
            except OSError as error:
                raise WorkspaceV2LifecycleError(
                    "sealed directory retirement did not complete"
                ) from error
            cursor = cursor.model_copy(update={"phase": "retired"})
            self._write_sealed_directory_checkpoint(checkpoint_path, cursor)
        return SealedDirectoryRetirementReceipt(checkpoint=cursor, completed_at=self._now())

    def write_sealed_directory_receipt(
        self, path: Path, receipt: SealedDirectoryRetirementReceipt
    ) -> SealedDirectoryRetirementReceipt:
        """Persist a terminal sealed-directory receipt without overwriting history."""
        target = _new_receipt_path(path, label="sealed directory receipt")
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

    def _read_sealed_directory_checkpoint(
        self, path: Path
    ) -> SealedDirectoryRetirementCheckpoint | None:
        if not path.exists():
            return None
        if path.is_symlink() or not path.is_file():
            raise WorkspaceV2LifecycleError("sealed directory checkpoint is unsafe")
        try:
            return SealedDirectoryRetirementCheckpoint.model_validate_json(
                path.read_text(encoding="utf-8")
            )
        except (OSError, ValueError) as error:
            raise WorkspaceV2LifecycleError("sealed directory checkpoint is malformed") from error

    def _write_sealed_directory_checkpoint(
        self, path: Path, checkpoint: SealedDirectoryRetirementCheckpoint
    ) -> None:
        target = path.expanduser().resolve(strict=False)
        if not target.is_absolute() or target.is_symlink():
            raise WorkspaceV2LifecycleError("sealed directory checkpoint path is unsafe")
        _replace_json(target, checkpoint.model_dump(mode="json"))

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise WorkspaceV2LifecycleError("lifecycle clock must be timezone-aware")
        return value.astimezone(UTC)


def _resume_preparatory_unlink_tail(journal: ImmutableLinkJournal) -> None:
    """Finish only the durable bounded intent, retaining all unknown extra data."""
    from investment_analyst.workspace.workspace_v2_physical_delta import (
        ImmutableLinkEntry,
        _iter_delta_files,
        _safe_file,
    )

    path = _safe_file(journal.root, f"page-{journal._page:08d}.intents.jsonl")
    if not path.exists():
        return
    entries = []
    with path.open("rb") as stream:
        while line := stream.readline(1 << 20):
            if len(entries) == 256 or not line.endswith(b"\n"):
                raise WorkspaceV2LifecycleError("preparatory unlink intent exceeds bounds")
            entries.append(ImmutableLinkEntry.model_validate_json(line))
    wanted = {entry.inventory_ordinal for entry in entries}
    members = []
    for ordinal, item in enumerate(_iter_delta_files(journal.header.inventory.path)):
        if ordinal in wanted:
            members.append((ordinal, item))
        if len(members) == len(entries):
            break
    if not entries or len(members) != len(entries):
        raise WorkspaceV2LifecycleError("preparatory unlink intent membership differs")
    journal.apply_page(tuple(members))


def _verify_preparatory_slot_remaining(
    root: Path,
    inventory: Path,
    link_reference: ReconciliationFileReference,
    journal: ImmutableLinkJournal,
    *,
    allow_removed: bool = False,
) -> tuple[int, int]:
    """Recheck every surviving file even after a partial controlled UNLINK."""
    from investment_analyst.workspace.workspace_v2_physical_delta import (
        ImmutableLinkEntry,
        ImmutableLinkHeader,
        ImmutableLinkIdentity,
        ImmutableLinkJournal,
        PhysicalDeltaFile,
        _iter_delta_files,
        _path_order,
        _source_files,
    )

    old_header = ImmutableLinkHeader.model_validate_json(
        (link_reference.path.parent / "header.json").read_bytes()
    )
    prior = (
        ImmutableLinkJournal(link_reference.path.parent, old_header)._prior_witnesses(
            link_reference
        )
        if link_reference.path.name != "header.json"
        else iter(())
    )

    def linked_members() -> Iterator[tuple[ImmutableLinkEntry, PhysicalDeltaFile]]:
        entry = next(prior, None)
        for ordinal, item in enumerate(_iter_delta_files(old_header.inventory.path)):
            if entry is None:
                return
            if ordinal == entry.inventory_ordinal:
                yield entry, item
                entry = next(prior, None)
        if entry is not None:
            raise WorkspaceV2LifecycleError("preparatory witness inventory is incomplete")

    linked_inventory = linked_members()
    witnessed = next(linked_inventory, None)
    actual = iter(_source_files(root))
    current = next(actual, None)
    count = total = 0
    for item in _iter_delta_files(inventory):
        if item.deleted:
            continue
        if witnessed is not None and _path_order(witnessed[1].path) < _path_order(item.path):
            raise WorkspaceV2LifecycleError("preparatory witness member is missing from photo")
        linked = witnessed is not None and witnessed[1].path == item.path
        entry = witnessed[0] if linked else None
        if linked and (item.size_bytes, item.sha256) != (
            witnessed[1].size_bytes,
            witnessed[1].sha256,
        ):
            raise WorkspaceV2LifecycleError("preparatory witness member content differs")
        path = root / item.path
        if entry is not None and entry.inventory_ordinal <= journal.confirmed_inventory_ordinal:
            if path.exists() or path.is_symlink():
                raise WorkspaceV2LifecycleError("confirmed unlink member reappeared")
        else:
            if current != item.path:
                if allow_removed and (
                    current is None or _path_order(current) > _path_order(item.path)
                ):
                    count += 1
                    total += item.size_bytes
                    if linked:
                        witnessed = next(linked_inventory, None)
                    continue
                raise WorkspaceV2LifecycleError("preparatory slot contains unique or missing files")
            if linked:
                assert entry is not None
                if (
                    entry.after is None
                    or ImmutableLinkIdentity.observe(path) != entry.after
                    or ImmutableLinkIdentity.observe(old_header.retained_root / item.path)
                    != entry.after
                ):
                    raise WorkspaceV2LifecycleError("preparatory immutable slot changed")
            elif _hash_file(path) != (item.size_bytes, item.sha256):
                raise WorkspaceV2LifecycleError("preparatory slot content changed")
            current = next(actual, None)
        if linked:
            witnessed = next(linked_inventory, None)
        count += 1
        total += item.size_bytes
    if current is not None or witnessed is not None:
        raise WorkspaceV2LifecycleError("preparatory slot contains unique or missing suffix")
    return count, total


def _safe_existing_root(path: Path, *, label: str) -> Path:
    candidate = path.expanduser()
    if candidate.is_symlink() or not candidate.is_dir():
        raise WorkspaceV2LifecycleError(f"{label} root is missing or unsafe")
    resolved = candidate.resolve()
    if resolved != candidate.resolve(strict=True):
        raise WorkspaceV2LifecycleError(f"{label} root is unsafe")
    return resolved


def _owned_payload_scope(request: OwnedPayloadRetirementInput) -> None:
    """Literal matrix roots only; final cache/scratch gates are not yet admitted."""
    home = Path.home()
    ops = home / ".local/share/investment-analyst/ops/data-chassis-45"
    repository = home / "projects/investment-analyst"
    caches = {
        parent / name
        for parent in (repository, repository / ".worktrees/data-chassis-45")
        for name in (".pytest_cache", ".ruff_cache")
    }
    if not request.retained_controls_root.is_relative_to(ops):
        raise WorkspaceV2LifecycleError("owned payload controls are outside the matrix")
    if request.role == OwnedPayloadRole.REGENERABLE_LOCAL_CACHE:
        if request.source_root not in caches or request.evidence or request.consuming_generation:
            raise WorkspaceV2LifecycleError("regenerable cache root or identity differs")
        for process in Path("/proc").iterdir():
            if not process.name.isdecimal() or int(process.name) == os.getpid():
                continue
            try:
                arguments = (process / "cmdline").read_bytes().split(b"\0")
                if any(
                    Path(os.fsdecode(arg)).name in {"pytest", "ruff", "pytest.py"}
                    for arg in arguments
                    if arg
                ):
                    raise WorkspaceV2LifecycleError("test or linter still uses regenerable caches")
            except (FileNotFoundError, ProcessLookupError, PermissionError):
                continue
        return
    if request.role == OwnedPayloadRole.DELTA_STAGING_OBJECTS:
        expected = ops / "physical-delta-W-20261017/physical-delta/objects"
        if request.source_root != expected or request.releasing_stage != "E0":
            raise WorkspaceV2LifecycleError("delta staging is protected by pending consumers")
        _require_no_directory_users(expected.parent.parent)
        return
    raise WorkspaceV2LifecycleError("owned payload still requires its final consumer release")


def _verify_owned_payload_inventory(
    root: Path, inventory: Path, *, allow_missing: bool, regenerable_venv_links: bool = False
) -> None:
    """Detect additions and edits after inventory; partial own removals may resume."""
    found = 0
    with inventory.open(encoding="utf-8") as stream:
        for line in stream:
            row = json.loads(line)
            path = root / row["path"]
            if not path.exists() and allow_missing:
                continue
            if path.is_symlink() or not path.is_file():
                raise WorkspaceV2LifecycleError("owned payload inventory member is unavailable")
            stat = path.stat()
            if (
                stat.st_dev,
                stat.st_ino,
                stat.st_size,
                stat.st_mtime_ns,
                stat.st_ctime_ns,
                stat.st_nlink,
            ) != (
                row["device"],
                row["inode"],
                row["size_bytes"],
                row["mtime_ns"],
                row["ctime_ns"],
                row["links"],
            ):
                raise WorkspaceV2LifecycleError("owned payload changed after inventory")
            found += 1
    actual = 0
    for parent, dirs, files in os.walk(root, followlinks=False):
        for name in dirs + files:
            path = Path(parent) / name
            if path.is_symlink() and not (
                regenerable_venv_links and path.relative_to(root).parts[0] == ".venv"
            ):
                raise WorkspaceV2LifecycleError("owned payload contains a new symlink")
        actual += sum(not (Path(parent) / name).is_symlink() for name in files)
    if actual != found:
        raise WorkspaceV2LifecycleError("owned payload contains new or missing content")


def _owned_payload_proofs(
    request: OwnedPayloadRetirementInput,
) -> dict[str, tuple[int, str]]:
    if request.role == OwnedPayloadRole.REGENERABLE_LOCAL_CACHE:
        return {}
    from investment_analyst.workspace.workspace_v2_acceptance import CompatibleRecoveryReadback
    from investment_analyst.workspace.workspace_v2_physical_delta import (
        PhysicalDeltaManifest,
        PhysicalDeltaReceipt,
        _iter_delta_files,
    )

    refs = {ref.kind: ref for ref in request.evidence}
    required = {
        "delta_manifest",
        "files_inventory",
        "remote_upload",
        "restore_receipt",
        "compatible_readback",
    }
    if set(refs) != required or len(refs) != len(request.evidence):
        raise WorkspaceV2LifecycleError("owned payload recovery proofs are incomplete")
    for ref in refs.values():
        if ref.path.is_relative_to(request.source_root) or _hash_file(ref.path) != (
            ref.size_bytes,
            ref.sha256,
        ):
            raise WorkspaceV2LifecycleError("owned payload proof digest differs")
    delta = request.source_root.parent.parent
    if (
        refs["delta_manifest"].path != delta / "delta-manifest.json"
        or refs["files_inventory"].path != delta / "files.jsonl"
    ):
        raise WorkspaceV2LifecycleError("owned payload proofs belong to another delta")
    manifest = PhysicalDeltaManifest.model_validate_json(refs["delta_manifest"].path.read_bytes())
    candidate = (
        Path.home()
        / ".local/share/investment-analyst/workspaces/data-chassis-43-rehearsal/restore-v2"
    )
    if manifest.source_root != candidate:
        raise WorkspaceV2LifecycleError("owned payload belongs to another workspace")
    if (
        manifest.epoch != request.consuming_generation
        or manifest.files_sha256 != refs["files_inventory"].sha256
    ):
        raise WorkspaceV2LifecycleError("owned payload generation or inventory differs")
    receipts = []
    for name, kind in (("remote_upload", "upload"), ("restore_receipt", "restore")):
        receipt = PhysicalDeltaReceipt.model_validate_json(refs[name].path.read_bytes())
        if (
            receipt.kind,
            receipt.epoch,
            receipt.manifest_sha256,
            receipt.files_sha256,
            receipt.confirmed_files,
            receipt.historical_state,
        ) != (
            kind,
            manifest.epoch,
            refs["delta_manifest"].sha256,
            manifest.files_sha256,
            manifest.file_count,
            manifest.historical_state,
        ):
            raise WorkspaceV2LifecycleError("owned payload terminal recovery differs")
        receipts.append(receipt)
    readback = CompatibleRecoveryReadback.model_validate_json(
        refs["compatible_readback"].path.read_bytes()
    )
    if readback.reader_sha != "dbbdfd021b0fee20be31b2d2a845874b0e861067":
        raise WorkspaceV2LifecycleError("owned payload lacks the compatible release readback")
    if (
        readback.operation_id,
        readback.workspace_id,
        readback.restored_root,
        readback.manifest_sha256,
        readback.recovery_receipt_sha256,
    ) != (
        manifest.epoch,
        manifest.source_workspace_id,
        receipts[1].restored_root,
        refs["delta_manifest"].sha256,
        hashlib.sha256(receipts[1].model_dump_json().encode()).hexdigest(),
    ):
        raise WorkspaceV2LifecycleError("owned payload readback belongs to another recovery")
    expected: dict[str, tuple[int, str]] = {}
    count = 0
    for item in _iter_delta_files(refs["files_inventory"].path):
        count += 1
        for block in item.blocks:
            if block.origin == "delta":
                name = f"{block.sha256}.bin"
                value = (block.length_bytes, block.sha256)
                if name in expected and expected[name] != value:
                    raise WorkspaceV2LifecycleError("owned payload object identity conflicts")
                expected[name] = value
    if count != manifest.file_count:
        raise WorkspaceV2LifecycleError("owned payload file coverage differs")
    return expected


def _owned_payload_member(
    request: OwnedPayloadRetirementInput, relative: str, expected: dict[str, tuple[int, str]]
) -> None:
    if request.role == OwnedPayloadRole.DELTA_STAGING_OBJECTS:
        if relative not in expected:
            raise WorkspaceV2LifecycleError("owned payload contains unique or unknown objects")
        return
    if relative in {".gitignore", "CACHEDIR.TAG"}:
        return
    if request.source_root.name == ".pytest_cache" and relative in {
        "README.md",
        "v/cache/nodeids",
        "v/cache/lastfailed",
        "v/cache/stepwise",
    }:
        return
    if request.source_root.name == ".ruff_cache" and re.fullmatch(
        r"[0-9]+\.[0-9]+\.[0-9]+/[0-9]+", relative
    ):
        return
    raise WorkspaceV2LifecycleError("regenerable cache contains unknown content")


def _require_no_directory_users(root: Path) -> None:
    """Reject another same-user process with this root open or in its command line."""
    for process in Path("/proc").iterdir():
        if not process.name.isdecimal() or int(process.name) == os.getpid():
            continue
        try:
            if process.stat().st_uid != os.getuid():
                continue
            command = (process / "cmdline").read_bytes().split(b"\0")
            if any(
                arg == os.fsencode(root) or arg.startswith(os.fsencode(root) + b"/")
                for arg in command
            ):
                raise WorkspaceV2LifecycleError("backup directory remains in use")
            for descriptor in (process / "fd").iterdir():
                try:
                    target = Path(os.readlink(descriptor))
                except (FileNotFoundError, PermissionError):
                    continue
                if target.is_absolute() and target.is_relative_to(root):
                    raise WorkspaceV2LifecycleError("backup directory remains in use")
        except (FileNotFoundError, ProcessLookupError, PermissionError):
            continue


def _write_new_bytes(path: Path, payload: bytes) -> None:
    if path.is_symlink():
        raise WorkspaceV2LifecycleError("preserved backup control path is unsafe")
    with path.open("xb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    _fsync_directory(path.parent)


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


def _new_receipt_path(path: Path, *, label: str) -> Path:
    target = path.expanduser().resolve(strict=False)
    if not target.is_absolute() or target.is_symlink() or target.exists():
        raise WorkspaceV2LifecycleError(f"{label} path is unavailable or unsafe")
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.parent.is_symlink() or not target.parent.is_dir():
        raise WorkspaceV2LifecycleError(f"{label} parent is unavailable or unsafe")
    return target


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
def _closed_duckdb_file(path: Path):
    """Hold DuckDB's POSIX file-lock boundary without opening/replaying the DB.

    An exclusive lock excludes both native readers and writers throughout spill
    retirement. Unlike inspecting /proc, this also covers protected processes.
    """
    import fcntl

    descriptor = os.open(path, os.O_RDWR | getattr(os, "O_NOFOLLOW", 0))
    try:
        try:
            fcntl.lockf(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            raise WorkspaceV2LifecycleError("database remains in use") from error
        yield
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
        try:
            for index, path in enumerate(paths):
                if path.is_symlink() or (index == 0 and not path.is_file()):
                    raise WorkspaceV2LifecycleError("recovery writer lock is missing or unsafe")
                if not path.exists():
                    continue
                descriptor = os.open(path, os.O_RDWR | getattr(os, "O_NOFOLLOW", 0))
                descriptors.append(descriptor)
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise WorkspaceV2LifecycleError("recovery writer remains active") from error
        except OSError as error:
            raise WorkspaceV2LifecycleError("recovery writer lock cannot be acquired") from error
        yield
    finally:
        for descriptor in reversed(descriptors):
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)


__all__ = ["WorkspaceV2LifecycleError", "WorkspaceV2LifecycleService"]
