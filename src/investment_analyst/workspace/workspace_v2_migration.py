"""Bounded, resumable migration from a read-only v1 workspace to workspace v2.

The module only coordinates the existing verified importers.  It deliberately
does not read provider data or derive financial values: every model is copied
through its existing v1/v2 persistence contracts and is re-read by those
contracts before a page checkpoint is advanced.
"""

from __future__ import annotations

import ast
import csv
import hashlib
import inspect
import json
import os
import time
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager, nullcontext
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Annotated, Literal
from uuid import UUID, uuid4

from duckdb import DuckDBPyConnection
from pydantic import ConfigDict, Field

from investment_analyst.core.models.base import ContractModel, NonEmptyStr, UTCDateTime
from investment_analyst.storage.compact_analytical_v2 import CompactAnalyticalStore
from investment_analyst.storage.historical_analytical_import import (
    DirectHistoricalAnalyticalSource,
)
from investment_analyst.storage.local import LocalStorage
from investment_analyst.storage.observation_v2_import import (
    ObservationV2Importer,
    ObservationV2ImportProgress,
    ObservationV2ImportSummary,
)
from investment_analyst.storage.raw_v2_import import (
    RawV2Importer,
    RawV2ImportProgress,
    RawV2ImportState,
    RawV2ImportSummary,
)
from investment_analyst.storage.resource_policy import (
    MaintenanceExecution,
    MaintenanceResourceReceipt,
    connect_duckdb,
    connection_policy,
    execute_bounded,
    require_maintenance_admission,
)
from investment_analyst.storage.serialization import (
    canonical_json_bytes,
    canonical_json_digest,
    sha256_hex,
)
from investment_analyst.storage.workspace_v2 import WorkspaceV2Store
from investment_analyst.storage.workspace_v2_inventory import (
    WorkspaceV2AnalyticalImporter,
    WorkspaceV2GenerationLedger,
    WorkspaceV2InventorySummary,
    WorkspaceV2ProgressReconciler,
    historical_transition_fingerprints,
    reconciliation_file_reference,
)
from investment_analyst.workspace.backup import WorkspaceBackupService, _workspace_process_guard
from investment_analyst.workspace.models import WorkspaceAccessMode, WorkspaceInspection
from investment_analyst.workspace.service import (
    WorkspaceAccessError,
    WorkspaceService,
)
from investment_analyst.workspace.workspace_v2_lifecycle_models import FrozenSourceCut
from investment_analyst.workspace.workspace_v2_migration_models import (
    CutOwner,
    GenerationContentProof,
    ProgressReconciliation,
    ProgressReconciliationReceipt,
    ProgressReconciliationV2,
    ProgressRelationshipProof,
    ReconciliationFileReference,
    SuccessorCoverageProof,
    WorkspaceV1MigrationSourceSnapshot,
    WorkspaceV2MigrationPhase,
    WorkspaceV2SourceTransitionState,
)

_MAX_PAGE_LIMIT = 256
_DEFAULT_RESERVE_BYTES = 5_000_000_000
_SOURCE_SNAPSHOT_FILENAME = "workspace-v1-migration-source-snapshot.json"


class WorkspaceV2MigrationError(RuntimeError):
    """Raised when a migration cannot prove its source or destination boundary."""


@dataclass(frozen=True)
class _QuiescentSourceIntent:
    """One-use session identity allocated by the worker before the stop intent.

    The identifier cannot be supplied from a historical reservation. PREPARED
    records intention only; the same live worker consumes it when obtaining the
    actual source locks. Closing/reopening can never recreate this session.
    """

    source_root: Path
    operation_id: UUID
    writer_generation: UUID = field(default_factory=uuid4, init=False)
    creator_pid: int = field(default_factory=os.getpid, init=False)
    consumed: bool = field(default=False, init=False)

    def consume(self, root: Path, operation_id: UUID) -> UUID:
        if (
            self.consumed
            or self.creator_pid != os.getpid()
            or (root, operation_id)
            != (
                self.source_root,
                self.operation_id,
            )
        ):
            raise WorkspaceV2MigrationError("source session intention was consumed or is foreign")
        object.__setattr__(self, "consumed", True)
        return self.writer_generation


@dataclass(frozen=True)
class _QuiescentSourceProof:
    cut: FrozenSourceCut
    inspection: WorkspaceInspection
    metadata_sha256: str
    writer_generation: UUID
    release_native_reader: Callable[[], None]


_QUIESCENT_SOURCE: ContextVar[_QuiescentSourceProof | None] = ContextVar(
    "workspace_v2_quiescent_source", default=None
)


def _source_generation(root: Path) -> str:
    """Detect replacements under held locks; never use this as a content hash."""
    digest = hashlib.sha256()
    candidates = [root / "manifest.json", root / "storage/data/processed/investment_analyst.duckdb"]
    documents = root / "storage/data/documents/sha256"

    def files() -> Iterator[Path]:
        yield from candidates
        if documents.exists():
            for parent, directories, filenames in os.walk(documents, followlinks=False):
                directories.sort()
                filenames.sort()
                for name in directories:
                    if (Path(parent) / name).is_symlink():
                        raise WorkspaceV2MigrationError("quiescent source documents are unsafe")
                for name in filenames:
                    yield Path(parent) / name

    for path in files():
        if path.is_symlink() or not path.is_file():
            raise WorkspaceV2MigrationError("quiescent source generation is unsafe")
        stat = path.stat()
        digest.update(
            json.dumps(
                (
                    path.relative_to(root).as_posix(),
                    stat.st_dev,
                    stat.st_ino,
                    stat.st_size,
                    stat.st_mtime_ns,
                    stat.st_ctime_ns,
                ),
                separators=(",", ":"),
            ).encode()
        )
    return digest.hexdigest()


@contextmanager
def quiescent_source_cut(
    service: WorkspaceService,
    source_root: Path,
    *,
    operation_id: UUID,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    _session_intent: _QuiescentSourceIntent | None = None,
) -> Iterator[FrozenSourceCut]:
    """Own the locks and reader for the whole cut; proof cannot survive this session."""
    if _QUIESCENT_SOURCE.get() is not None:
        raise WorkspaceV2MigrationError("quiescent source session is already active")
    root = service.resolve(source_root).root
    with _workspace_process_guard(root):
        session = (_session_intent or _QuiescentSourceIntent(root, operation_id)).consume(
            root, operation_id
        )
        inspection = service.inspect(root)
        identity = service.manifest(root)
        if inspection.status != "ready" or identity.format_version != 1:
            raise WorkspaceV2MigrationError("quiescent source must be a ready workspace v1")
        before = _source_generation(root)
        fingerprint = source_fingerprint(service, root)
        if _source_generation(root) != before:
            raise WorkspaceV2MigrationError("source changed while establishing quiescence")
        cut = FrozenSourceCut(
            operation_id=operation_id,
            source_root=root,
            workspace_id=identity.workspace_id,
            workspace_format_version=identity.format_version,
            created_at=identity.created_at,
            source_fingerprint=fingerprint,
            frozen_at=clock(),
        )
        # Hash the physical file while native readers are closed. Opening and
        # closing another descriptor of a file protected by process POSIX locks
        # can release those locks; keep the native reader for the session only
        # after the physical fingerprint has been established.
        with ExitStack() as native, ExitStack() as closed_database:
            native.enter_context(
                service.open_storage(service.resolve(root), WorkspaceAccessMode.READ_ONLY)
            )
            if _source_generation(root) != before:
                raise WorkspaceV2MigrationError("source changed before quiescent reader opened")
            reader_closed = False

            def release_reader() -> None:
                nonlocal reader_closed
                if reader_closed:
                    return
                from investment_analyst.workspace.workspace_v2_lifecycle import _closed_duckdb_file

                native.close()
                closed_database.enter_context(
                    _closed_duckdb_file(root / "storage/data/processed/investment_analyst.duckdb")
                )
                reader_closed = True

            proof = _QuiescentSourceProof(cut, inspection, before, session, release_reader)
            token = _QUIESCENT_SOURCE.set(proof)
            try:
                yield cut
                if _source_generation(root) != before:
                    raise WorkspaceV2MigrationError("source changed during quiescent session")
            finally:
                _QUIESCENT_SOURCE.reset(token)


def _live_quiescent_source(
    root: Path, cut: FrozenSourceCut | None = None
) -> _QuiescentSourceProof | None:
    proof = _QUIESCENT_SOURCE.get()
    if proof is None:
        return None
    if proof.cut.source_root != root or (cut is not None and proof.cut != cut):
        raise WorkspaceV2MigrationError("quiescent source session binding differs")
    if _source_generation(root) != proof.metadata_sha256:
        raise WorkspaceV2MigrationError("source changed during quiescent session")
    return proof


def close_quiescent_native_reader(root: Path, cut: FrozenSourceCut) -> None:
    """Keep the actual session and writer exclusion while admitting one restore reader."""
    proof = _live_quiescent_source(root, cut)
    if proof is None:
        raise WorkspaceV2MigrationError("source reader handoff requires its live session")
    proof.release_native_reader()


