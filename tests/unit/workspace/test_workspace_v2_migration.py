"""Tests for the v1 to v2 migration coordinator."""

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

import pytest

from investment_analyst.core.models import (
    DataFrequency,
    DataQuality,
    NormalizedObservation,
    RawRecord,
    SourceReference,
)
from investment_analyst.workspace.models import WorkspaceAccessMode
from investment_analyst.workspace.service import WorkspaceService
from investment_analyst.workspace.workspace_v2_migration import (
    WorkspaceV2MigrationError,
    create_v1_migration_source_snapshot,
    migrate_workspace_v1_to_v2,
)
from investment_analyst.workspace.workspace_v2_migration_models import (
    WorkspaceV2MigrationVerificationProof,
    WorkspaceV2MigrationWorkCheckpoint,
    WorkspaceV2MigrationWorkPhase,
)

_MOMENT = datetime(2026, 10, 7, tzinfo=UTC)


def _seed(service: WorkspaceService, root: Path) -> None:
    initialized = service.initialize(root)
    source = SourceReference(
        source_id="fixture:migration",
        record_key="record",
        retrieved_at=_MOMENT,
    )
    raw = RawRecord(
        record_id=uuid5(NAMESPACE_URL, "migration-raw"),
        asset_id="equity:us:aapl",
        source=source,
        event_time=_MOMENT,
        available_at=_MOMENT,
        received_at=_MOMENT,
        payload={"close": "200.00"},
        schema_version="migration-test-v1",
    )
    observation = NormalizedObservation(
        observation_id=uuid5(NAMESPACE_URL, "migration-observation"),
        raw_record_id=raw.record_id,
        asset_id=raw.asset_id,
        field_name="close",
        value=Decimal("200.00"),
        unit="USD",
        frequency=DataFrequency.DAY_1,
        observed_at=_MOMENT,
        available_at=_MOMENT,
        normalized_at=_MOMENT,
        source=source,
        quality=DataQuality.VALID,
        transformation_version="migration-test-v1",
    )
    with service.open_storage(initialized.paths, WorkspaceAccessMode.READ_WRITE) as storage:
        storage.raw_records.save(raw)
        storage.observations.save(observation)


def test_migration_copies_v1_models_to_a_disjoint_v2_workspace(tmp_path: Path) -> None:
    service = WorkspaceService(environ={}, home=tmp_path / "home")
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    _seed(service, source)

    receipt = migrate_workspace_v1_to_v2(
        service=service,
        source_root=source,
        destination_root=destination,
        page_limit=1,
        host_free_bytes=lambda: 30_000_000_000,
        clock=lambda: _MOMENT,
    )

    assert receipt.raw.imported_count == 1
    assert receipt.observations.imported_count == 1
    assert receipt.historical.metric_count == 0
    assert receipt.analytical.metric_count == 0
    assert service.inspect(source).format_version == 1
    target = service.inspect(destination)
    assert target.format_version == 2
    assert target.raw_record_count == target.observation_count == 1


def test_migration_rejects_destination_inside_source(tmp_path: Path) -> None:
    service = WorkspaceService(environ={}, home=tmp_path / "home")
    source = tmp_path / "source"
    _seed(service, source)

    with pytest.raises(WorkspaceV2MigrationError, match="disjoint"):
        migrate_workspace_v1_to_v2(
            service=service,
            source_root=source,
            destination_root=source / "destination",
            host_free_bytes=lambda: 30_000_000_000,
        )


