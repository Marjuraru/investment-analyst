"""A real finite fixture cutover does not manufacture operational acceptance."""

import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from investment_analyst.workspace.service import WorkspaceService
from investment_analyst.workspace.workspace_v2_acceptance import (
    REQUIRED_GATES,
    AcceptanceGate,
    AcceptanceReference,
    AcceptanceScope,
    FinalAcceptanceError,
    FinalAcceptanceInput,
    PhysicalByteCategories,
    build_final_acceptance,
)
from investment_analyst.workspace.workspace_v2_cutover import WorkspaceV2CutoverService
from investment_analyst.workspace.workspace_v2_migration import (
    migrate_workspace_v1_to_v2,
    quiescent_source_cut,
)


def test_terminal_core_and_operational_fixture_still_require_real_acceptance(
    tmp_path: Path,
) -> None:
    service = WorkspaceService(environ={}, home=tmp_path / "home")
    source, destination = tmp_path / "source", tmp_path / "destination"
    service.initialize(source, format_version=1)
    initial = datetime(2026, 10, 15, 10, tzinfo=UTC)
    migrate_workspace_v1_to_v2(
        service=service,
        source_root=source,
        destination_root=destination,
        host_free_bytes=lambda: 30_000_000_000,
        page_limit=1,
        clock=lambda: initial,
    )
    controls = tmp_path / "controls"
    controls.mkdir()
    operation = uuid4()
    with quiescent_source_cut(
        service, source, operation_id=operation, clock=lambda: initial + timedelta(hours=1)
    ) as cut:
        WorkspaceV2CutoverService(service, clock=lambda: initial + timedelta(hours=2)).catch_up(
            cut,
            destination,
            checkpoint_path=controls / "cutover-checkpoint.json",
            host_free_bytes=lambda: 30_000_000_000,
            reserve_bytes=25_000_000_000,
            operational_inventory_path=controls / "operational-inventory.jsonl",
            operational_seal_path=controls / "operational-seal.json",
            operational_checkpoint_path=controls / "operational-checkpoint.json",
            operational_receipt_path=controls / "operational-receipt.json",
            page_limit=1,
        )
    checkpoint = controls / "cutover-checkpoint.json"
    reference = AcceptanceReference(
        path=checkpoint,
        sha256=hashlib.sha256(checkpoint.read_bytes()).hexdigest(),
        code_sha="a" * 40,
        environment="isolated integration fixture",
    )
    scope = AcceptanceScope(
        inventory_sha256=cut.source_fingerprint,
        inputs_sha256=cut.source_fingerprint,
        results_sha256=cut.source_fingerprint,
        algorithm_versions_sha256="b" * 64,
        provider_checks_sha256="c" * 64,
        universe=("fixture:empty",),
        workload="fixture cutover",
    )
    database = destination / "storage/v2/index.duckdb"
    value = FinalAcceptanceInput(
        base_sha="a" * 40,
        code_sha="a" * 40,
        runtime_sha="a" * 40,
        environment="isolated integration fixture",
        data_scope="fixture",
        workspace_root=destination,
        workspace_id=service.manifest(destination).workspace_id,
        operation_id=operation,
        reported_at=initial + timedelta(hours=3),
        scope=scope,
        required_families=("market",),
        required_job_pairs=(("market", "fixture:empty"),),
        required_memory_scenarios=("ordinary_cycle",),
        required_gates=REQUIRED_GATES,
        gates=tuple(
            AcceptanceGate(gate=name, status="UNKNOWN", reason="not operationally measured")
            for name in REQUIRED_GATES
        ),
        receipts=(reference,),
        bytes=PhysicalByteCategories(
            active_logical_bytes=database.stat().st_size,
            database_bytes=database.stat().st_size,
            wal_bytes=0,
            raw_bytes=0,
            parquet_bytes=0,
            linux_allocated_bytes=database.stat().st_blocks * 512,
            legacy_bytes=0,
            local_backup_bytes=0,
            controls_bytes=checkpoint.stat().st_size,
            spill_bytes=0,
            host_c_free_bytes=30_000_000_000,
            measured_at=initial + timedelta(hours=3),
        ),
    )
    report = build_final_acceptance(value)
    assert (report.met, report.not_met, report.unknown, report.accepted) == (0, 0, 7, False)
    assert all(item.status == "UNKNOWN" for item in report.objectives)
    original = checkpoint.read_bytes()
    checkpoint.write_bytes(original + b"\n")
    with pytest.raises(FinalAcceptanceError, match="digest_mismatch"):
        build_final_acceptance(value)