class _SuccessorReservation(ContractModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")
    reconciliation_id: UUID
    successor: FrozenSourceCut
    source_session: UUID
    baseline: ReconciliationFileReference
    baseline_dataset_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    module_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    resource_receipt: ReconciliationFileReference
    verifier_compatibility: ReconciliationFileReference | None = None
    recorded_at: UTCDateTime


_BASELINE_CODE_PATHS = (
    "storage/workspace_v2_inventory.py",
    "storage/compact_analytical_v2.py",
    "storage/historical_analytical_import.py",
    "workspace/workspace_v2_migration_models.py",
)


def _baseline_verifier_ast(source: bytes, module: str, *, candidate: bool) -> str:
    """Keep the full validated verifier and its module dependencies unchanged.

    The W verifier does not call the analytical importer. The candidate may
    change that importer or add the two explicit successor entry points, while
    every existing compact-store method and module statement remains identical.
    Archived source is parsed as evidence only; it is never imported/executed.
    """
    tree = ast.parse(source)
    if module == "storage/workspace_v2_inventory.py":
        for node in tree.body:
            if isinstance(node, ast.FunctionDef) and node.name == "historical_transition_status":
                # Lifecycle recognition does not participate in validating W.
                # Preserve its definition/signature while allowing the explicit
                # completed-successor exception for the covered predecessor.
                node.body = [ast.Pass()]
            if isinstance(node, ast.ClassDef) and node.name == "WorkspaceV2AnalyticalImporter":
                retained: list[ast.stmt] = []
                for member in node.body:
                    if isinstance(member, ast.FunctionDef) and member.name in {
                        "__init__",
                        "_prepare_transition",
                        "run",
                    }:
                        # Preserve signatures/decorators/defaults and class
                        # creation. Only these uncalled importer bodies differ.
                        member.body = [ast.Pass()]
                    if isinstance(member, ast.FunctionDef) and member.name in {
                        "_run_recovered_successor",
                        "bind_recovered_successor",
                    }:
                        if (
                            not candidate
                            or member.args.defaults
                            or any(value is not None for value in member.args.kw_defaults)
                            or member.decorator_list
                        ):
                            raise WorkspaceV2MigrationError(
                                "successor importer definition is unsafe"
                            )
                        continue
                    retained.append(member)
                node.body = retained
    elif module == "storage/historical_analytical_import.py":
        for node in tree.body:
            if isinstance(node, ast.ClassDef) and node.name == "DirectHistoricalAnalyticalSource":
                retained = []
                for member in node.body:
                    if (
                        isinstance(member, ast.FunctionDef)
                        and member.name == "iter_recovered_pages"
                    ):
                        if (
                            not candidate
                            or member.args.defaults
                            or any(value is not None for value in member.args.kw_defaults)
                            or member.decorator_list
                        ):
                            raise WorkspaceV2MigrationError("successor source definition is unsafe")
                        continue
                    retained.append(member)
                node.body = retained
    elif module == "storage/compact_analytical_v2.py":
        additions = {"recovered_historical_transition", "publish_recovered_transition_seal"}
        for node in tree.body:
            if isinstance(node, ast.ClassDef) and node.name == "CompactAnalyticalStore":
                retained: list[ast.stmt] = []
                for member in node.body:
                    if isinstance(member, ast.FunctionDef) and member.name in additions:
                        if not candidate:
                            raise WorkspaceV2MigrationError(
                                "baseline contains successor entry point"
                            )
                        # A method addition must not execute arbitrary defaults
                        # or decorators while its class/module is being loaded.
                        if (
                            member.args.defaults
                            or any(value is not None for value in member.args.kw_defaults)
                            or any(
                                not isinstance(decorator, ast.Name)
                                or decorator.id != "contextmanager"
                                for decorator in member.decorator_list
                            )
                        ):
                            raise WorkspaceV2MigrationError("successor method definition is unsafe")
                    else:
                        retained.append(member)
                node.body = retained
    return ast.dump(tree, include_attributes=False)


class _BaselineVerifierCompatibility(ContractModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")
    schema_version: Literal["workspace-v2-baseline-verifier-compatibility-v1"] = (
        "workspace-v2-baseline-verifier-compatibility-v1"
    )
    baseline: ReconciliationFileReference
    dataset_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    baseline_tree_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    candidate_tree_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    environment_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    baseline_code: tuple[ReconciliationFileReference, ...]
    candidate_code: tuple[ReconciliationFileReference, ...]
    policy_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    verifier_module_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    equivalence_policy: Literal["unchanged-w-verifier-ast-v1"] = "unchanged-w-verifier-ast-v1"
    verified_at: UTCDateTime


def _verify_baseline_code_compatibility(
    reconciler: WorkspaceV2ProgressReconciler,
    progress: ProgressReconciliation,
    baseline_code_root: Path | None,
) -> ReconciliationFileReference | None:
    tree, environment = historical_transition_fingerprints(
        progress.page_limit, progress.verifier_policy
    )
    if environment != progress.current_environment_fingerprint:
        raise WorkspaceV2MigrationError("successor verifier environment changed")
    if tree == progress.current_tree_fingerprint:
        return None
    if baseline_code_root is None:
        raise WorkspaceV2MigrationError(
            "successor verifier binding requires archived baseline code"
        )
    assert progress.inventory is not None
    current_root = Path(__file__).parent.parent
    old_digest = hashlib.sha256()
    new_digest = hashlib.sha256()
    baseline_refs: list[ReconciliationFileReference] = []
    candidate_refs: list[ReconciliationFileReference] = []
    for relative in _BASELINE_CODE_PATHS:
        baseline_path = baseline_code_root / "src/investment_analyst" / relative
        candidate_path = current_root / relative
        if any(item.is_symlink() for item in (baseline_path, *baseline_path.parents)):
            raise WorkspaceV2MigrationError("baseline code archive is unsafe")
        old, new = baseline_path.read_bytes(), candidate_path.read_bytes()
        old_digest.update(hashlib.sha256(old).digest())
        new_digest.update(hashlib.sha256(new).digest())
        if _baseline_verifier_ast(old, relative, candidate=False) != _baseline_verifier_ast(
            new, relative, candidate=True
        ):
            raise WorkspaceV2MigrationError("validated W verifier or dependencies changed")
        baseline_refs.append(reconciliation_file_reference(baseline_path, "code"))
        candidate_refs.append(reconciliation_file_reference(candidate_path, "code"))
    if (old_digest.hexdigest(), new_digest.hexdigest()) != (
        progress.current_tree_fingerprint,
        tree,
    ):
        raise WorkspaceV2MigrationError("baseline code archive fingerprint differs")
    # R3's module/policy fingerprints retain their original provenance. Check
    # the policy against that proof instead of silently accepting a new policy.
    relationships = ProgressRelationshipProof.model_validate_json(
        progress.inventory.relationships.path.read_bytes()
    )
    policy_sha = sha256_hex((current_root / "storage/resource_policy.py").read_bytes())
    if relationships.resource_policy_sha256 != policy_sha:
        raise WorkspaceV2MigrationError("baseline resource policy changed")
    if progress.reserved_successor_id is not None:
        reservation_path = reconciler.state / (
            f"progress-successor-reservation.{progress.reserved_successor_id}.json"
        )
        if reservation_path.is_symlink() or not reservation_path.is_file():
            raise WorkspaceV2MigrationError("successor reservation session receipt is missing")
        reservation = _SuccessorReservation.model_validate_json(reservation_path.read_bytes())
        reconciler._verify(reservation.baseline)
        baseline_reference = reservation.baseline
    else:
        baseline_reference = reconciler._publish(progress)
    compatibility = _BaselineVerifierCompatibility(
        baseline=baseline_reference,
        dataset_sha256=progress.inventory.dataset_sha256,
        baseline_tree_fingerprint=progress.current_tree_fingerprint,
        candidate_tree_fingerprint=tree,
        environment_fingerprint=environment,
        baseline_code=tuple(baseline_refs),
        candidate_code=tuple(candidate_refs),
        policy_sha256=policy_sha,
        verifier_module_sha256=sha256_hex(Path(__file__).read_bytes()),
        verified_at=progress.inventory.verified_at,
    )
    document = canonical_json_bytes(compatibility) + b"\n"
    path = reconciler.state / f"progress-verifier-compatibility.{sha256_hex(document)}.json"
    reconciler._write_once(path, document)
    return reconciliation_file_reference(path, "control")


def _generation_core_evidence(progress: ProgressReconciliationV2) -> GenerationContentProof:
    """Reuse actual C2 proofs; never synthesize a legacy reconciliation/core proof."""
    from investment_analyst.storage.workspace_v2_inventory import (
        GenerationRawCoverageProof,
        verify_generation_contents,
        verify_generation_raw_files,
    )

    if progress.inventory is None or progress.core_proof is None:
        raise WorkspaceV2MigrationError("generation lacks a certified baseline")
    policy = sha256_hex((Path(__file__).parent.parent / "storage/resource_policy.py").read_bytes())
    core = GenerationContentProof.model_validate_json(
        WorkspaceV2GenerationLedger._read(progress.core_proof)
    )
    if (
        core.generation_id != progress.generation_id
        or core.workspace_id != progress.destination_workspace_id
        or core.database != progress.database
        or core.policy_sha256 != policy
        or core.verifier_sha256
        != sha256_hex(inspect.getsource(verify_generation_contents).encode())
        or (
            core.raw_count,
            core.observation_count,
            core.metric_count,
            core.metric_digest,
            core.diagnostic_count,
            core.diagnostic_digest,
        )
        != (
            progress.inventory.raw_count,
            progress.inventory.observation_count,
            progress.inventory.metric_count,
            progress.inventory.metric_digest,
            progress.inventory.diagnostic_count,
            progress.inventory.diagnostic_digest,
        )
    ):
        raise WorkspaceV2MigrationError("generation corrective content binding differs")
    raw_proofs = []
    for ref in progress.corrective_proofs:
        document = WorkspaceV2GenerationLedger._read(ref)
        try:
            raw = GenerationRawCoverageProof.model_validate_json(document)
        except ValueError:
            continue
        raw_proofs.append(raw)
    if len(raw_proofs) != 1:
        raise WorkspaceV2MigrationError("generation requires its complete raw coverage")
    raw = raw_proofs[0]
    if (
        raw.generation_id != progress.generation_id
        or raw.destination_root != progress.destination_root
        or raw.workspace_id != progress.destination_workspace_id
        or raw.database != progress.database
        or raw.content_proof != progress.core_proof
        or raw.raw_count != core.raw_count
        or raw.policy_sha256 != policy
        or raw.verifier_sha256
        != sha256_hex(inspect.getsource(verify_generation_raw_files).encode())
    ):
        raise WorkspaceV2MigrationError("generation raw coverage binding differs")
    WorkspaceV2ProgressReconciler._verify(progress.inventory.delta_membership)
    WorkspaceV2ProgressReconciler._verify(progress.inventory.relationships)
    WorkspaceV2ProgressReconciler._verify(progress.parent_membership)
    return core


def _reserve_generation_successor(
    *,
    service: WorkspaceService,
    destination_root: Path,
    reconciliation_id: UUID,
    frozen_source: FrozenSourceCut,
    admission_path: Path,
) -> ProgressReconciliationV2:
    from investment_analyst.workspace.workspace_v2_lifecycle import (
        _closed_duckdb_file,
        _v2_writer_closed,
    )

    ledger = WorkspaceV2GenerationLedger(service, destination_root)
    resolved = ledger.resolve()
    if resolved is None:
        raise WorkspaceV2MigrationError("generation head is missing")
    head, progress = resolved
    expected_head = ledger._head_digest()
    if progress.generation_id != reconciliation_id or progress.phase == "PREPARED":
        raise WorkspaceV2MigrationError("successor requires the current certified generation")
    if (progress.source_root, progress.source_workspace_id, progress.source_created_at) != (
        frozen_source.source_root,
        frozen_source.workspace_id,
        frozen_source.created_at,
    ) or ledger.attempt_invalidated(frozen_source.operation_id):
        raise WorkspaceV2MigrationError("generation source or attempt differs")
    session = _live_quiescent_source(frozen_source.source_root, frozen_source)
    if session is None:
        raise WorkspaceV2MigrationError("generation requires a live source session")
    admission = MaintenanceResourceReceipt.model_validate_json(admission_path.read_bytes())
    ledger._admit(admission)
    owner = CutOwner(
        source_session=session.writer_generation,
        pid=admission.pid,
        start_ticks=admission.process_start_ticks,
        boot_id=admission.boot_id,
        cgroup=admission.cgroup_path,
    )
    _generation_core_evidence(progress)
    assert progress.inventory is not None
    path = ledger.state / f"progress-successor-reservation.{frozen_source.operation_id}.json"
    module_sha = sha256_hex(Path(__file__).read_bytes())
    resource = reconciliation_file_reference(admission_path, "resource")
    if path.exists():
        reservation = _SuccessorReservation.model_validate_json(
            WorkspaceV2GenerationLedger._read(reconciliation_file_reference(path, "control"))
        )
        baseline = ProgressReconciliationV2.model_validate_json(ledger._read(reservation.baseline))
        if (
            (
                reservation.reconciliation_id,
                reservation.successor,
                reservation.source_session,
                reservation.module_sha256,
                reservation.resource_receipt,
                reservation.baseline_dataset_sha256,
            )
            != (
                reconciliation_id,
                frozen_source,
                session.writer_generation,
                module_sha,
                resource,
                progress.inventory.dataset_sha256,
            )
            or baseline.generation_id != reconciliation_id
            or baseline.phase != "BASELINE_VERIFIED"
            or baseline.reserved_successor is not None
            or baseline.inventory != progress.inventory
            or baseline.database != progress.database
        ):
            raise WorkspaceV2MigrationError("generation reservation session or baseline differs")
    else:
        if progress.reserved_successor is not None:
            raise WorkspaceV2MigrationError("generation reservation receipt is missing")
        # An intent whose session died must pass C1 invalidation/reconciliation;
        # it must never be silently replaced by a different attempt.
        for other in ledger.state.glob("progress-successor-reservation.*.json"):
            if other.is_symlink():
                raise WorkspaceV2MigrationError("generation reservation path is unsafe")
            previous = _SuccessorReservation.model_validate_json(other.read_bytes())
            if previous.baseline == head.certificate and previous.successor != frozen_source:
                raise WorkspaceV2MigrationError("generation has another pending reservation")
        reservation = _SuccessorReservation(
            reconciliation_id=reconciliation_id,
            successor=frozen_source,
            source_session=session.writer_generation,
            baseline=head.certificate,
            baseline_dataset_sha256=progress.inventory.dataset_sha256,
            module_sha256=module_sha,
            resource_receipt=resource,
            recorded_at=datetime.now(UTC),
        )
    if progress.reserved_successor is not None:
        if (
            progress.reserved_successor != frozen_source
            or progress.successor_owner != owner
            or progress.successor_admission != resource
        ):
            raise WorkspaceV2MigrationError("generation already reserved a different owner")
        return progress
    with _v2_writer_closed(destination_root), _closed_duckdb_file(progress.database.path):
        ledger._read(progress.database)
        if progress.wal is not None:
            ledger._read(progress.wal)
        elif progress.database.path.with_suffix(".duckdb.wal").exists():
            raise WorkspaceV2MigrationError("generation baseline has an unexpected WAL")
        WorkspaceV2ProgressReconciler._write_once(path, canonical_json_bytes(reservation) + b"\n")
        reserved = progress.model_copy(
            update={
                "previous_certificate": head.certificate,
                "reserved_successor": frozen_source,
                "successor_owner": owner,
                "successor_admission": resource,
                "recorded_at": datetime.now(UTC),
            }
        )
        ledger.publish(reserved, expected_head_sha256=expected_head, admission=admission)
    return reserved


def reserve_recovered_successor(
    *,
    service: WorkspaceService,
    destination_root: Path,
    reconciliation_id: UUID,
    frozen_source: FrozenSourceCut,
    admission_path: Path,
    baseline_code_root: Path | None = None,
) -> ProgressReconciliationReceipt | ProgressReconciliationV2:
    """Reserve exactly one live F1 without claiming W subset F1 or completing F0."""
    from investment_analyst.workspace.workspace_v2_lifecycle import (
        _closed_duckdb_file,
        _v2_writer_closed,
    )

    admission = MaintenanceResourceReceipt.model_validate_json(admission_path.read_bytes())
    require_maintenance_admission(admission)
    source_database = (
        service.resolve(frozen_source.source_root).storage_root
        / "data/processed/investment_analyst.duckdb"
    )
    destination_database = destination_root / "storage/v2/index.duckdb"
    if (
        admission.budget.operation_id != frozen_source.operation_id
        or admission.budget.profile != "verification"
        or admission.budget.reader_worker is not None
        or {item.database for item in admission.budget.assignments}
        != {source_database, destination_database}
    ):
        raise WorkspaceV2MigrationError("successor reservation resource binding differs")
    session = _live_quiescent_source(frozen_source.source_root, frozen_source)
    if session is None:
        raise WorkspaceV2MigrationError("successor reservation requires live source quiescence")
    if (destination_root / "state/reconciliation-head.json").exists():
        return _reserve_generation_successor(
            service=service,
            destination_root=destination_root,
            reconciliation_id=reconciliation_id,
            frozen_source=frozen_source,
            admission_path=admission_path,
        )
    reconciler = WorkspaceV2ProgressReconciler(service, destination_root, reconciliation_id)
    with reconciler._exclusive():
        progress = ProgressReconciliation.model_validate_json(reconciler.path.read_bytes())
        if progress.phase not in {"BASELINE_VERIFIED", "SUCCESSOR_BOUND"}:
            raise WorkspaceV2MigrationError("successor reservation requires verified W")
        reconciler._controls(progress)
        assert progress.inventory is not None and progress.database_after is not None
        reconciler._verify(progress.inventory.delta_membership)
        reconciler._verify(progress.inventory.relationships)
        compatibility = _verify_baseline_code_compatibility(
            reconciler, progress, baseline_code_root
        )
        reservation_path = (
            reconciler.state / f"progress-successor-reservation.{frozen_source.operation_id}.json"
        )
        module_sha = sha256_hex(Path(__file__).read_bytes())
        if reservation_path.exists():
            if reservation_path.is_symlink():
                raise WorkspaceV2MigrationError("successor reservation artifact is unsafe")
            receipt = _SuccessorReservation.model_validate_json(reservation_path.read_bytes())
            if (
                receipt.reconciliation_id != reconciliation_id
                or receipt.successor != frozen_source
                or receipt.source_session != session.writer_generation
                or receipt.module_sha256 != module_sha
                or receipt.baseline_dataset_sha256 != progress.inventory.dataset_sha256
            ):
                raise WorkspaceV2MigrationError("successor reservation session or code changed")
            reconciler._verify(receipt.baseline)
            reconciler._verify(receipt.resource_receipt)
            if receipt.verifier_compatibility is not None:
                reconciler._verify(receipt.verifier_compatibility)
            elif compatibility is not None:
                raise WorkspaceV2MigrationError("successor verifier compatibility receipt missing")
        elif progress.reserved_successor is not None:
            raise WorkspaceV2MigrationError("successor reservation session receipt is missing")
        if progress.reserved_successor is not None:
            if progress.reserved_successor != frozen_source:
                raise WorkspaceV2MigrationError("reconciliation already reserved another successor")
            return ProgressReconciliationReceipt(
                reconciliation=progress,
                certificate=reconciler._publish(progress),
                stage="successor_bound"
                if progress.phase == "SUCCESSOR_BOUND"
                else "baseline_verified",
                recorded_at=datetime.now(UTC),
            )
        reserved = ProgressReconciliation.model_validate(
            {
                **progress.model_dump(),
                "reserved_successor_id": frozen_source.operation_id,
                "reserved_successor": frozen_source,
                "updated_at": datetime.now(UTC),
            }
        )
        with _v2_writer_closed(destination_root), _closed_duckdb_file(destination_database):
            reconciler._verify(progress.database_after)
            wal = destination_database.with_suffix(".duckdb.wal")
            if progress.wal_after is not None:
                reconciler._verify(progress.wal_after)
            elif wal.exists():
                raise WorkspaceV2MigrationError("successor reservation found unexpected WAL")
            if not reservation_path.exists():
                receipt = _SuccessorReservation(
                    reconciliation_id=reconciliation_id,
                    successor=frozen_source,
                    source_session=session.writer_generation,
                    baseline=reconciler._publish(progress),
                    baseline_dataset_sha256=progress.inventory.dataset_sha256,
                    module_sha256=module_sha,
                    resource_receipt=reconciliation_file_reference(admission_path, "resource"),
                    verifier_compatibility=compatibility,
                    recorded_at=datetime.now(UTC),
                )
                reconciler._write_once(reservation_path, canonical_json_bytes(receipt) + b"\n")
            certificate = reconciler._publish(reserved)
        return ProgressReconciliationReceipt(
            reconciliation=reserved,
            certificate=certificate,
            stage="baseline_verified",
            recorded_at=datetime.now(UTC),
        )


class _SuccessorCoverageWork(ContractModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")
    successor: FrozenSourceCut
    baseline_dataset_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    module_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    family: Literal["metrics", "diagnostics", "complete"] = "metrics"
    available_at: UTCDateTime | None = None
    identifier: UUID | None = None
    metric_count: int = Field(default=0, ge=0)
    diagnostic_count: int = Field(default=0, ge=0)
    covered_metric_count: int = Field(default=0, ge=0)
    covered_diagnostic_count: int = Field(default=0, ge=0)
    metric_digest: str = Field(default_factory=lambda: hashlib.sha256(b"").hexdigest())
    diagnostic_digest: str = Field(default_factory=lambda: hashlib.sha256(b"").hexdigest())
    fast_count: int = Field(default=0, ge=0)
    typed_count: int = Field(default=0, ge=0)
    new_index_bytes: int = Field(default=0, ge=0)
    new_index_sha256: str = Field(default_factory=lambda: hashlib.sha256(b"").hexdigest())


class _SuccessorComparison(ContractModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")
    schema_version: Literal["workspace-v2-successor-comparison-v1"] = (
        "workspace-v2-successor-comparison-v1"
    )
    baseline: ReconciliationFileReference
    work: _SuccessorCoverageWork
    new_index: ReconciliationFileReference
    resource_receipt: ReconciliationFileReference
    verifier_tree_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    environment_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    verified_at: UTCDateTime


def _utc_document_timestamp(column: str) -> str:
    # Columns are fixed internal schema names, never caller-controlled SQL.
    utc = f"{column} AT TIME ZONE 'UTC'"
    fraction = f"(CAST(date_part('microseconds',{utc}) AS BIGINT)%1000000)"
    return (
        f"strftime({utc},'%Y-%m-%dT%H:%M:%S') || "
        f"CASE WHEN {fraction}=0 THEN 'Z' ELSE '.'||"
        f"lpad(CAST({fraction} AS VARCHAR),6,'0')||'Z' END"
    )


def _observation_canonical_sql(alias: str) -> str:
    """Reconstruct the existing observation document from its typed v2 row."""
    from investment_analyst.core.models import NormalizedObservation, SourceReference

    fields = {
        "asset_id": "asset_id",
        "available_at": "available_at",
        "field_name": "field_name",
        "frequency": "frequency",
        "normalized_at": "normalized_at",
        "observation_id": "observation_id",
        "observed_at": "observed_at",
        "period_end": "period_end",
        "period_start": "period_start",
        "quality": "quality",
        "raw_record_id": "raw_record_id",
        "source": "source",
        "transformation_version": "transformation_version",
        "unit": "unit",
        "value": "value_text",
    }
    source = {
        "checksum_sha256": "source_checksum_sha256",
        "raw_uri": "source_raw_uri",
        "record_key": "source_record_key",
        "retrieved_at": "source_retrieved_at",
        "source_id": "source_id",
    }
    if set(NormalizedObservation.model_fields) != set(fields) or set(
        SourceReference.model_fields
    ) != set(source):
        raise WorkspaceV2MigrationError("observation canonical contract changed")

    def value(column: str, *, timestamp: bool) -> str:
        return (
            _utc_document_timestamp(f"CAST({alias}.{column} AS TIMESTAMPTZ)")
            if timestamp
            else f"{alias}.{column}"
        )

    source_sql = (
        "json_object("
        + ",".join(
            f"'{name}',{value(column, timestamp=name == 'retrieved_at')}"
            for name, column in sorted(source.items())
        )
        + ")"
    )
    timestamps = {"available_at", "normalized_at", "observed_at", "period_end", "period_start"}
    return (
        "json_object("
        + ",".join(
            f"'{name}',"
            f"{source_sql if name == 'source' else value(column, timestamp=name in timestamps)}"
            for name, column in sorted(fields.items())
        )
        + ")"
    )


class _RecoveredCoreAppendIntent(ContractModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")
    schema_version: Literal["workspace-v2-core-append-intent-v1"] = (
        "workspace-v2-core-append-intent-v1"
    )
    successor: FrozenSourceCut
    source_session: UUID
    destination_root: Path
    destination_workspace_id: UUID
    core_baseline: ReconciliationFileReference
    module_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    family: Literal["raw", "observations"]
    facts: tuple[tuple[UUID, Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]], ...] = Field(
        min_length=1, max_length=256
    )


class _RecoveredCoreComparison(ContractModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")
    schema_version: Literal["workspace-v2-recovered-core-comparison-v1"] = (
        "workspace-v2-recovered-core-comparison-v1"
    )
    successor: FrozenSourceCut
    source_session: UUID
    core_baseline: ReconciliationFileReference
    resource_receipt: ReconciliationFileReference
    module_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    raw_count: int = Field(ge=0)
    raw_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    observation_count: int = Field(ge=0)
    observation_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    append_intents: tuple[ReconciliationFileReference, ...]


def _publish_recovered_core_comparison(
    destination: LocalStorage,
    frozen_source: FrozenSourceCut,
    reference: ReconciliationFileReference,
    admission_path: Path,
    raw: RawV2ImportSummary,
    observations: ObservationV2ImportSummary,
) -> None:
    session = _QUIESCENT_SOURCE.get()
    if session is None or session.cut != frozen_source:
        raise WorkspaceV2MigrationError("core comparison source session differs")
    state = destination.paths.root.parent.parent / "state"
    references = []
    module_sha = sha256_hex(Path(__file__).read_bytes())
    for path in sorted(state.glob(f"progress-core-append.{frozen_source.operation_id}.*.json")):
        control = reconciliation_file_reference(path, "control")
        intent = _RecoveredCoreAppendIntent.model_validate_json(path.read_bytes())
        if (
            intent.successor != frozen_source
            or intent.source_session != session.writer_generation
            or intent.core_baseline != reference
            or intent.module_sha256 != module_sha
            or intent.destination_workspace_id != destination.paths.workspace_id
            or intent.destination_root != state.parent
        ):
            raise WorkspaceV2MigrationError("core append intent binding differs")
        references.append(control)
    comparison = _RecoveredCoreComparison(
        successor=frozen_source,
        source_session=session.writer_generation,
        core_baseline=reference,
        resource_receipt=reconciliation_file_reference(admission_path, "resource"),
        module_sha256=module_sha,
        raw_count=raw.imported_count,
        raw_digest=raw.corpus_digest,
        observation_count=observations.imported_count,
        observation_digest=observations.corpus_digest,
        append_intents=tuple(references),
    )
    WorkspaceV2ProgressReconciler._write_once(
        state / f"progress-core-comparison.{frozen_source.operation_id}.json",
        canonical_json_bytes(comparison) + b"\n",
    )


def _require_recovered_core(
    destination: LocalStorage,
    frozen_source: FrozenSourceCut,
    reference: ReconciliationFileReference,
    admission_path: Path,
) -> MaintenanceExecution:
    """Bind native reuse to W and the exclusive same-session F1 reservation."""
    if reference.role != "control" or reference.path.is_symlink():
        raise WorkspaceV2MigrationError("recovered core proof reference differs")
    WorkspaceV2ProgressReconciler._verify(reference)
    if (
        json.loads(reference.path.read_bytes()).get("schema_version")
        == "workspace-v2-generation-content-proof-v1"
    ):
        return _require_generation_core(destination, frozen_source, reference, admission_path)
    core = _RecoveredCoreBaseline.model_validate_json(reference.path.read_bytes())
    baseline = ProgressReconciliation.model_validate_json(core.reconciliation.path.read_bytes())
    for control in (
        core.reconciliation,
        core.raw_state,
        core.raw_terminal,
        core.observation_state,
        core.resource_receipt,
    ):
        WorkspaceV2ProgressReconciler._verify(control)
    root = destination.paths.root.parent.parent
    admission = MaintenanceResourceReceipt.model_validate_json(admission_path.read_bytes())
    execution = require_maintenance_admission(admission)
    if (
        admission.budget.operation_id != frozen_source.operation_id
        or admission.budget.profile != "verification"
        or admission.budget.reader_worker is not None
        or {item.database for item in admission.budget.assignments}
        != {
            frozen_source.source_root / "storage/data/processed/investment_analyst.duckdb",
            destination.paths.database_path,
        }
    ):
        raise WorkspaceV2MigrationError("recovered core admission binding differs")
    reservation = _SuccessorReservation.model_validate_json(
        (
            root / "state" / f"progress-successor-reservation.{frozen_source.operation_id}.json"
        ).read_bytes()
    )
    session = _live_quiescent_source(frozen_source.source_root, frozen_source)
    progress = ProgressReconciliation.model_validate_json(
        (root / "state" / f"progress-reconciliation.{baseline.operation_id}.json").read_bytes()
    )
    from investment_analyst.storage import raw_v2_import

    if (
        session is None
        or reservation.source_session != session.writer_generation
        or reservation.successor != frozen_source
        or reservation.baseline != core.reconciliation
        or core.verifier_module_sha256 != sha256_hex(Path(__file__).read_bytes())
        or core.raw_verifier_sha256 != sha256_hex(Path(raw_v2_import.__file__).read_bytes())
        or core.resource_policy_sha256
        != sha256_hex((Path(__file__).parent.parent / "storage/resource_policy.py").read_bytes())
        or baseline.phase != "BASELINE_VERIFIED"
        or baseline.reserved_successor is not None
        or baseline.source_workspace_id != frozen_source.workspace_id
        or baseline.source_root != frozen_source.source_root
        or baseline.destination_root != root
        or baseline.destination_workspace_id != destination.paths.workspace_id
        or core.database != baseline.database_after
        or core.database.path != destination.paths.database_path
        or baseline.inventory is None
        or (core.raw_count, core.observation_count)
        != (baseline.inventory.raw_count, baseline.inventory.observation_count)
        or progress.phase != "SUCCESSOR_BOUND"
        or progress.reserved_successor != frozen_source
        or progress.inventory != baseline.inventory
    ):
        raise WorkspaceV2MigrationError("recovered core proof or source session changed")
    return execution


def _require_generation_core(
    destination: LocalStorage,
    frozen_source: FrozenSourceCut,
    reference: ReconciliationFileReference,
    admission_path: Path,
) -> MaintenanceExecution:
    root = destination.paths.root.parent.parent
    ledger = WorkspaceV2GenerationLedger(WorkspaceService(), root)
    resolved = ledger.resolve()
    if resolved is None:
        raise WorkspaceV2MigrationError("core generation head is missing")
    _, progress = resolved
    session = _live_quiescent_source(frozen_source.source_root, frozen_source)
    admission = MaintenanceResourceReceipt.model_validate_json(admission_path.read_bytes())
    execution = require_maintenance_admission(admission)
    reservation_path = (
        root / "state" / f"progress-successor-reservation.{frozen_source.operation_id}.json"
    )
    reservation = _SuccessorReservation.model_validate_json(
        ledger._read(reconciliation_file_reference(reservation_path, "control"))
    )
    baseline = ProgressReconciliationV2.model_validate_json(ledger._read(reservation.baseline))
    core = _generation_core_evidence(progress)
    if (
        progress.phase != "SUCCESSOR_BOUND"
        or progress.core_proof != reference
        or progress.reserved_successor != frozen_source
        or session is None
        or progress.successor_owner
        != CutOwner(
            source_session=session.writer_generation,
            pid=admission.pid,
            start_ticks=admission.process_start_ticks,
            boot_id=admission.boot_id,
            cgroup=admission.cgroup_path,
        )
        or progress.successor_admission != reconciliation_file_reference(admission_path, "resource")
        or reservation.reconciliation_id != progress.generation_id
        or reservation.successor != frozen_source
        or reservation.source_session != session.writer_generation
        or reservation.module_sha256 != sha256_hex(Path(__file__).read_bytes())
        or baseline.phase != "BASELINE_VERIFIED"
        or baseline.reserved_successor is not None
        or baseline.inventory != progress.inventory
        or baseline.core_proof != reference
        or baseline.database != core.database
        or core.database.path != destination.paths.database_path
        or admission.budget.operation_id != frozen_source.operation_id
        or admission.budget.profile != "verification"
        or admission.budget.reader_worker is not None
        or {item.database for item in admission.budget.assignments}
        != {
            frozen_source.source_root / "storage/data/processed/investment_analyst.duckdb",
            destination.paths.database_path,
        }
        or ledger.attempt_invalidated(frozen_source.operation_id)
    ):
        raise WorkspaceV2MigrationError("generation core proof or source session changed")
    return execution


def _core_native_pages(
    connection: DuckDBPyConnection,
    execution: MaintenanceExecution,
    limit: int,
) -> Iterator[list[tuple[object, ...]]]:
    while True:
        with execution.step("sql", execution.budget.sql_timeout_seconds):
            rows = connection.fetchmany(limit)
            if not rows:
                return
            yield rows


def _load_recovered_core_projection(
    target: DuckDBPyConnection,
    stream: DuckDBPyConnection,
    source_database: Path,
    execution: MaintenanceExecution,
    page_limit: int,
    *,
    raw: bool,
) -> None:
    """Load only the two cut-comparison indexes through one admitted spill file."""
    policy = connection_policy(source_database)
    if policy.temp_directory is None:
        raise WorkspaceV2MigrationError("core comparison requires its admitted spool")
    table = "_recovered_raw" if raw else "_recovered_observations"
    columns = "{'identifier':'VARCHAR','digest':'VARCHAR'" + (
        ",'metadata':'VARCHAR'}" if raw else "}"
    )
    count = 0
    longest = 1
    with NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        newline="",
        dir=policy.temp_directory,
        prefix="recovered-core-index-",
        suffix=".tsv",
    ) as projection:
        writer = csv.writer(projection, delimiter="\t", quoting=csv.QUOTE_ALL, lineterminator="\n")
        for rows in _core_native_pages(target, execution, page_limit):
            for row in rows:
                values = tuple(str(value) for value in row)
                # Size the parser from the actual projected record, rather than
                # imposing a new domain limit on an identifier or metadata text.
                longest = max(
                    longest,
                    sum(len(v.encode()) + v.count('"') for v in values) + 3 * len(values) + 2,
                )
                writer.writerow(values)
                count += 1
        projection.flush()
        if count:
            # This is an unordered temporary lookup, never the persisted index.
            # Avoid retaining an insertion-order buffer beside the join/sort.
            execute_bounded(stream, "SET preserve_insertion_order=false")
            execute_bounded(
                stream,
                f"INSERT INTO {table} SELECT * FROM read_csv(?,columns={columns}, "
                "auto_detect=false, delim='\t', header=false, "
                "quote='\"', escape='\"', strict_mode=true, max_line_size=?)",
                [projection.name, max(2_097_152, longest)],
            )
            if (
                execute_bounded(
                    stream,
                    f"SELECT identifier FROM {table} GROUP BY identifier "
                    "HAVING count(*)<>1 LIMIT 1",
                ).fetchone()
                is not None
            ):
                raise WorkspaceV2MigrationError(
                    "core comparison projection has duplicate identities"
                )


def _record_recovered_core_append(
    destination: LocalStorage,
    frozen_source: FrozenSourceCut,
    reference: ReconciliationFileReference,
    family: Literal["raw", "observations"],
    facts: tuple[tuple[UUID, str], ...],
) -> None:
    session = _QUIESCENT_SOURCE.get()
    if session is None or session.cut != frozen_source or destination.paths.workspace_id is None:
        raise WorkspaceV2MigrationError("core append source session differs")
    root = destination.paths.root.parent.parent
    intent = _RecoveredCoreAppendIntent(
        successor=frozen_source,
        source_session=session.writer_generation,
        destination_root=root,
        destination_workspace_id=destination.paths.workspace_id,
        core_baseline=reference,
        module_sha256=sha256_hex(Path(__file__).read_bytes()),
        family=family,
        facts=facts,
    )
    document = canonical_json_bytes(intent) + b"\n"
    path = (
        root
        / "state"
        / (
            f"progress-core-append.{frozen_source.operation_id}.{family}.{sha256_hex(document)}.json"
        )
    )
    WorkspaceV2ProgressReconciler._write_once(path, document)


def _recovered_raw_comparison_sql() -> tuple[str, str]:
    """Share the exact metadata comparison with the read-only E4 measurement."""
    fields = (
        "record_id",
        "asset_id",
        "source_id",
        "event_time",
        "available_at",
        "received_at",
        "schema_version",
    )
    timestamps = {"event_time", "available_at", "received_at"}

    def envelope(alias: str, *, target: bool) -> str:
        return (
            "json_array("
            + ",".join(
                _utc_document_timestamp(f"CAST({alias}.{field} AS TIMESTAMPTZ)")
                if field in timestamps and target
                else _utc_document_timestamp(f"{alias}.{field}")
                if field in timestamps
                else f"{alias}.{field}"
                for field in fields
            )
            + ")"
        )

    paths = (
        "["
        + ",".join(
            "'$.source.source_id'" if field == "source_id" else f"'$.{field}'" for field in fields
        )
        + "]"
    )
    expected = (
        "["
        + ",".join(
            _utc_document_timestamp(f"r.{field}") if field in timestamps else f"r.{field}"
            for field in fields
        )
        + "]"
    )
    return (
        "SELECT record_id,checksum_sha256,CAST("
        + envelope("r", target=True)
        + " AS VARCHAR) FROM raw_v2_index r",
        "SELECT r.record_id,r.checksum_sha256,sha256(r.document_json),w.digest,CAST("
        + envelope("r", target=False)
        + " AS VARCHAR) IS NOT DISTINCT FROM w.metadata,"
        + f"json_extract_string(r.document_json,{paths}) IS NOT DISTINCT FROM "
        + expected
        + ",octet_length(encode(r.document_json)),r.relative_path,"
        "r.source_id,r.schema_version,CAST("
        + envelope("r", target=False)
        + " AS VARCHAR) FROM raw_record_index r LEFT JOIN _recovered_raw w "
        "ON r.record_id=w.identifier ORDER BY r.received_at,r.record_id",
    )


def _recovered_observations_comparison_sql() -> tuple[str, str, tuple[str, ...], set[str]]:
    """Share the exact metadata comparison with the read-only E4 measurement."""
    fields = (
        "observation_id",
        "raw_record_id",
        "asset_id",
        "field_name",
        "frequency",
        "observed_at",
        "period_end",
        "available_at",
        "quality",
    )
    timestamps = {"observed_at", "period_end", "available_at"}
    paths = "[" + ",".join(f"'$.{name}'" for name in fields) + "]"
    envelope = (
        "["
        + ",".join(
            _utc_document_timestamp(f"s.{name}") if name in timestamps else f"s.{name}"
            for name in fields
        )
        + "]"
    )
    return (
        "SELECT observation_id,sha256(CAST("
        + _observation_canonical_sql("o")
        + " AS VARCHAR)) FROM normalized_observations_v2 o",
        "SELECT "
        + ",".join(
            _utc_document_timestamp(f"s.{name}") if name in timestamps else f"s.{name}"
            for name in fields
        )
        + ",sha256(s.document_json),w.digest,"
        + f"json_extract_string(s.document_json,{paths}) IS NOT DISTINCT FROM "
        + envelope
        + ",octet_length(encode(s.document_json)),"
        "json_extract_string(s.document_json,'$.source.source_id') "
        "FROM normalized_observations s LEFT JOIN _recovered_observations w "
        "ON s.observation_id=w.identifier ORDER BY s.available_at,s.observation_id",
        fields,
        timestamps,
    )


def _successor_comparison_fields(family: str) -> tuple[str, str, list[str]]:
    metric = family == "metrics"
    table, key = (
        ("metric_results", "result_id") if metric else ("diagnostic_results", "diagnostic_id")
    )
    fields = [
        key,
        "asset_id",
        *(["metric_key"] if metric else ["mode", "verdict"]),
        "as_of",
        "available_at",
        "computed_at",
        "quality",
    ]
    return table, key, fields


def _successor_comparison_sql(family: str, after: str = "") -> str:
    table, key, fields = _successor_comparison_fields(family)
    json_paths = "[" + ",".join(f"'$.{field}'" for field in fields) + "]"
    envelope = (
        "["
        + ",".join(
            _utc_document_timestamp(f"s.{field}")
            if field in {"as_of", "available_at", "computed_at"}
            else f"s.{field}"
            for field in fields
        )
        + "]"
    )
    return (
        f"SELECT s.{key},CAST(s.available_at AS VARCHAR),sha256(s.document_json),w.digest,"
        f"json_extract_string(s.document_json,{json_paths})={envelope},"
        "octet_length(encode(s.document_json)) "
        f"FROM {table} s LEFT JOIN _successor_w w "
        f"ON w.family_name='{family}' AND w.identifier=CAST(s.{key} AS UUID) "
        f"WHERE TRUE{after} ORDER BY s.available_at,s.{key}"
    )


def _validate_successor_comparison_rows(
    source: LocalStorage | DuckDBPyConnection,
    family: str,
    rows: list[tuple[object, ...]],
    *,
    timings: dict[str, float] | None = None,
    execution: MaintenanceExecution | None = None,
) -> dict[UUID, str]:
    """Keep the existing typed fallback, in <=256 model/1 MiB source batches."""
    from investment_analyst.core.models import DiagnosticResult, MetricResult
    from investment_analyst.storage.compact_analytical_v2 import _instant
    from investment_analyst.storage.serialization import model_from_json

    if len(rows) > 256:
        raise WorkspaceV2MigrationError("successor comparison page exceeds bounds")
    table, key, fields = _successor_comparison_fields(family)
    connection = source if isinstance(source, DuckDBPyConnection) else source.store.connection
    metadata_columns = ",".join(
        f"CAST({column} AS VARCHAR)"
        if column in {"as_of", "available_at", "computed_at"}
        else column
        for column in fields
    )
    pending = [row for row in rows if row[3] is None or row[2] != row[3] or row[4] is not True]
    verified: dict[UUID, str] = {}
    start = 0
    while start < len(pending):
        end, size = start, 0
        while end < len(pending):
            length = int(pending[end][5])
            if length > 1 << 20:
                raise WorkspaceV2MigrationError("successor model exceeds buffer budget")
            if end > start and size + length > 1 << 20:
                break
            size += length
            end += 1
        group = pending[start:end]
        identifiers = tuple(UUID(str(row[0])) for row in group)
        placeholders = ",".join("?" for _ in identifiers)
        lookup = f" FROM {table} WHERE {key} IN ({placeholders})"
        parameters = [str(identifier) for identifier in identifiers]
        # Both document and physical columns come from the same bounded read.
        # The stream is ordered by available_at; its range also enables native
        # zone-map pruning instead of rescanning all historical documents.
        query = (
            f"SELECT {key},document_json,{metadata_columns}"
            + lookup
            + " AND available_at BETWEEN CAST(? AS TIMESTAMPTZ) AND CAST(? AS TIMESTAMPTZ)"
        )
        instants = [_instant(row[1]) for row in group]
        bounded_parameters = [*parameters, min(instants), max(instants)]
        if execution is not None and f"{family}_shared_lookup" not in execution.query_plans():
            for name, sql, values in (
                ("documents_lookup", f"SELECT {key},document_json" + lookup, parameters),
                ("metadata_lookup", f"SELECT {key},{metadata_columns}" + lookup, parameters),
                ("shared_lookup", query, bounded_parameters),
            ):
                plan = execute_bounded(connection, "EXPLAIN " + sql, values).fetchall()
                execution.record_query_plan(
                    f"{family}_{name}", "\n".join(str(row[-1]) for row in plan)
                )
        started = time.monotonic()
        documents = execute_bounded(connection, query, bounded_parameters).fetchall()
        if timings is not None:
            timings["lookup_sql_seconds"] += time.monotonic() - started
        if (
            len(documents) != len(identifiers)
            or {UUID(str(row[0])) for row in documents} != set(identifiers)
            or sum(len(str(row[1]).encode("utf-8")) for row in documents) != size
        ):
            raise WorkspaceV2MigrationError("successor comparison metadata is incomplete")
        for row in documents:
            identifier = UUID(str(row[0]))
            started = time.monotonic()
            model = (
                model_from_json(MetricResult, row[1])
                if family == "metrics"
                else model_from_json(DiagnosticResult, row[1])
            )
            if timings is not None:
                timings["hydration_seconds"] += time.monotonic() - started
            started = time.monotonic()
            if family == "metrics":
                CompactAnalyticalStore._validate_metric_identity(model)
            for column, value in zip(fields, row[2:], strict=True):
                expected = getattr(model, column)
                equal = (
                    (_instant(value) == expected)
                    if column in {"as_of", "available_at", "computed_at"}
                    else str(value) == str(getattr(expected, "value", expected))
                )
                if not equal:
                    raise WorkspaceV2MigrationError("successor row metadata differs from model")
            verified[identifier] = sha256_hex(canonical_json_bytes(model))
            if timings is not None:
                timings["canonical_validation_seconds"] += time.monotonic() - started
        # Never retain hydrated models between bounded source batches.
        del documents, model
        start = end
    return verified


def measure_recovered_comparison(
    service: WorkspaceService,
    source_root: Path,
    destination_root: Path,
    generation_id: UUID,
    admission_path: Path,
) -> dict[str, object]:
    """Time the current comparison queries without a cut, reservation or imports.

    This is only E4 workload calibration, including the typed fallback. The
    final source session still needs its own comparison and completion proof.
    Both native databases are read-only and all projections are temporary.
    """
    receipt = MaintenanceResourceReceipt.model_validate_json(admission_path.read_bytes())
    execution = require_maintenance_admission(receipt)
    ledger = WorkspaceV2GenerationLedger(service, destination_root)
    resolved = ledger.resolve()
    if resolved is None:
        raise WorkspaceV2MigrationError("comparison measurement requires certified W2")
    head, baseline = resolved
    if (
        baseline.generation_id != generation_id
        or baseline.phase != "BASELINE_VERIFIED"
        or baseline.reserved_successor is not None
        or baseline.source_root != source_root
        or baseline.destination_root != destination_root
        or baseline.inventory is None
        or baseline.parent_membership is None
        or service.manifest(source_root).format_version != 1
        or service.manifest(destination_root).format_version != 2
    ):
        raise WorkspaceV2MigrationError("comparison measurement baseline binding differs")
    source_database = source_root / "storage/data/processed/investment_analyst.duckdb"
    target_database = destination_root / "storage/v2/index.duckdb"

    def identities() -> tuple[tuple[int, ...], ...]:
        observed = []
        for database in (source_database, target_database):
            if database.is_symlink() or database.with_suffix(database.suffix + ".wal").exists():
                raise WorkspaceV2MigrationError("comparison measurement database is not closed")
            metadata = database.stat()
            observed.append(
                (
                    metadata.st_dev,
                    metadata.st_ino,
                    metadata.st_size,
                    metadata.st_mtime_ns,
                    metadata.st_ctime_ns,
                )
            )
        return tuple(observed)

    before = identities()
    measurements: dict[str, object] = {}
    with (
        connect_duckdb(source_database, read_only=True) as source,
        connect_duckdb(target_database, read_only=True) as target,
    ):
        for family in ("raw", "observations"):
            started = time.monotonic()
            raw = family == "raw"
            table = "_recovered_raw" if raw else "_recovered_observations"
            execute_bounded(
                source,
                f"CREATE TEMP TABLE {table} "
                "(identifier VARCHAR,digest VARCHAR" + (",metadata VARCHAR)" if raw else ")"),
            )
            target_sql, source_sql = (
                _recovered_raw_comparison_sql()
                if raw
                else _recovered_observations_comparison_sql()[:2]
            )
            execute_bounded(target, target_sql)
            _load_recovered_core_projection(
                target, source, source_database, execution, 256, raw=raw
            )
            plan = execute_bounded(source, "EXPLAIN " + source_sql).fetchall()
            execution.record_query_plan(family, "\n".join(str(row[-1]) for row in plan))
            execute_bounded(source, source_sql)
            count = known = typed = 0
            digest = hashlib.sha256(b"").hexdigest()
            for rows in _core_native_pages(source, execution, 256):
                for row in rows:
                    count += 1
                    known += row[3 if raw else 10] is not None
                    fast = (
                        (
                            row[3] is not None
                            and row[3] == row[1] == row[2]
                            and row[4] is True
                            and row[5] is True
                        )
                        if raw
                        else row[9] == row[10] and row[11] is True
                    )
                    typed += not fast
                    digest = hashlib.sha256(f"{digest}:{row[1 if raw else 9]}".encode()).hexdigest()
            execute_bounded(
                target,
                "SELECT checksum_sha256 FROM raw_v2_index "
                "ORDER BY CAST(received_at AS TIMESTAMPTZ),record_id"
                if raw
                else "SELECT sha256(CAST("
                + _observation_canonical_sql("o")
                + " AS VARCHAR)) FROM normalized_observations_v2 o "
                "ORDER BY CAST(available_at AS TIMESTAMPTZ),observation_id",
            )
            target_count = 0
            target_digest = hashlib.sha256(b"").hexdigest()
            for rows in _core_native_pages(target, execution, 256):
                for row in rows:
                    target_count += 1
                    target_digest = hashlib.sha256(f"{target_digest}:{row[0]}".encode()).hexdigest()
            measurements[family] = {
                "elapsed_seconds": time.monotonic() - started,
                "source_count": count,
                "baseline_known_count": known,
                "target_count": target_count,
                "requires_typed_validation": typed,
                "source_document_digest": digest,
                "target_digest": target_digest,
            }
            execute_bounded(source, f"DROP TABLE {table}")
        started = time.monotonic()
        membership = (baseline.parent_membership, baseline.inventory.delta_membership)
        for reference in membership:
            WorkspaceV2ProgressReconciler._verify(reference)
        execute_bounded(
            source,
            "CREATE TEMP TABLE _successor_w AS SELECT "
            "json_extract_string(json,'$[0]') AS family_name,"
            "CAST(json_extract_string(json,'$[1]') AS UUID) AS identifier,"
            "json_extract_string(json,'$[2]') AS digest "
            "FROM read_json_objects(?,format='newline_delimited',maximum_object_size=1024)",
            [[str(reference.path) for reference in membership]],
        )
        duplicate = execute_bounded(
            source,
            "SELECT 1 FROM _successor_w GROUP BY family_name,identifier HAVING count(*)<>1 LIMIT 1",
        ).fetchone()
        if duplicate is not None:
            raise WorkspaceV2MigrationError("comparison measurement membership is ambiguous")
        measurements["financial_membership_seconds"] = time.monotonic() - started
        execute_bounded(source, "SET index_scan_max_count=0")
        execute_bounded(source, "SET index_scan_percentage=0")
        # The ordinary v1 facade opens a physical RW engine even in logical
        # read mode. Reuse the admitted physical RO instance through another
        # registered cursor and the same typed repositories instead.
        with connect_duckdb(source_database, read_only=True) as reader:
            for family in ("metrics", "diagnostics"):
                started = time.monotonic()
                execute_bounded(source, _successor_comparison_sql(family))
                comparison_sql_seconds = time.monotonic() - started
                count = known = typed = 0
                digest = hashlib.sha256(b"").hexdigest()
                canonical_digest = hashlib.sha256(b"").hexdigest()
                known_hash_mismatch = known_metadata_mismatch = 0
                timings = dict.fromkeys(
                    ("lookup_sql_seconds", "hydration_seconds", "canonical_validation_seconds"), 0.0
                )
                stream_started = time.monotonic()
                typed_started = 0.0
                for rows in _core_native_pages(source, execution, 256):
                    started_typed = time.monotonic()
                    validated = _validate_successor_comparison_rows(
                        reader, family, rows, timings=timings, execution=execution
                    )
                    typed_started += time.monotonic() - started_typed
                    for row in rows:
                        count += 1
                        known += row[3] is not None
                        typed += not (row[3] is not None and row[2] == row[3] and row[4] is True)
                        digest = hashlib.sha256(f"{digest}:{row[2]}".encode()).hexdigest()
                        known_hash_mismatch += row[3] is not None and row[2] != row[3]
                        known_metadata_mismatch += row[3] is not None and row[4] is not True
                        actual = validated.get(UUID(str(row[0])), str(row[2]))
                        if row[3] is not None and actual != row[3]:
                            raise WorkspaceV2MigrationError("successor changes recovered W content")
                        canonical_digest = hashlib.sha256(
                            f"{canonical_digest}:{actual}".encode()
                        ).hexdigest()
                measurements[family] = {
                    "elapsed_seconds": time.monotonic() - started,
                    "source_count": count,
                    "baseline_known_count": known,
                    "requires_typed_validation": typed,
                    "source_document_digest": digest,
                    "canonical_digest": canonical_digest,
                    "known_source_hash_mismatch": known_hash_mismatch,
                    "known_metadata_mismatch": known_metadata_mismatch,
                    "known_semantic_mismatch": 0,
                    "comparison_sql_seconds": comparison_sql_seconds,
                    "new_count": count - known,
                    "comparison_stream_and_digest_seconds": (
                        time.monotonic() - stream_started - typed_started
                    ),
                    **timings,
                }
    if identities() != before or ledger.resolve() != resolved:
        raise WorkspaceV2MigrationError("comparison measurement inputs changed")
    return {
        "calibration_only": True,
        "quiescence_claimed": False,
        "successor_reserved": False,
        "imports_performed": False,
        "generation_certificate": head.certificate.model_dump(mode="json"),
        "families": measurements,
    }


def _append_recovered_raw(
    *,
    source: LocalStorage,
    destination: LocalStorage,
    frozen_source: FrozenSourceCut,
    core_baseline: ReconciliationFileReference,
    admission_path: Path,
    page_limit: int,
) -> RawV2ImportSummary:
    """Internal recovered-W primitive; never reread confirmed immutable blobs.

    The coordinator must validate the preserved core proof before calling this
    primitive. This function publishes neither a checkpoint nor a cut seal.
    """
    from investment_analyst.core.models import RawRecord
    from investment_analyst.storage.serialization import model_from_json

    if isinstance(page_limit, bool) or not 1 <= page_limit <= 256:
        raise WorkspaceV2MigrationError("recovered raw page limit differs")
    if (
        source.paths.root != frozen_source.source_root / "storage"
        or source.paths.workspace_id != frozen_source.workspace_id
        or not source.read_only
        or _live_quiescent_source(frozen_source.source_root, frozen_source) is None
        or not isinstance(destination.store, WorkspaceV2Store)
    ):
        raise WorkspaceV2MigrationError("recovered raw requires its live source and v2 destination")
    execution = _require_recovered_core(destination, frozen_source, core_baseline, admission_path)
    target_sql, source_sql = _recovered_raw_comparison_sql()

    total = reused = max_hydrated = 0
    digest = hashlib.sha256(b"").hexdigest()
    by_source: dict[str, int] = {}
    by_schema: dict[str, int] = {}
    with connect_duckdb(source.paths.database_path, read_only=False) as stream:
        execute_bounded(
            stream,
            "CREATE TEMP TABLE _recovered_raw (identifier VARCHAR,digest VARCHAR,metadata VARCHAR)",
        )
        target = destination.store.connection
        execute_bounded(
            target,
            target_sql,
        )
        _load_recovered_core_projection(
            target, stream, source.paths.database_path, execution, page_limit, raw=True
        )
        execute_bounded(
            stream,
            source_sql,
        )
        for rows in _core_native_pages(stream, execution, page_limit):
            pending_groups: list[tuple[UUID, ...]] = []
            pending_ids: list[UUID] = []
            pending_size = 0
            large_ids: set[UUID] = set()
            for row in rows:
                if (
                    row[3] is not None
                    and row[3] == row[1]
                    and row[4] is True
                    and row[2] == row[1]
                    and row[5] is True
                ):
                    continue
                path = source.paths.raw_dir / str(row[7])
                if path.is_symlink() or not path.resolve().is_relative_to(source.paths.raw_dir):
                    raise WorkspaceV2MigrationError("recovered raw source path is unsafe")
                length = path.stat().st_size
                if length > 1 << 20 or (row[6] is not None and int(row[6]) > 1 << 20):
                    if pending_ids:
                        pending_groups.append(tuple(pending_ids))
                        pending_ids, pending_size = [], 0
                    large_ids.add(UUID(str(row[0])))
                    pending_groups.append((UUID(str(row[0])),))
                    continue
                if pending_ids and (pending_size + length > 1 << 20 or len(pending_ids) == 255):
                    pending_groups.append(tuple(pending_ids))
                    pending_ids, pending_size = [], 0
                pending_ids.append(UUID(str(row[0])))
                pending_size += length
            if pending_ids:
                pending_groups.append(tuple(pending_ids))
            groups = iter(pending_groups)
            models: dict[UUID, RawRecord] = {}
            model: RawRecord | None = None
            for row in rows:
                known = row[3]
                actual = str(row[1])
                fast = (
                    known is not None
                    and known == actual
                    and row[4] is True
                    and row[2] == actual
                    and row[5] is True
                )
                if not fast:
                    identifier = UUID(str(row[0]))
                    if identifier not in models:
                        # Release the preceding batch before loading another;
                        # leave one model slot for noncanonical inline fallback.
                        model = None
                        models = {}
                        group = next(groups)
                        models = (
                            {group[0]: source.raw_records.get_verified_bounded(group[0])}
                            if len(group) == 1 and group[0] in large_ids
                            else source.raw_records.get_many(group)
                        )
                        max_hydrated = max(max_hydrated, len(models))
                    model = models[identifier]
                    actual = canonical_json_digest(model)
                    expected_metadata = [
                        str(model.record_id),
                        model.asset_id,
                        model.source.source_id,
                        *(
                            None if value is None else value.isoformat().replace("+00:00", "Z")
                            for value in (model.event_time, model.available_at, model.received_at)
                        ),
                        model.schema_version,
                    ]
                    if json.loads(str(row[10])) != expected_metadata or actual != row[1]:
                        raise WorkspaceV2MigrationError(
                            "recovered raw metadata or checksum changed"
                        )
                    if row[2] is not None and row[2] != actual:
                        inline = execute_bounded(
                            source.store.connection,
                            "SELECT document_json FROM raw_record_index WHERE record_id=?",
                            [str(identifier)],
                        ).fetchone()
                        assert inline is not None
                        inline_model = model_from_json(RawRecord, str(inline[0]))
                        if canonical_json_digest(inline_model) != actual:
                            raise WorkspaceV2MigrationError("recovered raw inline content changed")
                        del inline_model
                    if known is not None and known != actual:
                        raise WorkspaceV2MigrationError("recovered raw content changed")
                    if known is None:
                        _record_recovered_core_append(
                            destination,
                            frozen_source,
                            core_baseline,
                            "raw",
                            ((identifier, actual),),
                        )
                        staging = destination.store.raw_staging
                        if identifier in large_ids:
                            policy = connection_policy(destination.paths.database_path)
                            if policy.temp_directory is None:
                                raise WorkspaceV2MigrationError(
                                    "raw spool requires admitted directory"
                                )
                            staging.save_bounded(model, spool_directory=policy.temp_directory)
                        else:
                            relative = staging._write_blob(actual, canonical_json_bytes(model))
                            blob = staging._resolve_blob(relative.as_posix())
                            with blob.open("rb") as stream_file:
                                os.fsync(stream_file.fileno())
                            parent = blob.parent
                            while True:
                                descriptor = os.open(parent, os.O_RDONLY)
                                try:
                                    os.fsync(descriptor)
                                finally:
                                    os.close(descriptor)
                                if parent == destination.paths.raw_dir:
                                    break
                                parent = parent.parent
                            destination.raw_records.save_many((model,))
                    elif row[4] is not True:
                        raise WorkspaceV2MigrationError(
                            "recovered raw destination metadata changed"
                        )
                digest = hashlib.sha256(f"{digest}:{actual}".encode()).hexdigest()
                total += 1
                reused += known is not None
                by_source[str(row[8])] = by_source.get(str(row[8]), 0) + 1
                by_schema[str(row[9])] = by_schema.get(str(row[9]), 0) + 1
        target_count = 0
        target_digest = hashlib.sha256(b"").hexdigest()
        execute_bounded(
            target,
            "SELECT checksum_sha256 FROM raw_v2_index "
            "ORDER BY CAST(received_at AS TIMESTAMPTZ),record_id",
        )
        for rows in _core_native_pages(target, execution, page_limit):
            for row in rows:
                target_count += 1
                target_digest = hashlib.sha256(f"{target_digest}:{row[0]}".encode()).hexdigest()
        if (target_count, target_digest) != (total, digest):
            raise WorkspaceV2MigrationError("recovered raw full inventory differs")
    return RawV2ImportSummary(
        source_workspace_id=str(frozen_source.workspace_id),
        source_fingerprint=frozen_source.source_fingerprint,
        destination=str(destination.store.raw_staging.destination.absolute()),
        complete=True,
        imported_count=total,
        reused_count=reused,
        corpus_digest=digest,
        counts_by_source=by_source,
        counts_by_schema=by_schema,
        max_page_requested=page_limit,
        max_page_hydrated=max_hydrated,
    )


def _append_recovered_observations(
    *,
    source: LocalStorage,
    destination: LocalStorage,
    frozen_source: FrozenSourceCut,
    core_baseline: ReconciliationFileReference,
    admission_path: Path,
    raw_digest: str,
    page_limit: int,
) -> ObservationV2ImportSummary:
    """Compare all identities natively; hydrate only new/noncanonical rows.

    This internal primitive requires the caller's recovered-W proof and live
    source session. It publishes no migration checkpoint or completion seal.
    A replay checks already-written rows by their full canonical content.
    """
    if isinstance(page_limit, bool) or not 1 <= page_limit <= 256:
        raise WorkspaceV2MigrationError("recovered observation page limit differs")
    if (
        source.paths.root != frozen_source.source_root / "storage"
        or source.paths.workspace_id != frozen_source.workspace_id
        or not source.read_only
        or _live_quiescent_source(frozen_source.source_root, frozen_source) is None
    ):
        raise WorkspaceV2MigrationError("recovered observations require live source quiescence")
    if not isinstance(destination.store, WorkspaceV2Store):
        raise WorkspaceV2MigrationError("recovered observation destination is not v2")
    execution = _require_recovered_core(destination, frozen_source, core_baseline, admission_path)
    target_sql, source_sql, fields, timestamps = _recovered_observations_comparison_sql()
    source_database = source.paths.database_path
    digest = hashlib.sha256(b"").hexdigest()
    total = reused = max_hydrated = 0
    by_source: dict[str, int] = {}
    by_frequency: dict[str, int] = {}
    # Use registered connections, not unregistered DuckDB .cursor() clones.
    with connect_duckdb(source_database, read_only=False) as stream:
        execute_bounded(
            stream,
            "CREATE TEMP TABLE _recovered_observations (identifier VARCHAR,digest VARCHAR)",
        )
        target = destination.store.connection
        execute_bounded(
            target,
            target_sql,
        )
        _load_recovered_core_projection(
            target, stream, source_database, execution, page_limit, raw=False
        )
        execute_bounded(
            stream,
            source_sql,
        )
        for rows in _core_native_pages(stream, execution, page_limit):
            # Only metadata crosses this cursor. Bound hydrated UTF-8 documents
            # independently from the model count, including noncanonical rows.
            pending = [row for row in rows if row[9] != row[10] or row[11] is not True]
            canonical: dict[str, str] = {}
            start = 0
            while start < len(pending):
                end, size = start, 0
                while end < len(pending):
                    length = pending[end][12]
                    if length is None or int(length) > 1 << 20:
                        raise WorkspaceV2MigrationError(
                            "recovered observation exceeds buffer budget"
                        )
                    if size + int(length) > 1 << 20:
                        break
                    size += int(length)
                    end += 1
                batch = pending[start:end]
                models = source.observations.get_many(tuple(UUID(str(row[0])) for row in batch))
                max_hydrated = max(max_hydrated, len(models))
                encoded_size = 0
                for row in batch:
                    model = models[UUID(str(row[0]))]
                    for field, actual in zip(fields, row[:9], strict=True):
                        expected = getattr(model, field)
                        if field in timestamps:
                            equal = (
                                None if actual is None else datetime.fromisoformat(str(actual))
                            ) == expected
                        else:
                            equal = str(actual) == str(getattr(expected, "value", expected))
                        if not equal:
                            raise WorkspaceV2MigrationError(
                                "recovered observation metadata changed"
                            )
                    document = canonical_json_bytes(model)
                    encoded_size += len(document)
                    if encoded_size > 1 << 20:
                        raise WorkspaceV2MigrationError(
                            "canonical observations exceed buffer budget"
                        )
                    checksum = sha256_hex(document)
                    if row[10] is not None and row[10] != checksum:
                        raise WorkspaceV2MigrationError("recovered observation content changed")
                    canonical[str(row[0])] = checksum
                # Existing append-only writer validates lineage and all typed
                # fields. Never pass the confirmed canonical W rows to it.
                missing_ids = {UUID(str(row[0])) for row in batch if row[10] is None}
                missing = tuple(
                    model for identifier, model in models.items() if identifier in missing_ids
                )
                if missing:
                    _record_recovered_core_append(
                        destination,
                        frozen_source,
                        core_baseline,
                        "observations",
                        tuple(
                            (model.observation_id, canonical[str(model.observation_id)])
                            for model in missing
                        ),
                    )
                    destination.observations.save_many(missing)
                start = end
            for row in rows:
                checksum = canonical.get(str(row[0]), str(row[9]))
                digest = hashlib.sha256(f"{digest}:{checksum}".encode()).hexdigest()
                total += 1
                reused += row[10] is not None
                source_id = str(row[13])
                by_source[source_id] = by_source.get(source_id, 0) + 1
                frequency = str(row[4])
                by_frequency[frequency] = by_frequency.get(frequency, 0) + 1
        # A full ordered comparison catches missing W and foreign target rows,
        # including IDs inserted earlier than the old cursor.
        execute_bounded(
            target,
            "SELECT sha256(CAST("
            + _observation_canonical_sql("o")
            + " AS VARCHAR)) FROM normalized_observations_v2 o "
            "ORDER BY CAST(available_at AS TIMESTAMPTZ),observation_id",
        )
        target_count = 0
        target_digest = hashlib.sha256(b"").hexdigest()
        for rows in _core_native_pages(target, execution, page_limit):
            for row in rows:
                target_count += 1
                target_digest = hashlib.sha256(f"{target_digest}:{row[0]}".encode()).hexdigest()
        if (target_count, target_digest) != (total, digest):
            raise WorkspaceV2MigrationError("recovered observation full inventory differs")
    return ObservationV2ImportSummary(
        source_workspace_id=str(frozen_source.workspace_id),
        source_fingerprint=frozen_source.source_fingerprint,
        raw_digest=raw_digest,
        destination=str(destination.store.raw_staging.destination.absolute()),
        complete=True,
        imported_count=total,
        reused_count=reused,
        corpus_digest=digest,
        counts_by_source=by_source,
        counts_by_frequency=by_frequency,
        max_page_requested=page_limit,
        max_page_hydrated=max_hydrated,
    )


class _RecoveredCoreBaseline(ContractModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")
    schema_version: Literal["workspace-v2-core-reuse-proof-v1"] = "workspace-v2-core-reuse-proof-v1"
    reconciliation: ReconciliationFileReference
    raw_state: ReconciliationFileReference
    raw_terminal: ReconciliationFileReference
    observation_state: ReconciliationFileReference
    database: ReconciliationFileReference
    resource_receipt: ReconciliationFileReference
    raw_count: int = Field(ge=0)
    raw_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    raw_metadata_binding: str = Field(pattern=r"^[0-9a-f]{64}$")
    observation_count: int = Field(ge=0)
    observation_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    raw_verifier_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    verifier_module_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    resource_policy_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    immutable_payload_scope: Literal["reuse-preserved-content-addressed-raw-terminal-proof"] = (
        "reuse-preserved-content-addressed-raw-terminal-proof"
    )
    verified_at: UTCDateTime


def verify_recovered_core_baseline(
    *,
    service: WorkspaceService,
    destination_root: Path,
    reconciliation_id: UUID,
    admission_path: Path,
) -> ReconciliationFileReference:
    """Reconcile native raw metadata and observation bytes without rehydrating W.

    The old raw terminal proof already verified its immutable blob bytes. This
    pass proves the full native metadata/projection binding is still identical;
    it does not claim to have read every immutable payload again.
    """
    from investment_analyst.storage import raw_v2_import
    from investment_analyst.storage.observation_v2_import import ObservationV2ImportState
    from investment_analyst.storage.raw_v2 import (
        _INDEX_COLUMNS,
        WORKSPACE_RAW_JSON_PROJECTIONS_TABLE,
    )
    from investment_analyst.workspace.workspace_v2_lifecycle import (
        _closed_duckdb_file,
        _v2_writer_closed,
    )
    from investment_analyst.workspace.workspace_v2_migration_models import (
        HistoricalTransition,
        WorkspaceV2MigrationWorkCheckpoint,
    )

    admission = MaintenanceResourceReceipt.model_validate_json(admission_path.read_bytes())
    require_maintenance_admission(admission)
    database = destination_root / "storage/v2/index.duckdb"
    if (
        admission.budget.operation_id != reconciliation_id
        or admission.budget.profile != "verification"
        or admission.budget.reader_worker is not None
        or tuple(item.database for item in admission.budget.assignments) != (database,)
    ):
        raise WorkspaceV2MigrationError("core reuse admission binding differs")
    reconciler = WorkspaceV2ProgressReconciler(service, destination_root, reconciliation_id)
    with reconciler._exclusive():
        progress = ProgressReconciliation.model_validate_json(reconciler.path.read_bytes())
        if progress.phase != "BASELINE_VERIFIED" or progress.reserved_successor_id is not None:
            raise WorkspaceV2MigrationError("core reuse requires unreserved verified W")
        assert progress.database_after is not None and progress.inventory is not None
        reconciler._controls(progress)
        predecessor = progress.predecessor_operation_id
        staging_root = destination_root / "storage/v2"
        raw_path = staging_root / f"raw-v2-import-state.F.{predecessor}.json"
        terminal_path = staging_root / f"raw-v2-terminal-verification.F.{predecessor}.json"
        observation_path = staging_root / f"observation-v2-import-state.F.{predecessor}.json"
        raw_reference = reconciliation_file_reference(raw_path, "control")
        terminal_reference = reconciliation_file_reference(terminal_path, "control")
        observation_reference = reconciliation_file_reference(observation_path, "control")
        raw = RawV2ImportState.model_validate_json(raw_path.read_bytes())
        terminal = WorkspaceV2MigrationWorkCheckpoint.model_validate_json(
            terminal_path.read_bytes()
        )
        observation = ObservationV2ImportState.model_validate_json(observation_path.read_bytes())
        raw_code_sha = sha256_hex(Path(raw_v2_import.__file__).read_bytes())
        predecessor_control = HistoricalTransition.model_validate_json(
            progress.predecessor.path.read_bytes()
        )
        predecessor_fingerprint = predecessor_control.source_fingerprint
        if (
            terminal.operation_id != predecessor
            or terminal.snapshot_id != predecessor
            or terminal.source_root != progress.source_root
            or terminal.destination_root != destination_root
            or terminal.destination_workspace_id != progress.destination_workspace_id
            or terminal.verifier_policy != "paired-raw-terminal-F-v1"
            or terminal.tree_fingerprint != raw_code_sha
            or terminal.source_fingerprint != predecessor_fingerprint
            or raw.source_fingerprint != predecessor_fingerprint
            or observation.source_fingerprint != predecessor_fingerprint
            or raw.source_workspace_id != str(progress.source_workspace_id)
            or observation.source_workspace_id != raw.source_workspace_id
            or terminal.confirmed_count != raw.confirmed_count
            or terminal.accumulated_digest != raw.accumulated_digest
            or observation.raw_digest != raw.accumulated_digest
            or raw.confirmed_count != progress.inventory.raw_count
            or observation.confirmed_count != progress.inventory.observation_count
            or len(terminal.input_references) != 4
            or terminal.input_references[:3] != ("raw", raw.source_workspace_id, raw.staging_id)
        ):
            raise WorkspaceV2MigrationError("preserved core verification binding differs")
        with _v2_writer_closed(destination_root), _closed_duckdb_file(database):
            reconciler._verify(progress.database_after)
            if database.with_suffix(".duckdb.wal").exists():
                raise WorkspaceV2MigrationError("core reuse found unexpected WAL")
        metadata = hashlib.sha256()
        raw_digest = hashlib.sha256(b"").hexdigest()
        raw_count = observation_count = 0
        observation_digest = hashlib.sha256(b"").hexdigest()
        with service.open_storage(
            service.resolve(destination_root), WorkspaceAccessMode.READ_ONLY
        ) as target:
            if not isinstance(target.store, WorkspaceV2Store):
                raise WorkspaceV2MigrationError("core reuse destination is not v2")
            if (
                target.store.raw_staging._read_marker(
                    staging_root / "raw-v2-staging.json"
                ).staging_id
                != raw.staging_id
            ):
                raise WorkspaceV2MigrationError("core reuse staging identity differs")
            connection = target.store.connection
            execute_bounded(
                connection,
                "SELECT "
                + ",".join(_INDEX_COLUMNS)
                + " FROM raw_v2_index ORDER BY received_at,record_id",
            )
            while rows := connection.fetchmany(progress.page_limit):
                for row in rows:
                    checksum = str(row[2])
                    metadata.update(json.dumps(row, separators=(",", ":")).encode())
                    metadata.update(bytes.fromhex(checksum))
                    raw_digest = hashlib.sha256(f"{raw_digest}:{checksum}".encode()).hexdigest()
                    raw_count += 1
            exists = execute_bounded(
                connection,
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_schema='main' AND table_name=?",
                [WORKSPACE_RAW_JSON_PROJECTIONS_TABLE],
            ).fetchone()
            if exists is not None:
                execute_bounded(
                    connection,
                    "SELECT record_id,field_name,field_value FROM "
                    + WORKSPACE_RAW_JSON_PROJECTIONS_TABLE
                    + " ORDER BY record_id,field_name",
                )
                while rows := connection.fetchmany(progress.page_limit):
                    for row in rows:
                        metadata.update(json.dumps(row, separators=(",", ":")).encode())
            execute_bounded(
                connection,
                "SELECT sha256(CAST("
                + _observation_canonical_sql("o")
                + " AS VARCHAR)) FROM normalized_observations_v2 o "
                "ORDER BY CAST(available_at AS TIMESTAMPTZ),observation_id",
            )
            while rows := connection.fetchmany(progress.page_limit):
                for row in rows:
                    observation_digest = hashlib.sha256(
                        f"{observation_digest}:{row[0]}".encode()
                    ).hexdigest()
                    observation_count += 1
        if (
            (raw_count, raw_digest) != (raw.confirmed_count, raw.accumulated_digest)
            or metadata.hexdigest() != terminal.input_references[3]
            or (observation_count, observation_digest)
            != (observation.confirmed_count, observation.accumulated_digest)
        ):
            raise WorkspaceV2MigrationError("recovered core metadata or canonical content changed")
        baseline_document = canonical_json_bytes(progress) + b"\n"
        baseline_path = reconciler.state / (
            f"progress-reconciliation-generation.{reconciliation_id}."
            f"{sha256_hex(baseline_document)}.json"
        )
        baseline_reference = reconciliation_file_reference(baseline_path, "control")
        if baseline_reference.sha256 != sha256_hex(baseline_document):
            raise WorkspaceV2MigrationError("core reuse baseline certificate changed")
        artifact = _RecoveredCoreBaseline(
            reconciliation=baseline_reference,
            raw_state=raw_reference,
            raw_terminal=terminal_reference,
            observation_state=observation_reference,
            database=progress.database_after,
            resource_receipt=reconciliation_file_reference(admission_path, "resource"),
            raw_count=raw_count,
            raw_digest=raw_digest,
            raw_metadata_binding=metadata.hexdigest(),
            observation_count=observation_count,
            observation_digest=observation_digest,
            raw_verifier_sha256=raw_code_sha,
            verifier_module_sha256=sha256_hex(Path(__file__).read_bytes()),
            resource_policy_sha256=sha256_hex(
                (Path(__file__).parent.parent / "storage/resource_policy.py").read_bytes()
            ),
            verified_at=datetime.now(UTC),
        )
        document = canonical_json_bytes(artifact) + b"\n"
        path = (
            reconciler.state
            / f"progress-core-reuse.{reconciliation_id}.{sha256_hex(document)}.json"
        )
        reconciler._write_once(path, document)
        return reconciliation_file_reference(path, "control")


def verify_recovered_successor(
    *,
    service: WorkspaceService,
    destination_root: Path,
    reconciliation_id: UUID,
    frozen_source: FrozenSourceCut,
    admission_path: Path,
    baseline_code_root: Path | None = None,
) -> ProgressReconciliationReceipt | ProgressReconciliationV2:
    """Prove W subset F1 by IDs and content, with one ordered source hash stream.

    A known canonical hash reuses the validated W model. Every new or
    noncanonical document uses the existing typed repository. This records
    coverage only; it imports nothing, changes no seal and activates nothing.
    """
    from investment_analyst.storage.compact_analytical_v2 import _instant, _json_bytes
    from investment_analyst.workspace.workspace_v2_lifecycle import (
        _closed_duckdb_file,
        _v2_writer_closed,
    )

    reservation = reserve_recovered_successor(
        service=service,
        destination_root=destination_root,
        reconciliation_id=reconciliation_id,
        frozen_source=frozen_source,
        admission_path=admission_path,
        baseline_code_root=baseline_code_root,
    )
    admission = MaintenanceResourceReceipt.model_validate_json(admission_path.read_bytes())
    execution = require_maintenance_admission(admission)
    reconciler = WorkspaceV2ProgressReconciler(service, destination_root, reconciliation_id)
    module_sha = sha256_hex(Path(__file__).read_bytes())
    generational = isinstance(reservation, ProgressReconciliationV2)
    ledger = WorkspaceV2GenerationLedger(service, destination_root) if generational else None
    with nullcontext() if generational else reconciler._exclusive():
        if ledger is not None:
            resolved = ledger.resolve()
            if resolved is None or resolved[1] != reservation:
                raise WorkspaceV2MigrationError("successor generation head changed")
            progress = resolved[1]
            baseline_reference = resolved[0].certificate
            expected_head = ledger._head_digest()
            database = progress.database
            page_limit = 256
        else:
            progress = ProgressReconciliation.model_validate_json(reconciler.path.read_bytes())
            baseline_reference = reservation.certificate
            database = progress.database_after
            page_limit = progress.page_limit
        assert progress.inventory is not None and database is not None
        inventory = progress.inventory
        tree, environment = historical_transition_fingerprints(page_limit, progress.verifier_policy)
        if progress.phase == "SUCCESSOR_BOUND":
            assert progress.coverage is not None
            path = reconciler.state / (
                f"progress-successor-comparison.{frozen_source.operation_id}."
                f"{progress.coverage.comparison_sha256}.json"
            )
            if (
                reconciliation_file_reference(path, "control").sha256
                != progress.coverage.comparison_sha256
            ):
                raise WorkspaceV2MigrationError("successor comparison artifact differs")
            comparison = _SuccessorComparison.model_validate_json(path.read_bytes())
            if (
                comparison.work.module_sha256 != module_sha
                or comparison.work.successor != frozen_source
                or comparison.work.baseline_dataset_sha256 != inventory.dataset_sha256
                or comparison.work.family != "complete"
                or comparison.verifier_tree_fingerprint != tree
                or comparison.environment_fingerprint != environment
                or (comparison.work.covered_metric_count, comparison.work.covered_diagnostic_count)
                != (inventory.metric_count, inventory.diagnostic_count)
                or comparison.new_index.role != "inventory"
                or comparison.baseline.role != "control"
                or comparison.resource_receipt.role != "resource"
            ):
                raise WorkspaceV2MigrationError("successor comparison code or cut changed")
            reconciler._verify(comparison.baseline)
            reconciler._verify(comparison.resource_receipt)
            reconciler._verify(comparison.new_index)
            original = (
                ProgressReconciliationV2.model_validate_json(comparison.baseline.path.read_bytes())
                if generational
                else ProgressReconciliation.model_validate_json(
                    comparison.baseline.path.read_bytes()
                )
            )
            if original.phase != "BASELINE_VERIFIED" or original.inventory != inventory:
                raise WorkspaceV2MigrationError("successor comparison baseline differs")
            return reservation
        if progress.phase != "BASELINE_VERIFIED" or progress.reserved_successor != frozen_source:
            raise WorkspaceV2MigrationError(
                "successor comparison requires its exclusive reservation"
            )
        expected = _SuccessorCoverageWork(
            successor=frozen_source,
            baseline_dataset_sha256=inventory.dataset_sha256,
            module_sha256=module_sha,
        )
        work_path = reconciler.state / f"progress-successor-work.{frozen_source.operation_id}.json"
        index_path = reconciler.state / f"progress-successor-new.{frozen_source.operation_id}.jsonl"
        work = expected
        if work_path.exists():
            if work_path.is_symlink():
                raise WorkspaceV2MigrationError("successor checkpoint is unsafe")
            work = _SuccessorCoverageWork.model_validate_json(work_path.read_bytes())
            if (work.successor, work.baseline_dataset_sha256, work.module_sha256) != (
                expected.successor,
                expected.baseline_dataset_sha256,
                expected.module_sha256,
            ):
                raise WorkspaceV2MigrationError("successor checkpoint binding changed")
        if index_path.is_symlink():
            raise WorkspaceV2MigrationError("successor new-index path is unsafe")
        source_paths = service.resolve(frozen_source.source_root)
        source_database = source_paths.storage_root / "data/processed/investment_analyst.duckdb"
        index_hash = hashlib.sha256()
        with (
            _v2_writer_closed(destination_root),
            _closed_duckdb_file(database.path),
            index_path.open("a+b") as index,
        ):
            reconciler._verify(database)
            index.seek(0)
            remaining = work.new_index_bytes
            while remaining:
                chunk = index.read(min(1 << 20, remaining))
                if not chunk:
                    raise WorkspaceV2MigrationError("successor index is truncated")
                index_hash.update(chunk)
                remaining -= len(chunk)
            if index_hash.hexdigest() != work.new_index_sha256:
                raise WorkspaceV2MigrationError("successor index checksum differs")
            with service.open_storage(source_paths, WorkspaceAccessMode.READ_ONLY) as source:
                with connect_duckdb(source_database, read_only=False) as stream:
                    execute_bounded(stream, "SET index_scan_max_count=0")
                    execute_bounded(stream, "SET index_scan_percentage=0")
                    execute_bounded(source.store.connection, "SET index_scan_max_count=0")
                    execute_bounded(source.store.connection, "SET index_scan_percentage=0")
                    execute_bounded(
                        stream,
                        "CREATE TEMP TABLE _successor_w AS SELECT "
                        "json_extract_string(json,'$[0]') AS family_name,"
                        "CAST(json_extract_string(json,'$[1]') AS UUID) AS identifier,"
                        "json_extract_string(json,'$[2]') AS digest "
                        "FROM read_json_objects(?,format='newline_delimited',"
                        "maximum_object_size=1024)",
                        [
                            [
                                str(progress.parent_membership.path),
                                str(inventory.delta_membership.path),
                            ]
                        ],
                    )
                    duplicates = execute_bounded(
                        stream,
                        "SELECT 1 FROM _successor_w GROUP BY family_name,identifier "
                        "HAVING count(*)<>1 LIMIT 1",
                    ).fetchone()
                    counts = execute_bounded(
                        stream,
                        "SELECT count(*) FILTER(WHERE family_name='metrics'),"
                        "count(*) FILTER(WHERE family_name='diagnostics') FROM _successor_w",
                    ).fetchone()
                    if duplicates is not None or counts != (
                        inventory.metric_count,
                        inventory.diagnostic_count,
                    ):
                        raise WorkspaceV2MigrationError("successor baseline index coverage differs")
                    for family in ("metrics", "diagnostics"):
                        if work.family == "complete" or (
                            family == "metrics" and work.family == "diagnostics"
                        ):
                            continue
                        metric = family == "metrics"
                        _, key, _ = _successor_comparison_fields(family)
                        after, parameters = "", []
                        if work.available_at is not None:
                            after = (
                                f" AND (s.available_at,s.{key})>"
                                "(CAST(? AS TIMESTAMPTZ),CAST(? AS VARCHAR))"
                            )
                            parameters = [work.available_at.isoformat(), str(work.identifier)]
                        execute_bounded(
                            stream,
                            _successor_comparison_sql(family, after),
                            parameters,
                        )
                        while rows := stream.fetchmany(page_limit):
                            with execution.step("sql", admission.budget.sql_timeout_seconds):
                                typed = _validate_successor_comparison_rows(source, family, rows)
                                updates = work.model_dump()
                                additions = bytearray()
                                for row in rows:
                                    identifier = UUID(str(row[0]))
                                    known, actual = row[3], str(row[2])
                                    if known is not None and actual == known and row[4] is True:
                                        updates["fast_count"] += 1
                                    else:
                                        actual = typed[identifier]
                                        updates["typed_count"] += 1
                                    if known is not None and actual != known:
                                        raise WorkspaceV2MigrationError(
                                            "successor changes recovered W content"
                                        )
                                    count_field = "metric_count" if metric else "diagnostic_count"
                                    digest_field = (
                                        "metric_digest" if metric else "diagnostic_digest"
                                    )
                                    updates[count_field] += 1
                                    updates[digest_field] = hashlib.sha256(
                                        f"{updates[digest_field]}:{actual}".encode()
                                    ).hexdigest()
                                    if known is not None:
                                        covered = (
                                            "covered_metric_count"
                                            if metric
                                            else "covered_diagnostic_count"
                                        )
                                        updates[covered] += 1
                                    else:
                                        additions.extend(
                                            _json_bytes(
                                                [
                                                    family,
                                                    str(identifier),
                                                    actual,
                                                    _instant(row[1])
                                                    .isoformat()
                                                    .replace("+00:00", "Z"),
                                                ]
                                            )
                                            + b"\n"
                                        )
                                if len(additions) > 1 << 20:
                                    raise WorkspaceV2MigrationError(
                                        "successor index page exceeds buffer budget"
                                    )
                                index.seek(work.new_index_bytes)
                                tail = index.read(len(additions))
                                if not additions.startswith(tail):
                                    raise WorkspaceV2MigrationError(
                                        "successor index uncheckpointed tail differs"
                                    )
                                index.seek(0, os.SEEK_END)
                                index.write(additions[len(tail) :])
                                index.flush()
                                os.fsync(index.fileno())
                                index_hash.update(additions)
                                updates.update(
                                    available_at=_instant(rows[-1][1]),
                                    identifier=UUID(str(rows[-1][0])),
                                    new_index_bytes=work.new_index_bytes + len(additions),
                                    new_index_sha256=index_hash.hexdigest(),
                                )
                                work = _SuccessorCoverageWork.model_validate(updates)
                                reconciler._financial_checkpoint(work_path, work)
                        work = _SuccessorCoverageWork.model_validate(
                            {
                                **work.model_dump(),
                                "available_at": None,
                                "identifier": None,
                                "family": "diagnostics" if metric else "complete",
                            }
                        )
                        reconciler._financial_checkpoint(work_path, work)
                    execute_bounded(stream, "DROP TABLE _successor_w")
                totals = execute_bounded(
                    source.store.connection,
                    "SELECT (SELECT count(*) FROM metric_results),"
                    "(SELECT count(*) FROM diagnostic_results)",
                ).fetchone()
                if totals != (work.metric_count, work.diagnostic_count):
                    raise WorkspaceV2MigrationError("successor source inventory count differs")
            if index.seek(0, os.SEEK_END) != work.new_index_bytes:
                raise WorkspaceV2MigrationError("successor index contains unverified extra bytes")
            if (work.covered_metric_count, work.covered_diagnostic_count) != (
                inventory.metric_count,
                inventory.diagnostic_count,
            ):
                raise WorkspaceV2MigrationError("successor is missing recovered W IDs")
            if sha256_hex(Path(__file__).read_bytes()) != module_sha:
                raise WorkspaceV2MigrationError("successor verifier changed during execution")
            if _live_quiescent_source(frozen_source.source_root, frozen_source) is None:
                raise WorkspaceV2MigrationError("successor lost source quiescence")
            comparison = _SuccessorComparison(
                baseline=baseline_reference,
                work=work,
                new_index=reconciliation_file_reference(index_path, "inventory"),
                resource_receipt=reconciliation_file_reference(admission_path, "resource"),
                verifier_tree_fingerprint=tree,
                environment_fingerprint=environment,
                verified_at=datetime.now(UTC),
            )
            document = canonical_json_bytes(comparison) + b"\n"
            digest = sha256_hex(document)
            reconciler._write_once(
                reconciler.state
                / f"progress-successor-comparison.{frozen_source.operation_id}.{digest}.json",
                document,
            )
            coverage = SuccessorCoverageProof(
                successor=frozen_source,
                baseline_dataset_sha256=inventory.dataset_sha256,
                covered_metric_count=work.covered_metric_count,
                covered_diagnostic_count=work.covered_diagnostic_count,
                comparison_sha256=digest,
                verifier_tree_fingerprint=tree,
                verified_at=comparison.verified_at,
            )
            if ledger is not None:
                assert isinstance(progress, ProgressReconciliationV2)
                verifier_root = Path(__file__).resolve().parents[1]
                current_code = tuple(
                    reconciliation_file_reference(verifier_root / relative, "code")
                    for relative in (
                        "storage/workspace_v2_inventory.py",
                        "storage/compact_analytical_v2.py",
                        "storage/historical_analytical_import.py",
                        "workspace/workspace_v2_migration_models.py",
                    )
                )
                bound_v2 = progress.model_copy(
                    update={
                        "phase": "SUCCESSOR_BOUND",
                        "coverage": coverage,
                        "code": current_code,
                        "tree_fingerprint": tree,
                        "environment_fingerprint": environment,
                        "previous_certificate": baseline_reference,
                        "recorded_at": datetime.now(UTC),
                    }
                )
                bound_v2 = ProgressReconciliationV2.model_validate(bound_v2.model_dump())
                ledger.publish(bound_v2, expected_head_sha256=expected_head, admission=admission)
                return bound_v2
            bound = ProgressReconciliation.model_validate(
                {
                    **progress.model_dump(),
                    "phase": "SUCCESSOR_BOUND",
                    "coverage": coverage,
                    "updated_at": datetime.now(UTC),
                }
            )
            certificate = reconciler._publish(bound)
        return ProgressReconciliationReceipt(
            reconciliation=bound,
            certificate=certificate,
            stage="successor_bound",
            recorded_at=datetime.now(UTC),
        )


class WorkspaceV2MigrationProgress(ContractModel):
    """Safe event emitted by a resumable workspace migration phase."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    phase: Literal[
        "inspection_started",
        "inspection_completed",
        "source_hash_started",
        "source_hash_completed",
        "identity_bound",
        "documents_started",
        "documents_completed",
        "raw_prefix_validation_started",
        "raw_prefix_validation_page_verified",
        "raw_prefix_validation_completed",
        "raw_import_page_started",
        "raw_import_page_checkpointed",
        "raw_completion_validation_started",
        "raw_completion_validation_page_verified",
        "raw_completion_validation_completed",
        "observations_started",
        "observations_prefix_validation_started",
        "observations_prefix_validation_page_verified",
        "observations_prefix_validation_completed",
        "observations_import_page_started",
        "observations_import_page_checkpointed",
        "observations_completion_validation_started",
        "observations_completion_validation_page_verified",
        "observations_completion_validation_completed",
        "observations_completed",
        "historical_started",
        "historical_completed",
        "analytical_started",
        "analytical_completed",
        "closure_completed",
    ]
    confirmed_count: int = Field(ge=0)
    page_count: int = Field(ge=0)
    page_size: int = Field(ge=0, le=_MAX_PAGE_LIMIT)

    def to_json_dict(self) -> dict[str, object]:
        """Return only counters and a phase name for operational evidence."""
        return self.model_dump(mode="json")


class WorkspaceV2MigrationSummary(ContractModel):
    """Exact, serializable receipt for one complete migration run."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: str = "workspace-v2-migration-summary-v1"
    source_workspace_id: NonEmptyStr
    source_fingerprint: NonEmptyStr
    source_root: Path
    destination_root: Path
    destination_workspace_id: NonEmptyStr
    source_snapshot_id: NonEmptyStr | None = None
    page_limit: int = Field(ge=1, le=_MAX_PAGE_LIMIT)
    raw: RawV2ImportSummary
    observations: ObservationV2ImportSummary
    historical: WorkspaceV2InventorySummary
    analytical: WorkspaceV2InventorySummary
    document_count: int = Field(ge=0)
    completed_at: UTCDateTime

    def to_json_dict(self) -> dict[str, object]:
        return self.model_dump(mode="json")


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def source_fingerprint(service: WorkspaceService, source_root: Path) -> str:
    """Return a stable identity binding manifest and v1 database bytes."""
    paths = service.resolve(source_root)
    if paths.root.is_symlink() or paths.manifest_path.is_symlink():
        raise WorkspaceV2MigrationError("migration source must not contain symbolic links")
    inspection = service.inspect(paths.root)
    if inspection.format_version != 1 or inspection.errors:
        raise WorkspaceV2MigrationError("migration source must be a ready workspace v1")
    database = paths.storage_root / "data" / "processed" / "investment_analyst.duckdb"
    if not database.is_file() or database.is_symlink():
        raise WorkspaceV2MigrationError("migration source database is unavailable or unsafe")
    digest = hashlib.sha256()
    for label, path in (("manifest", paths.manifest_path), ("database", database)):
        digest.update(label.encode("ascii"))
        digest.update(b"\0")
        digest.update(_hash_file(path).encode("ascii"))
        digest.update(b"\0")
    documents = paths.storage_root / "data" / "documents" / "sha256"
    if documents.exists():
        if documents.is_symlink() or not documents.is_dir():
            raise WorkspaceV2MigrationError("migration source document store is unsafe")
        for document in sorted(documents.rglob("*")):
            if document.is_symlink():
                raise WorkspaceV2MigrationError("migration source document store is unsafe")
            if not document.is_file():
                continue
            digest.update(document.relative_to(documents).as_posix().encode("ascii"))
            digest.update(b"\0")
            digest.update(_hash_file(document).encode("ascii"))
            digest.update(b"\0")
    return digest.hexdigest()


def _snapshot_inventory_digest(files: object) -> str:
    """Hash the backup service's already verified physical inventory."""
    encoded = json.dumps(
        [item.model_dump(mode="json") for item in files],
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def create_v1_migration_source_snapshot(
    *,
    service: WorkspaceService,
    source_root: Path,
    snapshot_root: Path,
    host_free_bytes: Callable[[], int],
    reserve_bytes: int = _DEFAULT_RESERVE_BYTES,
) -> WorkspaceV1MigrationSourceSnapshot:
    """Create a quiesced, verified source copy and publish its descriptor.

    ``WorkspaceBackupService`` owns the process guard, immutable file inventory,
    copy and verification.  The descriptor is written only after the backup has
    atomically appeared, so an interrupted snapshot is never a migration source.
    """
    source_paths = service.resolve(source_root)
    snapshot_paths = service.resolve(snapshot_root)
    _validate_destination(source_paths.root, snapshot_paths.root)
    inspection = service.inspect(source_paths.root)
    if inspection.format_version != 1 or inspection.status != "ready":
        raise WorkspaceV2MigrationError("migration snapshot source must be a ready workspace v1")
    fingerprint = source_fingerprint(service, source_paths.root)
    estimated_copy_bytes = sum(
        path.stat().st_size for path in source_paths.root.rglob("*") if path.is_file()
    )
    _require_host_capacity(
        host_free_bytes,
        reserve_bytes=reserve_bytes,
        expected_write_bytes=estimated_copy_bytes,
    )
    manifest = WorkspaceBackupService(service).create(source_paths.root, snapshot_paths.root)
    descriptor = WorkspaceV1MigrationSourceSnapshot(
        snapshot_id=manifest.backup_id,
        source_workspace_id=inspection.workspace_id,
        source_created_at=service.manifest(source_paths.root).created_at,
        source_fingerprint=fingerprint,
        snapshot_root=snapshot_paths.root,
        created_at=manifest.created_at,
        physical_file_count=len(manifest.files),
        physical_total_bytes=sum(item.size_bytes for item in manifest.files),
        physical_inventory_sha256=_snapshot_inventory_digest(manifest.files),
    )
    try:
        service.publish_v1_migration_source_snapshot(
            snapshot_paths=snapshot_paths,
            descriptor=descriptor,
        )
    except Exception as error:
        raise WorkspaceV2MigrationError(
            "migration source snapshot descriptor could not be published"
        ) from error
    return descriptor


def load_v1_migration_source_snapshot(
    *, service: WorkspaceService, snapshot_root: Path
) -> WorkspaceV1MigrationSourceSnapshot:
    """Open a published source snapshot and prove its logical binding again."""
    paths = service.resolve(snapshot_root)
    descriptor_path = paths.state_root / _SOURCE_SNAPSHOT_FILENAME
    if descriptor_path.is_symlink() or not descriptor_path.is_file():
        raise WorkspaceV2MigrationError("migration source snapshot descriptor is missing or unsafe")
    try:
        descriptor = WorkspaceV1MigrationSourceSnapshot.model_validate_json(
            descriptor_path.read_text(encoding="utf-8")
        )
    except (OSError, ValueError) as error:
        raise WorkspaceV2MigrationError(
            "migration source snapshot descriptor is incompatible"
        ) from error
    if descriptor.snapshot_root != paths.root:
        raise WorkspaceV2MigrationError("migration source snapshot root does not match descriptor")
    inspection = service.inspect(paths.root)
    if (
        inspection.format_version != 1
        or inspection.errors
        or inspection.workspace_id != descriptor.source_workspace_id
        or source_fingerprint(service, paths.root) != descriptor.source_fingerprint
    ):
        raise WorkspaceV2MigrationError(
            "migration source snapshot no longer matches its descriptor"
        )
    return descriptor


def _validate_destination(source: Path, destination: Path) -> None:
    if not destination.is_absolute():
        raise WorkspaceV2MigrationError("migration destination must be absolute")
    if destination.is_symlink():
        raise WorkspaceV2MigrationError("migration destination must not be a symbolic link")
    source = source.resolve()
    destination = destination.resolve(strict=False)
    if destination == source or source in destination.parents or destination in source.parents:
        raise WorkspaceV2MigrationError("migration destination must be disjoint from source")


def _require_host_capacity(
    host_free_bytes: Callable[[], int],
    *,
    reserve_bytes: int,
    expected_write_bytes: int = 0,
) -> int:
    """Read host capacity through the caller-provided host API, never WSL statvfs."""
    available = host_free_bytes()
    if isinstance(available, bool) or not isinstance(available, int) or available < 0:
        raise WorkspaceV2MigrationError("host capacity reader returned an invalid value")
    if (
        isinstance(expected_write_bytes, bool)
        or not isinstance(expected_write_bytes, int)
        or expected_write_bytes < 0
    ):
        raise WorkspaceV2MigrationError("migration expected write bytes must be non-negative")
    required = reserve_bytes + expected_write_bytes
    if available < required:
        raise WorkspaceV2MigrationError("migration host disk reserve is insufficient")
    return available


def _emit(
    progress: Callable[[WorkspaceV2MigrationProgress], None] | None,
    phase: str,
    *,
    confirmed_count: int = 0,
    page_count: int = 0,
    page_size: int = 0,
) -> None:
    if progress is None:
        return
    try:
        progress(
            WorkspaceV2MigrationProgress(
                phase=phase,
                confirmed_count=confirmed_count,
                page_count=page_count,
                page_size=page_size,
            )
        )
    except Exception as error:
        raise WorkspaceV2MigrationError("migration progress observer failed") from error


def _raw_phase(progress: RawV2ImportProgress) -> WorkspaceV2MigrationProgress:
    mapping = {
        "prefix_validation_started": "raw_prefix_validation_started",
        "prefix_validation_page_verified": "raw_prefix_validation_page_verified",
        "prefix_validation_completed": "raw_prefix_validation_completed",
        "import_page_started": "raw_import_page_started",
        "import_page_checkpointed": "raw_import_page_checkpointed",
        "completion_validation_started": "raw_completion_validation_started",
        "completion_validation_page_verified": "raw_completion_validation_page_verified",
        "completion_validation_completed": "raw_completion_validation_completed",
    }
    return WorkspaceV2MigrationProgress(
        phase=mapping[progress.phase],
        confirmed_count=progress.confirmed_count,
        page_count=progress.page_count,
        page_size=progress.page_size,
    )


def _observation_phase(progress: ObservationV2ImportProgress) -> WorkspaceV2MigrationProgress:
    mapping = {
        "prefix_validation_started": "observations_prefix_validation_started",
        "prefix_validation_page_verified": "observations_prefix_validation_page_verified",
        "prefix_validation_completed": "observations_prefix_validation_completed",
        "import_page_started": "observations_import_page_started",
        "import_page_checkpointed": "observations_import_page_checkpointed",
        "completion_validation_started": "observations_completion_validation_started",
        "completion_validation_page_verified": "observations_completion_validation_page_verified",
        "completion_validation_completed": "observations_completion_validation_completed",
    }
    return WorkspaceV2MigrationProgress(
        phase=mapping[progress.phase],
        confirmed_count=progress.confirmed_count,
        page_count=progress.page_count,
        page_size=progress.page_size,
    )


def _copy_documents(source_documents: Path, destination: LocalStorage) -> int:
    if not source_documents.exists():
        return 0
    if source_documents.is_symlink() or not source_documents.is_dir():
        raise WorkspaceV2MigrationError("migration source document store is unsafe")
    copied = 0
    for document in sorted(source_documents.rglob("*")):
        if document.is_symlink():
            raise WorkspaceV2MigrationError("migration source document store is unsafe")
        if not document.is_file():
            continue
        checksum = document.name
        if len(checksum) != 64 or any(char not in "0123456789abcdef" for char in checksum):
            raise WorkspaceV2MigrationError("migration source document name is not a SHA-256")
        receipt = destination.documents.copy_verified_file(document, checksum)
        if receipt.sha256 != checksum:
            raise WorkspaceV2MigrationError("migration document identity changed during copy")
        copied += 1
    return copied


def migrate_workspace_v1_to_v2(
    *,
    service: WorkspaceService,
    source_root: Path,
    destination_root: Path,
    page_limit: int = _MAX_PAGE_LIMIT,
    reserve_bytes: int = _DEFAULT_RESERVE_BYTES,
    host_free_bytes: Callable[[], int] | None = None,
    progress: Callable[[WorkspaceV2MigrationProgress], None] | None = None,
    clock: Callable[[], datetime] | None = None,
    reconcile_source: bool = False,
    frozen_source: FrozenSourceCut | None = None,
    analytical_failure_injector: Callable[[str], None] | None = None,
    reconciliation_id: UUID | None = None,
    reconciliation_admission_path: Path | None = None,
    baseline_code_root: Path | None = None,
    core_baseline_path: Path | None = None,
) -> WorkspaceV2MigrationSummary:
    """Migrate an unchanged v1 source into a disjoint, resumable v2 workspace."""
    if (
        isinstance(page_limit, bool)
        or not isinstance(page_limit, int)
        or not 1 <= page_limit <= 256
    ):
        raise WorkspaceV2MigrationError("migration page limit must be an integer between 1 and 256")
    if isinstance(reserve_bytes, bool) or not isinstance(reserve_bytes, int) or reserve_bytes < 0:
        raise WorkspaceV2MigrationError("migration reserve bytes must be a non-negative integer")
    if host_free_bytes is None:
        raise WorkspaceV2MigrationError("migration requires an explicit host capacity reader")
    if (
        (reconciliation_id is None) != (core_baseline_path is None)
        or (reconciliation_id is None) != (reconciliation_admission_path is None)
        or (reconciliation_id is None and baseline_code_root is not None)
    ):
        raise WorkspaceV2MigrationError("migration reconciliation bindings are incomplete")
    if reconciliation_id is not None:
        if frozen_source is None or reconcile_source:
            raise WorkspaceV2MigrationError("migration reconciliation requires a live frozen cut")
        assert reconciliation_admission_path is not None
        verify_recovered_successor(
            service=service,
            destination_root=destination_root,
            reconciliation_id=reconciliation_id,
            frozen_source=frozen_source,
            admission_path=reconciliation_admission_path,
            baseline_code_root=baseline_code_root,
        )
    _require_host_capacity(host_free_bytes, reserve_bytes=reserve_bytes)
    _emit(progress, "inspection_started")
    source_paths = service.resolve(source_root)
    destination_paths = service.resolve(destination_root)
    _validate_destination(source_paths.root, destination_paths.root)
    if reconcile_source and frozen_source is not None:
        raise WorkspaceV2MigrationError("migration source cannot be both snapshot and frozen cut")
    snapshot = (
        load_v1_migration_source_snapshot(service=service, snapshot_root=source_paths.root)
        if reconcile_source
        else None
    )
    quiescence = _live_quiescent_source(source_paths.root, frozen_source)
    source_inspection = (
        service.inspect(source_paths.root) if quiescence is None else quiescence.inspection
    )
    if (
        source_inspection.format_version != 1
        or source_inspection.errors
        or (snapshot is None and source_inspection.status != "ready")
    ):
        raise WorkspaceV2MigrationError("migration source must be a ready workspace v1")
    _emit(progress, "inspection_completed")
    _require_host_capacity(host_free_bytes, reserve_bytes=reserve_bytes)
    _emit(progress, "source_hash_started")
    fingerprint = (
        source_fingerprint(service, source_paths.root)
        if quiescence is None
        else quiescence.cut.source_fingerprint
    )
    if snapshot is not None and snapshot.source_fingerprint != fingerprint:
        raise WorkspaceV2MigrationError("migration source snapshot fingerprint changed")
    if frozen_source is not None:
        source_manifest = service.manifest(source_paths.root)
        if (
            frozen_source.source_root != source_paths.root
            or frozen_source.workspace_id != source_manifest.workspace_id
            or frozen_source.workspace_format_version != source_manifest.format_version
            or frozen_source.created_at != source_manifest.created_at
            or frozen_source.source_fingerprint != fingerprint
        ):
            raise WorkspaceV2MigrationError(
                "frozen source cut no longer binds the migration source"
            )
    _emit(progress, "source_hash_completed")
    if destination_paths.root.exists() and not destination_paths.manifest_path.exists():
        raise WorkspaceV2MigrationError(
            "migration destination must be new or an initialized workspace v2"
        )
    initialized = service.initialize(destination_paths.root, format_version=2)
    historic_fingerprint: str | None = None
    historic_raw_digest: str | None = None
    transition: WorkspaceV2SourceTransitionState | None = None
    try:
        historic_identity = service.v1_migration_identity(initialized.paths)
    except WorkspaceAccessError:
        historic_identity = None
    if historic_identity is None:
        service.bind_v1_migration_identity(
            source_paths=source_paths,
            destination_paths=initialized.paths,
            source_fingerprint=fingerprint,
        )
    elif historic_identity.source_fingerprint == fingerprint:
        pass
    elif snapshot is None and frozen_source is None:
        raise WorkspaceV2MigrationError("migration source differs from the historic identity")
    else:
        source_manifest = service.manifest(source_paths.root)
        if (
            historic_identity.source_workspace_id != source_manifest.workspace_id
            or historic_identity.source_created_at != source_manifest.created_at
        ):
            raise WorkspaceV2MigrationError(
                "migration snapshot has another logical source identity"
            )
        historic_fingerprint = historic_identity.source_fingerprint
        with service.open_storage(initialized.paths, WorkspaceAccessMode.READ_ONLY) as target:
            if not isinstance(target.store, WorkspaceV2Store):
                raise WorkspaceV2MigrationError("migration destination did not open workspace v2")
            checkpoint = target.store.raw_staging.destination / "raw-v2-import-state.json"
            if checkpoint.is_symlink() or not checkpoint.is_file():
                raise WorkspaceV2MigrationError("historic raw checkpoint is missing or unsafe")
            checkpoint_hash = _hash_file(checkpoint)
            try:
                historic_raw_digest = RawV2ImportState.model_validate_json(
                    checkpoint.read_text(encoding="utf-8")
                ).accumulated_digest
            except (OSError, ValueError) as error:
                raise WorkspaceV2MigrationError(
                    "historic raw checkpoint is incompatible"
                ) from error
        transition = service.v2_source_transition(initialized.paths)
        if frozen_source is not None:
            if (
                transition is not None
                and transition.phase is not WorkspaceV2MigrationPhase.COMPLETE
            ):
                raise WorkspaceV2MigrationError(
                    "frozen source cut conflicts with an incomplete snapshot transition"
                )
            # A/B provenance and cursors remain intact. F owns separate files;
            # its complete bounded traversal uses save_many's identity anti-join.
            transition = None
            historic_fingerprint = None
            historic_raw_digest = None
        elif transition is None:
            transition = WorkspaceV2SourceTransitionState(
                phase=WorkspaceV2MigrationPhase.PREPARED,
                source_workspace_id=historic_identity.source_workspace_id,
                source_created_at=historic_identity.source_created_at,
                historic_source_fingerprint=historic_fingerprint,
                historic_checkpoint_sha256=checkpoint_hash,
                snapshot_id=snapshot.snapshot_id,
                snapshot_source_fingerprint=snapshot.source_fingerprint,
                destination_workspace_id=initialized.manifest.workspace_id,
                confirmed_equivalent_count=0,
                unconfirmed_equivalent_count=0,
                missing_count=0,
                conflicting_count=0,
                updated_at=(clock or (lambda: datetime.now(UTC)))(),
            )
            service.publish_v2_source_transition(
                destination_paths=initialized.paths,
                state=transition,
            )
        elif transition is not None:
            historic_checkpoint = checkpoint.with_name(
                f"raw-v2-import-state.A.{transition.historic_checkpoint_sha256}.json"
            )
            if (
                historic_checkpoint.is_symlink()
                or not historic_checkpoint.is_file()
                or _hash_file(historic_checkpoint) != transition.historic_checkpoint_sha256
                or transition.snapshot_id != snapshot.snapshot_id
                or transition.snapshot_source_fingerprint != snapshot.source_fingerprint
            ):
                raise WorkspaceV2MigrationError(
                    "migration source transition does not match the checkpoint"
                )
            try:
                historic_raw_digest = RawV2ImportState.model_validate_json(
                    historic_checkpoint.read_text(encoding="utf-8")
                ).accumulated_digest
            except (OSError, ValueError) as error:
                raise WorkspaceV2MigrationError(
                    "historic raw checkpoint is incompatible"
                ) from error
    _emit(progress, "identity_bound")
    now = clock or (lambda: datetime.now(UTC))
    with (
        service.open_storage(source_paths, WorkspaceAccessMode.READ_ONLY) as source,
        service.open_storage(initialized.paths, WorkspaceAccessMode.READ_WRITE) as destination,
    ):
        if not isinstance(destination.store, WorkspaceV2Store):
            raise WorkspaceV2MigrationError("migration destination did not open workspace v2")
        core_reference = (
            None
            if core_baseline_path is None
            else reconciliation_file_reference(core_baseline_path, "control")
        )
        if core_reference is not None:
            assert frozen_source is not None and reconciliation_admission_path is not None
            _require_recovered_core(
                destination, frozen_source, core_reference, reconciliation_admission_path
            )
        for asset in source.assets.list_all():
            destination.assets.upsert(asset)
        for definition in source.sources.list_all():
            destination.sources.upsert(definition)
        for definition in source.metric_definitions.list_all():
            destination.metric_definitions.upsert(definition)
        _require_host_capacity(host_free_bytes, reserve_bytes=reserve_bytes)
        _emit(progress, "documents_started")
        document_count = _copy_documents(source.paths.documents_dir / "sha256", destination)
        _emit(progress, "documents_completed")
        _require_host_capacity(host_free_bytes, reserve_bytes=reserve_bytes)
        staging = destination.store.raw_staging
        if core_reference is None:
            raw = RawV2Importer(
                source,
                staging,
                source_workspace_id=str(source_inspection.workspace_id),
                source_fingerprint=fingerprint,
                destination_connection=(
                    None if frozen_source is None else destination.store.connection
                ),
                historic_source_fingerprint=historic_fingerprint,
                epoch=None if frozen_source is None else frozen_source.operation_id,
                progress=(
                    lambda event: _emit(
                        progress,
                        _raw_phase(event).phase,
                        confirmed_count=event.confirmed_count,
                        page_count=event.page_count,
                        page_size=event.page_size,
                    )
                ),
            ).run(page_limit=page_limit)
        else:
            assert frozen_source is not None and reconciliation_admission_path is not None
            raw = _append_recovered_raw(
                source=source,
                destination=destination,
                frozen_source=frozen_source,
                core_baseline=core_reference,
                admission_path=reconciliation_admission_path,
                page_limit=page_limit,
            )
        if transition is not None and transition.phase is not WorkspaceV2MigrationPhase.COMPLETE:
            transition = service.advance_v2_source_transition(
                destination_paths=initialized.paths,
                state=transition.model_copy(
                    update={
                        "phase": WorkspaceV2MigrationPhase.VERIFYING,
                        "confirmed_equivalent_count": raw.imported_count,
                        "updated_at": now(),
                    }
                ),
            )
        _require_host_capacity(host_free_bytes, reserve_bytes=reserve_bytes)
        _emit(progress, "observations_started")
        if core_reference is None:
            observations = ObservationV2Importer(
                source,
                staging,
                source_workspace_id=str(source_inspection.workspace_id),
                source_fingerprint=fingerprint,
                raw_digest=raw.corpus_digest,
                historic_source_fingerprint=historic_fingerprint,
                historic_raw_digest=historic_raw_digest,
                epoch=None if frozen_source is None else frozen_source.operation_id,
                progress=(
                    lambda event: _emit(
                        progress,
                        _observation_phase(event).phase,
                        confirmed_count=event.confirmed_count,
                        page_count=event.page_count,
                        page_size=event.page_size,
                    )
                ),
            ).run(page_limit=page_limit)
        else:
            assert frozen_source is not None and reconciliation_admission_path is not None
            observations = _append_recovered_observations(
                source=source,
                destination=destination,
                frozen_source=frozen_source,
                core_baseline=core_reference,
                admission_path=reconciliation_admission_path,
                raw_digest=raw.corpus_digest,
                page_limit=page_limit,
            )
            _publish_recovered_core_comparison(
                destination,
                frozen_source,
                core_reference,
                reconciliation_admission_path,
                raw,
                observations,
            )
        _emit(progress, "observations_completed")
        _require_host_capacity(host_free_bytes, reserve_bytes=reserve_bytes)
        _emit(progress, "analytical_started")
        analytical_importer = WorkspaceV2AnalyticalImporter(
            DirectHistoricalAnalyticalSource(source),
            CompactAnalyticalStore(destination.store.connection),
            source_root=source_paths.root,
            destination_root=initialized.paths.root,
            state_root=initialized.paths.state_root,
            source_workspace_id=str(source_inspection.workspace_id),
            source_fingerprint=fingerprint,
            page_limit=page_limit,
            allow_source_transition=transition is not None or frozen_source is not None,
            direct_transport=True,
            epoch=None if frozen_source is None else frozen_source.operation_id,
            frozen_source=frozen_source,
            source_snapshot=snapshot,
            failure_injector=analytical_failure_injector,
        )
        if reconciliation_id is not None:
            assert reconciliation_admission_path is not None
            analytical_importer.bind_recovered_successor(
                reconciliation_id=reconciliation_id,
                admission_path=reconciliation_admission_path,
                baseline_code_root=baseline_code_root,
            )
        analytical = analytical_importer.run()
        historical = analytical
        _emit(progress, "historical_completed")
        _emit(progress, "analytical_completed")
    _emit(progress, "closure_completed")
    if transition is not None:
        service.advance_v2_source_transition(
            destination_paths=initialized.paths,
            state=transition.model_copy(
                update={"phase": WorkspaceV2MigrationPhase.COMPLETE, "updated_at": now()}
            ),
        )
    return WorkspaceV2MigrationSummary(
        source_workspace_id=str(source_inspection.workspace_id),
        source_fingerprint=fingerprint,
        source_root=source_paths.root,
        destination_root=initialized.paths.root,
        destination_workspace_id=str(initialized.manifest.workspace_id),
        source_snapshot_id=str(snapshot.snapshot_id) if snapshot is not None else None,
        page_limit=page_limit,
        raw=raw,
        observations=observations,
        historical=historical,
        analytical=analytical,
        document_count=document_count,
        completed_at=now(),
    )


__all__ = [
    "create_v1_migration_source_snapshot",
    "load_v1_migration_source_snapshot",
    "WorkspaceV2MigrationError",
    "WorkspaceV2MigrationProgress",
    "WorkspaceV2MigrationSummary",
    "migrate_workspace_v1_to_v2",
    "source_fingerprint",
]
