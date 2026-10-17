"""Finite, checkpointed catch-up from a quiesced v1 workspace to v2."""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4

from pydantic import ConfigDict, Field

from investment_analyst.core.models.base import ContractModel, NonEmptyStr, UTCDateTime
from investment_analyst.workspace.service import WorkspaceService
from investment_analyst.workspace.workspace_v2_lifecycle import WorkspaceV2LifecycleService
from investment_analyst.workspace.workspace_v2_lifecycle_models import (
    FrozenSourceCut,
    OperationalStateSeal,
    OperationalStateTransferReceipt,
)
from investment_analyst.workspace.workspace_v2_migration import (
    WorkspaceV2MigrationError,
    WorkspaceV2MigrationProgress,
    WorkspaceV2MigrationSummary,
    _live_quiescent_source,
    migrate_workspace_v1_to_v2,
    source_fingerprint,
)
from investment_analyst.workspace.workspace_v2_migration_models import (
    CutWindow,
    CutWindowGo,
    CutWindowPhase,
    ReconciliationFileReference,
)
from investment_analyst.workspace.workspace_v2_operational_state import (
    OperationalStateTransferError,
    seal_operational_state,
    transfer_operational_state,
)


class WorkspaceV2CutoverError(RuntimeError):
    """A cutover operation cannot advance without an immutable source cut."""


