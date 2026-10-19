"""Resumable transfer of a verified historical archive into compact workspace v2."""

from __future__ import annotations

import fcntl
import hashlib
import inspect
import json
import os
import sys
from collections import OrderedDict
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path
from typing import TYPE_CHECKING, Literal
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from duckdb import DuckDBPyConnection
from pydantic import ConfigDict, Field, field_validator

from investment_analyst.core.models import DiagnosticResult, MetricResult
from investment_analyst.core.models.base import ContractModel, NonEmptyStr, UTCDateTime
from investment_analyst.storage.compact_analytical_v2 import (
    _METRIC_SELECT_COLUMNS,
    CompactAnalyticalStore,
    _decimal,
    _instant,
    _json_bytes,
)
from investment_analyst.storage.errors import StorageError
from investment_analyst.storage.historical_analytical_archive import (
    HistoricalAnalyticalArchive,
    HistoricalAnalyticalCursor,
)
from investment_analyst.storage.historical_analytical_import import (
    DirectHistoricalAnalyticalSource,
)
from investment_analyst.storage.resource_policy import (
    MaintenanceResourceReceipt,
    require_maintenance_admission,
)
from investment_analyst.storage.serialization import canonical_json_bytes, sha256_hex
from investment_analyst.workspace.workspace_v2_lifecycle_models import FrozenSourceCut
from investment_analyst.workspace.workspace_v2_migration_models import (
    CutInvalidation,
    GenerationContentProof,
    HistoricalFrozenSource,
    HistoricalSealSnapshot,
    HistoricalSnapshotSource,
    HistoricalTransition,
    HistoricalTransitionReceipt,
    ProgressReconciliation,
    ProgressReconciliationReceipt,
    ProgressReconciliationV2,
    ProgressRelationshipProfile,
    ProgressRelationshipProof,
    ReconciliationCodeArchive,
    ReconciliationFileReference,
    ReconciliationHead,
    RecoveredProgressInventory,
    WorkspaceV1MigrationSourceSnapshot,
    WorkspaceV2MigrationVerificationProof,
    WorkspaceV2MigrationWorkCheckpoint,
    WorkspaceV2MigrationWorkPhase,
)

if TYPE_CHECKING:
    from investment_analyst.workspace.service import WorkspaceService


class _VerifiedParentMetricHasher:
    """Reconstruct exact canonical bytes only for an attested parent ID/hash.

    Shared JSON fragments are validated through the existing compact loaders.
    An exact hash match proves the persisted financial fields still equal the
    already validated parent model. New IDs always use the full typed reader.
    Noncanonical representations fall back to that same reader; conflicts fail.
    """

    _BUFFER = 1 << 20
    _FIELDS = frozenset(
        {
            "result_id",
            "asset_id",
            "metric_key",
            "value",
            "unit",
            "as_of",
            "available_at",
            "computed_at",
            "parameters",
            "input_observation_ids",
            "input_metric_result_ids",
            "algorithm_version",
            "quality",
        }
    )

    def __init__(self, target: CompactAnalyticalStore) -> None:
        if set(MetricResult.model_fields) != self._FIELDS:
            raise WorkspaceV2InventoryError("metric canonical contract changed")
        self.target = target
        self.cache: OrderedDict[tuple[str, str], bytes] = OrderedDict()
        self.cache_bytes = 0
        self.fast_count = 0
        self.typed_fallback_count = 0

    def _remember(self, kind: str, key: str, value: bytes) -> None:
        if len(value) > self._BUFFER:
            raise WorkspaceV2InventoryError("canonical parent fragment exceeds buffer budget")
        cache_key = kind, key
        previous = self.cache.pop(cache_key, None)
        if previous is not None:
            self.cache_bytes -= len(previous)
        while self.cache and self.cache_bytes + len(value) > self._BUFFER:
            _, removed = self.cache.popitem(last=False)
            self.cache_bytes -= len(removed)
        self.cache[cache_key] = value
        self.cache_bytes += len(value)

    def prepare(self, rows: Sequence[tuple[object, ...]]) -> None:
        if len(rows) > 256 or any(len(row) != 17 or row[16] is None for row in rows):
            raise WorkspaceV2InventoryError("parent canonical page is not an attested bounded page")
        parameters = list(
            dict.fromkeys(
                str(row[9]) for row in rows if ("parameters", str(row[9])) not in self.cache
            )
        )
        for batch in self._fragment_batches("parameters", parameters):
            for key, value in self.target._load_content_batch(
                batch, expected_kind="parameters"
            ).items():
                self._remember("parameters", key, value)
        sequences = list(
            dict.fromkeys(
                str(row[column])
                for row in rows
                for column in (10, 11)
                if (
                    ("metric-input-observation-v1" if column == 10 else "metric-input-result-v1"),
                    str(row[column]),
                )
                not in self.cache
            )
        )
        for batch in self._fragment_batches("sequence", sequences):
            for key, (kind, identifiers) in self.target._load_sequences(batch).items():
                if kind not in {"metric-input-observation-v1", "metric-input-result-v1"}:
                    raise WorkspaceV2InventoryError("parent metric sequence type differs")
                if len(identifiers) * 39 + 2 > self._BUFFER:
                    raise WorkspaceV2InventoryError(
                        "parent canonical sequence exceeds buffer budget"
                    )
                self._remember(
                    kind, key, _json_bytes([str(identifier) for identifier in identifiers])
                )

    def _fragment_batches(self, kind: str, keys: Sequence[str]) -> Iterator[tuple[str, ...]]:
        from investment_analyst.storage.resource_policy import execute_with_resource_budget

        for start in range(0, len(keys), 256):
            page = keys[start : start + 256]
            columns, table, key_column = (
                (
                    "greatest(byte_length,octet_length(value_bytes))",
                    "workspace_analytical_content_v2",
                    "content_id",
                )
                if kind == "parameters"
                else ("member_count*39+2", "workspace_analytical_sequences_v2", "sequence_id")
            )
            sizes = dict(
                execute_with_resource_budget(
                    self.target._connection,
                    f"SELECT {key_column},{columns} FROM {table} "
                    f"WHERE {key_column} IN ({','.join('?' for _ in page)})",
                    page,
                ).fetchall()
            )
            batch: list[str] = []
            batch_bytes = 0
            for key in page:
                if key not in sizes or not 0 <= int(sizes[key]) <= self._BUFFER:
                    raise WorkspaceV2InventoryError(
                        "canonical parent fragment exceeds buffer budget"
                    )
                size = int(sizes[key])
                if batch and batch_bytes + size > self._BUFFER:
                    yield tuple(batch)
                    batch, batch_bytes = [], 0
                batch.append(key)
                batch_bytes += size
            if batch:
                yield tuple(batch)

    def _fragment(self, kind: str, key: str) -> bytes:
        cache_key = kind, key
        if cache_key not in self.cache:
            # Load only the requested fragment. Preparing all three again can
            # evict this fragment when their combined size exceeds the cache.
            tuple(self._fragment_batches(kind, (key,)))
            if kind == "parameters":
                value = self.target._load_content_batch((key,), expected_kind=kind)[key]
            else:
                actual_kind, identifiers = self.target._load_sequences((key,))[key]
                if actual_kind != kind or len(identifiers) * 39 + 2 > self._BUFFER:
                    raise WorkspaceV2InventoryError(
                        "parent canonical sequence exceeds budget or type"
                    )
                value = _json_bytes([str(identifier) for identifier in identifiers])
            self._remember(kind, key, value)
        self.cache.move_to_end(cache_key)
        return self.cache[cache_key]

    def verify(self, row: tuple[object, ...]) -> str:
        if len(row) != 17 or row[16] is None:
            raise WorkspaceV2InventoryError("canonical fast path requires an attested parent ID")
        identifier, expected = UUID(str(row[0])), str(row[16])
        if (
            str(row[1]) != "HISTORICAL"
            or str(row[15]) != expected
            or (identifier.version or 0) != int(row[14])
        ):
            raise WorkspaceV2InventoryError("parent identity, origin or stored checksum differs")
        prefix = _json_bytes(
            {
                "algorithm_version": str(row[12]),
                "as_of": _instant(row[6]).isoformat().replace("+00:00", "Z"),
                "asset_id": str(row[2]),
                "available_at": _instant(row[7]).isoformat().replace("+00:00", "Z"),
                "computed_at": _instant(row[8]).isoformat().replace("+00:00", "Z"),
            }
        )
        suffix = _json_bytes(
            {
                "quality": str(row[13]),
                "result_id": str(identifier),
                "unit": str(row[5]),
                "value": str(_decimal(row[4])),
            }
        )
        checksum = hashlib.sha256(prefix[:-1])
        checksum.update(b',"input_metric_result_ids":')
        checksum.update(self._fragment("metric-input-result-v1", str(row[11])))
        checksum.update(b',"input_observation_ids":')
        checksum.update(self._fragment("metric-input-observation-v1", str(row[10])))
        checksum.update(b',"metric_key":')
        checksum.update(_json_bytes(str(row[3])))
        checksum.update(b',"parameters":')
        checksum.update(self._fragment("parameters", str(row[9])))
        checksum.update(b"," + suffix[1:])
        if checksum.hexdigest() == expected:
            self.fast_count += 1
            return expected
        # Representation differences may be semantically equivalent under the
        # existing model. Only its complete typed reconstruction may resolve them.
        actual = self.target.get_metrics((identifier,))[identifier]
        if sha256_hex(canonical_json_bytes(actual)) != expected:
            raise WorkspaceV2InventoryError("parent canonical financial content differs")
        self.typed_fallback_count += 1
        return expected


def reconciliation_file_reference(
    path: Path, role: Literal["control", "database", "wal", "inventory", "code", "resource"]
) -> ReconciliationFileReference:
    """Hash a closed physical artifact with bounded buffers and stable metadata."""
    if not path.is_absolute() or any(item.is_symlink() for item in (path, *path.parents)):
        raise WorkspaceV2InventoryError("reconciliation artifact path is unsafe")
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        before = os.fstat(handle.fileno())
        offset = 0
        while block := handle.read(1 << 20):
            digest.update(block)
            if hasattr(os, "posix_fadvise"):
                os.posix_fadvise(handle.fileno(), offset, len(block), os.POSIX_FADV_DONTNEED)
            offset += len(block)
        after = os.fstat(handle.fileno())
    if (before.st_ino, before.st_size, before.st_mtime_ns) != (
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    ) or path.stat().st_ino != before.st_ino:
        raise WorkspaceV2InventoryError("reconciliation artifact changed while hashing")
    return ReconciliationFileReference(
        path=path, role=role, sha256=digest.hexdigest(), size_bytes=before.st_size
    )


class _ProgressFinancialWork(ContractModel):
    """Bounded resumable financial scan of one recovered physical image."""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")
    operation_id: UUID
    database_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    parent_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    relationships_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    tree_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    environment_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    family: Literal["metrics", "diagnostics", "complete"] = "metrics"
    available_at: UTCDateTime | None = None
    identifier: UUID | None = None
    reference_order: tuple[int, int, int] | None = None
    metric_count: int = Field(default=0, ge=0)
    diagnostic_count: int = Field(default=0, ge=0)
    parent_metric_count: int = Field(default=0, ge=0)
    parent_diagnostic_count: int = Field(default=0, ge=0)
    metric_digest: str = Field(default_factory=lambda: hashlib.sha256(b"").hexdigest())
    diagnostic_digest: str = Field(default_factory=lambda: hashlib.sha256(b"").hexdigest())
    delta_bytes: int = Field(default=0, ge=0)
    delta_sha256: str = Field(default_factory=lambda: hashlib.sha256(b"").hexdigest())


