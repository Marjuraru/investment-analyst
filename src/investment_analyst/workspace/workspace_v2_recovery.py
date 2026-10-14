"""Verified backup, restore, and reopen receipt for an isolated workspace v2."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from pydantic import ConfigDict

from investment_analyst.core.models.base import ContractModel, NonEmptyStr
from investment_analyst.workspace.service import WorkspaceService
from investment_analyst.workspace.workspace_v2_backup import (
    WorkspaceV2BackupError,
    WorkspaceV2BackupService,
)


class WorkspaceV2RecoveryError(RuntimeError):
    """Raised when a v2 recovery target cannot be proven equivalent."""


class WorkspaceV2RecoveryReceipt(ContractModel):
    """Small receipt binding a backup to a ready, disjoint restore workspace."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: str = "workspace-v2-recovery-receipt-v1"
    source_workspace_id: NonEmptyStr
    restored_workspace_id: NonEmptyStr
    backup_root: Path
    restore_root: Path
    raw_record_count: int
    observation_count: int
    metric_result_count: int
    diagnostic_result_count: int


def backup_and_restore_workspace_v2(
    *,
    service: WorkspaceService,
    source_root: Path,
    backup_root: Path,
    restore_root: Path,
    available_bytes: Callable[[], int] | None = None,
    reserve_bytes: int = 5_000_000_000,
) -> WorkspaceV2RecoveryReceipt:
    """Create and restore a complete v2 backup without accepting overlapping paths."""
    source = service.inspect(source_root)
    if source.format_version != 2 or source.status != "ready":
        raise WorkspaceV2RecoveryError("recovery source must be a ready workspace v2")
    roots = tuple(path.resolve(strict=False) for path in (source_root, backup_root, restore_root))
    overlaps = any(left in right.parents for left in roots for right in roots if left != right)
    if len(set(roots)) != len(roots) or overlaps:
        raise WorkspaceV2RecoveryError("recovery roots must be pairwise disjoint")
    if available_bytes is None:
        raise WorkspaceV2RecoveryError("recovery requires a host capacity reader")
    backup = WorkspaceV2BackupService(service)
    try:
        if backup_root.exists():
            _, manifest = backup.inspect_packed_for_restore(backup_root)
            if (
                manifest.source_workspace_id != source.workspace_id
                or manifest.counts.raw_records != source.raw_record_count
                or manifest.counts.observations != source.observation_count
                or manifest.counts.metric_results != source.metric_result_count
                or manifest.counts.diagnostic_results != source.diagnostic_result_count
            ):
                raise WorkspaceV2RecoveryError(
                    "existing packed backup differs from recovery source"
                )
        else:
            backup.create_packed(
                source_root,
                backup_root,
                available_bytes=available_bytes,
                reserve_bytes=reserve_bytes,
            )
        restored = backup.restore_packed(
            backup_root,
            restore_root,
            available_bytes=available_bytes,
            reserve_bytes=reserve_bytes,
        )
    except WorkspaceV2BackupError as error:
        raise WorkspaceV2RecoveryError(str(error)) from error
    if (
        restored.format_version != 2
        or restored.workspace_id != source.workspace_id
        or restored.raw_record_count != source.raw_record_count
        or restored.observation_count != source.observation_count
        or restored.metric_result_count != source.metric_result_count
        or restored.diagnostic_result_count != source.diagnostic_result_count
    ):
        raise WorkspaceV2RecoveryError("restored workspace is not equivalent to the v2 source")
    return WorkspaceV2RecoveryReceipt(
        source_workspace_id=str(source.workspace_id),
        restored_workspace_id=str(restored.workspace_id),
        backup_root=backup_root.resolve(strict=False),
        restore_root=restore_root.resolve(strict=False),
        raw_record_count=source.raw_record_count,
        observation_count=source.observation_count,
        metric_result_count=source.metric_result_count,
        diagnostic_result_count=source.diagnostic_result_count,
    )


__all__ = [
    "WorkspaceV2RecoveryError",
    "WorkspaceV2RecoveryReceipt",
    "backup_and_restore_workspace_v2",
]
