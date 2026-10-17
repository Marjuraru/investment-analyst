"""Focused safety tests for local preservation before snapshot retirement."""

import hashlib
import json
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

import investment_analyst.workspace.workspace_v2_lifecycle as lifecycle_module
from investment_analyst.core.models import RawRecord, SourceReference
from investment_analyst.workspace.models import WorkspaceAccessMode
from investment_analyst.workspace.service import WorkspaceService
from investment_analyst.workspace.workspace_v2_backup import WorkspaceV2BackupService
from investment_analyst.workspace.workspace_v2_lifecycle import (
    WorkspaceV2LifecycleError,
    WorkspaceV2LifecycleService,
)
from investment_analyst.workspace.workspace_v2_lifecycle_models import LifecycleContentRole
from investment_analyst.workspace.workspace_v2_physical_delta import (
    WorkspaceV2PhysicalDeltaIndexService,
)
from investment_analyst.workspace.workspace_v2_recovery import (
    backup_and_restore_workspace_v2,
)
from investment_analyst.workspace.workspace_v2_remote import (
    RemoteBackupRestoreReceipt,
    WorkspaceV2RemoteService,
)


@pytest.mark.parametrize("fault", [None, "unknown", "symlink", "writer"])
def test_orphan_spill_retirement_preserves_database_wal_and_state(
    tmp_path: Path, fault: str | None
) -> None:
    service = WorkspaceService(environ={}, home=tmp_path / "home")
    root = tmp_path / "v2"
    service.initialize(root, format_version=2)
    parent = root / "storage/v2"
    database = parent / "index.duckdb"
    original = database.read_bytes()
    wal = parent / "index.duckdb.wal"
    wal.write_bytes(b"wal must survive")
    state = root / "state/important.json"
    state.write_text('{"preserved":true}')
    spill = parent / "index.duckdb.tmp"
    spill.mkdir()
    temporary = spill / "duckdb_temp_storage_DEFAULT-0.tmp"
    temporary.write_bytes(b"disposable spill")
    if fault == "unknown":
        (spill / "important.json").write_text("unique")
    if fault == "symlink":
        (spill / "duckdb_temp_storage_S64K-0.tmp").symlink_to(wal)
    receipt = tmp_path / "ops/spill.json"
    lifecycle = WorkspaceV2LifecycleService(service)
    if fault == "writer":
        with (
            lifecycle_module._v2_writer_closed(root),
            pytest.raises(WorkspaceV2LifecycleError, match="writer"),
        ):
            lifecycle.retire_orphaned_duckdb_spill(root, operation_id=uuid4(), receipt_path=receipt)
    elif fault is not None:
        with pytest.raises(WorkspaceV2LifecycleError, match="unknown or unsafe"):
            lifecycle.retire_orphaned_duckdb_spill(root, operation_id=uuid4(), receipt_path=receipt)
    else:
        count, logical, allocated = lifecycle.retire_orphaned_duckdb_spill(
            root, operation_id=uuid4(), receipt_path=receipt
        )
        assert count == 1 and logical == len(b"disposable spill") and allocated > 0
        assert not spill.exists()
        assert json.loads(receipt.read_text())["files_retired"] == 1
        assert receipt.with_name(receipt.name + ".inventory.json").is_file()
    assert database.read_bytes() == original
    assert wal.read_bytes() == b"wal must survive"
    assert state.read_text() == '{"preserved":true}'
    if fault is not None:
        assert temporary.read_bytes() == b"disposable spill"
        assert not receipt.exists()


