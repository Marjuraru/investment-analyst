"""Bounded verification of optional incremental artifacts in workspace format v2."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import ConfigDict, Field

from investment_analyst.core.models.base import ContractModel
from investment_analyst.storage.analysis_snapshot_v2 import (
    analysis_snapshot_v2_tables_exist,
)
from investment_analyst.storage.daily_evidence_v2 import (
    DailyEvidenceV2Store,
    daily_evidence_v2_tables_exist,
)
from investment_analyst.storage.local import LocalStorage
from investment_analyst.storage.market_checkpoint_v2 import (
    MarketCheckpointV2Store,
    market_checkpoint_v2_tables_exist,
)

_PAGE_SIZE = 256


class WorkspaceIncrementalV2Error(RuntimeError):
    """Optional incremental artifacts are incomplete or internally inconsistent."""


class WorkspaceIncrementalV2Verification(ContractModel):
    """Counts verified from one bounded read-only pass over incremental artifacts."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    daily_evidence_prefixes: int = Field(ge=0)
    market_recursive_checkpoints: int = Field(ge=0)
    analysis_snapshots: int = Field(ge=0)
    maximum_batch_size: int = Field(ge=0, le=_PAGE_SIZE)


def verify_workspace_incremental_v2(
    storage: LocalStorage,
) -> WorkspaceIncrementalV2Verification:
    """Verify adopted prefixes, checkpoints and snapshots without requiring old files."""
    storage.require_open()
    raw_staging = storage.store.raw_staging
    connection = storage.store.connection
    prefix_count = 0
    checkpoint_count = 0
    snapshot_count = 0
    maximum_batch_size = 0

    has_prefixes = daily_evidence_v2_tables_exist(connection)
    if has_prefixes:
        evidence_store = DailyEvidenceV2Store(connection)
        cursor: UUID | None = None
        while True:
            identifiers = evidence_store.list_ids_page(limit=_PAGE_SIZE, after_prefix_id=cursor)
            if not identifiers:
                break
            try:
                resolved = evidence_store.get_many(identifiers)
            except Exception as error:
                raise WorkspaceIncrementalV2Error(
                    "daily evidence prefix page failed verification"
                ) from error
            if set(resolved) != set(identifiers):
                raise WorkspaceIncrementalV2Error("daily evidence prefix inventory is incomplete")
            cursor = identifiers[-1]
            prefix_count += len(resolved)
            maximum_batch_size = max(maximum_batch_size, len(resolved))

    if market_checkpoint_v2_tables_exist(connection):
        if not has_prefixes:
            raise WorkspaceIncrementalV2Error("market checkpoints have no daily evidence table")
        checkpoint_store = MarketCheckpointV2Store(connection)
        cursor = None
        while True:
            identifiers = checkpoint_store.list_ids_page(
                limit=_PAGE_SIZE,
                after_checkpoint_id=cursor,
            )
            if not identifiers:
                break
            try:
                resolved = raw_staging.get_market_recursive_checkpoints(
                    identifiers,
                    metric_results=storage.metric_results,
                )
            except Exception as error:
                raise WorkspaceIncrementalV2Error(
                    "market checkpoint page failed verification"
                ) from error
            if set(resolved) != set(identifiers):
                raise WorkspaceIncrementalV2Error("market checkpoint inventory is incomplete")
            cursor = identifiers[-1]
            checkpoint_count += len(resolved)
            maximum_batch_size = max(maximum_batch_size, len(resolved))

    if analysis_snapshot_v2_tables_exist(connection):
        cursor_at: datetime | None = None
        cursor_id: UUID | None = None
        while True:
            try:
                snapshots = raw_staging.list_analysis_snapshots(
                    limit=_PAGE_SIZE,
                    cursor_at=cursor_at,
                    cursor_id=cursor_id,
                )
            except Exception as error:
                raise WorkspaceIncrementalV2Error(
                    "analysis snapshot page failed verification"
                ) from error
            if not snapshots:
                break
            identifiers = tuple(item.snapshot_id for item in snapshots)
            try:
                resolved = raw_staging.get_analysis_snapshots(identifiers)
            except Exception as error:
                raise WorkspaceIncrementalV2Error(
                    "analysis snapshot references failed verification"
                ) from error
            if set(resolved) != set(identifiers):
                raise WorkspaceIncrementalV2Error("analysis snapshot inventory is incomplete")
            cursor_at = snapshots[-1].created_at
            cursor_id = snapshots[-1].snapshot_id
            snapshot_count += len(resolved)
            maximum_batch_size = max(maximum_batch_size, len(resolved))

    return WorkspaceIncrementalV2Verification(
        daily_evidence_prefixes=prefix_count,
        market_recursive_checkpoints=checkpoint_count,
        analysis_snapshots=snapshot_count,
        maximum_batch_size=maximum_batch_size,
    )


__all__ = [
    "WorkspaceIncrementalV2Error",
    "WorkspaceIncrementalV2Verification",
    "verify_workspace_incremental_v2",
]