class WorkspaceV2CutWindowService:
    """Persist one boot-bound deadline; phase retries cannot grant extra downtime."""

    _NEXT: dict[str, frozenset[str]] = {
        "PREPARED": frozenset({"STOPPING", "ABORTED"}),
        "STOPPING": frozenset({"QUIESCENT", "RETURNING_V1"}),
        "QUIESCENT": frozenset({"APPLYING", "RETURNING_V1"}),
        "APPLYING": frozenset({"RECOVERY_VERIFIED", "RETURNING_V1"}),
        "RECOVERY_VERIFIED": frozenset({"ACTIVATING", "RETURNING_V1"}),
        "ACTIVATING": frozenset({"HEALTHY_V2", "ABORTED"}),
        "RETURNING_V1": frozenset({"HEALTHY_V1", "ABORTED"}),
        "HEALTHY_V1": frozenset(),
        "HEALTHY_V2": frozenset(),
        "ABORTED": frozenset(),
    }

    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> CutWindow:
        if any(p.is_symlink() for p in (self.path, *self.path.parents)):
            raise WorkspaceV2CutoverError("cut window path is unsafe")
        return CutWindow.model_validate_json(self.path.read_bytes())

    def prepare(self, window: CutWindow, go: CutWindowGo) -> ReconciliationFileReference:
        from investment_analyst.storage.workspace_v2_inventory import (
            WorkspaceV2ProgressReconciler,
            reconciliation_file_reference,
        )

        if (
            window.phase != "PREPARED"
            or window.source_workspace_id != go.source_workspace_id
            or window.destination_workspace_id != go.destination_workspace_id
            or window.baseline != go.baseline
            or window.recovery_reserve_seconds
            != next(b.upper_seconds for b in go.bounds if b.step == "cancel_close_restart")
        ):
            raise WorkspaceV2CutoverError("cut window GO binding differs")
        if self.path.exists():
            # Resume an existing identity only byte-exactly; never recalculate its clock.
            if self.load() != window:
                raise WorkspaceV2CutoverError("cut window already has another immutable start")
            return reconciliation_file_reference(self.path, "control")
        if self.path.parent.is_symlink() or not self.path.parent.is_dir():
            raise WorkspaceV2CutoverError("cut window controls directory is unsafe")
        if CutWindowGo.model_validate_json(window.go.path.read_bytes()) != go:
            raise WorkspaceV2CutoverError("cut window GO content differs")
        for reference in (
            window.go,
            window.code,
            window.baseline,
            window.resource_receipt,
            go.guard,
            go.capacity,
            *go.prerequisites,
            *(b.measurement for b in go.bounds),
        ):
            WorkspaceV2ProgressReconciler._verify(reference)
        document = window.model_dump_json().encode() + b"\n"
        WorkspaceV2ProgressReconciler._write_once(self.path, document)
        return reconciliation_file_reference(self.path, "control")

    def remaining_work_ns(self, *, boot_id: UUID, monotonic_ns: int) -> int:
        current = self.load()
        if boot_id != current.boot_id or monotonic_ns < current.started_monotonic_ns:
            raise WorkspaceV2CutoverError("cut window clock changed; quiescence invalid")
        return max(
            0,
            current.deadline_monotonic_ns
            - monotonic_ns
            - current.recovery_reserve_seconds * 1_000_000_000,
        )

    def admit_operation(self, *, operation: str, boot_id: UUID, monotonic_ns: int) -> CutWindow:
        """Admit an existing cut command against its original clock and measured GO.

        This does not create a window, advance a phase or provide supervision.
        In particular activation is admitted only after its durable intention.
        """
        from investment_analyst.storage.workspace_v2_inventory import (
            WorkspaceV2ProgressReconciler,
        )

        phases = {
            "catchup": {"QUIESCENT", "APPLYING"},
            "plan": {"APPLYING"},
            "upload": {"APPLYING"},
            "restore": {"APPLYING"},
            "activate": {"ACTIVATING"},
        }
        steps = {
            "catchup": ("comparison_delta", "operational"),
            "plan": ("physical_plan",),
            "upload": ("upload",),
            "restore": ("restore_open",),
            "activate": ("activation_health",),
        }
        current = self.load()
        if operation not in phases or current.phase not in phases[operation]:
            raise WorkspaceV2CutoverError("cut command is outside its durable window phase")
        WorkspaceV2ProgressReconciler._verify(current.go)
        go = CutWindowGo.model_validate_json(current.go.path.read_bytes())
        if (
            go.baseline != current.baseline
            or go.source_workspace_id != current.source_workspace_id
            or go.destination_workspace_id != current.destination_workspace_id
            or current.recovery_reserve_seconds
            != next(b.upper_seconds for b in go.bounds if b.step == "cancel_close_restart")
        ):
            raise WorkspaceV2CutoverError("cut command GO binding differs")
        for reference in (
            current.code,
            current.baseline,
            current.resource_receipt,
            go.guard,
            go.capacity,
            *go.prerequisites,
            *(b.measurement for b in go.bounds),
            current.activation_intent,
            current.recovery_receipt,
        ):
            if reference is not None:
                WorkspaceV2ProgressReconciler._verify(reference)
        remaining = self.remaining_work_ns(boot_id=boot_id, monotonic_ns=monotonic_ns)
        required = sum(b.upper_seconds for b in go.bounds if b.step in steps[operation])
        if remaining < required * 1_000_000_000:
            raise WorkspaceV2CutoverError("cut command cannot consume its recovery reserve")
        return current

    def advance(
        self,
        phase: CutWindowPhase,
        *,
        expected: ReconciliationFileReference,
        boot_id: UUID,
        monotonic_ns: int,
        at: datetime,
        step: str,
        activation_intent: ReconciliationFileReference | None = None,
        recovery_receipt: ReconciliationFileReference | None = None,
        binding: ReconciliationFileReference | None = None,
    ) -> ReconciliationFileReference:
        from investment_analyst.storage.workspace_v2_inventory import (
            WorkspaceV2ProgressReconciler,
            _reconciliation_lock,
            reconciliation_file_reference,
        )

        with _reconciliation_lock(self.path.parent):
            if reconciliation_file_reference(self.path, "control") != expected:
                raise WorkspaceV2CutoverError("cut window compare-and-swap failed")
            current = self.load()
            if phase not in self._NEXT[current.phase]:
                raise WorkspaceV2CutoverError("cut window phase transition is forbidden")
            remaining = self.remaining_work_ns(boot_id=boot_id, monotonic_ns=monotonic_ns)
            if (
                phase not in {"RETURNING_V1", "HEALTHY_V1", "ABORTED", "HEALTHY_V2"}
                and remaining <= 0
            ):
                raise WorkspaceV2CutoverError("cut window recovery reserve reached")
            if monotonic_ns > current.deadline_monotonic_ns and phase != "ABORTED":
                raise WorkspaceV2CutoverError("cut window deadline exceeded")
            for reference in (activation_intent, recovery_receipt, binding):
                if reference is not None:
                    WorkspaceV2ProgressReconciler._verify(reference)
            values = current.model_dump()
            values.update(phase=phase, step=step, updated_at=at, previous_receipt=expected)
            for key, reference in (
                ("activation_intent", activation_intent),
                ("recovery_receipt", recovery_receipt),
                ("binding", binding),
            ):
                if reference is not None:
                    values[key] = reference
            updated = CutWindow.model_validate(values)
            document = updated.model_dump_json().encode() + b"\n"
            digest = hashlib.sha256(document).hexdigest()
            WorkspaceV2ProgressReconciler._write_once(
                self.path.parent / f"cut-window-step.{updated.window_id}.{digest}.json", document
            )
            # Archive the exact prior state before replacing only this operational pointer.
            WorkspaceV2ProgressReconciler._write_once(
                self.path.parent / f"cut-window-step.{current.window_id}.{expected.sha256}.json",
                self.path.read_bytes(),
            )
            temporary = self.path.with_name(f".cut-window.{uuid4().hex}.tmp")
            try:
                WorkspaceV2ProgressReconciler._write_once(temporary, document)
                os.replace(temporary, self.path)
                descriptor = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
            finally:
                temporary.unlink(missing_ok=True)
            return reconciliation_file_reference(self.path, "control")


