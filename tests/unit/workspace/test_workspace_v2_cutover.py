"""Focused tests for a finite source cut and direct catch-up into v2."""

from datetime import UTC, datetime
from pathlib import Path
from uuid import NAMESPACE_URL, uuid4, uuid5

import pytest

from investment_analyst.core.models import RawRecord, SourceReference
from investment_analyst.workspace.models import WorkspaceAccessMode
from investment_analyst.workspace.service import WorkspaceService
from investment_analyst.workspace.workspace_v2_cutover import (
    WorkspaceV2CutoverError,
    WorkspaceV2CutoverPhase,
    WorkspaceV2CutoverService,
)
from investment_analyst.workspace.workspace_v2_migration import migrate_workspace_v1_to_v2

_MOMENT = datetime(2026, 10, 14, tzinfo=UTC)


def _save_raw(service: WorkspaceService, root: Path, key: str) -> None:
    record = RawRecord(
        record_id=uuid5(NAMESPACE_URL, key),
        asset_id="equity:us:aapl",
        source=SourceReference(source_id="fixture:cutover", record_key=key, retrieved_at=_MOMENT),
        event_time=_MOMENT,
        available_at=_MOMENT,
        received_at=_MOMENT,
        payload={"value": key},
        schema_version="fixture-cutover-v1",
    )
    with service.open_storage(service.resolve(root), WorkspaceAccessMode.READ_WRITE) as storage:
        storage.raw_records.save(record)


def test_frozen_cut_catches_up_changed_source_without_a_snapshot_copy(tmp_path: Path) -> None:
    service = WorkspaceService(environ={}, home=tmp_path / "home")
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    service.initialize(source, format_version=1)
    _save_raw(service, source, "base")
    migrate_workspace_v1_to_v2(
        service=service,
        source_root=source,
        destination_root=destination,
        page_limit=1,
        host_free_bytes=lambda: 30_000_000_000,
        clock=lambda: _MOMENT,
    )
    _save_raw(service, source, "late-revision")
    cutover = WorkspaceV2CutoverService(service, clock=lambda: _MOMENT)
    frozen = cutover.freeze(source, operation_id=uuid4(), writer_closed=True)

    summary = cutover.catch_up(
        frozen,
        destination,
        checkpoint_path=tmp_path / "cutover.json",
        host_free_bytes=lambda: 30_000_000_000,
        reserve_bytes=5_000_000_000,
        page_limit=1,
    )

    assert summary.raw.imported_count == 2
    checkpoint = (tmp_path / "cutover.json").read_text(encoding="utf-8")
    assert WorkspaceV2CutoverPhase.CATCHUP_COMPLETE.value in checkpoint
    assert service.inspect(destination).raw_record_count == 2


def test_cutover_rejects_active_writer_and_changed_frozen_source(tmp_path: Path) -> None:
    service = WorkspaceService(environ={}, home=tmp_path / "home")
    source = tmp_path / "source"
    service.initialize(source, format_version=1)
    cutover = WorkspaceV2CutoverService(service, clock=lambda: _MOMENT)
    with pytest.raises(WorkspaceV2CutoverError, match="writer must be closed"):
        cutover.freeze(source, operation_id=uuid4(), writer_closed=False)

    frozen = cutover.freeze(source, operation_id=uuid4(), writer_closed=True)
    _save_raw(service, source, "changed-after-cut")
    with pytest.raises(WorkspaceV2CutoverError, match="frozen cut changed"):
        cutover.catch_up(
            frozen,
            tmp_path / "destination",
            checkpoint_path=tmp_path / "cutover.json",
            host_free_bytes=lambda: 30_000_000_000,
            reserve_bytes=5_000_000_000,
        )
