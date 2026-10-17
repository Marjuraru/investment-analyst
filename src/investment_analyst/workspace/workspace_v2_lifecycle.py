"""Service-owned preservation and retirement of isolated workspace snapshots."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from collections.abc import Callable, Iterable
from contextlib import contextmanager
from datetime import UTC, datetime
from itertools import zip_longest
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
    ProvenCopyRetirementReceipt,
    SealedDirectoryRetirementCheckpoint,
    SealedDirectoryRetirementReceipt,
    WorkspaceV2RetirementProof,
)
from investment_analyst.workspace.workspace_v2_recovery import WorkspaceV2RecoveryReceipt

if TYPE_CHECKING:
    from investment_analyst.workspace.workspace_v2_physical_delta import PhysicalDeltaReceipt

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


def _safe_existing_root(path: Path, *, label: str) -> Path:
    candidate = path.expanduser()
    if candidate.is_symlink() or not candidate.is_dir():
        raise WorkspaceV2LifecycleError(f"{label} root is missing or unsafe")
    resolved = candidate.resolve()
    if resolved != candidate.resolve(strict=True):
        raise WorkspaceV2LifecycleError(f"{label} root is unsafe")
    return resolved


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
