"""Finite, checkpointed catch-up from a quiesced v1 workspace to v2."""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
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
    WorkspaceV2MigrationSummary,
    migrate_workspace_v1_to_v2,
    source_fingerprint,
)
from investment_analyst.workspace.workspace_v2_operational_state import (
    OperationalStateTransferError,
    seal_operational_state,
    transfer_operational_state,
)


class WorkspaceV2CutoverError(RuntimeError):
    """A cutover operation cannot advance without an immutable source cut."""


class WorkspaceV2CutoverPhase(StrEnum):
    """Monotonic phases persisted around the existing importer checkpoints."""

    FROZEN = "frozen"
    CATCHUP_RUNNING = "catchup_running"
    CATCHUP_COMPLETE = "catchup_complete"


class WorkspaceV2CutoverCheckpoint(ContractModel):
    """Small durable binding for a finite cutover epoch."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: str = "workspace-v2-cutover-checkpoint-v1"
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
    ) -> WorkspaceV2MigrationSummary:
        """Apply a finite frozen cut, preserving importer checkpoints on interruption."""
        destination = destination_root.expanduser().resolve(strict=False)
        checkpoint = checkpoint_path.expanduser().resolve(strict=False)
        if not destination.is_absolute() or not checkpoint.is_absolute() or checkpoint.is_symlink():
            raise WorkspaceV2CutoverError("cutover destination or checkpoint is unsafe")
        if source.source_root == destination or source.source_root in destination.parents:
            raise WorkspaceV2CutoverError("cutover destination must be disjoint from frozen source")
        observed = source_fingerprint(self._workspace_service, source.source_root)
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