def test_work_checkpoint_is_strict_and_json_serializable() -> None:
    checkpoint = WorkspaceV2MigrationWorkCheckpoint(
        operation_id=uuid5(NAMESPACE_URL, "operation"),
        phase=WorkspaceV2MigrationWorkPhase.IMPORT,
        snapshot_id=uuid5(NAMESPACE_URL, "snapshot"),
        source_fingerprint="a" * 64,
        destination_workspace_id=uuid5(NAMESPACE_URL, "destination"),
        source_root=Path("/tmp/source"),
        destination_root=Path("/tmp/destination"),
        staging_root=Path("/tmp/staging"),
        policy_version="workspace-v2-migration-v1",
        reader_policy="snapshot-raw-readonly-v1",
        verifier_policy="full-model-lineage-v1",
        tree_fingerprint="c" * 64,
        environment_fingerprint="d" * 64,
        cursor="raw:256",
        ordinal=1,
        relative_path="storage/v2/raw/page-0001",
        offset_bytes=4096,
        page_limit=256,
        confirmed_count=256,
        confirmed_bytes=4096,
        accumulated_digest="b" * 64,
        input_references=("source-inventory:" + "e" * 64,),
        updated_at=_MOMENT,
    )

    assert (
        WorkspaceV2MigrationWorkCheckpoint.model_validate_json(checkpoint.model_dump_json())
        == checkpoint
    )


def test_verification_proof_is_strict_and_json_serializable() -> None:
    proof = WorkspaceV2MigrationVerificationProof(
        operation_id=uuid5(NAMESPACE_URL, "verification-operation"),
        snapshot_id=uuid5(NAMESPACE_URL, "verification-snapshot"),
        source_workspace_id=uuid5(NAMESPACE_URL, "verification-source"),
        destination_workspace_id=uuid5(NAMESPACE_URL, "verification-destination"),
        source_root=Path("/tmp/source"),
        destination_root=Path("/tmp/destination"),
        source_fingerprint="a" * 64,
        inventory_digest="b" * 64,
        family="analytical",
        subphase="paired_models",
        reader_policy="snapshot-raw-readonly-v1",
        verifier_policy="full-model-lineage-v1",
        tree_fingerprint="c" * 64,
        environment_fingerprint="d" * 64,
        coverage_count=256,
        source_digest="e" * 64,
        destination_digest="f" * 64,
        ordered_unit_proof_root="g" * 64,
        physical_binding="h" * 64,
        verified_at=_MOMENT,
    )

    assert (
        WorkspaceV2MigrationVerificationProof.model_validate_json(proof.model_dump_json()) == proof
    )


def test_snapshot_transition_preserves_a_checkpoint_and_imports_b_suffix(tmp_path: Path) -> None:
    service = WorkspaceService(environ={}, home=tmp_path / "home")
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    _seed(service, source)
    migrate_workspace_v1_to_v2(
        service=service,
        source_root=source,
        destination_root=destination,
        page_limit=1,
        host_free_bytes=lambda: 30_000_000_000,
        clock=lambda: _MOMENT,
    )
    reference = SourceReference(
        source_id="fixture:migration",
        record_key="record-b",
        retrieved_at=_MOMENT,
    )
    appended = RawRecord(
        record_id=uuid5(NAMESPACE_URL, "migration-raw-b"),
        asset_id="equity:us:aapl",
        source=reference,
        event_time=_MOMENT.replace(hour=1),
        available_at=_MOMENT.replace(hour=1),
        received_at=_MOMENT.replace(hour=1),
        payload={"close": "201.00"},
        schema_version="migration-test-v1",
    )
    with service.open_storage(service.resolve(source), WorkspaceAccessMode.READ_WRITE) as storage:
        storage.raw_records.save(appended)
    snapshot = create_v1_migration_source_snapshot(
        service=service,
        source_root=source,
        snapshot_root=tmp_path / "source-copy",
        host_free_bytes=lambda: 30_000_000_000,
    )

    receipt = migrate_workspace_v1_to_v2(
        service=service,
        source_root=snapshot.snapshot_root,
        destination_root=destination,
        page_limit=1,
        host_free_bytes=lambda: 30_000_000_000,
        reconcile_source=True,
        clock=lambda: _MOMENT,
    )

    assert receipt.source_snapshot_id == str(snapshot.snapshot_id)
    assert service.inspect(destination).raw_record_count == 2
    transition = service.v2_source_transition(service.resolve(destination))
    assert transition is not None
    assert transition.phase.value == "COMPLETE"
    assert transition.confirmed_equivalent_count == 2
    assert list((destination / "storage" / "v2").glob("raw-v2-import-state.A.*.json"))