class _OrderedFinancialDigest(ContractModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")
    verification_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_count: int = Field(ge=0)
    confirmed_count: int = Field(default=0, ge=0)
    available_at: UTCDateTime | None = None
    identifier: UUID | None = None
    digest: str = Field(default_factory=lambda: hashlib.sha256(b"").hexdigest())


class WorkspaceV2ProgressReconciler:
    """Recover an inactive destination without resuming or rewriting its old cut."""

    def __init__(self, service: WorkspaceService, root: Path, operation_id: UUID) -> None:
        self.service = service
        self.root = root
        self.operation_id = operation_id
        self.state = root / "state"
        self.path = self.state / f"progress-reconciliation.{operation_id}.json"

    @contextmanager
    def _exclusive(self) -> Iterator[None]:
        with _reconciliation_lock(self.state):
            if (self.state / "reconciliation-head.json").exists():
                raise WorkspaceV2InventoryError("legacy reconciliation was replaced by a v2 head")
            for other in self.state.glob("progress-reconciliation.*.json"):
                if other != self.path:
                    raise WorkspaceV2InventoryError("another reconciliation owns the destination")
            if any(self.state.glob("cut-invalidation.*.json")):
                raise WorkspaceV2InventoryError("legacy reconciliation has an invalidated attempt")
            yield

    def _publish(self, progress: ProgressReconciliation) -> ReconciliationFileReference:
        document = canonical_json_bytes(progress) + b"\n"
        generation = self.state / (
            f"progress-reconciliation-generation.{self.operation_id}.{sha256_hex(document)}.json"
        )
        self._write_once(generation, document)
        temporary = self.path.with_name(f".{self.path.name}.{uuid4().hex}.tmp")
        try:
            self._write_once(temporary, document)
            temporary.replace(self.path)
            _fsync_parent(self.path)
        finally:
            temporary.unlink(missing_ok=True)
        return reconciliation_file_reference(generation, "control")

    @staticmethod
    def _write_once(path: Path, document: bytes) -> None:
        if path.exists():
            if path.is_symlink() or path.read_bytes() != document:
                raise WorkspaceV2InventoryError("immutable reconciliation artifact differs")
            return
        temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        try:
            with temporary.open("xb") as handle:
                handle.write(document)
                handle.flush()
                os.fsync(handle.fileno())
            os.link(temporary, path)
            _fsync_parent(path)
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _verify(reference: ReconciliationFileReference) -> None:
        if reconciliation_file_reference(reference.path, reference.role) != reference:
            raise WorkspaceV2InventoryError("reconciliation artifact binding differs")

    def _controls(self, progress: ProgressReconciliation) -> None:
        for reference in (
            progress.predecessor,
            *progress.predecessor_proofs,
            progress.parent_membership,
            progress.predecessor_code,
        ):
            self._verify(reference)
        predecessor = HistoricalTransition.model_validate_json(
            progress.predecessor.path.read_bytes()
        )
        archive = ReconciliationCodeArchive.model_validate_json(
            progress.predecessor_code.path.read_bytes()
        )
        expected_names = (
            "workspace_v2_inventory.py",
            "compact_analytical_v2.py",
            "historical_analytical_import.py",
            "workspace_v2_migration_models.py",
        )
        code_digest = hashlib.sha256()
        for entry, name in zip(archive.files, expected_names, strict=True):
            reference = reconciliation_file_reference(entry.archive, "code")
            if (
                reference.sha256 != entry.sha256
                or reference.size_bytes != entry.size_bytes
                or entry.source.name != name
                or entry.archive.name != name
            ):
                raise WorkspaceV2InventoryError("predecessor code archive differs")
            code_digest.update(bytes.fromhex(entry.sha256))
        if (
            code_digest.hexdigest() != progress.predecessor_tree_fingerprint
            or archive.tree_fingerprint != progress.predecessor_tree_fingerprint
            or archive.environment_fingerprint != progress.predecessor_environment_fingerprint
            or archive.predecessor_operation_id != progress.predecessor_operation_id
            or archive.reconciliation_operation_id != progress.operation_id
        ):
            raise WorkspaceV2InventoryError("predecessor code provenance bindings differ")
        if (
            predecessor.phase != "APPLYING"
            or predecessor.operation_id != progress.predecessor_operation_id
            or predecessor.parent_seal != progress.parent_seal
            or predecessor.parent_membership_path != progress.parent_membership.path
            or predecessor.parent_membership_sha256 != progress.parent_membership.sha256
            or predecessor.tree_fingerprint != progress.predecessor_tree_fingerprint
            or predecessor.environment_fingerprint != progress.predecessor_environment_fingerprint
            or predecessor.source_root != progress.source_root
            or predecessor.source_workspace_id != progress.source_workspace_id
            or predecessor.source_created_at != progress.source_created_at
            or predecessor.destination_root != progress.destination_root
            or predecessor.destination_workspace_id != progress.destination_workspace_id
        ):
            raise WorkspaceV2InventoryError("preserved predecessor bindings differ")
        source, target = (
            self.service.manifest(progress.source_root),
            self.service.manifest(self.root),
        )
        if (
            source.workspace_id != progress.source_workspace_id
            or source.created_at != progress.source_created_at
            or source.format_version != 1
            or target.workspace_id != progress.destination_workspace_id
            or target.format_version != 2
            or self.root != progress.destination_root
            or self.operation_id != progress.operation_id
        ):
            raise WorkspaceV2InventoryError("reconciliation workspace identity differs")

    def recover(
        self,
        *,
        predecessor_path: Path,
        predecessor_proofs: tuple[Path, ...],
        predecessor_code: Path,
        admission_path: Path,
    ) -> ProgressReconciliationReceipt:
        """Native replay only. Semantic W validation belongs to the new verifier."""
        from investment_analyst.workspace.models import WorkspaceAccessMode
        from investment_analyst.workspace.workspace_v2_lifecycle import (
            _closed_duckdb_file,
            _v2_writer_closed,
        )

        admission = MaintenanceResourceReceipt.model_validate_json(admission_path.read_bytes())
        execution = require_maintenance_admission(admission)
        database = self.root / "storage/v2/index.duckdb"
        wal = database.with_suffix(".duckdb.wal")
        if (
            admission.budget.operation_id != self.operation_id
            or admission.budget.profile != "wal_recovery"
            or tuple(item.database for item in admission.budget.assignments) != (database,)
        ):
            raise WorkspaceV2InventoryError("recovery requires its single-destination WAL budget")
        with self._exclusive():
            if self.path.exists():
                progress = ProgressReconciliation.model_validate_json(self.path.read_bytes())
                self._controls(progress)
                if (
                    progress.predecessor.path != predecessor_path
                    or tuple(item.path for item in progress.predecessor_proofs)
                    != predecessor_proofs
                    or progress.predecessor_code.path != predecessor_code
                ):
                    raise WorkspaceV2InventoryError(
                        "recovery request differs from preserved operation"
                    )
                if progress.recovered_at is not None:
                    if progress.phase != "PREPARED":
                        raise WorkspaceV2InventoryError("verified progress cannot replay again")
                    assert progress.database_after is not None
                    self._verify(progress.database_after)
                    if progress.wal_after is not None:
                        self._verify(progress.wal_after)
                    elif wal.exists():
                        raise WorkspaceV2InventoryError("unexpected WAL after recorded recovery")
                    certificate = self._publish(progress)
                    return ProgressReconciliationReceipt(
                        reconciliation=progress,
                        certificate=certificate,
                        stage="recovered",
                        recorded_at=datetime.now(UTC),
                    )
            else:
                predecessor = HistoricalTransition.model_validate_json(
                    predecessor_path.read_bytes()
                )
                now = datetime.now(UTC)
                with _v2_writer_closed(self.root), _closed_duckdb_file(database):
                    progress = ProgressReconciliation(
                        operation_id=self.operation_id,
                        predecessor_operation_id=predecessor.operation_id,
                        predecessor=reconciliation_file_reference(predecessor_path, "control"),
                        predecessor_proofs=tuple(
                            reconciliation_file_reference(path, "control")
                            for path in predecessor_proofs
                        ),
                        parent_membership=reconciliation_file_reference(
                            predecessor.parent_membership_path, "inventory"
                        ),
                        parent_seal=predecessor.parent_seal,
                        source_workspace_id=predecessor.source_workspace_id,
                        source_created_at=predecessor.source_created_at,
                        source_root=predecessor.source_root,
                        destination_root=self.root,
                        destination_workspace_id=predecessor.destination_workspace_id,
                        database_before=reconciliation_file_reference(database, "database"),
                        wal_before=reconciliation_file_reference(wal, "wal")
                        if wal.exists()
                        else None,
                        predecessor_code=reconciliation_file_reference(predecessor_code, "code"),
                        predecessor_tree_fingerprint=predecessor.tree_fingerprint,
                        predecessor_environment_fingerprint=predecessor.environment_fingerprint,
                        resource_receipt=reconciliation_file_reference(admission_path, "resource"),
                        prepared_at=now,
                        updated_at=now,
                    )
                    self._controls(progress)
                    self._publish(progress)
            # Every attempt records its physical preimage before opening. A crash after
            # native commit but before the final receipt does not lose the original one.
            with _v2_writer_closed(self.root), _closed_duckdb_file(database):
                attempt = (
                    self.state / f"progress-recovery-attempt.{self.operation_id}.{uuid4()}.json"
                )
                document = {
                    "schema_version": "workspace-v2-progress-recovery-attempt-v1",
                    "operation_id": str(self.operation_id),
                    "database": reconciliation_file_reference(database, "database").model_dump(
                        mode="json"
                    ),
                    "wal": reconciliation_file_reference(wal, "wal").model_dump(mode="json")
                    if wal.exists()
                    else None,
                    "admission": reconciliation_file_reference(
                        admission_path, "resource"
                    ).model_dump(mode="json"),
                    "recorded_at": datetime.now(UTC).isoformat(),
                }
                self._write_once(attempt, json.dumps(document, sort_keys=True).encode() + b"\n")
                progress = ProgressReconciliation.model_validate(
                    {
                        **progress.model_dump(),
                        "recovery_attempts": (
                            *progress.recovery_attempts,
                            reconciliation_file_reference(attempt, "control"),
                        ),
                        "updated_at": datetime.now(UTC),
                    }
                )
                self._publish(progress)
            with (
                execution.step("open", admission.budget.open_timeout_seconds),
                self.service.open_storage(
                    self.service.resolve(self.root), WorkspaceAccessMode.READ_WRITE
                ),
            ):
                pass  # Opening and normal close perform native WAL recovery/checkpoint.
            self._controls(progress)
            with _v2_writer_closed(self.root), _closed_duckdb_file(database):
                progress = ProgressReconciliation.model_validate(
                    {
                        **progress.model_dump(),
                        "database_after": reconciliation_file_reference(database, "database"),
                        "wal_after": reconciliation_file_reference(wal, "wal")
                        if wal.exists()
                        else None,
                        "recovered_at": datetime.now(UTC),
                        "updated_at": datetime.now(UTC),
                    }
                )
                certificate = self._publish(progress)
            return ProgressReconciliationReceipt(
                reconciliation=progress,
                certificate=certificate,
                stage="recovered",
                recorded_at=datetime.now(UTC),
            )

    def verify_financial_baseline(
        self, *, admission_path: Path, relationships_path: Path
    ) -> ProgressReconciliationReceipt:
        """Validate B subset W and financial content; never seal W or complete F0."""
        from investment_analyst.storage.resource_policy import connect_duckdb, execute_bounded
        from investment_analyst.workspace.models import WorkspaceAccessMode
        from investment_analyst.workspace.workspace_v2_lifecycle import (
            _closed_duckdb_file,
            _v2_writer_closed,
        )

        admission = MaintenanceResourceReceipt.model_validate_json(admission_path.read_bytes())
        execution = require_maintenance_admission(admission)
        database = self.root / "storage/v2/index.duckdb"
        if (
            admission.budget.operation_id != self.operation_id
            or admission.budget.profile != "verification"
            or tuple(item.database for item in admission.budget.assignments) != (database,)
        ):
            raise WorkspaceV2InventoryError("financial verification budget differs")
        with self._exclusive():
            progress = ProgressReconciliation.model_validate_json(self.path.read_bytes())
            if progress.phase not in {"PREPARED", "BASELINE_VERIFIED"}:
                raise WorkspaceV2InventoryError("financial verification requires unbound recovery")
            if progress.database_after is None:
                raise WorkspaceV2InventoryError(
                    "financial verification requires recovered database"
                )
            self._controls(progress)
            relationship_reference = reconciliation_file_reference(relationships_path, "control")
            relationships = ProgressRelationshipProof.model_validate_json(
                relationships_path.read_bytes()
            )
            self._verify(relationships.reconciliation)
            original = ProgressReconciliation.model_validate_json(
                relationships.reconciliation.path.read_bytes()
            )
            if (
                original.phase != "PREPARED"
                or original.operation_id != progress.operation_id
                or original.database_after != progress.database_after
                or original.wal_after != progress.wal_after
                or original.parent_membership != progress.parent_membership
                or relationships.operation_id != progress.operation_id
                or relationships.destination_root != self.root
                or relationships.destination_workspace_id != progress.destination_workspace_id
                or relationships.database != progress.database_after
                or relationships.wal != progress.wal_after
                or relationships.parent_membership != progress.parent_membership
                or relationships.verifier_policy != progress.verifier_policy
                or relationships.verifier_module_sha256
                != sha256_hex(Path(__file__).with_name("compact_analytical_v2.py").read_bytes())
                or relationships.resource_policy_sha256
                != sha256_hex(Path(__file__).with_name("resource_policy.py").read_bytes())
            ):
                raise WorkspaceV2InventoryError("relationship proof does not bind recovered W")
            with _v2_writer_closed(self.root), _closed_duckdb_file(database):
                self._verify(progress.database_after)
                if progress.wal_after is not None:
                    self._verify(progress.wal_after)
                elif database.with_suffix(".duckdb.wal").exists():
                    raise WorkspaceV2InventoryError("unexpected WAL after recovery")
            if progress.phase == "BASELINE_VERIFIED":
                assert progress.inventory is not None
                self._verify(progress.inventory.delta_membership)
                if progress.inventory.relationships != relationship_reference:
                    raise WorkspaceV2InventoryError("baseline relationship proof changed")
                return ProgressReconciliationReceipt(
                    reconciliation=progress,
                    certificate=self._publish(progress),
                    stage="baseline_verified",
                    recorded_at=datetime.now(UTC),
                )
            tree, environment = historical_transition_fingerprints(
                progress.page_limit, progress.verifier_policy
            )
            work_path = self.state / f"progress-financial-work.{self.operation_id}.{tree}.json"
            delta_path = self.state / f"progress-delta-membership.{self.operation_id}.{tree}.jsonl"
            expected = _ProgressFinancialWork(
                operation_id=self.operation_id,
                database_sha256=progress.database_after.sha256,
                parent_sha256=progress.parent_membership.sha256,
                relationships_sha256=relationship_reference.sha256,
                tree_fingerprint=tree,
                environment_fingerprint=environment,
            )
            work = expected
            if work_path.exists():
                if work_path.is_symlink():
                    raise WorkspaceV2InventoryError("financial checkpoint is unsafe")
                work = _ProgressFinancialWork.model_validate_json(work_path.read_bytes())
                for field in (
                    "operation_id",
                    "database_sha256",
                    "parent_sha256",
                    "relationships_sha256",
                    "tree_fingerprint",
                    "environment_fingerprint",
                ):
                    if getattr(work, field) != getattr(expected, field):
                        raise WorkspaceV2InventoryError("financial checkpoint bindings changed")
            if delta_path.is_symlink():
                raise WorkspaceV2InventoryError("financial delta path is unsafe")
            delta_hash = hashlib.sha256()
            with delta_path.open("a+b") as delta:
                delta.seek(0)
                remaining = work.delta_bytes
                while remaining:
                    block = delta.read(min(1 << 20, remaining))
                    if not block:
                        raise WorkspaceV2InventoryError("financial delta checkpoint is truncated")
                    delta_hash.update(block)
                    remaining -= len(block)
                if delta_hash.hexdigest() != work.delta_sha256:
                    raise WorkspaceV2InventoryError("financial delta checkpoint checksum differs")
                with self.service.open_storage(
                    self.service.resolve(self.root), WorkspaceAccessMode.READ_ONLY
                ) as storage:
                    compact = CompactAnalyticalStore(storage.store.connection)
                    if compact.historical_seal().model_dump() != progress.parent_seal.model_dump():
                        raise WorkspaceV2InventoryError("recovered historical parent seal changed")
                    # Point lookups can load large persistent ART indexes whose
                    # allocations cannot be spilled like query intermediates.
                    # This read-only scan uses bounded vectorized table scans;
                    # no persistent index/schema or resource cap is changed.
                    execute_bounded(storage.store.connection, "SET index_scan_max_count=0")
                    execute_bounded(storage.store.connection, "SET index_scan_percentage=0")
                    settings = execute_bounded(
                        storage.store.connection,
                        "SELECT current_setting('index_scan_max_count'),"
                        "current_setting('index_scan_percentage')",
                    ).fetchone()
                    if settings != (0, 0.0):
                        raise WorkspaceV2InventoryError("financial scan optimizer settings differ")
                    execution.record_measurement("financial_index_scans_disabled", 1)
                    # The auxiliary cursor shares the admitted native instance;
                    # typed lookups on the root never replace its streamed result.
                    with connect_duckdb(database, read_only=True) as stream:
                        execute_bounded(
                            stream,
                            "CREATE TEMP TABLE _progress_parent AS SELECT "
                            "json_extract_string(json,'$[0]') AS member_family, "
                            "try_cast(json_extract_string(json,'$[1]') AS UUID) identifier, "
                            "json_extract_string(json,'$[2]') digest "
                            "FROM read_json_objects(?,format='newline_delimited',"
                            "maximum_object_size=1024)",
                            [str(progress.parent_membership.path)],
                        )
                        invalid = execute_bounded(
                            stream,
                            "SELECT count(*) FROM _progress_parent WHERE "
                            "member_family IS NULL OR "
                            "member_family NOT IN ('metrics','diagnostics') "
                            "OR identifier IS NULL "
                            "OR digest IS NULL OR NOT regexp_full_match(digest,'[0-9a-f]{64}')",
                        ).fetchone()
                        duplicate = execute_bounded(
                            stream,
                            "SELECT 1 FROM _progress_parent GROUP BY member_family,identifier "
                            "HAVING count(*)<>1 LIMIT 1",
                        ).fetchone()
                        counts = execute_bounded(
                            stream,
                            "SELECT count(*) FILTER(WHERE member_family='metrics'),"
                            "count(*) FILTER(WHERE member_family='diagnostics') "
                            "FROM _progress_parent",
                        ).fetchone()
                        if (
                            invalid != (0,)
                            or duplicate is not None
                            or counts
                            != (
                                progress.parent_seal.metric_count,
                                progress.parent_seal.diagnostic_count,
                            )
                        ):
                            raise WorkspaceV2InventoryError("original B membership is invalid")
                        hasher = _VerifiedParentMetricHasher(compact)
                        for family in ("metrics", "diagnostics"):
                            if work.family == "complete" or (
                                family == "metrics" and work.family == "diagnostics"
                            ):
                                continue
                            metric = family == "metrics"
                            table = (
                                "workspace_metric_results_v2"
                                if metric
                                else "workspace_diagnostic_results_v2"
                            )
                            key = "result_id" if metric else "diagnostic_id"
                            bindings: list[object] = []
                            after = ""
                            if metric and work.identifier is not None:
                                if work.reference_order is None:
                                    raise WorkspaceV2InventoryError(
                                        "reference cursor is incomplete"
                                    )
                                after = (
                                    " AND (hash(m.observation_sequence_id),"
                                    "hash(m.metric_sequence_id),hash(m.parameters_content_id),"
                                    "m.result_id)>(CAST(? AS UBIGINT),CAST(? AS UBIGINT),"
                                    "CAST(? AS UBIGINT),CAST(? AS VARCHAR))"
                                )
                                bindings = [*work.reference_order, str(work.identifier)]
                            elif work.available_at is not None:
                                if work.identifier is None:
                                    raise WorkspaceV2InventoryError(
                                        "financial cursor is incomplete"
                                    )
                                after = (
                                    f" AND (m.available_at,m.{key})>"
                                    "(CAST(? AS TIMESTAMPTZ),CAST(? AS VARCHAR))"
                                )
                                bindings = [work.available_at.isoformat(), str(work.identifier)]
                            first_partition = (
                                work.reference_order[0] >> 60
                                if metric and work.reference_order is not None
                                else 0
                            )
                            for partition in range(first_partition, 16 if metric else 1):
                                partition_filter = (
                                    " AND hash(m.observation_sequence_id) BETWEEN "
                                    "CAST(? AS UBIGINT) AND CAST(? AS UBIGINT)"
                                    if metric
                                    else ""
                                )
                                query_bindings = [*bindings]
                                if metric:
                                    query_bindings.extend(
                                        (partition << 60, ((partition + 1) << 60) - 1)
                                    )
                                group_columns = (
                                    ",hash(m.observation_sequence_id),hash(m.metric_sequence_id),"
                                    "hash(m.parameters_content_id)"
                                    if metric
                                    else ""
                                )
                                ordering = (
                                    "hash(m.observation_sequence_id),hash(m.metric_sequence_id),"
                                    "hash(m.parameters_content_id),m.result_id"
                                    if metric
                                    else "m.available_at,m.diagnostic_id"
                                )
                                execute_bounded(
                                    stream,
                                    f"SELECT m.{key},CAST(m.available_at AS VARCHAR),p.digest"
                                    f"{group_columns} "
                                    f"FROM {table} m "
                                    "LEFT JOIN _progress_parent p ON "
                                    f"p.member_family='{family}' "
                                    f"AND p.identifier=m.{key} "
                                    f"WHERE m.origin='HISTORICAL'{after}{partition_filter} "
                                    f"ORDER BY {ordering}",
                                    query_bindings,
                                )
                                while metadata := stream.fetchmany(progress.page_limit):
                                    with execution.step(
                                        "sql", admission.budget.sql_timeout_seconds
                                    ):
                                        from investment_analyst.storage.resource_policy import (
                                            execute_with_resource_budget,
                                        )

                                        columns = (
                                            _METRIC_SELECT_COLUMNS
                                            if metric
                                            else (
                                                "diagnostic_id,origin,checksum_sha256,"
                                                "CAST(available_at AS VARCHAR)"
                                            )
                                        )
                                        page = execute_with_resource_budget(
                                            storage.store.connection,
                                            f"SELECT {columns} FROM {table} "
                                            f"WHERE {key} IN ({','.join('?' for _ in metadata)})",
                                            [str(row[0]) for row in metadata],
                                        ).fetchall()
                                        indexed = {str(row[0]): row for row in page}
                                        if len(indexed) != len(metadata):
                                            raise WorkspaceV2InventoryError(
                                                "financial page is incomplete"
                                            )
                                        rows = [(*indexed[str(row[0])], row[2]) for row in metadata]
                                        if any(
                                            _instant(row[7] if metric else row[3])
                                            != _instant(item[1])
                                            for row, item in zip(rows, metadata, strict=True)
                                        ):
                                            raise WorkspaceV2InventoryError(
                                                "financial page time differs"
                                            )
                                        known = [
                                            row for row in rows if metric and row[16] is not None
                                        ]
                                        hasher.prepare(known)
                                        ids = [
                                            UUID(str(row[0]))
                                            for row in rows
                                            if not metric or row[16] is None
                                        ]
                                        typed = (
                                            compact.get_metrics(ids)
                                            if metric
                                            else compact.get_diagnostics(ids)
                                        )
                                        updates = work.model_dump()
                                        additions = bytearray()
                                        for row in rows:
                                            identifier = UUID(str(row[0]))
                                            parent = row[16] if metric else row[4]
                                            stored = str(row[15] if metric else row[2])
                                            actual = (
                                                hasher.verify(row)
                                                if metric and parent is not None
                                                else sha256_hex(
                                                    canonical_json_bytes(typed[identifier])
                                                )
                                            )
                                            if actual != stored or (
                                                parent is not None and actual != parent
                                            ):
                                                raise WorkspaceV2InventoryError(
                                                    "financial content differs"
                                                )
                                            count_field = (
                                                "metric_count" if metric else "diagnostic_count"
                                            )
                                            digest_field = (
                                                "metric_digest" if metric else "diagnostic_digest"
                                            )
                                            updates[count_field] += 1
                                            updates[digest_field] = hashlib.sha256(
                                                f"{updates[digest_field]}:{actual}".encode()
                                            ).hexdigest()
                                            if parent is not None:
                                                parent_field = (
                                                    "parent_metric_count"
                                                    if metric
                                                    else "parent_diagnostic_count"
                                                )
                                                updates[parent_field] += 1
                                            else:
                                                additions.extend(
                                                    _json_bytes([family, str(identifier), actual])
                                                )
                                                additions.extend(b"\n")
                                        if len(additions) > 1 << 20:
                                            raise WorkspaceV2InventoryError(
                                                "financial delta page exceeds budget"
                                            )
                                        # A crash after append but before checkpoint may leave
                                        # exactly this page's tail. Verify and reuse it.
                                        # Never truncate persisted evidence.
                                        delta.seek(work.delta_bytes)
                                        tail = delta.read(len(additions))
                                        if not additions.startswith(tail):
                                            raise WorkspaceV2InventoryError(
                                                "uncheckpointed financial delta differs"
                                            )
                                        delta.seek(0, os.SEEK_END)
                                        delta.write(additions[len(tail) :])
                                        delta.flush()
                                        os.fsync(delta.fileno())
                                        delta_hash.update(additions)
                                        updates.update(
                                            available_at=_instant(
                                                rows[-1][7] if metric else rows[-1][3]
                                            ),
                                            identifier=UUID(str(rows[-1][0])),
                                            reference_order=tuple(
                                                int(value) for value in metadata[-1][3:]
                                            )
                                            if metric
                                            else None,
                                            delta_bytes=work.delta_bytes + len(additions),
                                            delta_sha256=delta_hash.hexdigest(),
                                        )
                                        work = _ProgressFinancialWork.model_validate(updates)
                                        self._financial_checkpoint(work_path, work)
                            metric_digest = work.metric_digest
                            if metric:
                                metric_digest = self._ordered_metric_digest(stream, work)
                            work = _ProgressFinancialWork.model_validate(
                                {
                                    **work.model_dump(),
                                    "available_at": None,
                                    "identifier": None,
                                    "reference_order": None,
                                    "metric_digest": metric_digest,
                                    "family": "diagnostics" if metric else "complete",
                                }
                            )
                            self._financial_checkpoint(work_path, work)
                        execute_bounded(stream, "DROP TABLE _progress_parent")
                    if delta.seek(0, os.SEEK_END) != work.delta_bytes:
                        raise WorkspaceV2InventoryError(
                            "financial delta has unverified extra bytes"
                        )
            if (
                work.parent_metric_count != progress.parent_seal.metric_count
                or work.parent_diagnostic_count != progress.parent_seal.diagnostic_count
                or work.metric_count != relationships.metric_count
                or work.diagnostic_count != relationships.diagnostic_count
            ):
                raise WorkspaceV2InventoryError(
                    "B subset W or recovered inventory coverage differs"
                )
            inventory = RecoveredProgressInventory(
                raw_count=relationships.raw_count,
                observation_count=relationships.observation_count,
                metric_count=work.metric_count,
                diagnostic_count=work.diagnostic_count,
                metric_digest=work.metric_digest,
                diagnostic_digest=work.diagnostic_digest,
                dataset_sha256=sha256_hex(canonical_json_bytes(work)),
                delta_membership=reconciliation_file_reference(delta_path, "inventory"),
                relationships=relationship_reference,
                verified_at=datetime.now(UTC),
            )
            self._write_once(
                self.state
                / f"progress-financial-proof.{self.operation_id}.{inventory.dataset_sha256}.json",
                canonical_json_bytes(work) + b"\n",
            )
            progress = ProgressReconciliation.model_validate(
                {
                    **progress.model_dump(),
                    "phase": "BASELINE_VERIFIED",
                    "inventory": inventory,
                    "current_tree_fingerprint": tree,
                    "current_environment_fingerprint": environment,
                    "updated_at": datetime.now(UTC),
                }
            )
            return ProgressReconciliationReceipt(
                reconciliation=progress,
                certificate=self._publish(progress),
                stage="baseline_verified",
                recorded_at=datetime.now(UTC),
            )

    def _ordered_metric_digest(
        self, stream: DuckDBPyConnection, work: _ProgressFinancialWork
    ) -> str:
        """Fold only already validated hashes in canonical time/ID order.

        Reference grouping affects cache locality, never the historical digest.
        This narrow pass reads metadata only and has its own durable cursor.
        """
        from investment_analyst.storage.resource_policy import execute_bounded

        verification = sha256_hex(canonical_json_bytes(work))
        path = self.state / f"progress-ordered-digest.{self.operation_id}.{verification}.json"
        expected = _OrderedFinancialDigest(
            verification_sha256=verification, expected_count=work.metric_count
        )
        state = expected
        if path.exists():
            if path.is_symlink():
                raise WorkspaceV2InventoryError("ordered digest checkpoint is unsafe")
            state = _OrderedFinancialDigest.model_validate_json(path.read_bytes())
            if (
                state.verification_sha256 != verification
                or state.expected_count != work.metric_count
                or state.confirmed_count > state.expected_count
            ):
                raise WorkspaceV2InventoryError("ordered digest checkpoint binding differs")
        if state.confirmed_count == state.expected_count:
            return state.digest
        after, bindings = "", []
        if state.available_at is not None:
            if state.identifier is None:
                raise WorkspaceV2InventoryError("ordered digest cursor is incomplete")
            after = " AND (available_at,result_id)>(CAST(? AS TIMESTAMPTZ),CAST(? AS VARCHAR))"
            bindings = [state.available_at.isoformat(), str(state.identifier)]
        execute_bounded(
            stream,
            "SELECT CAST(available_at AS VARCHAR),result_id,checksum_sha256 "
            "FROM workspace_metric_results_v2 WHERE origin='HISTORICAL'"
            f"{after} ORDER BY available_at,result_id",
            bindings,
        )
        while rows := stream.fetchmany(256):
            digest = state.digest
            for row in rows:
                digest = hashlib.sha256(f"{digest}:{row[2]}".encode()).hexdigest()
            state = _OrderedFinancialDigest(
                verification_sha256=verification,
                expected_count=work.metric_count,
                confirmed_count=state.confirmed_count + len(rows),
                available_at=_instant(rows[-1][0]),
                identifier=UUID(str(rows[-1][1])),
                digest=digest,
            )
            self._financial_checkpoint(path, state)
        if state.confirmed_count != state.expected_count:
            raise WorkspaceV2InventoryError("ordered financial digest coverage differs")
        return state.digest

    @staticmethod
    def _financial_checkpoint(path: Path, work: ContractModel) -> None:
        temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        try:
            with temporary.open("xb") as handle:
                handle.write(canonical_json_bytes(work) + b"\n")
                handle.flush()
                os.fsync(handle.fileno())
            temporary.replace(path)
            _fsync_parent(path)
        finally:
            temporary.unlink(missing_ok=True)

    def verify_relationships(
        self, *, admission_path: Path, profile_only: bool = False
    ) -> ProgressRelationshipProof | ProgressRelationshipProfile:
        """Run only the new bounded graph verifier, without publishing baseline W."""
        from investment_analyst.storage.resource_policy import execute_bounded
        from investment_analyst.workspace.models import WorkspaceAccessMode
        from investment_analyst.workspace.workspace_v2_lifecycle import (
            _closed_duckdb_file,
            _v2_writer_closed,
        )

        admission = MaintenanceResourceReceipt.model_validate_json(admission_path.read_bytes())
        execution = require_maintenance_admission(admission)
        database = self.root / "storage/v2/index.duckdb"
        if (
            admission.budget.operation_id != self.operation_id
            or admission.budget.profile != "verification"
            or tuple(item.database for item in admission.budget.assignments) != (database,)
        ):
            raise WorkspaceV2InventoryError("relationship verification budget differs")
        with self._exclusive():
            progress = ProgressReconciliation.model_validate_json(self.path.read_bytes())
            if progress.phase != "PREPARED" or progress.database_after is None:
                raise WorkspaceV2InventoryError(
                    "relationship verification requires recovered progress"
                )
            self._controls(progress)
            with _v2_writer_closed(self.root), _closed_duckdb_file(database):
                self._verify(progress.database_after)
                if progress.wal_after is not None:
                    self._verify(progress.wal_after)
                elif database.with_suffix(".duckdb.wal").exists():
                    raise WorkspaceV2InventoryError("unexpected WAL after recorded recovery")
            with self.service.open_storage(
                self.service.resolve(self.root), WorkspaceAccessMode.READ_ONLY
            ) as storage:
                compact = CompactAnalyticalStore(storage.store.connection)
                if profile_only:
                    measurements = compact.profile_historical_relationships(
                        on_measurement=execution.record_measurement,
                        on_plan=execution.record_query_plan,
                    )
                    tree, environment = historical_transition_fingerprints(
                        progress.page_limit, progress.verifier_policy
                    )
                    generation = self.state / (
                        f"progress-reconciliation-generation.{self.operation_id}."
                        f"{sha256_hex(canonical_json_bytes(progress) + b'\n')}.json"
                    )
                    return ProgressRelationshipProfile(
                        operation_id=self.operation_id,
                        reconciliation=reconciliation_file_reference(generation, "control"),
                        database=progress.database_after,
                        resource_receipt=reconciliation_file_reference(admission_path, "resource"),
                        tree_fingerprint=tree,
                        environment_fingerprint=environment,
                        recorded_at=datetime.now(UTC),
                        **measurements,
                    )
                compact.verify_historical_relationships(read_only_units=True)
                row = execute_bounded(
                    storage.store.connection,
                    "SELECT (SELECT count(*) FROM raw_v2_index),"
                    "(SELECT count(*) FROM normalized_observations_v2),"
                    "(SELECT count(*) FROM workspace_metric_results_v2),"
                    "(SELECT count(*) FROM workspace_diagnostic_results_v2)",
                ).fetchone()
                assert row is not None
            tree, environment = historical_transition_fingerprints(
                progress.page_limit, progress.verifier_policy
            )
            proof = ProgressRelationshipProof(
                operation_id=self.operation_id,
                destination_root=self.root,
                destination_workspace_id=progress.destination_workspace_id,
                reconciliation=self._publish(progress),
                database=progress.database_after,
                wal=progress.wal_after,
                parent_membership=progress.parent_membership,
                tree_fingerprint=tree,
                environment_fingerprint=environment,
                verifier_module_sha256=sha256_hex(
                    Path(__file__).with_name("compact_analytical_v2.py").read_bytes()
                ),
                resource_policy_sha256=sha256_hex(
                    Path(__file__).with_name("resource_policy.py").read_bytes()
                ),
                resource_receipt=reconciliation_file_reference(admission_path, "resource"),
                raw_count=int(row[0]),
                observation_count=int(row[1]),
                metric_count=int(row[2]),
                diagnostic_count=int(row[3]),
                verified_at=datetime.now(UTC),
            )
            document = canonical_json_bytes(proof) + b"\n"
            self._write_once(
                self.state
                / f"progress-relationships.{self.operation_id}.{sha256_hex(document)}.json",
                document,
            )
            return proof


_PAGE = 256
_STATE_FILENAME = "workspace-v2-analytical-import-state.json"
_DIRECT_STATE_FILENAME = "direct-historical-import-state.json"
_ARCHIVE_POLICY = "workspace-v2-analytical-import-v1"
_DIRECT_POLICY = "direct-historical-import-v1"
_EMPTY_DIGEST = hashlib.sha256(b"").hexdigest()


def _fsync_parent(path: Path) -> None:
    descriptor = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class WorkspaceV2InventoryError(StorageError):
    """Historical import state, source identity or confirmed prefix is invalid."""


class WorkspaceV2InventoryState(ContractModel):
    """Atomic checkpoint binding one source archive to one scratch destination."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["workspace-v2-analytical-import-state-v1"] = (
        "workspace-v2-analytical-import-state-v1"
    )
    source_workspace_id: NonEmptyStr
    source_fingerprint: NonEmptyStr
    policy_version: Literal["workspace-v2-analytical-import-v1", "direct-historical-import-v1"] = (
        _ARCHIVE_POLICY
    )
    metric_count: int = Field(ge=0)
    metric_digest: NonEmptyStr
    diagnostic_count: int = Field(ge=0)
    diagnostic_digest: NonEmptyStr
    phase: Literal["metrics", "diagnostics", "verification", "complete"]
    metric_cursor: HistoricalAnalyticalCursor | None = None
    diagnostic_cursor: HistoricalAnalyticalCursor | None = None
    page_limit: int = Field(ge=1, le=_PAGE)
    updated_at: UTCDateTime

    @field_validator("page_limit", mode="before")
    @classmethod
    def _integer_page_limit(cls, value: object) -> object:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("workspace v2 import page limit must be an integer")
        return value


class WorkspaceV2InventorySummary(ContractModel):
    """Result and bounded page evidence for one complete archive transfer."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    complete: Literal[True] = True
    source_workspace_id: NonEmptyStr
    source_fingerprint: NonEmptyStr
    metric_count: int = Field(ge=0)
    metric_digest: NonEmptyStr
    diagnostic_count: int = Field(ge=0)
    diagnostic_digest: NonEmptyStr
    metric_created_count: int = Field(ge=0)
    metric_reused_count: int = Field(ge=0)
    diagnostic_created_count: int = Field(ge=0)
    diagnostic_reused_count: int = Field(ge=0)
    max_page_requested: int = Field(ge=1, le=_PAGE)
    max_page_hydrated: int = Field(ge=0, le=_PAGE)


def _fold_digest(previous: str, model: MetricResult | DiagnosticResult) -> str:
    checksum = sha256_hex(canonical_json_bytes(model))
    return hashlib.sha256(f"{previous}:{checksum}".encode()).hexdigest()


def _inventory_digest(inventory: tuple[int, str, int, str]) -> str:
    return hashlib.sha256(
        json.dumps(inventory, separators=(",", ":"), ensure_ascii=True).encode("ascii")
    ).hexdigest()


def _read_state(path: Path) -> WorkspaceV2InventoryState:
    if path.is_symlink() or not path.is_file():
        raise WorkspaceV2InventoryError("workspace v2 import checkpoint is unsafe")
    try:
        return WorkspaceV2InventoryState.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as error:
        raise WorkspaceV2InventoryError("workspace v2 import checkpoint is malformed") from error


def historical_transition_fingerprints(page_limit: int, policy_version: str) -> tuple[str, str]:
    code_paths = (
        Path(__file__),
        Path(__file__).with_name("compact_analytical_v2.py"),
        Path(__file__).with_name("historical_analytical_import.py"),
        Path(__file__).parent.parent / "workspace" / "workspace_v2_migration_models.py",
    )
    code_digest = hashlib.sha256()
    for code_path in code_paths:
        code_digest.update(hashlib.sha256(code_path.read_bytes()).digest())
    tree = code_digest.hexdigest()
    environment = hashlib.sha256(
        f"{page_limit}:{policy_version}:{sys.version}:{version('duckdb')}:{version('pydantic')}".encode()
    ).hexdigest()
    return tree, environment


def historical_transition_status(root: Path) -> Literal["COMPLETE", "APPLYING"]:
    """Read companions without treating an incomplete physical photo as complete F."""
    from investment_analyst.workspace.service import WorkspaceService

    service = WorkspaceService()
    manifest = service.manifest(root)
    ledger = WorkspaceV2GenerationLedger(service, root)
    resolved = ledger.resolve()
    generation = None if resolved is None else resolved[1]
    if generation is not None and (
        generation.phase != "SUCCESSOR_BOUND"
        or generation.reserved_successor is None
        or ledger.attempt_invalidated(generation.reserved_successor.operation_id)
    ):
        return "APPLYING"
    result: Literal["COMPLETE", "APPLYING"] = "COMPLETE"
    incomplete: list[tuple[Path, HistoricalTransition]] = []
    complete: dict[UUID, HistoricalTransitionReceipt] = {}
    for path in (root / "state").glob("historical-transition.*.json"):
        if path.is_symlink() or not path.is_file():
            raise WorkspaceV2InventoryError("historical transition control is unsafe")
        try:
            transition = HistoricalTransition.model_validate_json(path.read_bytes())
        except (OSError, ValueError) as error:
            raise WorkspaceV2InventoryError("historical transition control is malformed") from error
        if transition.destination_workspace_id != manifest.workspace_id:
            raise WorkspaceV2InventoryError("historical transition workspace binding differs")
        if transition.phase != "COMPLETE":
            result = "APPLYING"
            incomplete.append((path, transition))
            continue
        receipt_path = (
            root / "state" / f"historical-transition-receipt.{transition.operation_id}.json"
        )
        if receipt_path.is_symlink() or not receipt_path.is_file():
            raise WorkspaceV2InventoryError("historical terminal receipt is missing")
        receipt = HistoricalTransitionReceipt.model_validate_json(receipt_path.read_bytes())
        if (
            receipt.transition != transition
            or receipt.parent_preservation_sha256 != transition.parent_membership_sha256
            or receipt.final_seal_sha256 != sha256_hex(canonical_json_bytes(receipt.final_seal))
        ):
            raise WorkspaceV2InventoryError("historical terminal receipt binding differs")
        complete[transition.operation_id] = receipt
    if generation is not None:
        from investment_analyst.workspace.workspace_v2_migration import _SuccessorComparison

        assert generation.reserved_successor is not None
        cut = generation.reserved_successor
        terminal = complete.get(cut.operation_id)
        if terminal is None:
            return "APPLYING"
        inventory, coverage = generation.inventory, generation.coverage
        assert inventory is not None and coverage is not None
        # Every prior transition must be an exact referenced ancestor. A new
        # head is not permission to ignore unrelated APPLYING controls/forks.
        ancestors: dict[Path, ReconciliationFileReference] = {}
        current = generation
        while True:
            for reference in (current.predecessor, current.previous_attempt):
                WorkspaceV2ProgressReconciler._verify(reference)
                ancestors[reference.path] = reference
            previous = current.previous_generation
            if previous.schema_type == "workspace-v2-progress-reconciliation-v1":
                legacy = ProgressReconciliation.model_validate_json(ledger._read(previous.artifact))
                ancestors[legacy.predecessor.path] = legacy.predecessor
                break
            current = ProgressReconciliationV2.model_validate_json(ledger._read(previous.artifact))
        controls = [
            *incomplete,
            *(
                (root / "state" / f"historical-transition.{identifier}.json", receipt.transition)
                for identifier, receipt in complete.items()
                if identifier != cut.operation_id
            ),
        ]
        for path, transition in controls:
            reference = ancestors.get(path)
            if (
                reference is None
                or reconciliation_file_reference(path, "control") != reference
                or transition.destination_workspace_id != manifest.workspace_id
            ):
                raise WorkspaceV2InventoryError(
                    "generation contains an uncovered historical control"
                )
        successor = terminal.transition
        comparison_path = (
            root
            / "state"
            / (
                f"progress-successor-comparison.{cut.operation_id}.{coverage.comparison_sha256}.json"
            )
        )
        if (
            reconciliation_file_reference(comparison_path, "control").sha256
            != coverage.comparison_sha256
        ):
            raise WorkspaceV2InventoryError("generational successor comparison changed")
        comparison = _SuccessorComparison.model_validate_json(comparison_path.read_bytes())
        parent, seal = successor.parent_seal, terminal.final_seal
        if (
            not isinstance(successor.source, HistoricalFrozenSource)
            or successor.source.cut != cut
            or successor.parent_membership_path != generation.parent_membership.path
            or successor.parent_membership_sha256 != generation.parent_membership.sha256
            or (
                parent.metric_count,
                parent.metric_digest,
                parent.diagnostic_count,
                parent.diagnostic_digest,
            )
            != (
                inventory.metric_count,
                inventory.metric_digest,
                inventory.diagnostic_count,
                inventory.diagnostic_digest,
            )
            or comparison.work.successor != cut
            or comparison.work.family != "complete"
            or comparison.work.baseline_dataset_sha256 != inventory.dataset_sha256
            or comparison.verifier_tree_fingerprint != coverage.verifier_tree_fingerprint
            or (comparison.work.covered_metric_count, comparison.work.covered_diagnostic_count)
            != (inventory.metric_count, inventory.diagnostic_count)
            or (
                seal.metric_count,
                seal.metric_digest,
                seal.diagnostic_count,
                seal.diagnostic_digest,
            )
            != (
                comparison.work.metric_count,
                comparison.work.metric_digest,
                comparison.work.diagnostic_count,
                comparison.work.diagnostic_digest,
            )
        ):
            raise WorkspaceV2InventoryError("generational historical successor binding differs")
        return "COMPLETE"
    if incomplete:
        from investment_analyst.workspace.workspace_v2_migration import _SuccessorComparison

        reconciliations = list((root / "state").glob("progress-reconciliation.*.json"))
        if not reconciliations:
            return "APPLYING"
        if len(reconciliations) != 1 or reconciliations[0].is_symlink():
            raise WorkspaceV2InventoryError("historical reconciliation ownership is ambiguous")
        progress = ProgressReconciliation.model_validate_json(reconciliations[0].read_bytes())
        if progress.phase != "SUCCESSOR_BOUND":
            return "APPLYING"
        assert progress.coverage is not None and progress.inventory is not None
        if (
            len(incomplete) != 1
            or incomplete[0][1].operation_id != progress.predecessor_operation_id
        ):
            return "APPLYING"
        if (
            reconciliation_file_reference(incomplete[0][0], "control").sha256
            != progress.predecessor.sha256
        ):
            raise WorkspaceV2InventoryError("historical predecessor control changed")
        terminal = complete.get(progress.reserved_successor_id)
        if terminal is None:
            return "APPLYING"
        successor = terminal.transition
        if (
            progress.destination_workspace_id != manifest.workspace_id
            or not isinstance(successor.source, HistoricalFrozenSource)
            or successor.source.cut != progress.reserved_successor
            or successor.parent_seal != progress.parent_seal
            or successor.parent_membership_sha256 != progress.parent_membership.sha256
            or successor.parent_membership_path != progress.parent_membership.path
        ):
            raise WorkspaceV2InventoryError("historical successor reconciliation binding differs")
        comparison_path = (
            root
            / "state"
            / (
                f"progress-successor-comparison.{successor.operation_id}."
                f"{progress.coverage.comparison_sha256}.json"
            )
        )
        if (
            reconciliation_file_reference(comparison_path, "control").sha256
            != progress.coverage.comparison_sha256
        ):
            raise WorkspaceV2InventoryError("historical successor comparison changed")
        comparison = _SuccessorComparison.model_validate_json(comparison_path.read_bytes())
        seal = terminal.final_seal
        if (
            comparison.work.successor != progress.reserved_successor
            or comparison.work.family != "complete"
            or comparison.work.baseline_dataset_sha256 != progress.inventory.dataset_sha256
            or comparison.verifier_tree_fingerprint != progress.coverage.verifier_tree_fingerprint
            or (comparison.work.covered_metric_count, comparison.work.covered_diagnostic_count)
            != (progress.inventory.metric_count, progress.inventory.diagnostic_count)
            or (
                seal.metric_count,
                seal.metric_digest,
                seal.diagnostic_count,
                seal.diagnostic_digest,
            )
            != (
                comparison.work.metric_count,
                comparison.work.metric_digest,
                comparison.work.diagnostic_count,
                comparison.work.diagnostic_digest,
            )
        ):
            raise WorkspaceV2InventoryError("historical successor final inventory differs")
        return "COMPLETE"
    return result


class WorkspaceV2AnalyticalImporter:
    """Copy an exact verified archive in committed pages into a v2 scratch store."""

    def __init__(
        self,
        source: HistoricalAnalyticalArchive | DirectHistoricalAnalyticalSource,
        target: CompactAnalyticalStore,
        *,
        source_root: Path,
        destination_root: Path,
        state_root: Path,
        source_workspace_id: str,
        source_fingerprint: str,
        page_limit: int = _PAGE,
        clock: Callable[[], datetime] | None = None,
        failure_injector: Callable[[str], None] | None = None,
        allow_source_transition: bool = False,
        direct_transport: bool = False,
        epoch: UUID | None = None,
        frozen_source: FrozenSourceCut | None = None,
        source_snapshot: WorkspaceV1MigrationSourceSnapshot | None = None,
    ) -> None:
        if (
            isinstance(page_limit, bool)
            or not isinstance(page_limit, int)
            or not 1 <= page_limit <= _PAGE
        ):
            raise WorkspaceV2InventoryError(
                "workspace v2 import page limit must be between 1 and 256"
            )
        if len(source_fingerprint) != 64 or any(
            character not in "0123456789abcdef" for character in source_fingerprint
        ):
            raise WorkspaceV2InventoryError("workspace v2 source fingerprint must be SHA-256")
        if destination_root.is_symlink() or state_root.is_symlink():
            raise WorkspaceV2InventoryError("workspace v2 import path cannot be a symbolic link")
        source_path = source_root.resolve(strict=False)
        destination_path = destination_root.resolve(strict=False)
        state_path = state_root.resolve(strict=False)
        if (
            source_path == destination_path
            or source_path in destination_path.parents
            or destination_path in source_path.parents
            or destination_path.is_symlink()
            or state_path != destination_path
            and destination_path not in state_path.parents
        ):
            raise WorkspaceV2InventoryError("workspace v2 import roots must be disjoint and safe")
        if any(parent.is_symlink() for parent in (destination_path, state_path)):
            raise WorkspaceV2InventoryError("workspace v2 import path cannot be a symbolic link")
        self._source = source
        self._target = target
        self._source_workspace_id = source_workspace_id
        self._source_fingerprint = source_fingerprint
        self._page_limit = page_limit
        self._clock = clock or (lambda: datetime.now(UTC))
        self._failure_injector = failure_injector
        self._allow_source_transition = allow_source_transition
        self._epoch = epoch
        self._frozen_source = frozen_source
        self._source_snapshot = source_snapshot
        self._historical_transition: HistoricalTransition | None = None
        self._recovered_progress: ProgressReconciliation | ProgressReconciliationV2 | None = None
        self._recovered_admission: Path | None = None
        self._recovered_code_root: Path | None = None
        if epoch is not None and not isinstance(source, DirectHistoricalAnalyticalSource):
            raise WorkspaceV2InventoryError("analytical epoch requires the direct source reader")
        self._policy_version = _DIRECT_POLICY if direct_transport else _ARCHIVE_POLICY
        self._tree_fingerprint, self._environment_fingerprint = historical_transition_fingerprints(
            page_limit,
            self._policy_version,
        )
        self._max_page_hydrated = 0
        self._source_root = source_path
        self._destination_root = destination_path
        self._state_root = state_path
        filename = _DIRECT_STATE_FILENAME if direct_transport else _STATE_FILENAME
        if epoch is not None:
            filename = f"direct-historical-import-state.F.{epoch}.json"
        self._state_path = state_path / filename

    def _verification_checkpoint_path(self, family: Literal["metrics", "diagnostics"]) -> Path:
        if self._epoch is not None:
            return (
                self._state_root
                / f"{self._policy_version}-{family}-verification-work.F.{self._epoch}.json"
            )
        if self._allow_source_transition:
            return self._state_root / (
                f"{self._policy_version}-{family}-verification-work.source.{self._source_fingerprint}.json"
            )
        return self._state_root / f"{self._policy_version}-{family}-verification-work.json"

    def _verification_checkpoint(
        self,
        *,
        family: Literal["metrics", "diagnostics"],
        cursor: HistoricalAnalyticalCursor,
        confirmed_count: int,
        digest: str,
    ) -> WorkspaceV2MigrationWorkCheckpoint:
        return WorkspaceV2MigrationWorkCheckpoint(
            operation_id=self._epoch
            or uuid5(
                NAMESPACE_URL,
                f"{self._policy_version}:{self._source_fingerprint}:{self._destination_root}",
            ),
            phase=WorkspaceV2MigrationWorkPhase.VERIFY,
            snapshot_id=self._epoch or uuid5(NAMESPACE_URL, self._source_fingerprint),
            source_fingerprint=self._source_fingerprint,
            destination_workspace_id=self._destination_id(),
            source_root=self._source_root,
            destination_root=self._destination_root,
            policy_version=self._policy_version,
            reader_policy="direct-historical-readonly-v1",
            verifier_policy=f"paired-{family}-inventory-v1",
            tree_fingerprint=self._tree_fingerprint,
            environment_fingerprint=self._environment_fingerprint,
            cursor=f"{cursor.available_at.isoformat()}|{cursor.identifier}",
            ordinal=(confirmed_count + self._page_limit - 1) // self._page_limit,
            offset_bytes=0,
            page_limit=self._page_limit,
            confirmed_count=confirmed_count,
            confirmed_bytes=0,
            accumulated_digest=digest,
            input_references=(family,),
            updated_at=self._clock(),
        )

    def _write_verification_checkpoint(
        self,
        *,
        family: Literal["metrics", "diagnostics"],
        cursor: HistoricalAnalyticalCursor,
        confirmed_count: int,
        digest: str,
    ) -> None:
        path = self._verification_checkpoint_path(family)
        checkpoint = self._verification_checkpoint(
            family=family,
            cursor=cursor,
            confirmed_count=confirmed_count,
            digest=digest,
        )
        if path.is_symlink():
            raise WorkspaceV2InventoryError("verification checkpoint is unsafe")
        if path.exists():
            existing = WorkspaceV2MigrationWorkCheckpoint.model_validate_json(path.read_bytes())
            if existing.model_copy(update={"updated_at": checkpoint.updated_at}) == checkpoint:
                return
        temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        try:
            with temporary.open("x", encoding="utf-8", newline="\n") as handle:
                handle.write(checkpoint.model_dump_json() + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            temporary.replace(path)
            _fsync_parent(path)
        finally:
            temporary.unlink(missing_ok=True)

    def _load_verification_checkpoint(
        self, family: Literal["metrics", "diagnostics"]
    ) -> tuple[HistoricalAnalyticalCursor | None, int, str]:
        path = self._verification_checkpoint_path(family)
        if not path.exists():
            return None, 0, _EMPTY_DIGEST
        if path.is_symlink() or not path.is_file():
            raise WorkspaceV2InventoryError("verification checkpoint is unsafe")
        try:
            checkpoint = WorkspaceV2MigrationWorkCheckpoint.model_validate_json(
                path.read_text(encoding="utf-8")
            )
            text_cursor = checkpoint.cursor
            timestamp, identifier = text_cursor.split("|", 1) if text_cursor else ("", "")
            cursor = HistoricalAnalyticalCursor(
                available_at=datetime.fromisoformat(timestamp.replace("Z", "+00:00")),
                identifier=UUID(identifier),
            )
        except (OSError, ValueError, TypeError) as error:
            raise WorkspaceV2InventoryError("verification checkpoint is malformed") from error
        if (
            checkpoint.phase is not WorkspaceV2MigrationWorkPhase.VERIFY
            or checkpoint.source_fingerprint != self._source_fingerprint
            or checkpoint.destination_root != self._destination_root
            or checkpoint.policy_version != self._policy_version
            or checkpoint.page_limit != self._page_limit
            or checkpoint.input_references != (family,)
        ):
            raise WorkspaceV2InventoryError("verification checkpoint binding changed")
        expected = self._verification_checkpoint(
            family=family,
            cursor=cursor,
            confirmed_count=checkpoint.confirmed_count,
            digest=checkpoint.accumulated_digest,
        )
        if checkpoint.model_copy(update={"updated_at": expected.updated_at}) != expected:
            raise WorkspaceV2InventoryError("verification checkpoint binding changed")
        return cursor, checkpoint.confirmed_count, checkpoint.accumulated_digest

    def _destination_id(self) -> UUID:
        from investment_analyst.workspace.service import WorkspaceService

        return WorkspaceService().manifest(self._destination_root).workspace_id

    def _transition_path(self, operation_id: UUID) -> Path:
        return self._state_root / f"historical-transition.{operation_id}.json"

    def _write_transition(self, transition: HistoricalTransition) -> None:
        self._write_companion(self._transition_path(transition.operation_id), transition)

    @staticmethod
    def _write_companion(
        path: Path, model: HistoricalTransition | HistoricalTransitionReceipt
    ) -> None:
        if path.is_symlink():
            raise WorkspaceV2InventoryError("historical companion path is unsafe")
        temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        try:
            with temporary.open("xb") as handle:
                handle.write(canonical_json_bytes(model) + b"\n")
                handle.flush()
                os.fsync(handle.fileno())
            temporary.replace(path)
            _fsync_parent(path)
            descriptor = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        finally:
            temporary.unlink(missing_ok=True)

    def bind_recovered_successor(
        self,
        *,
        reconciliation_id: UUID,
        admission_path: Path,
        baseline_code_root: Path | None,
    ) -> None:
        """Bind only an already verified, exclusive successor in its live session."""
        from investment_analyst.workspace.service import WorkspaceService
        from investment_analyst.workspace.workspace_v2_migration import verify_recovered_successor

        if self._frozen_source is None or not isinstance(
            self._source, DirectHistoricalAnalyticalSource
        ):
            raise WorkspaceV2InventoryError("recovered successor requires a direct frozen source")
        receipt = verify_recovered_successor(
            service=WorkspaceService(),
            destination_root=self._destination_root,
            reconciliation_id=reconciliation_id,
            frozen_source=self._frozen_source,
            admission_path=admission_path,
            baseline_code_root=baseline_code_root,
        )
        self._source.validate_transition_source(self._source_root, UUID(self._source_workspace_id))
        progress = (
            receipt if isinstance(receipt, ProgressReconciliationV2) else receipt.reconciliation
        )
        if (
            progress.phase != "SUCCESSOR_BOUND"
            or progress.reserved_successor != self._frozen_source
        ):
            raise WorkspaceV2InventoryError("recovered successor cut differs")
        self._recovered_progress = progress
        self._recovered_admission = admission_path
        self._recovered_code_root = baseline_code_root

    def _prepare_transition(self) -> HistoricalTransition | None:
        from investment_analyst.workspace.service import WorkspaceService

        parent = self._target.historical_seal()
        if self._frozen_source is None and self._source_snapshot is None:
            # Legacy provenance-only transitions retain their public behavior;
            # ordinary store methods still reject any growth of a sealed corpus.
            return None
        source = (
            HistoricalFrozenSource(cut=self._frozen_source)
            if self._frozen_source is not None
            else HistoricalSnapshotSource(snapshot=self._source_snapshot)
        )
        operation_id = (
            self._frozen_source.operation_id
            if self._frozen_source is not None
            else self._source_snapshot.snapshot_id
        )
        service = WorkspaceService()
        manifest = service.manifest(self._source_root)
        destination = service.manifest(self._destination_root)
        if str(manifest.workspace_id) != self._source_workspace_id:
            raise WorkspaceV2InventoryError("historical source manifest differs")
        path = self._transition_path(operation_id)
        self._state_root.mkdir(parents=True, exist_ok=True)
        # An incomplete generation is owned by its exact operation, never adopted.
        for other in self._state_root.glob("historical-transition.*.json"):
            if other.is_symlink():
                raise WorkspaceV2InventoryError("historical companion is unsafe")
            existing = HistoricalTransition.model_validate_json(other.read_bytes())
            if existing.phase != "COMPLETE" and existing.operation_id != operation_id:
                recovered = self._recovered_progress
                if (
                    recovered is not None
                    and recovered.phase == "SUCCESSOR_BOUND"
                    and recovered.reserved_successor == self._frozen_source
                    and other == recovered.predecessor.path
                    and reconciliation_file_reference(other, "control") == recovered.predecessor
                    and (
                        isinstance(recovered, ProgressReconciliationV2)
                        or existing.operation_id == recovered.predecessor_operation_id
                    )
                ):
                    continue
                raise WorkspaceV2InventoryError("another historical transition is incomplete")
        tree = self._tree_fingerprint
        environment = self._environment_fingerprint
        if path.exists():
            if path.is_symlink():
                raise WorkspaceV2InventoryError("historical companion is unsafe")
            transition = HistoricalTransition.model_validate_json(path.read_bytes())
            if (
                transition.source != source
                or transition.source_root != self._source_root
                or transition.source_workspace_id != manifest.workspace_id
                or transition.source_created_at != manifest.created_at
                or transition.source_fingerprint != self._source_fingerprint
                or transition.destination_root != self._destination_root
                or transition.destination_workspace_id != destination.workspace_id
                or transition.page_limit != self._page_limit
                or transition.tree_fingerprint != tree
                or transition.environment_fingerprint != environment
            ):
                raise WorkspaceV2InventoryError("historical transition binding changed")
        elif parent is None:
            return None
        else:
            recovered = self._recovered_progress
            if recovered is not None:
                if isinstance(recovered, ProgressReconciliationV2):
                    inventory = recovered.inventory
                    if inventory is None or (
                        parent.metric_count,
                        parent.metric_digest,
                        parent.diagnostic_count,
                        parent.diagnostic_digest,
                    ) != (
                        inventory.metric_count,
                        inventory.metric_digest,
                        inventory.diagnostic_count,
                        inventory.diagnostic_digest,
                    ):
                        raise WorkspaceV2InventoryError("recovered generation parent seal changed")
                elif parent.model_dump() != recovered.parent_seal.model_dump():
                    raise WorkspaceV2InventoryError("recovered successor parent seal changed")
            membership = (
                recovered.parent_membership.path
                if recovered is not None
                else self._state_root / f"historical-parent.{operation_id}.jsonl"
            )
            transition = HistoricalTransition(
                operation_id=operation_id,
                phase="PREPARED",
                source=source,
                source_workspace_id=manifest.workspace_id,
                source_created_at=manifest.created_at,
                source_root=self._source_root,
                source_fingerprint=self._source_fingerprint,
                destination_root=self._destination_root,
                destination_workspace_id=destination.workspace_id,
                parent_seal=HistoricalSealSnapshot.model_validate(parent.model_dump()),
                parent_seal_sha256=sha256_hex(canonical_json_bytes(parent)),
                parent_membership_path=membership,
                parent_membership_sha256=(
                    recovered.parent_membership.sha256 if recovered is not None else _EMPTY_DIGEST
                ),
                page_limit=self._page_limit,
                destination_inactive=True,
                writer_closed=True,
                tree_fingerprint=tree,
                environment_fingerprint=environment,
                prepared_at=self._clock(),
                updated_at=self._clock(),
            )
            self._write_transition(transition)
        if transition.phase == "PREPARED":
            if parent is None or parent.model_dump() != transition.parent_seal.model_dump():
                raise WorkspaceV2InventoryError("historical preparation parent changed")
            if self._recovered_progress is not None:
                reference = reconciliation_file_reference(
                    transition.parent_membership_path, "inventory"
                )
                if reference != self._recovered_progress.parent_membership:
                    raise WorkspaceV2InventoryError("recovered parent membership changed")
                membership_digest = reference.sha256
            else:
                membership_digest = self._target.write_parent_membership(
                    transition.parent_membership_path,
                    page_limit=self._page_limit,
                )
            transition = transition.model_copy(
                update={"parent_membership_sha256": membership_digest}
            )
            self._write_transition(transition)
        self._epoch = operation_id
        label = "F" if self._frozen_source is not None else "B"
        self._state_path = (
            self._state_root / f"direct-historical-import-state.{label}.{operation_id}.json"
        )
        return transition

    def run(self) -> WorkspaceV2InventorySummary:
        transition = self._prepare_transition()
        if transition is None:
            return self._run_legacy()
        seal = self._target.historical_seal()
        if seal is None:
            raise WorkspaceV2InventoryError("historical transition seal is missing")
        parent_active = seal.model_dump() == transition.parent_seal.model_dump()
        if parent_active:
            if transition.phase == "COMPLETE":
                raise WorkspaceV2InventoryError("completed transition has its parent active")
            transition = transition.model_copy(
                update={"phase": "APPLYING", "updated_at": self._clock()}
            )
            self._write_transition(transition)
            self._historical_transition = transition
            if self._recovered_progress is not None:
                assert self._recovered_admission is not None
                with self._target.recovered_historical_transition(
                    transition,
                    source=self._source,
                    reconciliation_id=(
                        self._recovered_progress.generation_id
                        if isinstance(self._recovered_progress, ProgressReconciliationV2)
                        else self._recovered_progress.operation_id
                    ),
                    admission_path=self._recovered_admission,
                    baseline_code_root=self._recovered_code_root,
                ):
                    summary = self._run_recovered_successor()
            else:
                with self._target.historical_transition(transition, source=self._source):
                    summary = self._run_epoch()
        else:
            # Commit may have succeeded immediately before the crash. Revalidate
            # both complete source coverage and parent archive before finishing.
            if seal.source_fingerprint != self._source_fingerprint:
                raise WorkspaceV2InventoryError("historical transition active seal differs")
            self._target.verify_transition_archive(transition)
            self._historical_transition = None
            self._target.verify_parent_membership(transition)
            summary = (
                self._run_recovered_successor()
                if self._recovered_progress is not None
                else self._run_epoch()
            )
        seal = self._target.historical_seal()
        assert seal is not None
        if transition.phase == "COMPLETE":
            receipt_path = (
                self._state_root / f"historical-transition-receipt.{transition.operation_id}.json"
            )
            if receipt_path.is_symlink() or not receipt_path.is_file():
                raise WorkspaceV2InventoryError("terminal historical receipt is missing")
            receipt = HistoricalTransitionReceipt.model_validate_json(receipt_path.read_bytes())
            if (
                receipt.transition != transition
                or receipt.final_seal.model_dump() != seal.model_dump()
                or receipt.parent_preservation_sha256 != transition.parent_membership_sha256
                or receipt.final_seal_sha256 != sha256_hex(canonical_json_bytes(seal))
            ):
                raise WorkspaceV2InventoryError("terminal historical receipt binding differs")
            return summary
        transition = transition.model_copy(
            update={"phase": "VERIFIED", "updated_at": self._clock()}
        )
        self._write_transition(transition)
        if self._failure_injector is not None:
            self._failure_injector("after_historical_seal_commit")
        final = HistoricalSealSnapshot.model_validate(seal.model_dump())
        completed = transition.model_copy(update={"phase": "COMPLETE", "updated_at": self._clock()})
        receipt = HistoricalTransitionReceipt(
            transition=completed,
            parent_preservation_sha256=transition.parent_membership_sha256,
            final_seal=final,
            final_seal_sha256=sha256_hex(canonical_json_bytes(final)),
            final_inventory_digest=_inventory_digest(
                (
                    final.metric_count,
                    final.metric_digest,
                    final.diagnostic_count,
                    final.diagnostic_digest,
                )
            ),
            metric_created_count=final.metric_count - transition.parent_seal.metric_count,
            metric_reused_count=transition.parent_seal.metric_count,
            diagnostic_created_count=final.diagnostic_count
            - transition.parent_seal.diagnostic_count,
            diagnostic_reused_count=transition.parent_seal.diagnostic_count,
            completed_at=self._clock(),
        )
        receipt_path = (
            self._state_root / f"historical-transition-receipt.{transition.operation_id}.json"
        )
        if receipt_path.exists():
            if receipt_path.is_symlink():
                raise WorkspaceV2InventoryError("historical receipt is unsafe")
            previous = HistoricalTransitionReceipt.model_validate_json(receipt_path.read_bytes())
            previous_binding = previous.transition.model_copy(
                update={"phase": completed.phase, "updated_at": completed.updated_at},
            )
            if previous_binding != completed:
                raise WorkspaceV2InventoryError("historical receipt transition differs")
            if (
                previous.model_copy(
                    update={"transition": completed, "completed_at": receipt.completed_at}
                )
                != receipt
            ):
                raise WorkspaceV2InventoryError("historical receipt binding differs")
            completed = previous.transition
        else:
            self._write_companion(receipt_path, receipt)
        if self._failure_injector is not None:
            self._failure_injector("after_historical_receipt")
        self._write_transition(completed)
        return summary

    def _run_recovered_successor(self) -> WorkspaceV2InventorySummary:
        """Reuse W, copy only verified F1 additions, then seal the full inventory."""
        from investment_analyst.workspace.workspace_v2_migration import _SuccessorComparison

        progress = self._recovered_progress
        assert (
            progress is not None
            and progress.coverage is not None
            and progress.inventory is not None
        )
        assert isinstance(self._source, DirectHistoricalAnalyticalSource)
        path = self._state_root / (
            f"progress-successor-comparison.{self._epoch}."
            f"{progress.coverage.comparison_sha256}.json"
        )
        if (
            reconciliation_file_reference(path, "control").sha256
            != progress.coverage.comparison_sha256
        ):
            raise WorkspaceV2InventoryError("recovered successor comparison changed")
        comparison = _SuccessorComparison.model_validate_json(path.read_bytes())
        expected = (
            comparison.work.metric_count,
            comparison.work.metric_digest,
            comparison.work.diagnostic_count,
            comparison.work.diagnostic_digest,
        )
        created = {"metrics": 0, "diagnostics": 0}
        reused = {
            "metrics": progress.inventory.metric_count,
            "diagnostics": progress.inventory.diagnostic_count,
        }
        if self._historical_transition is not None:
            for family in ("metrics", "diagnostics"):
                for page in self._source.iter_recovered_pages(
                    index_path=comparison.new_index.path,
                    index_sha256=comparison.new_index.sha256,
                    family=family,
                    limit=self._page_limit,
                ):
                    self._max_page_hydrated = max(self._max_page_hydrated, len(page))
                    if family == "metrics":
                        written = self._target.append_transition_metrics(
                            self._historical_transition, page
                        )
                    else:
                        written = self._target.append_transition_diagnostics(
                            self._historical_transition, page
                        )
                    created[family] += len(written.created_ids)
                    reused[family] += len(written.reused_ids)
                    self._target.compare_transition_page(self._historical_transition, page, family)
                    if self._failure_injector is not None:
                        self._failure_injector(
                            "after_metric_page_write"
                            if family == "metrics"
                            else "after_diagnostic_page_write"
                        )
            proof = self._verification_proof(expected).model_copy(
                update={
                    "subphase": "reconciled-models",
                    "reader_policy": "recovered-successor-readonly-v1",
                    "verifier_policy": "reconciled-source-hash-inventory-v1",
                }
            )
            self._target.publish_recovered_transition_seal(
                self._historical_transition, proof=proof, sealed_at=self._clock()
            )
        else:
            seal = self._target.historical_seal()
            if (
                seal is None
                or (
                    seal.metric_count,
                    seal.metric_digest,
                    seal.diagnostic_count,
                    seal.diagnostic_digest,
                )
                != expected
                or (
                    self._target.count_metrics(origin="HISTORICAL"),
                    self._target.count_diagnostics(origin="HISTORICAL"),
                )
                != (expected[0], expected[2])
            ):
                raise WorkspaceV2InventoryError("recovered successor final seal differs")
            reused = {"metrics": expected[0], "diagnostics": expected[2]}
        return WorkspaceV2InventorySummary(
            source_workspace_id=self._source_workspace_id,
            source_fingerprint=self._source_fingerprint,
            metric_count=expected[0],
            metric_digest=expected[1],
            diagnostic_count=expected[2],
            diagnostic_digest=expected[3],
            metric_created_count=created["metrics"],
            metric_reused_count=reused["metrics"],
            diagnostic_created_count=created["diagnostics"],
            diagnostic_reused_count=reused["diagnostics"],
            max_page_requested=self._page_limit,
            max_page_hydrated=self._max_page_hydrated,
        )

    def _run_legacy(self) -> WorkspaceV2InventorySummary:
        """Resume only from an exact source/destination-bound checkpoint."""
        if self._epoch is not None:
            return self._run_epoch()
        state = self._load_or_create_state()
        try:
            self._validate_binding(state)
        except WorkspaceV2InventoryError:
            if not self._allow_source_transition:
                raise
            self._archive_historic_state()
            state = WorkspaceV2InventoryState(
                source_workspace_id=self._source_workspace_id,
                source_fingerprint=self._source_fingerprint,
                policy_version=self._policy_version,
                metric_count=0,
                metric_digest=_EMPTY_DIGEST,
                diagnostic_count=0,
                diagnostic_digest=_EMPTY_DIGEST,
                phase="metrics",
                page_limit=self._page_limit,
                updated_at=self._clock(),
            )
            self._write_state(state)
        if state.phase not in ("verification", "complete"):
            self._validate_confirmed_prefix(state)

        metric_created = metric_reused = 0
        diagnostic_created = diagnostic_reused = 0
        if state.phase == "metrics":
            state, metric_created, metric_reused = self._import_metrics(state)
        if state.phase == "diagnostics":
            state, diagnostic_created, diagnostic_reused = self._import_diagnostics(state)
        if state.phase in ("verification", "complete"):
            actual = self._validate_confirmed_prefix(state)
            expected = (
                state.metric_count,
                state.metric_digest,
                state.diagnostic_count,
                state.diagnostic_digest,
            )
            if actual != expected:
                raise WorkspaceV2InventoryError(
                    "workspace v2 historical digest differs from source"
                )
            if (
                self._target.count_metrics(origin="HISTORICAL"),
                self._target.count_diagnostics(origin="HISTORICAL"),
            ) != (state.metric_count, state.diagnostic_count):
                raise WorkspaceV2InventoryError("workspace v2 historical count differs from source")
            metric_count, metric_digest, diagnostic_count, diagnostic_digest = actual
            self._target.seal_historical_inventory(
                source_fingerprint=self._source_fingerprint,
                metric_count=metric_count,
                metric_digest=metric_digest,
                diagnostic_count=diagnostic_count,
                diagnostic_digest=diagnostic_digest,
                sealed_at=self._clock(),
                allow_source_transition=self._allow_source_transition,
                verification_proof=(
                    self._verification_proof(actual)
                    if isinstance(self._source, DirectHistoricalAnalyticalSource)
                    else None
                ),
            )
            self._target.verify_historical_relationships()
            if state.phase != "complete":
                state = self._updated_state(state, phase="complete")
                self._write_state(state)
        if state.phase != "complete":
            raise WorkspaceV2InventoryError("workspace v2 historical import is incomplete")
        return WorkspaceV2InventorySummary(
            source_workspace_id=self._source_workspace_id,
            source_fingerprint=self._source_fingerprint,
            metric_count=state.metric_count,
            metric_digest=state.metric_digest,
            diagnostic_count=state.diagnostic_count,
            diagnostic_digest=state.diagnostic_digest,
            metric_created_count=metric_created,
            metric_reused_count=metric_reused,
            diagnostic_created_count=diagnostic_created,
            diagnostic_reused_count=diagnostic_reused,
            max_page_requested=self._page_limit,
            max_page_hydrated=self._max_page_hydrated,
        )

    def _run_epoch(self) -> WorkspaceV2InventorySummary:
        """Import and verify each F partition through one ordered source cursor."""
        assert isinstance(self._source, DirectHistoricalAnalyticalSource)
        state = self._load_or_create_state()
        self._validate_binding(state)
        created_metrics = reused_metrics = created_diagnostics = reused_diagnostics = 0
        for family in ("metrics", "diagnostics"):
            original_count = state.metric_count if family == "metrics" else state.diagnostic_count
            original_digest = (
                state.metric_digest if family == "metrics" else state.diagnostic_digest
            )
            original_cursor = (
                state.metric_cursor if family == "metrics" else state.diagnostic_cursor
            )
            if original_count == 0 and (
                original_digest != _EMPTY_DIGEST or original_cursor is not None
            ):
                raise WorkspaceV2InventoryError("analytical epoch empty prefix differs")
            verified_cursor, verified_count, verified_digest = self._load_verification_checkpoint(
                family
            )
            if verified_count > original_count:
                raise WorkspaceV2InventoryError("verification cursor exceeds durable import")
            count, digest = 0, _EMPTY_DIGEST
            cursor = None
            pages = (
                self._source.iter_metric_pages(limit=self._page_limit)
                if family == "metrics"
                else self._source.iter_diagnostic_pages(limit=self._page_limit)
            )
            for page in pages:
                self._max_page_hydrated = max(self._max_page_hydrated, len(page))
                prefix = count < original_count
                if prefix and count + len(page) > original_count:
                    raise WorkspaceV2InventoryError("analytical epoch prefix ends inside page")
                if family == "metrics":
                    if not prefix:
                        receipt = (
                            self._target.append_transition_metrics(
                                self._historical_transition, page
                            )
                            if self._historical_transition is not None
                            else self._target.save_metrics(page, origin="HISTORICAL")
                        )
                        created_metrics += len(receipt.created_ids)
                        reused_metrics += len(receipt.reused_ids)
                    expected = {item.result_id: item for item in page}
                    reread = (
                        self._target.compare_transition_page(
                            self._historical_transition, page, family
                        )
                        if self._historical_transition is not None
                        else self._target.get_existing_metrics(expected)
                    )
                    identifier = page[-1].result_id
                else:
                    if not prefix:
                        receipt = (
                            self._target.append_transition_diagnostics(
                                self._historical_transition, page
                            )
                            if self._historical_transition is not None
                            else self._target.save_diagnostics(page, origin="HISTORICAL")
                        )
                        created_diagnostics += len(receipt.created_ids)
                        reused_diagnostics += len(receipt.reused_ids)
                    expected = {item.diagnostic_id: item for item in page}
                    reread = (
                        self._target.compare_transition_page(
                            self._historical_transition, page, family
                        )
                        if self._historical_transition is not None
                        else self._target.get_existing_diagnostics(expected)
                    )
                    identifier = page[-1].diagnostic_id
                if self._failure_injector is not None and not prefix:
                    self._failure_injector(
                        "after_metric_page_write"
                        if family == "metrics"
                        else "after_diagnostic_page_write"
                    )
                if reread != expected:
                    raise WorkspaceV2InventoryError("analytical epoch projection differs")
                if self._historical_transition is not None:
                    compared_count, digest = self._target.transition_family_inventory(
                        self._historical_transition,
                        family,
                    )
                    if compared_count != count + len(page):
                        raise WorkspaceV2InventoryError("paired historical coverage differs")
                else:
                    for item in page:
                        digest = _fold_digest(digest, item)
                count += len(page)
                cursor = HistoricalAnalyticalCursor(
                    available_at=page[-1].available_at, identifier=identifier
                )
                if count == verified_count and (
                    digest != verified_digest or cursor != verified_cursor
                ):
                    raise WorkspaceV2InventoryError("verification cursor prefix differs")
                if (
                    prefix
                    and count == original_count
                    and (digest != original_digest or cursor != original_cursor)
                ):
                    raise WorkspaceV2InventoryError("analytical epoch confirmed prefix differs")
                if not prefix:
                    state = (
                        self._updated_state(
                            state, metric_count=count, metric_digest=digest, metric_cursor=cursor
                        )
                        if family == "metrics"
                        else self._updated_state(
                            state,
                            diagnostic_count=count,
                            diagnostic_digest=digest,
                            diagnostic_cursor=cursor,
                        )
                    )
                    self._write_state(state)
                    if self._failure_injector is not None:
                        self._failure_injector(
                            "after_metric_page_checkpoint"
                            if family == "metrics"
                            else "after_diagnostic_page_checkpoint"
                        )
                self._write_verification_checkpoint(
                    family=family, cursor=cursor, confirmed_count=count, digest=digest
                )
            target_count = (
                self._target.count_metrics(origin="HISTORICAL")
                if family == "metrics"
                else self._target.count_diagnostics(origin="HISTORICAL")
            )
            if target_count != count or count < original_count:
                raise WorkspaceV2InventoryError("analytical epoch inventory differs")
            if state.phase != "complete":
                state = self._updated_state(
                    state, phase="diagnostics" if family == "metrics" else "verification"
                )
                self._write_state(state)
            if family == "metrics" and self._failure_injector is not None:
                self._failure_injector("after_metrics_complete")
        actual = (
            state.metric_count,
            state.metric_digest,
            state.diagnostic_count,
            state.diagnostic_digest,
        )
        self._target.verify_historical_relationships()
        if self._failure_injector is not None:
            self._failure_injector("before_historical_seal")
        if self._historical_transition is not None:
            self._target.publish_transition_seal(
                self._historical_transition,
                inventory=actual,
                proof=self._verification_proof(actual),
                sealed_at=self._clock(),
            )
        else:
            self._target.seal_historical_inventory(
                source_fingerprint=self._source_fingerprint,
                metric_count=state.metric_count,
                metric_digest=state.metric_digest,
                diagnostic_count=state.diagnostic_count,
                diagnostic_digest=state.diagnostic_digest,
                sealed_at=self._clock(),
                verification_proof=self._verification_proof(actual),
            )
        self._target.verify_historical_relationships()
        if state.phase != "complete":
            state = self._updated_state(state, phase="complete")
            self._write_state(state)
        return WorkspaceV2InventorySummary(
            source_workspace_id=self._source_workspace_id,
            source_fingerprint=self._source_fingerprint,
            metric_count=state.metric_count,
            metric_digest=state.metric_digest,
            diagnostic_count=state.diagnostic_count,
            diagnostic_digest=state.diagnostic_digest,
            metric_created_count=created_metrics,
            metric_reused_count=reused_metrics,
            diagnostic_created_count=created_diagnostics,
            diagnostic_reused_count=reused_diagnostics,
            max_page_requested=self._page_limit,
            max_page_hydrated=self._max_page_hydrated,
        )

    def _verification_proof(
        self, inventory: tuple[int, str, int, str]
    ) -> WorkspaceV2MigrationVerificationProof:
        inventory_digest = _inventory_digest(inventory)
        operation_id = uuid5(
            NAMESPACE_URL,
            f"{self._policy_version}:{self._source_fingerprint}:{self._destination_root}",
        )
        destination_id = self._destination_id()
        source_id = UUID(self._source_workspace_id)
        tree_fingerprint = self._tree_fingerprint
        environment_fingerprint = self._environment_fingerprint
        physical_binding = hashlib.sha256(
            f"{self._source_fingerprint}:{self._destination_root}:{inventory_digest}".encode()
        ).hexdigest()
        return WorkspaceV2MigrationVerificationProof(
            operation_id=self._epoch or operation_id,
            snapshot_id=self._epoch or uuid5(NAMESPACE_URL, self._source_fingerprint),
            source_workspace_id=source_id,
            destination_workspace_id=destination_id,
            source_root=self._source_root,
            destination_root=self._destination_root,
            source_fingerprint=self._source_fingerprint,
            inventory_digest=inventory_digest,
            family="analytical",
            subphase="paired-models",
            reader_policy="direct-historical-readonly-v1",
            verifier_policy="paired-model-inventory-v1",
            tree_fingerprint=tree_fingerprint,
            environment_fingerprint=environment_fingerprint,
            coverage_count=inventory[0] + inventory[2],
            source_digest=inventory_digest,
            destination_digest=inventory_digest,
            ordered_unit_proof_root=inventory_digest,
            physical_binding=physical_binding,
            verified_at=self._clock(),
        )

    def _load_or_create_state(self) -> WorkspaceV2InventoryState:
        self._state_root.mkdir(parents=True, exist_ok=True)
        if self._state_path.exists() or self._state_path.is_symlink():
            return _read_state(self._state_path)
        state = WorkspaceV2InventoryState(
            source_workspace_id=self._source_workspace_id,
            source_fingerprint=self._source_fingerprint,
            policy_version=self._policy_version,
            metric_count=0,
            metric_digest=_EMPTY_DIGEST,
            diagnostic_count=0,
            diagnostic_digest=_EMPTY_DIGEST,
            phase="metrics",
            page_limit=self._page_limit,
            updated_at=self._clock(),
        )
        self._write_state(state)
        return state

    def _validate_binding(self, state: WorkspaceV2InventoryState) -> None:
        if (
            state.source_workspace_id != self._source_workspace_id
            or state.source_fingerprint != self._source_fingerprint
            or state.policy_version != self._policy_version
            or state.page_limit != self._page_limit
        ):
            raise WorkspaceV2InventoryError(
                "workspace v2 import checkpoint belongs to another source"
            )

    def _archive_historic_state(self) -> None:
        original = self._state_path.read_bytes()
        digest = hashlib.sha256(original).hexdigest()
        archive = self._state_path.with_name(
            f"workspace-v2-analytical-import-state.A.{digest}.json"
        )
        if archive.exists():
            if archive.is_symlink() or archive.read_bytes() != original:
                raise WorkspaceV2InventoryError(
                    "historic analytical checkpoint cannot be preserved"
                )
            return
        temporary = archive.with_name(f".{archive.name}.{uuid4().hex}.tmp")
        try:
            with temporary.open("xb") as handle:
                handle.write(original)
                handle.flush()
                os.fsync(handle.fileno())
            temporary.replace(archive)
            _fsync_parent(archive)
        finally:
            temporary.unlink(missing_ok=True)

    def _validate_confirmed_prefix(
        self,
        state: WorkspaceV2InventoryState,
    ) -> tuple[int, str, int, str]:
        metric_count, metric_digest = self._verify_metric_prefix(state.metric_cursor)
        diagnostic_count, diagnostic_digest = self._verify_diagnostic_prefix(
            state.diagnostic_cursor
        )
        if (metric_count, metric_digest) != (state.metric_count, state.metric_digest):
            raise WorkspaceV2InventoryError("workspace v2 metric checkpoint prefix changed")
        if (diagnostic_count, diagnostic_digest) != (
            state.diagnostic_count,
            state.diagnostic_digest,
        ):
            raise WorkspaceV2InventoryError("workspace v2 diagnostic checkpoint prefix changed")
        return metric_count, metric_digest, diagnostic_count, diagnostic_digest

    def _verify_metric_prefix(self, cursor: HistoricalAnalyticalCursor | None) -> tuple[int, str]:
        if isinstance(self._source, DirectHistoricalAnalyticalSource):
            return self._verify_direct_metric_prefix(cursor)
        count = 0
        digest = _EMPTY_DIGEST
        after: HistoricalAnalyticalCursor | None = None
        if cursor is None:
            return count, digest
        while cursor is None or after != cursor:
            page = self._source.list_metrics_page(limit=self._page_limit, after=after)
            if not page:
                break
            self._max_page_hydrated = max(self._max_page_hydrated, len(page))
            expected = {item.result_id: item for item in page}
            stored = self._target.get_existing_metrics(expected)
            if stored != expected:
                raise WorkspaceV2InventoryError("workspace v2 confirmed metric prefix differs")
            for item in page:
                digest = _fold_digest(digest, item)
                count += 1
            last = page[-1]
            after = HistoricalAnalyticalCursor(
                available_at=last.available_at, identifier=last.result_id
            )
        if cursor is not None and after != cursor:
            raise WorkspaceV2InventoryError("workspace v2 metric checkpoint cursor is absent")
        return count, digest

    def _verify_direct_metric_prefix(
        self, cursor: HistoricalAnalyticalCursor | None
    ) -> tuple[int, str]:
        after, count, digest = self._load_verification_checkpoint("metrics")
        if cursor is None:
            return count, digest
        if after == cursor:
            return count, digest
        for page in self._source.iter_metric_pages(limit=self._page_limit, after=after):
            self._max_page_hydrated = max(self._max_page_hydrated, len(page))
            expected = {item.result_id: item for item in page}
            stored = self._target.get_existing_metrics(expected)
            if stored != expected:
                raise WorkspaceV2InventoryError("workspace v2 confirmed metric prefix differs")
            for item in page:
                digest = _fold_digest(digest, item)
                count += 1
            last = page[-1]
            after = HistoricalAnalyticalCursor(
                available_at=last.available_at, identifier=last.result_id
            )
            self._write_verification_checkpoint(
                family="metrics",
                cursor=after,
                confirmed_count=count,
                digest=digest,
            )
            if after == cursor:
                return count, digest
            if after.available_at > cursor.available_at or (
                after.available_at == cursor.available_at and after.identifier > cursor.identifier
            ):
                break
        raise WorkspaceV2InventoryError("workspace v2 metric checkpoint cursor is absent")

    def _verify_diagnostic_prefix(
        self, cursor: HistoricalAnalyticalCursor | None
    ) -> tuple[int, str]:
        if isinstance(self._source, DirectHistoricalAnalyticalSource):
            return self._verify_direct_diagnostic_prefix(cursor)
        count = 0
        digest = _EMPTY_DIGEST
        after: HistoricalAnalyticalCursor | None = None
        if cursor is None:
            return count, digest
        while cursor is None or after != cursor:
            page = self._source.list_diagnostics_page(limit=self._page_limit, after=after)
            if not page:
                break
            self._max_page_hydrated = max(self._max_page_hydrated, len(page))
            expected = {item.diagnostic_id: item for item in page}
            stored = self._target.get_existing_diagnostics(expected)
            if stored != expected:
                raise WorkspaceV2InventoryError("workspace v2 confirmed diagnostic prefix differs")
            for item in page:
                digest = _fold_digest(digest, item)
                count += 1
            last = page[-1]
            after = HistoricalAnalyticalCursor(
                available_at=last.available_at, identifier=last.diagnostic_id
            )
        if cursor is not None and after != cursor:
            raise WorkspaceV2InventoryError("workspace v2 diagnostic checkpoint cursor is absent")
        return count, digest

    def _verify_direct_diagnostic_prefix(
        self, cursor: HistoricalAnalyticalCursor | None
    ) -> tuple[int, str]:
        after, count, digest = self._load_verification_checkpoint("diagnostics")
        if cursor is None:
            return count, digest
        if after == cursor:
            return count, digest
        for page in self._source.iter_diagnostic_pages(limit=self._page_limit, after=after):
            self._max_page_hydrated = max(self._max_page_hydrated, len(page))
            expected = {item.diagnostic_id: item for item in page}
            stored = self._target.get_existing_diagnostics(expected)
            if stored != expected:
                raise WorkspaceV2InventoryError("workspace v2 confirmed diagnostic prefix differs")
            for item in page:
                digest = _fold_digest(digest, item)
                count += 1
            last = page[-1]
            after = HistoricalAnalyticalCursor(
                available_at=last.available_at, identifier=last.diagnostic_id
            )
            self._write_verification_checkpoint(
                family="diagnostics",
                cursor=after,
                confirmed_count=count,
                digest=digest,
            )
            if after == cursor:
                return count, digest
            if after.available_at > cursor.available_at or (
                after.available_at == cursor.available_at and after.identifier > cursor.identifier
            ):
                break
        raise WorkspaceV2InventoryError("workspace v2 diagnostic checkpoint cursor is absent")

    def _import_metrics(
        self, state: WorkspaceV2InventoryState
    ) -> tuple[WorkspaceV2InventoryState, int, int]:
        created = reused = 0
        after = state.metric_cursor
        digest = state.metric_digest
        count = state.metric_count
        while True:
            page = self._source.list_metrics_page(limit=self._page_limit, after=after)
            if not page:
                state = self._updated_state(state, phase="diagnostics")
                self._write_state(state)
                return state, created, reused
            self._max_page_hydrated = max(self._max_page_hydrated, len(page))
            receipt = (
                self._target.append_transition_metrics(self._historical_transition, page)
                if self._historical_transition is not None
                else self._target.save_metrics(page, origin="HISTORICAL")
            )
            if self._failure_injector is not None:
                self._failure_injector("after_metric_page_write")
            for item in page:
                digest = _fold_digest(digest, item)
            count += len(page)
            last = page[-1]
            after = HistoricalAnalyticalCursor(
                available_at=last.available_at, identifier=last.result_id
            )
            state = self._updated_state(
                state,
                metric_cursor=after,
                metric_count=count,
                metric_digest=digest,
            )
            self._write_state(state)
            created += len(receipt.created_ids)
            reused += len(receipt.reused_ids)

    def _import_diagnostics(
        self, state: WorkspaceV2InventoryState
    ) -> tuple[WorkspaceV2InventoryState, int, int]:
        created = reused = 0
        after = state.diagnostic_cursor
        digest = state.diagnostic_digest
        count = state.diagnostic_count
        while True:
            page = self._source.list_diagnostics_page(limit=self._page_limit, after=after)
            if not page:
                state = self._updated_state(state, phase="verification")
                self._write_state(state)
                return state, created, reused
            self._max_page_hydrated = max(self._max_page_hydrated, len(page))
            receipt = (
                self._target.append_transition_diagnostics(self._historical_transition, page)
                if self._historical_transition is not None
                else self._target.save_diagnostics(page, origin="HISTORICAL")
            )
            if self._failure_injector is not None:
                self._failure_injector("after_diagnostic_page_write")
            for item in page:
                digest = _fold_digest(digest, item)
            count += len(page)
            last = page[-1]
            after = HistoricalAnalyticalCursor(
                available_at=last.available_at, identifier=last.diagnostic_id
            )
            state = self._updated_state(
                state,
                diagnostic_cursor=after,
                diagnostic_count=count,
                diagnostic_digest=digest,
            )
            self._write_state(state)
            created += len(receipt.created_ids)
            reused += len(receipt.reused_ids)

    def _updated_state(
        self,
        state: WorkspaceV2InventoryState,
        *,
        phase: Literal["metrics", "diagnostics", "verification", "complete"] | None = None,
        metric_cursor: HistoricalAnalyticalCursor | None = None,
        metric_count: int | None = None,
        metric_digest: str | None = None,
        diagnostic_cursor: HistoricalAnalyticalCursor | None = None,
        diagnostic_count: int | None = None,
        diagnostic_digest: str | None = None,
    ) -> WorkspaceV2InventoryState:
        return state.model_copy(
            update={
                "phase": state.phase if phase is None else phase,
                "metric_cursor": metric_cursor if metric_count is not None else state.metric_cursor,
                "metric_count": state.metric_count if metric_count is None else metric_count,
                "metric_digest": state.metric_digest if metric_digest is None else metric_digest,
                "diagnostic_cursor": (
                    diagnostic_cursor if diagnostic_count is not None else state.diagnostic_cursor
                ),
                "diagnostic_count": (
                    state.diagnostic_count if diagnostic_count is None else diagnostic_count
                ),
                "diagnostic_digest": (
                    state.diagnostic_digest if diagnostic_digest is None else diagnostic_digest
                ),
                "updated_at": self._clock(),
            }
        )

    def _write_state(self, state: WorkspaceV2InventoryState) -> None:
        if self._state_path.is_symlink():
            raise WorkspaceV2InventoryError("workspace v2 import checkpoint is a symbolic link")
        self._state_root.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(
            state.model_dump(mode="json"),
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        temporary = self._state_path.with_name(f".{self._state_path.name}.{uuid4().hex}.tmp")
        try:
            with temporary.open("x", encoding="utf-8", newline="\n") as handle:
                handle.write(f"{payload}\n")
                handle.flush()
                os.fsync(handle.fileno())
            temporary.replace(self._state_path)
            _fsync_parent(self._state_path)
        finally:
            temporary.unlink(missing_ok=True)


@contextmanager
def _reconciliation_lock(state: Path) -> Iterator[None]:
    """One lock shared by legacy and generational reconciliation operations."""
    path = state / "progress-reconciliation.lock"
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise WorkspaceV2InventoryError("reconciliation lock path is unsafe")
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    except BlockingIOError as error:
        raise WorkspaceV2InventoryError("reconciliation is already running") from error
    finally:
        os.close(descriptor)


class WorkspaceV2GenerationLedger:
    """C1 immutable certificates/events and CAS head over the existing state root."""

    def __init__(self, service: WorkspaceService, root: Path) -> None:
        self.service = service
        self.root = root
        self.identity = service.manifest(root)
        self.state = root / "state"
        self.head_path = self.state / "reconciliation-head.json"

    @staticmethod
    def _read(reference: ReconciliationFileReference) -> bytes:
        WorkspaceV2ProgressReconciler._verify(reference)
        return reference.path.read_bytes()

    def _head_digest(self) -> str | None:
        if not self.head_path.exists():
            return None
        return reconciliation_file_reference(self.head_path, "control").sha256

    def resolve(self) -> tuple[ReconciliationHead, ProgressReconciliationV2] | None:
        """Validate the pointer and complete ancestry, never choose by glob ordering."""
        digest = self._head_digest()
        if digest is None:
            return None
        document = self.head_path.read_bytes()
        seen: set[str] = set()
        current: tuple[ReconciliationHead, ProgressReconciliationV2] | None = None
        child: ProgressReconciliationV2 | None = None
        while True:
            if digest in seen:
                raise WorkspaceV2InventoryError("reconciliation head chain contains a cycle")
            seen.add(digest)
            head = ReconciliationHead.model_validate_json(document)
            if head.root != self.root or head.workspace_id != self.identity.workspace_id:
                raise WorkspaceV2InventoryError("reconciliation head workspace differs")
            archived = self.state / f"reconciliation-head.{digest}.json"
            if archived.is_symlink() or archived.read_bytes() != document:
                raise WorkspaceV2InventoryError("reconciliation head history differs")
            generation = ProgressReconciliationV2.model_validate_json(self._read(head.certificate))
            if child is not None:
                expected = (
                    child.previous_certificate
                    if child.generation_id == generation.generation_id
                    else child.previous_generation.artifact
                )
                if expected != head.certificate or child.ordinal != (
                    generation.ordinal
                    if child.generation_id == generation.generation_id
                    else generation.ordinal + 1
                ):
                    raise WorkspaceV2InventoryError(
                        "reconciliation head/certificate ancestry differs"
                    )
            if (
                generation.generation_id != head.generation_id
                or generation.ordinal != head.ordinal
                or generation.destination_root != self.root
                or generation.destination_workspace_id != self.identity.workspace_id
                or generation.invalidation != head.last_event
            ):
                raise WorkspaceV2InventoryError("reconciliation generation/head binding differs")
            event = CutInvalidation.model_validate_json(self._read(head.last_event))
            if event.destination_root != self.root or event.generation_id != head.generation_id:
                raise WorkspaceV2InventoryError("reconciliation event/head binding differs")
            previous = generation.previous_generation
            previous_document = self._read(previous.artifact)
            if previous.schema_type == "workspace-v2-progress-reconciliation-v1":
                ancestor = ProgressReconciliation.model_validate_json(previous_document)
                if ancestor.destination_workspace_id != self.identity.workspace_id:
                    raise WorkspaceV2InventoryError("reconciliation legacy ancestor differs")
            else:
                ancestor_v2 = ProgressReconciliationV2.model_validate_json(previous_document)
                if (
                    ancestor_v2.destination_workspace_id != self.identity.workspace_id
                    or ancestor_v2.ordinal != generation.ordinal - 1
                    or ancestor_v2.generation_id == generation.generation_id
                ):
                    raise WorkspaceV2InventoryError("reconciliation generation ancestry differs")
            if current is None:
                current = head, generation
            if head.previous_head_sha256 is None:
                if generation.ordinal != 1:
                    raise WorkspaceV2InventoryError("reconciliation initial ordinal differs")
                break
            digest = head.previous_head_sha256
            child = generation
            path = self.state / f"reconciliation-head.{digest}.json"
            reference = reconciliation_file_reference(path, "control")
            if reference.sha256 != digest:
                raise WorkspaceV2InventoryError("reconciliation previous head digest differs")
            document = path.read_bytes()
        return current

    def verify_relationships(self, *, admission_path: Path) -> ProgressRelationshipProof:
        """Verify W2's affected graph independently, and seal it once for C2 reuse."""
        from investment_analyst.storage.resource_policy import execute_bounded
        from investment_analyst.workspace.models import WorkspaceAccessMode
        from investment_analyst.workspace.workspace_v2_backup import _v2_writer_guard

        admission = MaintenanceResourceReceipt.model_validate_json(admission_path.read_bytes())
        self._admit(admission)
        resolved = self.resolve()
        if (
            resolved is None
            or admission.budget.operation_id != resolved[1].generation_id
            or admission.budget.profile != "verification"
            or admission.budget.reader_worker is not None
        ):
            raise WorkspaceV2InventoryError("generation relationship ownership differs")
        head, generation = resolved
        database = self.root / "storage/v2/index.duckdb"
        with _reconciliation_lock(self.state), _v2_writer_guard(self.root):
            if database.with_suffix(".duckdb.wal").exists():
                raise WorkspaceV2InventoryError("generation relationship image has new WAL")
            image = reconciliation_file_reference(database, "database")
            with self.service.open_storage(
                self.service.resolve(self.root), WorkspaceAccessMode.READ_ONLY
            ) as storage:
                connection = storage.store.connection
                CompactAnalyticalStore(connection).verify_historical_relationships(
                    read_only_units=True
                )
                counts = execute_bounded(
                    connection,
                    "SELECT (SELECT count(*) FROM raw_v2_index),"
                    "(SELECT count(*) FROM normalized_observations_v2),"
                    "(SELECT count(*) FROM workspace_metric_results_v2 WHERE origin='HISTORICAL'),"
                    "(SELECT count(*) FROM workspace_diagnostic_results_v2 "
                    "WHERE origin='HISTORICAL')",
                ).fetchone()
            if image != reconciliation_file_reference(database, "database"):
                raise WorkspaceV2InventoryError("generation relationship image changed")
        tree, environment = historical_transition_fingerprints(256, generation.verifier_policy)
        return ProgressRelationshipProof(
            operation_id=generation.generation_id,
            destination_root=self.root,
            destination_workspace_id=self.identity.workspace_id,
            reconciliation=head.certificate,
            database=image,
            wal=None,
            parent_membership=generation.parent_membership,
            tree_fingerprint=tree,
            environment_fingerprint=environment,
            verifier_module_sha256=sha256_hex(
                Path(__file__).with_name("compact_analytical_v2.py").read_bytes()
            ),
            resource_policy_sha256=admission.resource_policy_sha256,
            resource_receipt=reconciliation_file_reference(admission_path, "resource"),
            raw_count=int(counts[0]),
            observation_count=int(counts[1]),
            metric_count=int(counts[2]),
            diagnostic_count=int(counts[3]),
            verified_at=datetime.now(UTC),
        )

    def reuse_relationships(self, path: Path, *, admission_path: Path) -> ProgressRelationshipProof:
        """Reuse the sealed graph only for its unchanged image, owner and policy."""
        from investment_analyst.workspace.workspace_v2_backup import _v2_writer_guard

        admission = MaintenanceResourceReceipt.model_validate_json(admission_path.read_bytes())
        self._admit(admission)
        resolved = self.resolve()
        proof = ProgressRelationshipProof.model_validate_json(path.read_bytes())
        if resolved is None:
            raise WorkspaceV2InventoryError("generation relationship head is missing")
        head, generation = resolved
        _, environment = historical_transition_fingerprints(256, generation.verifier_policy)
        if (
            proof.operation_id != generation.generation_id
            or admission.budget.operation_id != generation.generation_id
            or proof.destination_root != self.root
            or proof.destination_workspace_id != self.identity.workspace_id
            or proof.reconciliation != head.certificate
            or proof.parent_membership != generation.parent_membership
            or proof.environment_fingerprint != environment
            or proof.wal is not None
            or proof.database.path != self.root / "storage/v2/index.duckdb"
            or proof.verifier_module_sha256
            != sha256_hex(Path(__file__).with_name("compact_analytical_v2.py").read_bytes())
            or proof.resource_policy_sha256 != admission.resource_policy_sha256
        ):
            raise WorkspaceV2InventoryError("generation relationship reuse binding differs")
        with _reconciliation_lock(self.state), _v2_writer_guard(self.root):
            if (self.root / "storage/v2/index.duckdb.wal").exists():
                raise WorkspaceV2InventoryError("generation relationship image has new WAL")
            for reference in (
                proof.database,
                proof.reconciliation,
                proof.parent_membership,
                proof.resource_receipt,
            ):
                WorkspaceV2ProgressReconciler._verify(reference)
        return proof

    def certify_baseline(
        self,
        *,
        content_path: Path,
        raw_coverage_path: Path,
        relationships_path: Path,
        admission_path: Path,
        code: tuple[ReconciliationFileReference, ...],
    ) -> ReconciliationFileReference:
        """Publish W2 only after corrected content, graph and complete raw coverage."""
        from investment_analyst.storage.resource_policy import execute_bounded
        from investment_analyst.workspace.models import WorkspaceAccessMode
        from investment_analyst.workspace.workspace_v2_backup import _v2_writer_guard

        admission = MaintenanceResourceReceipt.model_validate_json(admission_path.read_bytes())
        self._admit(admission)
        resolved = self.resolve()
        if resolved is None or resolved[1].generation_id != admission.budget.operation_id:
            raise WorkspaceV2InventoryError("baseline generation owner differs")
        head, prepared = resolved
        if prepared.phase != "PREPARED":
            if (
                prepared.core_proof != reconciliation_file_reference(content_path, "control")
                or reconciliation_file_reference(raw_coverage_path, "control")
                not in prepared.corrective_proofs
                or reconciliation_file_reference(relationships_path, "control")
                not in prepared.corrective_proofs
                or code != prepared.code
            ):
                raise WorkspaceV2InventoryError("certified baseline retry bindings differ")
            for reference in (prepared.database, *prepared.corrective_proofs, *prepared.code):
                WorkspaceV2ProgressReconciler._verify(reference)
            return head.certificate
        content_ref = reconciliation_file_reference(content_path, "control")
        raw_ref = reconciliation_file_reference(raw_coverage_path, "control")
        relationship_ref = reconciliation_file_reference(relationships_path, "control")
        content = GenerationContentProof.model_validate_json(self._read(content_ref))
        raw = GenerationRawCoverageProof.model_validate_json(self._read(raw_ref))
        relationships = self.reuse_relationships(relationships_path, admission_path=admission_path)
        if (
            content.generation_id != prepared.generation_id
            or content.baseline_certificate != prepared.verified_w
            or content.workspace_id != self.identity.workspace_id
            or content.database != prepared.database
            or raw.database != content.database
            or raw.content_proof != content_ref
            or raw.generation_id != prepared.generation_id
            or raw.destination_root != self.root
            or raw.workspace_id != self.identity.workspace_id
            or raw.raw_count != content.raw_count
            or raw.witnessed_files + raw.hashed_files != raw.raw_count
            or (
                relationships.raw_count,
                relationships.observation_count,
                relationships.metric_count,
                relationships.diagnostic_count,
            )
            != (
                content.raw_count,
                content.observation_count,
                content.metric_count,
                content.diagnostic_count,
            )
            or content.verifier_sha256
            != sha256_hex(inspect.getsource(verify_generation_contents).encode())
            or raw.verifier_sha256
            != sha256_hex(inspect.getsource(verify_generation_raw_files).encode())
            or content.policy_sha256 != admission.resource_policy_sha256
            or raw.policy_sha256 != admission.resource_policy_sha256
            or not code
            or any(ref.role != "code" for ref in code)
        ):
            raise WorkspaceV2InventoryError("corrective baseline proof binding differs")
        for reference in (
            content.resource_receipt,
            raw.resource_receipt,
            raw.link_witness,
            prepared.parent_membership,
            *code,
        ):
            self._read(reference)
        expected_head = self._head_digest()
        delta_path = self.state / f"generation-delta-membership.{prepared.generation_id}.jsonl"
        with _v2_writer_guard(self.root):
            with _reconciliation_lock(self.state):
                if self._head_digest() != expected_head:
                    raise WorkspaceV2InventoryError("baseline head changed before verification")
                WorkspaceV2ProgressReconciler._verify(content.database)
                with (
                    self.service.open_storage(
                        self.service.resolve(self.root), WorkspaceAccessMode.READ_ONLY
                    ) as storage,
                    delta_path.open("a+b") as delta,
                ):
                    connection = storage.store.connection
                    execute_bounded(
                        connection,
                        "CREATE TEMP TABLE _generation_b AS SELECT "
                        "json_extract_string(json,'$[0]') AS family,"
                        "try_cast(json_extract_string(json,'$[1]') AS UUID) identifier,"
                        "json_extract_string(json,'$[2]') digest "
                        "FROM read_json_objects(?,format='newline_delimited',"
                        "maximum_object_size=1024)",
                        [str(prepared.parent_membership.path)],
                    )
                    invalid = execute_bounded(
                        connection,
                        "SELECT 1 FROM _generation_b WHERE family NOT IN ('metrics','diagnostics') "
                        "OR family IS NULL OR identifier IS NULL OR digest IS NULL "
                        "OR NOT regexp_full_match(digest,'[0-9a-f]{64}') LIMIT 1",
                    ).fetchone()
                    duplicate = execute_bounded(
                        connection,
                        "SELECT 1 FROM _generation_b GROUP BY family,identifier "
                        "HAVING count(*)<>1 LIMIT 1",
                    ).fetchone()
                    parent_counts = execute_bounded(
                        connection,
                        "SELECT count(*) FILTER(WHERE family='metrics'),"
                        "count(*) FILTER(WHERE family='diagnostics') FROM _generation_b",
                    ).fetchone()
                    if (
                        invalid is not None
                        or duplicate is not None
                        or parent_counts
                        != (
                            prepared.parent_seal.metric_count,
                            prepared.parent_seal.diagnostic_count,
                        )
                    ):
                        raise WorkspaceV2InventoryError("baseline original B membership differs")
                    delta.seek(0)
                    for family, table, key, expected in (
                        (
                            "metrics",
                            "workspace_metric_results_v2",
                            "result_id",
                            content.metric_count,
                        ),
                        (
                            "diagnostics",
                            "workspace_diagnostic_results_v2",
                            "diagnostic_id",
                            content.diagnostic_count,
                        ),
                    ):
                        missing = execute_bounded(
                            connection,
                            f"SELECT 1 FROM _generation_b b LEFT JOIN {table} t "
                            f"ON t.{key}=b.identifier WHERE b.family=? AND "
                            f"(t.{key} IS NULL OR t.origin<>'HISTORICAL' OR "
                            "t.checksum_sha256<>b.digest) LIMIT 1",
                            [family],
                        ).fetchone()
                        if missing is not None:
                            raise WorkspaceV2InventoryError("baseline lost or changed original B")
                        execute_bounded(
                            connection,
                            f"SELECT t.{key},t.checksum_sha256 FROM {table} t "
                            "WHERE t.origin='HISTORICAL' AND NOT EXISTS "
                            f"(SELECT 1 FROM _generation_b b WHERE b.identifier=t.{key} "
                            f"AND b.family=?) ORDER BY t.{key}",
                            [family],
                        )
                        count = 0
                        while rows := connection.fetchmany(256):
                            document = b"".join(
                                _json_bytes([family, str(row[0]), str(row[1])]) + b"\n"
                                for row in rows
                            )
                            previous = delta.read(len(document))
                            if not document.startswith(previous):
                                raise WorkspaceV2InventoryError(
                                    "baseline delta membership prefix differs"
                                )
                            if len(previous) != len(document):
                                delta.write(document[len(previous) :])
                                delta.flush()
                                os.fsync(delta.fileno())
                            count += len(rows)
                        base = parent_counts[0 if family == "metrics" else 1]
                        if base + count != expected:
                            raise WorkspaceV2InventoryError(
                                "baseline B plus delta coverage differs"
                            )
                    if delta.read(1):
                        raise WorkspaceV2InventoryError("baseline delta membership has extra bytes")
                    execute_bounded(connection, "DROP TABLE _generation_b")
                WorkspaceV2ProgressReconciler._verify(content.database)
                delta_ref = reconciliation_file_reference(delta_path, "inventory")
                dataset = sha256_hex(
                    _json_bytes(
                        [
                            content.raw_count,
                            content.raw_digest,
                            content.observation_count,
                            content.observation_digest,
                            content.metric_count,
                            content.metric_digest,
                            content.diagnostic_count,
                            content.diagnostic_digest,
                            prepared.parent_membership.sha256,
                            delta_ref.sha256,
                        ]
                    )
                )
                inventory = RecoveredProgressInventory(
                    raw_count=content.raw_count,
                    observation_count=content.observation_count,
                    metric_count=content.metric_count,
                    diagnostic_count=content.diagnostic_count,
                    metric_digest=content.metric_digest,
                    diagnostic_digest=content.diagnostic_digest,
                    dataset_sha256=dataset,
                    delta_membership=delta_ref,
                    relationships=relationship_ref,
                    verified_at=raw.verified_at,
                )
                tree, environment = historical_transition_fingerprints(
                    256, prepared.verifier_policy
                )
                baseline = ProgressReconciliationV2.model_validate(
                    {
                        **prepared.model_dump(),
                        "phase": "BASELINE_VERIFIED",
                        "previous_certificate": head.certificate,
                        "inventory": inventory,
                        "core_proof": content_ref,
                        "corrective_proofs": (
                            content_ref,
                            raw_ref,
                            relationship_ref,
                            raw.link_witness,
                        ),
                        "code": code,
                        "tree_fingerprint": tree,
                        "environment_fingerprint": environment,
                        "resource_receipt": reconciliation_file_reference(
                            admission_path, "resource"
                        ),
                        "recorded_at": raw.verified_at,
                    }
                )
                intent = self.state / f"generation-baseline.{prepared.generation_id}.intent.json"
                if intent.exists():
                    prior = ProgressReconciliationV2.model_validate_json(intent.read_bytes())
                    if (
                        baseline.model_copy(update={"resource_receipt": prior.resource_receipt})
                        != prior
                    ):
                        raise WorkspaceV2InventoryError("baseline publication intent changed")
                    self._read(prior.resource_receipt)
                    baseline = prior
                else:
                    WorkspaceV2ProgressReconciler._write_once(
                        intent, canonical_json_bytes(baseline) + b"\n"
                    )
            return self.publish(baseline, expected_head_sha256=expected_head, admission=admission)

    def attempt_invalidated(self, attempt_id: UUID) -> bool:
        """An event revokes activation even if a crash prevented publishing a head."""
        found = False
        for path in self.state.glob(f"cut-invalidation.{attempt_id}.*.json"):
            if path.is_symlink():
                raise WorkspaceV2InventoryError("invalidation event path is unsafe")
            event = CutInvalidation.model_validate_json(path.read_bytes())
            if (
                event.attempt_id != attempt_id
                or event.destination_root != self.root
                or event.destination_workspace_id != self.identity.workspace_id
                or not path.name.endswith(f".{sha256_hex(path.read_bytes())}.json")
            ):
                raise WorkspaceV2InventoryError("invalidation event binding differs")
            if event.previous_event is not None:
                previous = CutInvalidation.model_validate_json(self._read(event.previous_event))
                if previous.attempt_id != attempt_id:
                    raise WorkspaceV2InventoryError("invalidation event ancestry differs")
            found = True
        return found

    def _admit(self, admission: MaintenanceResourceReceipt) -> None:
        require_maintenance_admission(admission)
        if self.root / "storage/v2/index.duckdb" not in {
            item.database for item in admission.budget.assignments
        }:
            raise WorkspaceV2InventoryError("generation requires its destination allocation")

    def invalidate(
        self, event: CutInvalidation, *, admission: MaintenanceResourceReceipt
    ) -> ReconciliationFileReference:
        """Publish only with real destination locks, absent owner and no v2 activation."""
        from investment_analyst.application.local_release import LocalReleaseService
        from investment_analyst.workspace.workspace_v2_backup import _v2_writer_guard

        self._admit(admission)
        with _reconciliation_lock(self.state), _v2_writer_guard(self.root):
            if event.expected_head_sha256 != self._head_digest():
                raise WorkspaceV2InventoryError("invalidation expected head differs")
            if (
                event.destination_root != self.root
                or event.destination_workspace_id != self.identity.workspace_id
            ):
                raise WorkspaceV2InventoryError("invalidation destination differs")
            if event.wal is None and (self.root / "storage/v2/index.duckdb.wal").exists():
                raise WorkspaceV2InventoryError("invalidation found an unrecorded WAL")
            if event.owner.pid is not None and self._owner_alive(event):
                raise WorkspaceV2InventoryError("invalidation owner is still alive")
            bindings = LocalReleaseService().load_workspace_bindings()
            if any(
                binding is not None and binding.workspace_format_version == 2
                for binding in (bindings.current, getattr(bindings, "previous", None))
            ):
                raise WorkspaceV2InventoryError(
                    "preactivation invalidation cannot follow v2 binding"
                )
            for reference in (
                event.certificate.artifact,
                event.reservation,
                event.database,
                event.write_generation,
                event.owner_absence,
                event.exclusivity,
                event.no_activation,
                event.wal,
                event.historical_seal,
                event.historical_receipt,
                event.previous_event,
            ):
                if reference is not None:
                    WorkspaceV2ProgressReconciler._verify(reference)
            if event.certificate.schema_type == "workspace-v2-progress-reconciliation-v1":
                from investment_analyst.workspace.workspace_v2_migration import (
                    _SuccessorReservation,
                )

                certificate = ProgressReconciliation.model_validate_json(
                    self._read(event.certificate.artifact)
                )
                reservation = _SuccessorReservation.model_validate_json(
                    self._read(event.reservation)
                )
                pending = certificate.reserved_successor is None
                if pending:
                    # The immutable reservation can precede publication of the
                    # reserved certificate/head. Anchor it to the actual legacy
                    # baseline, without publishing a dead session or releasing
                    # its reservation by hand.
                    legacy = list(self.state.glob("progress-reconciliation.*.json"))
                    if len(legacy) != 1 or legacy[0].is_symlink():
                        raise WorkspaceV2InventoryError("pending legacy reservation is ambiguous")
                    current = ProgressReconciliation.model_validate_json(legacy[0].read_bytes())
                    resolved = self.resolve()
                    verified_ancestor = (
                        resolved is not None
                        and resolved[1].verified_w == event.certificate.artifact
                    )
                    transient = {
                        "phase",
                        "updated_at",
                        "reserved_successor_id",
                        "reserved_successor",
                        "coverage",
                    }
                    if (
                        certificate.phase != "BASELINE_VERIFIED"
                        or certificate.inventory is None
                        or reservation.baseline != event.certificate.artifact
                        or reservation.baseline_dataset_sha256
                        != certificate.inventory.dataset_sha256
                        or (
                            not verified_ancestor
                            and certificate.model_dump(exclude=transient)
                            != current.model_dump(exclude=transient)
                        )
                    ):
                        raise WorkspaceV2InventoryError("pending legacy baseline differs")
                if (
                    certificate.operation_id != reservation.reconciliation_id
                    or (not pending and certificate.reserved_successor != reservation.successor)
                    or certificate.source_root != reservation.successor.source_root
                    or certificate.source_workspace_id != reservation.successor.workspace_id
                    or certificate.source_created_at != reservation.successor.created_at
                    or certificate.destination_root != self.root
                    or certificate.destination_workspace_id != self.identity.workspace_id
                    or reservation.successor.operation_id != event.attempt_id
                    or reservation.source_session != event.owner.source_session
                    or reservation.successor.source_root != event.source_root
                    or reservation.successor.workspace_id != event.source_workspace_id
                ):
                    raise WorkspaceV2InventoryError("invalidation reservation identity differs")
            else:
                from investment_analyst.workspace.workspace_v2_migration import (
                    _SuccessorReservation,
                )

                certificate_v2 = ProgressReconciliationV2.model_validate_json(
                    self._read(event.certificate.artifact)
                )
                reservation_v2 = _SuccessorReservation.model_validate_json(
                    self._read(event.reservation)
                )
                old_admission = MaintenanceResourceReceipt.model_validate_json(
                    self._read(reservation_v2.resource_receipt)
                )
                pending_v2 = certificate_v2.reserved_successor is None
                resolved = self.resolve()
                if resolved is None or resolved[0].certificate != event.certificate.artifact:
                    raise WorkspaceV2InventoryError("generational invalidation head differs")
                if pending_v2 and (
                    certificate_v2.phase != "BASELINE_VERIFIED"
                    or reservation_v2.baseline != event.certificate.artifact
                    or certificate_v2.inventory is None
                    or reservation_v2.baseline_dataset_sha256
                    != certificate_v2.inventory.dataset_sha256
                ):
                    raise WorkspaceV2InventoryError("pending generational baseline differs")
                if (
                    certificate_v2.generation_id != reservation_v2.reconciliation_id
                    or certificate_v2.inventory is None
                    or reservation_v2.baseline_dataset_sha256
                    != certificate_v2.inventory.dataset_sha256
                    or event.reservation.path
                    != self.state / f"progress-successor-reservation.{event.attempt_id}.json"
                    or certificate_v2.destination_root != self.root
                    or certificate_v2.destination_workspace_id != self.identity.workspace_id
                    or certificate_v2.source_root != reservation_v2.successor.source_root
                    or certificate_v2.source_workspace_id != reservation_v2.successor.workspace_id
                    or certificate_v2.source_created_at != reservation_v2.successor.created_at
                    or reservation_v2.successor.operation_id != event.attempt_id
                    or reservation_v2.successor.source_root != event.source_root
                    or reservation_v2.successor.workspace_id != event.source_workspace_id
                    or reservation_v2.source_session != event.owner.source_session
                    or old_admission.budget.operation_id != event.attempt_id
                    or (
                        old_admission.pid,
                        old_admission.process_start_ticks,
                        old_admission.boot_id,
                        old_admission.cgroup_path,
                    )
                    != (
                        event.owner.pid,
                        event.owner.start_ticks,
                        event.owner.boot_id,
                        event.owner.cgroup,
                    )
                    or (
                        not pending_v2
                        and (
                            certificate_v2.reserved_successor != reservation_v2.successor
                            or certificate_v2.successor_owner != event.owner
                            or certificate_v2.successor_admission != reservation_v2.resource_receipt
                        )
                    )
                ):
                    raise WorkspaceV2InventoryError("invalidation generational reservation differs")
            if event.previous_event is not None:
                previous = CutInvalidation.model_validate_json(self._read(event.previous_event))
                if (
                    previous.attempt_id != event.attempt_id
                    or previous.generation_id != event.generation_id
                    or previous.destination_workspace_id != event.destination_workspace_id
                ):
                    raise WorkspaceV2InventoryError("invalidation event chain differs")
            document = canonical_json_bytes(event) + b"\n"
            path = self.state / (f"cut-invalidation.{event.attempt_id}.{sha256_hex(document)}.json")
            WorkspaceV2ProgressReconciler._write_once(path, document)
            return reconciliation_file_reference(path, "control")

    @staticmethod
    def _owner_alive(event: CutInvalidation) -> bool:
        owner = event.owner
        if owner.boot_id != UUID(Path("/proc/sys/kernel/random/boot_id").read_text().strip()):
            return False
        path = Path("/proc") / str(owner.pid) / "stat"
        try:
            ticks = int(path.read_text().rsplit(") ", 1)[1].split()[19])
        except FileNotFoundError:
            return False
        return ticks == owner.start_ticks

    def publish(
        self,
        generation: ProgressReconciliationV2,
        *,
        expected_head_sha256: str | None,
        admission: MaintenanceResourceReceipt,
    ) -> ReconciliationFileReference:
        """Publish immutable bytes before a compare-and-swap of the sole mutable head."""
        self._admit(admission)
        with _reconciliation_lock(self.state):
            if self._head_digest() != expected_head_sha256:
                raise WorkspaceV2InventoryError("reconciliation head compare-and-swap failed")
            current = self.resolve()
            if (
                generation.destination_root != self.root
                or generation.destination_workspace_id != self.identity.workspace_id
            ):
                raise WorkspaceV2InventoryError("generation destination differs")
            if current is None:
                if generation.ordinal != 1 or generation.phase != "PREPARED":
                    raise WorkspaceV2InventoryError("initial generation must be prepared")
                legacy = list(self.state.glob("progress-reconciliation.*.json"))
                if len(legacy) != 1:
                    raise WorkspaceV2InventoryError("legacy reconciliation ownership is ambiguous")
                if legacy[0].read_bytes() != self._read(generation.previous_generation.artifact):
                    raise WorkspaceV2InventoryError(
                        "legacy generation is not the unique predecessor"
                    )
            else:
                head, previous = current
                if generation.generation_id == previous.generation_id:
                    phases = {"PREPARED": 0, "BASELINE_VERIFIED": 1, "SUCCESSOR_BOUND": 2}
                    if (
                        generation.ordinal != previous.ordinal
                        or generation.previous_certificate != head.certificate
                        or generation.previous_generation != previous.previous_generation
                        or phases[generation.phase] < phases[previous.phase]
                        or phases[generation.phase] > phases[previous.phase] + 1
                        or (
                            previous.reserved_successor is not None
                            and generation.reserved_successor != previous.reserved_successor
                        )
                    ):
                        raise WorkspaceV2InventoryError("generation transition differs")
                    if previous.phase != "PREPARED":
                        allowed = {
                            "phase",
                            "previous_certificate",
                            "recorded_at",
                            "reserved_successor",
                            "successor_owner",
                            "successor_admission",
                            "coverage",
                        }
                        old_values, new_values = previous.model_dump(), generation.model_dump()
                        if any(old_values[k] != new_values[k] for k in old_values.keys() - allowed):
                            raise WorkspaceV2InventoryError(
                                "certified baseline cannot be rewritten"
                            )
                        if previous.successor_owner is not None and (
                            generation.successor_owner != previous.successor_owner
                            or generation.successor_admission != previous.successor_admission
                        ):
                            raise WorkspaceV2InventoryError("reserved source session cannot change")
                    else:
                        mutable = {
                            "phase",
                            "previous_certificate",
                            "recorded_at",
                            "inventory",
                            "core_proof",
                            "corrective_proofs",
                            "code",
                            "tree_fingerprint",
                            "environment_fingerprint",
                            "resource_receipt",
                        }
                        old_values, new_values = previous.model_dump(), generation.model_dump()
                        if any(old_values[k] != new_values[k] for k in old_values.keys() - mutable):
                            raise WorkspaceV2InventoryError(
                                "prepared generation identity cannot change"
                            )
                elif (
                    generation.ordinal != previous.ordinal + 1
                    or generation.previous_generation.artifact != head.certificate
                    or generation.phase != "PREPARED"
                ):
                    raise WorkspaceV2InventoryError("new generation predecessor differs")
            if generation.reserved_successor is not None:
                from investment_analyst.workspace.workspace_v2_migration import (
                    _live_quiescent_source,
                )

                cut, owner = generation.reserved_successor, generation.successor_owner
                session = _live_quiescent_source(generation.source_root, cut)
                assert owner is not None and generation.successor_admission is not None
                native_admission = MaintenanceResourceReceipt.model_validate_json(
                    self._read(generation.successor_admission)
                )
                require_maintenance_admission(native_admission)
                if (
                    session is None
                    or owner.source_session != session.writer_generation
                    or owner.pid != native_admission.pid
                    or owner.start_ticks != native_admission.process_start_ticks
                    or owner.boot_id != native_admission.boot_id
                    or owner.cgroup != native_admission.cgroup_path
                    or self.attempt_invalidated(cut.operation_id)
                ):
                    raise WorkspaceV2InventoryError(
                        "generation requires its real live source session"
                    )
            event = CutInvalidation.model_validate_json(self._read(generation.invalidation))
            if event.generation_id != generation.generation_id or event.expected_head_sha256 != (
                expected_head_sha256
                if current is None or current[1].generation_id != generation.generation_id
                else CutInvalidation.model_validate_json(
                    self._read(current[1].invalidation)
                ).expected_head_sha256
            ):
                raise WorkspaceV2InventoryError("generation invalidation differs")
            if (
                event.reservation != generation.previous_reservation
                or event.certificate != generation.previous_generation
            ):
                raise WorkspaceV2InventoryError("generation ancestor invalidation differs")
            for reference in (
                generation.previous_generation.artifact,
                generation.previous_certificate,
                generation.predecessor,
                generation.previous_attempt,
                generation.previous_reservation,
                generation.verified_w,
                *generation.previous_receipts,
                *generation.code,
                generation.core_proof,
                *generation.corrective_proofs,
                generation.resource_receipt,
            ):
                if reference is not None:
                    self._read(reference)
            document = canonical_json_bytes(generation) + b"\n"
            target = self.state / (
                f"progress-generation.{generation.generation_id}.{sha256_hex(document)}.json"
            )
            WorkspaceV2ProgressReconciler._write_once(target, document)
            reference = reconciliation_file_reference(target, "control")
            head = ReconciliationHead(
                root=self.root,
                workspace_id=self.identity.workspace_id,
                generation_id=generation.generation_id,
                ordinal=generation.ordinal,
                certificate=reference,
                last_event=generation.invalidation,
                previous_head_sha256=expected_head_sha256,
                recorded_at=generation.recorded_at,
            )
            document = canonical_json_bytes(head) + b"\n"
            digest = sha256_hex(document)
            for path in self.state.glob("reconciliation-head.*.json"):
                existing = ReconciliationHead.model_validate_json(path.read_bytes())
                if (
                    existing.previous_head_sha256 == expected_head_sha256
                    and path.read_bytes() != document
                ):
                    raise WorkspaceV2InventoryError("reconciliation head fork detected")
            WorkspaceV2ProgressReconciler._write_once(
                self.state / f"reconciliation-head.{digest}.json", document
            )
            temporary = self.head_path.with_name(f".reconciliation-head.{uuid4().hex}.tmp")
            try:
                WorkspaceV2ProgressReconciler._write_once(temporary, document)
                os.replace(temporary, self.head_path)
                _fsync_parent(self.head_path)
            finally:
                temporary.unlink(missing_ok=True)
            return reference


class GenerationRawCoverageProof(ContractModel):
    """C2 corrective evidence for every raw path of the closed current image."""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")
    generation_id: UUID
    destination_root: Path
    workspace_id: UUID
    database: ReconciliationFileReference
    content_proof: ReconciliationFileReference
    link_witness: ReconciliationFileReference
    resource_receipt: ReconciliationFileReference
    raw_count: int = Field(ge=0)
    raw_bytes: int = Field(ge=0)
    witnessed_files: int = Field(ge=0)
    hashed_files: int = Field(ge=0)
    file_metadata_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    verifier_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    policy_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    verified_at: UTCDateTime


def verify_generation_raw_files(
    service: WorkspaceService,
    *,
    destination: Path,
    content_path: Path,
    link_witness: ReconciliationFileReference,
    admission_path: Path,
) -> GenerationRawCoverageProof:
    """Cover index/path membership completely, reusing only exact inode witnesses."""
    verifier_sha256 = sha256_hex(inspect.getsource(verify_generation_raw_files).encode())
    from investment_analyst.storage.resource_policy import execute_bounded
    from investment_analyst.workspace.models import WorkspaceAccessMode
    from investment_analyst.workspace.workspace_v2_backup import _v2_writer_guard
    from investment_analyst.workspace.workspace_v2_physical_delta import (
        ImmutableLinkHeader,
        ImmutableLinkIdentity,
        _iter_retained_link_witness,
        _safe_file,
        _source_files,
        _verify_file,
    )

    admission = MaintenanceResourceReceipt.model_validate_json(admission_path.read_bytes())
    execution = require_maintenance_admission(admission)
    content_ref = reconciliation_file_reference(content_path, "control")
    content = GenerationContentProof.model_validate_json(content_path.read_bytes())
    identity = service.manifest(destination)
    WorkspaceV2ProgressReconciler._verify(link_witness)
    header = ImmutableLinkHeader.model_validate_json(
        _safe_file(link_witness.path.parent, "header.json").read_bytes()
    )
    if (
        content.generation_id != admission.budget.operation_id
        or content.workspace_id != identity.workspace_id
        or content.database.path != destination / "storage/v2/index.duckdb"
        or content.policy_sha256 != admission.resource_policy_sha256
        or content.verifier_sha256
        != sha256_hex(inspect.getsource(verify_generation_contents).encode())
        or header.operation_id != content.generation_id
        or header.retained_root != destination
        or header.retained_workspace_id != identity.workspace_id
    ):
        raise WorkspaceV2InventoryError("generation raw coverage content binding differs")
    count = total = witnessed = hashed = 0
    metadata = hashlib.sha256()
    staging = destination / "storage/v2/raw"
    with _reconciliation_lock(destination / "state"), _v2_writer_guard(destination):
        if (destination / "storage/v2/index.duckdb.wal").exists():
            raise WorkspaceV2InventoryError("generation raw coverage image has WAL")
        WorkspaceV2ProgressReconciler._verify(content.database)
        witnesses = (
            (item, stamp)
            for item, stamp in _iter_retained_link_witness(link_witness)
            if item.path.startswith("storage/v2/raw/raw/sha256/")
        )
        witness = next(witnesses, None)
        actual = iter(_source_files(staging))
        with service.open_storage(
            service.resolve(destination), WorkspaceAccessMode.READ_ONLY
        ) as storage:
            connection = storage.store.connection
            execute_bounded(
                connection,
                "SELECT relative_path,checksum_sha256 FROM raw_v2_index ORDER BY relative_path",
            )
            while rows := connection.fetchmany(256):
                for row in rows:
                    relative, checksum = str(row[0]), str(row[1])
                    full = "storage/v2/raw/" + relative
                    canonical = f"raw/sha256/{checksum[:2]}/{checksum[2:4]}/{checksum}"
                    if relative != canonical or next(actual, None) != relative:
                        raise WorkspaceV2InventoryError(
                            "generation raw files/index membership differs"
                        )
                    path = _safe_file(staging, relative)
                    before = ImmutableLinkIdentity.observe(path)
                    if witness is not None and witness[0].path < full:
                        raise WorkspaceV2InventoryError("generation witnessed raw is unindexed")
                    if witness is not None and witness[0].path == full:
                        item, stamp = witness
                        if (
                            before != stamp
                            or item.sha256 != checksum
                            or item.size_bytes != before.size
                        ):
                            raise WorkspaceV2InventoryError(
                                "generation raw witness identity differs"
                            )
                        witnessed += 1
                        witness = next(witnesses, None)
                    else:
                        _verify_file(path, before.size, checksum)
                        if ImmutableLinkIdentity.observe(path) != before:
                            raise WorkspaceV2InventoryError(
                                "generation raw changed during verification"
                            )
                        hashed += 1
                    metadata.update(
                        _json_bytes([relative, checksum, before.model_dump(mode="json")])
                    )
                    count += 1
                    total += before.size
                execution.record_measurement("generation_raw_files", count)
        if next(actual, None) is not None or witness is not None or count != content.raw_count:
            raise WorkspaceV2InventoryError("generation raw coverage has missing or extra members")
        WorkspaceV2ProgressReconciler._verify(content.database)
    return GenerationRawCoverageProof(
        generation_id=content.generation_id,
        destination_root=destination,
        workspace_id=identity.workspace_id,
        database=content.database,
        content_proof=content_ref,
        link_witness=link_witness,
        resource_receipt=reconciliation_file_reference(admission_path, "resource"),
        raw_count=count,
        raw_bytes=total,
        witnessed_files=witnessed,
        hashed_files=hashed,
        file_metadata_sha256=metadata.hexdigest(),
        verifier_sha256=verifier_sha256,
        policy_sha256=admission.resource_policy_sha256,
        verified_at=datetime.now(UTC),
    )


def verify_generation_contents(
    service: WorkspaceService,
    *,
    generation_id: UUID,
    destination: Path,
    baseline_root: Path,
    baseline_certificate: ReconciliationFileReference,
    baseline_database_sha256: str,
    admission_path: Path,
    relationships_path: Path | None = None,
) -> GenerationContentProof:
    """C2: compare persisted W rows/fragments once and type-check every addition.

    W is an independently recovered, byte-identified database, not a new clone.
    EXCEPT ALL preserves complete row coverage, including additions outside any
    old cursor. It does not join metrics through shared segment memberships.
    This proves destination content, never source equivalence for historical F2.
    """
    verifier_sha256 = sha256_hex(inspect.getsource(verify_generation_contents).encode())
    from investment_analyst.storage.compact_analytical_v2 import _COMPACT_TABLES
    from investment_analyst.storage.resource_policy import connect_duckdb, execute_bounded
    from investment_analyst.storage.serialization import canonical_json_digest
    from investment_analyst.workspace.models import WorkspaceAccessMode
    from investment_analyst.workspace.workspace_v2_backup import _v2_writer_guard
    from investment_analyst.workspace.workspace_v2_migration import _observation_canonical_sql

    admission = MaintenanceResourceReceipt.model_validate_json(admission_path.read_bytes())
    execution = require_maintenance_admission(admission)
    database = destination / "storage/v2/index.duckdb"
    baseline_database = baseline_root / "storage/v2/index.duckdb"
    if (
        admission.budget.operation_id != generation_id
        or admission.budget.profile != "verification"
        or admission.budget.reader_worker is not None
        or {item.database for item in admission.budget.assignments} != {database, baseline_database}
        or destination == baseline_root
    ):
        raise WorkspaceV2InventoryError("corrective generation requires both admitted databases")
    identity, baseline_identity = service.manifest(destination), service.manifest(baseline_root)
    if identity.workspace_id != baseline_identity.workspace_id:
        raise WorkspaceV2InventoryError("corrective baseline workspace UUID differs")
    WorkspaceV2ProgressReconciler._verify(baseline_certificate)
    verified_w = ProgressReconciliation.model_validate_json(baseline_certificate.path.read_bytes())
    if (
        verified_w.phase != "BASELINE_VERIFIED"
        or verified_w.inventory is None
        or verified_w.destination_workspace_id != identity.workspace_id
        or verified_w.database_after is None
        or verified_w.database_after.sha256 != baseline_database_sha256
    ):
        raise WorkspaceV2InventoryError("corrective baseline certificate is not the preserved W")
    with (
        _reconciliation_lock(destination / "state"),
        _v2_writer_guard(destination),
        _v2_writer_guard(baseline_root),
    ):
        if any(p.with_suffix(".duckdb.wal").exists() for p in (database, baseline_database)):
            raise WorkspaceV2InventoryError(
                "corrective generation requires closed recovered images"
            )
        baseline_reference = reconciliation_file_reference(baseline_database, "database")
        if baseline_reference.sha256 != baseline_database_sha256:
            raise WorkspaceV2InventoryError("corrective W image differs from its original proof")
        database_reference = reconciliation_file_reference(database, "database")
        preserved: list[tuple[str, int]] = []
        validated: list[tuple[str, int]] = []
        with service.open_storage(
            service.resolve(destination), WorkspaceAccessMode.READ_ONLY
        ) as store:
            connection = store.store.connection
            # The attachment shares this one admitted native instance. It does
            # not get another unbounded opener, thread pool or spill quota.
            escaped = baseline_database.as_posix().replace("'", "''")
            execute_bounded(connection, f"ATTACH '{escaped}' AS _verified_w (READ_ONLY)")
            tables = (
                "raw_v2_index",
                "workspace_raw_json_projections_v2",
                "normalized_observations_v2",
                *(
                    name
                    for name in sorted(_COMPACT_TABLES)
                    if name != "workspace_v2_historical_seal"
                ),
            )
            identity_columns = {
                "raw_v2_index": "record_id",
                "workspace_raw_json_projections_v2": "record_id",
                "normalized_observations_v2": "observation_id",
                "workspace_analytical_content_v2": "content_id",
                "workspace_analytical_sequences_v2": "sequence_id",
                "workspace_analytical_segments_v2": "segment_id",
                "workspace_analytical_sequence_segments_v2": "sequence_id",
                "workspace_analytical_segment_members_v2": "segment_id",
                "workspace_analytical_components_v2": "diagnostic_id",
                "workspace_analytical_evidence_v2": "diagnostic_id",
                "workspace_metric_results_v2": "result_id",
                "workspace_diagnostic_results_v2": "diagnostic_id",
            }
            for table in tables:
                counts = execute_bounded(
                    connection,
                    f"SELECT (SELECT count(*) FROM _verified_w.main.{table}),"
                    f"(SELECT count(*) FROM main.{table})",
                ).fetchone()
                assert counts is not None
                # Bound native EXCEPT ALL state as well as Python batches.
                # SQL hash partitions identities only; canonical content hashes
                # and the financial identity algorithms are unchanged. Every
                # possible key belongs to exactly one bucket in this engine.
                buckets = 16 if max(int(c) for c in counts) > 65_536 else 1
                for bucket in range(buckets):
                    predicate = (
                        ""
                        if buckets == 1
                        else f" WHERE hash({identity_columns[table]}) % {buckets} = {bucket}"
                    )
                    missing = execute_bounded(
                        connection,
                        f"SELECT count(*) FROM (SELECT * FROM _verified_w.main.{table}{predicate} "
                        f"EXCEPT ALL SELECT * FROM main.{table}{predicate}) AS missing",
                    ).fetchone()
                    if missing != (0,):
                        raise WorkspaceV2InventoryError(
                            "verified W row or fragment changed or disappeared"
                        )
                    execution.record_measurement("corrective_native_bucket", 1)
                preserved.append((table, int(counts[0])))
            # A new row attached to an old sequence, segment or diagnostic can
            # change W's meaning without changing any of its existing rows.
            # Preserve those complete memberships, including absence of extras.
            for table, key, owners in (
                ("workspace_raw_json_projections_v2", "record_id", "raw_v2_index"),
                (
                    "workspace_analytical_sequence_segments_v2",
                    "sequence_id",
                    "workspace_analytical_sequences_v2",
                ),
                (
                    "workspace_analytical_segment_members_v2",
                    "segment_id",
                    "workspace_analytical_segments_v2",
                ),
                (
                    "workspace_analytical_components_v2",
                    "diagnostic_id",
                    "workspace_diagnostic_results_v2",
                ),
                (
                    "workspace_analytical_evidence_v2",
                    "diagnostic_id",
                    "workspace_diagnostic_results_v2",
                ),
            ):
                buckets = 16 if dict(preserved)[table] > 65_536 else 1
                for bucket in range(buckets):
                    current_bucket = (
                        "" if buckets == 1 else f" AND hash(c.{key}) % {buckets}={bucket}"
                    )
                    owner_bucket = (
                        "" if buckets == 1 else f" AND hash(w.{key}) % {buckets}={bucket}"
                    )
                    original_bucket = (
                        "" if buckets == 1 else f" WHERE hash({key}) % {buckets}={bucket}"
                    )
                    extra = execute_bounded(
                        connection,
                        f"SELECT count(*) FROM (SELECT c.* FROM main.{table} c "
                        f"WHERE EXISTS (SELECT 1 FROM _verified_w.main.{owners} w "
                        f"WHERE w.{key}=c.{key}{owner_bucket}){current_bucket} "
                        f"EXCEPT ALL SELECT * FROM _verified_w.main.{table}"
                        f"{original_bucket}) AS extra",
                    ).fetchone()
                    if extra != (0,):
                        raise WorkspaceV2InventoryError("verified W membership acquired extra rows")
            baseline_financial = execute_bounded(
                connection,
                "SELECT (SELECT count(*) FROM _verified_w.main.workspace_metric_results_v2 "
                "WHERE origin='HISTORICAL'),(SELECT count(*) FROM "
                "_verified_w.main.workspace_diagnostic_results_v2 WHERE origin='HISTORICAL')",
            ).fetchone()
            if (
                dict(preserved)["raw_v2_index"],
                dict(preserved)["normalized_observations_v2"],
                *baseline_financial,
            ) != (
                verified_w.inventory.raw_count,
                verified_w.inventory.observation_count,
                verified_w.inventory.metric_count,
                verified_w.inventory.diagnostic_count,
            ):
                raise WorkspaceV2InventoryError("W image inventory disagrees with its certificate")
            compact = CompactAnalyticalStore(connection)
            # Unknown fragments may have been committed outside an importer
            # cursor and may currently be unreferenced. Validate them too;
            # reading only added financial models does not cover that universe.
            for family, table, key in (
                ("content", "workspace_analytical_content_v2", "content_id"),
                ("sequences", "workspace_analytical_sequences_v2", "sequence_id"),
                ("segments", "workspace_analytical_segments_v2", "segment_id"),
            ):
                cursor = connect_duckdb(database, read_only=True)
                try:
                    execute_bounded(
                        cursor,
                        f"SELECT c.{key} FROM main.{table} c WHERE NOT EXISTS "
                        f"(SELECT 1 FROM _verified_w.main.{table} w WHERE w.{key}=c.{key}) "
                        f"ORDER BY c.{key}",
                    )
                    additions = 0
                    while rows := cursor.fetchmany(256):
                        identifiers = tuple(str(row[0]) for row in rows)
                        if family == "content":
                            for identifier in identifiers:
                                compact._load_content_batch((identifier,))
                        elif family == "sequences":
                            compact._load_sequences(identifiers)
                        else:
                            compact._load_segments(identifiers)
                        additions += len(identifiers)
                        execution.record_measurement("corrective_fragments_page", len(identifiers))
                    validated.append((family, additions))
                finally:
                    cursor.close()
            for table, key, owners in (
                (
                    "workspace_analytical_sequence_segments_v2",
                    "sequence_id",
                    "workspace_analytical_sequences_v2",
                ),
                (
                    "workspace_analytical_segment_members_v2",
                    "segment_id",
                    "workspace_analytical_segments_v2",
                ),
                ("workspace_raw_json_projections_v2", "record_id", "raw_v2_index"),
            ):
                orphan = execute_bounded(
                    connection,
                    f"SELECT 1 FROM main.{table} c WHERE NOT EXISTS "
                    f"(SELECT 1 FROM main.{owners} p WHERE p.{key}=c.{key}) LIMIT 1",
                ).fetchone()
                if orphan is not None:
                    raise WorkspaceV2InventoryError("corrective membership has no owning identity")
            families = (
                ("raw", "raw_v2_index", "record_id"),
                ("observations", "normalized_observations_v2", "observation_id"),
                ("metrics", "workspace_metric_results_v2", "result_id"),
                ("diagnostics", "workspace_diagnostic_results_v2", "diagnostic_id"),
            )
            for family, table, key in families:
                cursor = connect_duckdb(database, read_only=True)
                try:
                    execute_bounded(
                        cursor,
                        f"SELECT c.{key} FROM main.{table} c WHERE NOT EXISTS "
                        f"(SELECT 1 FROM _verified_w.main.{table} w WHERE w.{key}=c.{key}) "
                        f"ORDER BY c.{key}",
                    )
                    additions = 0
                    while rows := cursor.fetchmany(256):
                        identifiers = tuple(UUID(str(row[0])) for row in rows)
                        if family == "raw":
                            for identifier in identifiers:
                                # Large raws are parsed and released singly. No list
                                # of model-sized serialized documents is retained.
                                model = store.store.raw_staging.get_verified_bounded(identifier)
                                if (
                                    canonical_json_digest(model)
                                    != execute_bounded(
                                        connection,
                                        "SELECT checksum_sha256 FROM raw_v2_index "
                                        "WHERE record_id=?",
                                        [str(identifier)],
                                    ).fetchone()[0]
                                ):
                                    raise WorkspaceV2InventoryError(
                                        "new raw canonical checksum differs"
                                    )
                                del model
                        elif family == "observations":
                            models = store.observations.get_many(identifiers)
                            placeholders = ",".join("?" for _ in identifiers)
                            canonical = _observation_canonical_sql("o")
                            expected = dict(
                                execute_bounded(
                                    connection,
                                    f"SELECT observation_id,sha256({canonical}) "
                                    "FROM normalized_observations_v2 o "
                                    f"WHERE observation_id IN ({placeholders})",
                                    [str(identifier) for identifier in identifiers],
                                ).fetchall()
                            )
                            if any(
                                canonical_json_digest(model) != expected[str(identifier)]
                                for identifier, model in models.items()
                            ):
                                raise WorkspaceV2InventoryError(
                                    "new observation canonical content differs"
                                )
                            del models
                        else:
                            models = (
                                compact.get_metrics(identifiers)
                                if family == "metrics"
                                else compact.get_diagnostics(identifiers)
                            )
                            expected = dict(
                                execute_bounded(
                                    connection,
                                    f"SELECT {key},checksum_sha256 FROM {table} "
                                    f"WHERE {key} IN ({','.join('?' for _ in identifiers)})",
                                    [str(identifier) for identifier in identifiers],
                                ).fetchall()
                            )
                            if any(
                                canonical_json_digest(model) != expected[str(identifier)]
                                for identifier, model in models.items()
                            ):
                                raise WorkspaceV2InventoryError(
                                    "new financial canonical content differs"
                                )
                            del models
                        additions += len(identifiers)
                        execution.record_measurement("corrective_models_page", len(identifiers))
                    validated.append((family, additions))
                finally:
                    cursor.close()
            lineage = execute_bounded(
                connection,
                "SELECT count(*) FROM normalized_observations_v2 o "
                "LEFT JOIN raw_v2_index r ON r.record_id=o.raw_record_id "
                "WHERE r.record_id IS NULL OR r.asset_id<>o.asset_id",
            ).fetchone()
            if lineage != (0,):
                raise WorkspaceV2InventoryError("corrective raw observation lineage differs")
            # The new graph is not yet covered by W's proof. Verify this
            # affected invariant once, under R1, with the bounded verifier.
            if relationships_path is None:
                compact.verify_historical_relationships(read_only_units=True)
            else:
                reference = reconciliation_file_reference(relationships_path, "control")
                relationships = ProgressRelationshipProof.model_validate_json(
                    reference.path.read_bytes()
                )
                if (
                    relationships.operation_id != generation_id
                    or relationships.destination_root != destination
                    or relationships.destination_workspace_id != identity.workspace_id
                    or relationships.database != database_reference
                    or relationships.wal is not None
                    or relationships.verifier_module_sha256
                    != sha256_hex(Path(__file__).with_name("compact_analytical_v2.py").read_bytes())
                    or relationships.resource_policy_sha256 != admission.resource_policy_sha256
                ):
                    raise WorkspaceV2InventoryError("corrective relationship proof binding differs")
                WorkspaceV2ProgressReconciler._verify(relationships.reconciliation)
                WorkspaceV2ProgressReconciler._verify(relationships.resource_receipt)

            def folded(sql: str) -> tuple[int, str]:
                execute_bounded(connection, sql)
                digest, count = hashlib.sha256(b"").hexdigest(), 0
                while rows := connection.fetchmany(256):
                    for row in rows:
                        value = str(row[0])
                        if len(value) != 64:
                            raise WorkspaceV2InventoryError("corrective digest value is malformed")
                        digest = hashlib.sha256(f"{digest}:{value}".encode()).hexdigest()
                        count += 1
                return count, digest

            raw_count, raw_digest = folded(
                "SELECT checksum_sha256 FROM raw_v2_index "
                "ORDER BY CAST(received_at AS TIMESTAMPTZ),record_id"
            )
            observation_count, observation_digest = folded(
                f"SELECT sha256({_observation_canonical_sql('o')}) "
                "FROM normalized_observations_v2 o "
                "ORDER BY CAST(available_at AS TIMESTAMPTZ),observation_id"
            )
            metric_count, metric_digest = folded(
                "SELECT checksum_sha256 FROM workspace_metric_results_v2 "
                "WHERE origin='HISTORICAL' ORDER BY available_at,result_id"
            )
            diagnostic_count, diagnostic_digest = folded(
                "SELECT checksum_sha256 FROM workspace_diagnostic_results_v2 "
                "WHERE origin='HISTORICAL' ORDER BY available_at,diagnostic_id"
            )
            seal = compact.historical_seal()
            if seal is None or (
                metric_count,
                metric_digest,
                diagnostic_count,
                diagnostic_digest,
            ) != (
                seal.metric_count,
                seal.metric_digest,
                seal.diagnostic_count,
                seal.diagnostic_digest,
            ):
                raise WorkspaceV2InventoryError("corrective fresh inventory differs from F2 seal")
        proof = GenerationContentProof(
            generation_id=generation_id,
            workspace_id=identity.workspace_id,
            baseline_certificate=baseline_certificate,
            baseline_database=baseline_reference,
            database=database_reference,
            resource_receipt=reconciliation_file_reference(admission_path, "resource"),
            verifier_sha256=verifier_sha256,
            policy_sha256=admission.resource_policy_sha256,
            preserved_tables=tuple(preserved),
            raw_count=raw_count,
            raw_digest=raw_digest,
            observation_count=observation_count,
            observation_digest=observation_digest,
            metric_count=metric_count,
            metric_digest=metric_digest,
            diagnostic_count=diagnostic_count,
            diagnostic_digest=diagnostic_digest,
            validated_additions=tuple(validated),
            verified_at=datetime.now(UTC),
        )
        # The destination was read-only. Detect any unexpected native mutation
        # without blessing a different generation or silently rewriting a proof.
        if database_reference != reconciliation_file_reference(database, "database"):
            raise WorkspaceV2InventoryError("corrective database changed during verification")
        return proof


__all__ = [
    "WorkspaceV2AnalyticalImporter",
    "WorkspaceV2InventoryError",
    "WorkspaceV2InventoryState",
    "WorkspaceV2InventorySummary",
]