class WorkspaceV2CutoverPhase(StrEnum):
    """Monotonic phases persisted around the existing importer checkpoints."""

    FROZEN = "frozen"
    CATCHUP_RUNNING = "catchup_running"
    CATCHUP_COMPLETE = "catchup_complete"


class WorkspaceV2CutoverCheckpoint(ContractModel):
    """Small durable binding for a finite cutover epoch."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["workspace-v2-cutover-checkpoint-v1"] = (
        "workspace-v2-cutover-checkpoint-v1"
    )
    operation_id: UUID
    phase: WorkspaceV2CutoverPhase
    source: FrozenSourceCut
    destination_root: Path
    destination_workspace_id: UUID | None = None
    page_limit: int = Field(ge=1, le=256)
    updated_at: UTCDateTime
    migration_completed_at: UTCDateTime | None = None
    migration_source_fingerprint: NonEmptyStr | None = None


class WorkspaceV2CutoverService:
    """Coordinate a frozen source cut and existing resumable v1/v2 importers."""

    def __init__(
        self,
        workspace_service: WorkspaceService,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._workspace_service = workspace_service
        self._clock = clock

    def freeze(
        self,
        source_root: Path,
        *,
        operation_id: UUID,
        writer_closed: bool,
    ) -> FrozenSourceCut:
        """Bind a source only after its normal writer has been closed by the caller."""
        if not writer_closed:
            raise WorkspaceV2CutoverError("cutover source writer must be closed")
        try:
            proof = _live_quiescent_source(self._workspace_service.resolve(source_root).root)
            if proof is not None:
                if proof.cut.operation_id != operation_id:
                    raise WorkspaceV2CutoverError("quiescent source epoch differs")
                return proof.cut
            fingerprint = source_fingerprint(self._workspace_service, source_root)
            return WorkspaceV2LifecycleService(
                self._workspace_service, clock=self._clock
            ).freeze_source(
                source_root,
                operation_id=operation_id,
                source_fingerprint=fingerprint,
                writer_closed=True,
            )
        except (WorkspaceV2MigrationError, RuntimeError) as error:
            raise WorkspaceV2CutoverError("cutover source cannot be frozen") from error

    def catch_up(
        self,
        source: FrozenSourceCut,
        destination_root: Path,
        *,
        checkpoint_path: Path,
        host_free_bytes: Callable[[], int],
        reserve_bytes: int,
        operational_inventory_path: Path,
        operational_seal_path: Path,
        operational_checkpoint_path: Path,
        operational_receipt_path: Path,
        page_limit: int = 256,
        progress: Callable[[WorkspaceV2MigrationProgress], None] | None = None,
        reconciliation_id: UUID | None = None,
        reconciliation_admission_path: Path | None = None,
        baseline_code_root: Path | None = None,
        core_baseline_path: Path | None = None,
    ) -> WorkspaceV2MigrationSummary:
        """Apply a finite frozen cut, preserving importer checkpoints on interruption."""
        destination = destination_root.expanduser().resolve(strict=False)
        checkpoint = checkpoint_path.expanduser().resolve(strict=False)
        if not destination.is_absolute() or not checkpoint.is_absolute() or checkpoint.is_symlink():
            raise WorkspaceV2CutoverError("cutover destination or checkpoint is unsafe")
        if source.source_root == destination or source.source_root in destination.parents:
            raise WorkspaceV2CutoverError("cutover destination must be disjoint from frozen source")
        proof = _live_quiescent_source(source.source_root, source)
        observed = (
            source_fingerprint(self._workspace_service, source.source_root)
            if proof is None
            else proof.cut.source_fingerprint
        )
        if observed != source.source_fingerprint:
            raise WorkspaceV2CutoverError("frozen cut changed before catch-up")
        existing = _load_checkpoint(checkpoint)
        if existing is not None:
            if (
                existing.operation_id != source.operation_id
                or existing.source != source
                or existing.destination_root != destination
                or existing.page_limit != page_limit
            ):
                raise WorkspaceV2CutoverError("cutover checkpoint binding differs")
            if existing.phase is WorkspaceV2CutoverPhase.CATCHUP_COMPLETE:
                raise WorkspaceV2CutoverError("cutover checkpoint is already complete")
        _replace_checkpoint(
            checkpoint,
            WorkspaceV2CutoverCheckpoint(
                operation_id=source.operation_id,
                phase=WorkspaceV2CutoverPhase.CATCHUP_RUNNING,
                source=source,
                destination_root=destination,
                destination_workspace_id=(
                    self._workspace_service.manifest(destination).workspace_id
                    if (destination / "manifest.json").is_file()
                    else None
                ),
                page_limit=page_limit,
                updated_at=self._now(),
            ),
        )
        try:
            summary = migrate_workspace_v1_to_v2(
                service=self._workspace_service,
                source_root=source.source_root,
                destination_root=destination,
                page_limit=page_limit,
                reserve_bytes=reserve_bytes,
                host_free_bytes=host_free_bytes,
                frozen_source=source,
                clock=self._clock,
                progress=progress,
                reconciliation_id=reconciliation_id,
                reconciliation_admission_path=reconciliation_admission_path,
                baseline_code_root=baseline_code_root,
                core_baseline_path=core_baseline_path,
            )
        except WorkspaceV2MigrationError as error:
            raise WorkspaceV2CutoverError("finite cutover catch-up did not complete") from error
        self.complete_operational_state(
            source,
            destination,
            inventory_path=operational_inventory_path,
            seal_path=operational_seal_path,
            checkpoint_path=operational_checkpoint_path,
            receipt_path=operational_receipt_path,
            host_free_bytes=host_free_bytes,
            reserve_bytes=reserve_bytes,
        )
        _replace_checkpoint(
            checkpoint,
            WorkspaceV2CutoverCheckpoint(
                operation_id=source.operation_id,
                phase=WorkspaceV2CutoverPhase.CATCHUP_COMPLETE,
                source=source,
                destination_root=destination,
                destination_workspace_id=UUID(summary.destination_workspace_id),
                page_limit=page_limit,
                updated_at=self._now(),
                migration_completed_at=summary.completed_at,
                migration_source_fingerprint=summary.source_fingerprint,
            ),
        )
        return summary

    def preserve_recovery_controls(
        self,
        *,
        checkpoint_path: Path,
        operational_inventory_path: Path,
        operational_receipt_path: Path,
    ) -> tuple[Path, Path, Path]:
        """Include existing F receipts in its physical photo without rewriting history."""
        from investment_analyst.workspace.workspace_v2_backup import _v2_writer_guard

        checkpoint = _load_checkpoint(checkpoint_path)
        if (
            checkpoint is None
            or checkpoint.phase is not WorkspaceV2CutoverPhase.CATCHUP_COMPLETE
            or checkpoint.migration_source_fingerprint != checkpoint.source.source_fingerprint
        ):
            raise WorkspaceV2CutoverError(
                "recovery controls require complete core and operational F"
            )
        if operational_receipt_path.is_symlink() or not operational_receipt_path.is_file():
            raise WorkspaceV2CutoverError("operational recovery receipt is unsafe")
        try:
            receipt = OperationalStateTransferReceipt.model_validate_json(
                operational_receipt_path.read_bytes()
            )
        except (OSError, ValueError) as error:
            raise WorkspaceV2CutoverError("operational recovery receipt is malformed") from error
        destination = checkpoint.destination_root
        identity = self._workspace_service.manifest(destination)
        if (
            identity.workspace_id != checkpoint.destination_workspace_id
            or identity.format_version != 2
            or receipt.seal.source != checkpoint.source
            or receipt.seal.destination_root != destination
        ):
            raise WorkspaceV2CutoverError("recovery controls have different F/workspace bindings")
        state = self._workspace_service.resolve(destination).state_root
        operation_id = checkpoint.operation_id
        targets = (
            state / f"workspace-v2-cutover.F.{operation_id}.json",
            state / f"workspace-v2-operational-transfer.F.{operation_id}.json",
            state / f"workspace-v2-operational-inventory.F.{operation_id}.jsonl",
        )
        with _v2_writer_guard(destination):
            for source, target, expected in (
                (checkpoint_path, targets[0], _control_digest(checkpoint_path)),
                (operational_receipt_path, targets[1], _control_digest(operational_receipt_path)),
                (operational_inventory_path, targets[2], receipt.seal.inventory_sha256),
            ):
                _preserve_sealed_control(source, target, expected)
        return targets

    def complete_operational_state(
        self,
        source: FrozenSourceCut,
        destination_root: Path,
        *,
        inventory_path: Path,
        seal_path: Path,
        checkpoint_path: Path,
        receipt_path: Path,
        host_free_bytes: Callable[[], int],
        reserve_bytes: int,
    ) -> OperationalStateTransferReceipt:
        """Seal and transfer only the frozen operational state accompanying a v2 cut."""
        destination = destination_root.expanduser().resolve(strict=False)
        if not destination.is_absolute() or destination.is_symlink():
            raise WorkspaceV2CutoverError("operational cutover destination is unsafe")
        try:
            seal = _load_operational_seal(seal_path)
            if seal is None:
                seal = seal_operational_state(
                    source.source_root,
                    destination,
                    source=source,
                    destination_workspace_id=self._workspace_service.manifest(
                        destination
                    ).workspace_id,
                    inventory_path=inventory_path,
                    clock=self._clock,
                )
                _replace_json(seal_path, seal.model_dump(mode="json"))
            elif seal.source != source or seal.destination_root != destination:
                raise WorkspaceV2CutoverError("operational seal binding differs")
            return transfer_operational_state(
                source.source_root,
                destination,
                seal=seal,
                inventory_path=inventory_path,
                checkpoint_path=checkpoint_path,
                receipt_path=receipt_path,
                available_bytes=host_free_bytes,
                reserve_bytes=reserve_bytes,
                clock=self._clock,
            )
        except (OperationalStateTransferError, RuntimeError) as error:
            raise WorkspaceV2CutoverError("operational state catch-up did not complete") from error

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise WorkspaceV2CutoverError("cutover clock must be timezone-aware")
        return value.astimezone(UTC)


def _load_checkpoint(path: Path) -> WorkspaceV2CutoverCheckpoint | None:
    if not path.exists():
        return None
    if path.is_symlink() or not path.is_file():
        raise WorkspaceV2CutoverError("cutover checkpoint is unsafe")
    try:
        return WorkspaceV2CutoverCheckpoint.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise WorkspaceV2CutoverError("cutover checkpoint is malformed") from error


def _load_operational_seal(path: Path) -> OperationalStateSeal | None:
    if not path.exists():
        return None
    if path.is_symlink() or not path.is_file():
        raise WorkspaceV2CutoverError("operational seal is unsafe")
    try:
        return OperationalStateSeal.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise WorkspaceV2CutoverError("operational seal is malformed") from error


def _replace_checkpoint(path: Path, checkpoint: WorkspaceV2CutoverCheckpoint) -> None:
    _replace_json(path, checkpoint.model_dump(mode="json"))


def _replace_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, sort_keys=True, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


__all__ = [
    "WorkspaceV2CutoverCheckpoint",
    "WorkspaceV2CutoverError",
    "WorkspaceV2CutoverPhase",
    "WorkspaceV2CutoverService",
]


def _control_digest(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise WorkspaceV2CutoverError("recovery control is missing or unsafe")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _preserve_sealed_control(source: Path, target: Path, expected: str) -> None:
    if source.is_symlink() or not source.is_file() or target.is_symlink():
        raise WorkspaceV2CutoverError("recovery control is missing or unsafe")
    if target.exists():
        if _control_digest(source) != expected or _control_digest(target) != expected:
            raise WorkspaceV2CutoverError("sealed recovery control differs")
        return
    temporary = target.with_name(f".{target.name}.{uuid4().hex}.tmp")
    digest = hashlib.sha256()
    try:
        with source.open("rb") as input_stream, temporary.open("xb") as output:
            while chunk := input_stream.read(1024 * 1024):
                digest.update(chunk)
                output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
        if digest.hexdigest() != expected:
            raise WorkspaceV2CutoverError("recovery control digest differs")
        os.replace(temporary, target)
        descriptor = os.open(target.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        temporary.unlink(missing_ok=True)
