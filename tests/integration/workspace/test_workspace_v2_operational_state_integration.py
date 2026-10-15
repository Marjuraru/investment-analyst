"""Isolated v1/v2 operational state cutover integration coverage."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from investment_analyst.workspace.service import WorkspaceService
from investment_analyst.workspace.workspace_v2_cutover import WorkspaceV2CutoverService
from investment_analyst.workspace.workspace_v2_migration import migrate_workspace_v1_to_v2

_MOMENT = datetime(2026, 10, 15, tzinfo=UTC)


def test_isolated_cutover_seals_and_transfers_an_empty_operational_partition(
    tmp_path: Path,
) -> None:
    """A v1/v2 fixture has explicit absences and needs no provider or financial replay."""
    service = WorkspaceService(environ={}, home=tmp_path / "home")
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    service.initialize(source, format_version=1)
    migrate_workspace_v1_to_v2(
        service=service,
        source_root=source,
        destination_root=destination,
        page_limit=1,
        host_free_bytes=lambda: 30_000_000_000,
        clock=lambda: _MOMENT,
    )
    cutover = WorkspaceV2CutoverService(service, clock=lambda: _MOMENT)
    frozen = cutover.freeze(source, operation_id=uuid4(), writer_closed=True)

    receipt = cutover.complete_operational_state(
        frozen,
        destination,
        inventory_path=tmp_path / "operational-inventory.jsonl",
        seal_path=tmp_path / "operational-seal.json",
        checkpoint_path=tmp_path / "operational-checkpoint.json",
        receipt_path=tmp_path / "operational-receipt.json",
        host_free_bytes=lambda: 10_000_000,
        reserve_bytes=1,
    )

    assert receipt.artifact_count == 0
    assert receipt.completed_families == ()
    assert (
        cutover.complete_operational_state(
            frozen,
            destination,
            inventory_path=tmp_path / "operational-inventory.jsonl",
            seal_path=tmp_path / "operational-seal.json",
            checkpoint_path=tmp_path / "operational-checkpoint.json",
            receipt_path=tmp_path / "operational-receipt.json",
            host_free_bytes=lambda: 10_000_000,
            reserve_bytes=1,
        )
        == receipt
    )