@pytest.mark.parametrize("read_only", [True, False])
def test_orphan_spill_retirement_rejects_native_duckdb_users(
    tmp_path: Path, read_only: bool
) -> None:
    service = WorkspaceService(environ={}, home=tmp_path / "home")
    root = tmp_path / "v2"
    service.initialize(root, format_version=2)
    parent = root / "storage/v2"
    spill = parent / "index.duckdb.tmp"
    spill.mkdir()
    code = (
        "import duckdb,sys; "
        "c=duckdb.connect(sys.argv[1],read_only=sys.argv[2]=='True'); "
        "print('ready',flush=True); sys.stdin.readline(); c.close()"
    )
    process = subprocess.Popen(
        [sys.executable, "-c", code, str(parent / "index.duckdb"), str(read_only)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert process.stdout is not None
        assert process.stdout.readline().strip() == "ready"
        with pytest.raises(WorkspaceV2LifecycleError, match="database remains in use"):
            WorkspaceV2LifecycleService(service).retire_orphaned_duckdb_spill(
                root, operation_id=uuid4(), receipt_path=tmp_path / "receipt.json"
            )
        assert spill.exists()
    finally:
        process.communicate("\n", timeout=10)


def _packed_retirement(
    tmp_path: Path,
) -> tuple[WorkspaceV2LifecycleService, Path, dict[str, object]]:
    service = WorkspaceService(environ={}, home=tmp_path / "home")
    source = tmp_path / "workspace"
    service.initialize(source, format_version=2)
    backup = WorkspaceV2BackupService(service)
    root = tmp_path / "packed"
    backup.create_packed(
        source,
        root,
        available_bytes=lambda: 10_000_000_000,
        volume_bytes=1_048_576,
        fragment_bytes=65_536,
    )
    index = WorkspaceV2PhysicalDeltaIndexService(backup).build(root, tmp_path / "index")
    remote = (
        WorkspaceV2RemoteService(backup)
        .plan_base(root, configuration_fingerprint="a" * 64)
        .manifest
    )
    retained = tmp_path / "retained"
    retained.mkdir()
    for name in ("backup_manifest.json", "source-inventory.jsonl", "packed-backup-volumes.jsonl"):
        shutil.copyfile(root / name, retained / name)
    remote_path = retained / "remote.json"
    remote_path.write_text(remote.model_dump_json())
    receipt_path = retained / "restore.json"
    # Small real fixture reopen supports the synthetic transport receipt used by this unit test.
    backup.restore_packed(root, tmp_path / "recovered", available_bytes=lambda: 10_000_000_000)
    receipt = RemoteBackupRestoreReceipt(
        backup_id=remote.backup_id,
        remote_manifest_sha256=hashlib.sha256(remote_path.read_bytes()).hexdigest(),
        restored_workspace_id=index.source_workspace_id,
        volume_count=index.volume_count,
        downloaded_volumes=index.volume_count,
        downloaded_bytes=0,
        completed_at=datetime.now(UTC),
    )
    receipt_path.write_text(receipt.model_dump_json())
    options = {
        "retained_controls": retained,
        "parent_index": tmp_path / "index" / index.index_path,
        "remote_manifest_path": remote_path,
        "recovery_receipt_path": receipt_path,
        "operation_id": uuid4(),
        "inventory_path": tmp_path / "retirement.jsonl",
        "proof_path": tmp_path / "proof.json",
        "checkpoint_path": tmp_path / "checkpoint.json",
    }
    return WorkspaceV2LifecycleService(service), root, options


@pytest.mark.parametrize(
    "failure", ["unique", "retained_control", "wrong_recovery", "inventory_symlink"]
)
def test_packed_retirement_rejects_unproven_content(tmp_path: Path, failure: str) -> None:
    lifecycle, root, options = _packed_retirement(tmp_path)
    if failure == "unique":
        (root / "unique.txt").write_bytes(b"unique")
    elif failure == "retained_control":
        retained = options["retained_controls"]
        assert isinstance(retained, Path)
        (retained / "source-inventory.jsonl").write_bytes(b"changed")
    elif failure == "wrong_recovery":
        receipt_path = options["recovery_receipt_path"]
        assert isinstance(receipt_path, Path)
        receipt = RemoteBackupRestoreReceipt.model_validate_json(receipt_path.read_bytes())
        receipt_path.write_text(receipt.model_copy(update={"backup_id": uuid4()}).model_dump_json())
    else:
        inventory = options["inventory_path"]
        assert isinstance(inventory, Path)
        outside = tmp_path / "outside"
        outside.write_bytes(b"preserve")
        inventory.with_name(inventory.name + ".partial").symlink_to(outside)
    with pytest.raises(WorkspaceV2LifecycleError, match="unique|preservation|parent|unsafe"):
        lifecycle.retire_packed_backup(root, **options)
    assert root.is_dir()
    if failure == "inventory_symlink":
        assert (tmp_path / "outside").read_bytes() == b"preserve"


def test_packed_retirement_refuses_another_process_using_the_root(tmp_path: Path) -> None:
    lifecycle, root, options = _packed_retirement(tmp_path)
    process = subprocess.Popen(
        [sys.executable, "-c", "input()", str(root)], stdin=subprocess.PIPE, text=True
    )
    try:
        with pytest.raises(WorkspaceV2LifecycleError, match="remains in use"):
            lifecycle.retire_packed_backup(root, **options)
        assert root.is_dir()
    finally:
        process.communicate("\n", timeout=5)


def test_packed_retirement_preserves_proof_and_resumes_after_detach(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lifecycle, root, options = _packed_retirement(tmp_path)
    remove = lifecycle_module.shutil.rmtree

    def stop(path: Path) -> None:
        raise OSError("fixture interruption after detach")

    monkeypatch.setattr(lifecycle_module.shutil, "rmtree", stop)
    with pytest.raises(OSError, match="after detach"):
        lifecycle.retire_packed_backup(root, **options)
    assert not root.exists()
    proof_path = options["proof_path"]
    assert isinstance(proof_path, Path)
    proof = proof_path.read_bytes()
    monkeypatch.setattr(lifecycle_module.shutil, "rmtree", remove)
    receipt = lifecycle.retire_packed_backup(root, **options)
    assert receipt.checkpoint.phase == "retired"
    assert proof_path.read_bytes() == proof
    assert lifecycle.retire_packed_backup(root, **options).checkpoint == receipt.checkpoint


def test_restore_staging_retires_large_controls_without_copying_them(tmp_path: Path) -> None:
    lifecycle, root, options = _packed_retirement(tmp_path)
    staging = options["retained_controls"]
    assert isinstance(staging, Path)
    remote = options["remote_manifest_path"]
    assert isinstance(remote, Path)
    remote.rename(staging / "remote-backup-manifest.json")
    receipt = options["recovery_receipt_path"]
    assert isinstance(receipt, Path)
    receipt.rename(tmp_path / "restore-receipt.json")
    options["recovery_receipt_path"] = tmp_path / "restore-receipt.json"
    preserved = tmp_path / "preserved"
    lifecycle.preserve_small_backup_controls(staging, preserved)
    assert sorted(path.name for path in preserved.iterdir()) == [
        "backup_manifest.json",
        "remote-backup-manifest.json",
    ]
    assert lifecycle.preserve_small_backup_controls(staging, preserved) == (
        preserved / "backup_manifest.json",
        preserved / "remote-backup-manifest.json",
    )
    # The packed payload still exists and is untouched; staging may contain zero cached volumes.
    options.update(
        retained_controls=preserved,
        remote_manifest_path=preserved / "remote-backup-manifest.json",
        role="restore_staging",
    )
    result = lifecycle.retire_packed_backup(staging, **options)
    assert result.checkpoint.phase == "retired"
    assert not staging.exists()
    assert root.is_dir()
    assert sorted(path.name for path in preserved.iterdir()) == [
        "backup_manifest.json",
        "remote-backup-manifest.json",
    ]


def _seed_source(tmp_path: Path) -> tuple[WorkspaceService, Path]:
    service = WorkspaceService(
        environ={}, home=tmp_path / "home", clock=lambda: datetime(2026, 10, 13, tzinfo=UTC)
    )
    source = tmp_path / "source"
    initialized = service.initialize(source)
    (initialized.paths.state_root / "historic.json").write_text("historic\n", encoding="utf-8")
    (source / "backup_manifest.json").write_text("sealed-control\n", encoding="utf-8")
    processed = source / "storage" / "data" / "processed"
    processed.mkdir(parents=True, exist_ok=True)
    (processed / "event.json").write_text("event\n", encoding="utf-8")
    (source / "storage" / "data" / "unique.bin").write_bytes(b"unknown")
    return service, source


def _lifecycle(service: WorkspaceService) -> WorkspaceV2LifecycleService:
    return WorkspaceV2LifecycleService(
        service, clock=lambda: datetime(2026, 10, 13, hour=1, tzinfo=UTC)
    )


def test_current_v1_inventory_does_not_treat_all_storage_as_recoverable(tmp_path: Path) -> None:
    service, root = _seed_source(tmp_path)
    (root / "storage/data/unique.bin").unlink()
    moment = datetime(2026, 10, 13, tzinfo=UTC)
    raw = RawRecord(
        record_id=uuid4(),
        source=SourceReference(source_id="fixture:source", record_key="one", retrieved_at=moment),
        available_at=moment,
        received_at=moment,
        payload={"value": "1.00"},
        schema_version="fixture-v1",
    )
    with service.open_storage(service.resolve(root), WorkspaceAccessMode.READ_WRITE) as storage:
        storage.raw_records.save(raw)
    lifecycle = _lifecycle(service)
    frozen = lifecycle.freeze_source(
        root, operation_id=uuid4(), source_fingerprint="b" * 64, writer_closed=True
    )
    preserved = tmp_path / "preserved"
    lifecycle.publish_complement(frozen, preserved)
    assert lifecycle.inventory_current_v1(frozen, preserved).unknown_file_count == 0
    (root / "storage/data/raw/z-orphan.json").write_bytes(b"unindexed")
    (root / "storage/data/exports/unique.csv").write_bytes(b"unique")
    inventory = lifecycle.inventory_current_v1(frozen, preserved)
    assert inventory.unknown_file_count == 2
    assert root.is_dir()


def test_current_v1_inventory_rejects_changed_preserved_state(tmp_path: Path) -> None:
    service, root = _seed_source(tmp_path)
    lifecycle = _lifecycle(service)
    frozen = lifecycle.freeze_source(
        root, operation_id=uuid4(), source_fingerprint="b" * 64, writer_closed=True
    )
    preserved = tmp_path / "preserved"
    lifecycle.publish_complement(frozen, preserved)
    (root / "state/historic.json").write_bytes(b"changed after preservation")
    with pytest.raises(WorkspaceV2LifecycleError, match="changed since preservation"):
        lifecycle.inventory_current_v1(frozen, preserved)


def test_complement_is_atomic_hash_verified_and_retirement_rejects_unknown_content(
    tmp_path: Path,
) -> None:
    service, source_root = _seed_source(tmp_path)
    lifecycle = _lifecycle(service)
    frozen = lifecycle.freeze_source(
        source_root,
        operation_id=uuid4(),
        source_fingerprint="b" * 64,
        writer_closed=True,
    )
    inventory = lifecycle.inventory_source(frozen)
    assert inventory.roles[LifecycleContentRole.UNKNOWN] > 0

    complement = tmp_path / "complement"
    manifest = lifecycle.publish_complement(frozen, complement)
    assert [item.path for item in manifest.files] == sorted(item.path for item in manifest.files)
    assert (complement / "state" / "historic.json").read_text(encoding="utf-8") == "historic\n"
    assert (complement / "backup_manifest.json").read_text(encoding="utf-8") == "sealed-control\n"
    assert (complement / "storage" / "data" / "processed" / "event.json").is_file()

    with pytest.raises(WorkspaceV2LifecycleError, match="unknown"):
        lifecycle.retire_source(
            frozen,
            inventory,
            complement,
            checkpoint_path=tmp_path / "journal.json",
        )
    assert source_root.is_dir()


def test_retirement_requires_disjoint_complement_and_resumes_from_checkpoint(
    tmp_path: Path,
) -> None:
    service, source_root = _seed_source(tmp_path)
    (source_root / "storage" / "data" / "unique.bin").unlink()
    lifecycle = _lifecycle(service)
    frozen = lifecycle.freeze_source(
        source_root,
        operation_id=uuid4(),
        source_fingerprint="c" * 64,
        writer_closed=True,
    )
    inventory = lifecycle.inventory_source(
        frozen,
        recoverable_prefixes=("storage",),
    )
    assert inventory.unknown_file_count == 0

    with pytest.raises(WorkspaceV2LifecycleError, match="disjoint"):
        lifecycle.publish_complement(frozen, source_root / "state" / "complement")

    complement = tmp_path / "complement"
    lifecycle.publish_complement(frozen, complement)
    checkpoint = tmp_path / "journal.json"
    receipt = lifecycle.retire_source(frozen, inventory, complement, checkpoint_path=checkpoint)
    assert receipt.phase.value == "retired"
    assert not source_root.exists()
    assert '"phase":"retired"' in checkpoint.read_text(encoding="utf-8")


def test_reconciles_only_a_matching_sealed_baseline_without_rescan(tmp_path: Path) -> None:
    service, source_root = _seed_source(tmp_path)
    lifecycle = _lifecycle(service)
    frozen = lifecycle.freeze_source(
        source_root,
        operation_id=uuid4(),
        source_fingerprint="c" * 64,
        writer_closed=True,
    )
    inventory = lifecycle.inventory_source(frozen)
    complement = lifecycle.publish_complement(frozen, tmp_path / "complement")
    sealed_files = inventory.file_count - 1
    sealed_bytes = inventory.logical_bytes - len(b"sealed-control\n")
    reconciled = lifecycle.reconcile_sealed_baseline(
        inventory,
        complement,
        sealed_file_count=sealed_files,
        sealed_logical_bytes=sealed_bytes,
        post_seal_paths=("backup_manifest.json",),
        recovery_proven=True,
    )
    assert reconciled.unknown_file_count == 0
    assert reconciled.roles[LifecycleContentRole.MIGRATED_RECOVERABLE] > 0
    with pytest.raises(WorkspaceV2LifecycleError, match="recovery"):
        lifecycle.reconcile_sealed_baseline(
            inventory,
            complement,
            sealed_file_count=sealed_files,
            sealed_logical_bytes=sealed_bytes,
            post_seal_paths=("backup_manifest.json",),
            recovery_proven=False,
        )


def test_freeze_rejects_writer_and_complement_rejects_tampering(tmp_path: Path) -> None:
    service, source_root = _seed_source(tmp_path)
    lifecycle = _lifecycle(service)
    with pytest.raises(WorkspaceV2LifecycleError, match="writer"):
        lifecycle.freeze_source(
            source_root,
            operation_id=uuid4(),
            source_fingerprint="d" * 64,
            writer_closed=False,
        )
    frozen = lifecycle.freeze_source(
        source_root,
        operation_id=uuid4(),
        source_fingerprint="d" * 64,
        writer_closed=True,
    )
    complement = tmp_path / "complement"
    lifecycle.publish_complement(frozen, complement)
    (complement / "state" / "historic.json").write_text("tampered\n", encoding="utf-8")
    with pytest.raises(WorkspaceV2LifecycleError, match="hash"):
        lifecycle.verify_complement(frozen, complement)


@pytest.mark.parametrize("removed", [False, True])
def test_retirement_resumes_after_detach_before_recursive_removal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, removed: bool
) -> None:
    service, source_root = _seed_source(tmp_path)
    (source_root / "storage" / "data" / "unique.bin").unlink()
    lifecycle = _lifecycle(service)
    frozen = lifecycle.freeze_source(
        source_root,
        operation_id=uuid4(),
        source_fingerprint="e" * 64,
        writer_closed=True,
    )
    inventory = lifecycle.inventory_source(frozen, recoverable_prefixes=("storage",))
    complement = tmp_path / "complement"
    lifecycle.publish_complement(frozen, complement)
    checkpoint = tmp_path / "journal.json"
    original_rmtree = lifecycle_module.shutil.rmtree

    def fail_before_removal(path: Path) -> None:
        if removed:
            original_rmtree(path)
        raise OSError("interrupted")

    monkeypatch.setattr(lifecycle_module.shutil, "rmtree", fail_before_removal)
    with pytest.raises(WorkspaceV2LifecycleError, match="did not complete"):
        lifecycle.retire_source(frozen, inventory, complement, checkpoint_path=checkpoint)
    assert not source_root.exists()
    assert any(tmp_path.glob(".source.retiring-*")) is not removed
    monkeypatch.setattr(lifecycle_module.shutil, "rmtree", original_rmtree)
    resumed = lifecycle.retire_source(frozen, inventory, complement, checkpoint_path=checkpoint)
    assert resumed.phase.value == "retired"
    sealed = checkpoint.read_bytes()
    assert (
        lifecycle.retire_source(
            frozen, inventory, complement, checkpoint_path=checkpoint
        ).phase.value
        == "retired"
    )
    assert checkpoint.read_bytes() == sealed


def test_recovery_slot_retirement_uses_existing_receipt_without_new_restore(tmp_path: Path) -> None:
    service = WorkspaceService(environ={}, home=tmp_path / "home")
    source = tmp_path / "source-v2"
    service.initialize(source, format_version=2)
    receipt = backup_and_restore_workspace_v2(
        service=service,
        source_root=source,
        backup_root=tmp_path / "backup-v2",
        restore_root=tmp_path / "restore-v2",
        available_bytes=lambda: 10_000_000_000,
    )
    lifecycle = _lifecycle(service)
    retired = lifecycle.retire_recovery_slot(
        receipt,
        operation_id=uuid4(),
        checkpoint_path=tmp_path / "recovery-slot-checkpoint.json",
    )
    assert retired.phase.value == "retired"
    assert not receipt.restore_root.exists()


def test_proven_copy_retirement_streams_the_sealed_inventory_once(tmp_path: Path) -> None:
    service = WorkspaceService(environ={}, home=tmp_path / "home")
    source = tmp_path / "target-v2"
    service.initialize(source, format_version=2)
    recovery = backup_and_restore_workspace_v2(
        service=service,
        source_root=source,
        backup_root=tmp_path / "backup-v2",
        restore_root=tmp_path / "restore-v2",
        available_bytes=lambda: 10_000_000_000,
    )
    lifecycle = _lifecycle(service)
    operation_id = uuid4()
    checkpoint = tmp_path / "target-retirement-checkpoint.json"
    receipt = lifecycle.retire_proven_copy(
        source,
        recovery=recovery,
        remote_manifest_sha256="f" * 64,
        operation_id=operation_id,
        checkpoint_path=checkpoint,
    )
    receipt_path = tmp_path / "target-retirement-receipt.json"
    lifecycle.write_proven_copy_receipt(receipt_path, receipt)

    assert not source.exists()
    assert receipt.files_confirmed > 0
    assert receipt_path.is_file()
    resumed = lifecycle.retire_proven_copy(
        source,
        recovery=recovery,
        remote_manifest_sha256="f" * 64,
        operation_id=operation_id,
        checkpoint_path=checkpoint,
    )
    assert resumed.files_confirmed == receipt.files_confirmed


def test_proven_copy_retirement_rejects_an_extra_file(tmp_path: Path) -> None:
    service = WorkspaceService(environ={}, home=tmp_path / "home")
    source = tmp_path / "target-v2"
    service.initialize(source, format_version=2)
    recovery = backup_and_restore_workspace_v2(
        service=service,
        source_root=source,
        backup_root=tmp_path / "backup-v2",
        restore_root=tmp_path / "restore-v2",
        available_bytes=lambda: 10_000_000_000,
    )
    (source / "unexpected.bin").write_bytes(b"unique")

    with pytest.raises(WorkspaceV2LifecycleError, match="coverage"):
        _lifecycle(service).retire_proven_copy(
            source,
            recovery=recovery,
            remote_manifest_sha256="f" * 64,
            operation_id=uuid4(),
            checkpoint_path=tmp_path / "target-retirement-checkpoint.json",
        )
    assert source.is_dir()


def test_sealed_directory_retirement_preserves_a_durable_checkpoint(tmp_path: Path) -> None:
    service, source = _seed_source(tmp_path)
    lifecycle = _lifecycle(service)
    frozen = lifecycle.freeze_source(
        source, operation_id=uuid4(), source_fingerprint="f" * 64, writer_closed=True
    )
    complement_root = tmp_path / "complement"
    complement = lifecycle.publish_complement(frozen, complement_root)
    receipt = lifecycle.retire_sealed_directory(
        complement_root,
        complement=complement,
        remote_manifest_sha256="a" * 64,
        operation_id=uuid4(),
        checkpoint_path=tmp_path / "complement-checkpoint.json",
    )
    assert receipt.checkpoint.phase == "retired"
    assert not complement_root.exists()
