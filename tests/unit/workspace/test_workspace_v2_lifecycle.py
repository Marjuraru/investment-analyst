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


def _preparatory_slot_fixture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, w2_photo: bool = False
):
    import os
    from types import SimpleNamespace

    from investment_analyst.application import local_release
    from investment_analyst.storage import workspace_v2_inventory as inventory_module
    from investment_analyst.storage.resource_policy import (
        DatabaseResourceAssignment,
        DuckDBResourcePolicy,
        MaintenanceResourceBudget,
        MaintenanceResourceReceipt,
    )
    from investment_analyst.workspace import workspace_v2_physical_delta as physical
    from investment_analyst.workspace.drive_transport import DriveTransportConfig
    from investment_analyst.workspace.workspace_v2_acceptance import CompatibleRecoveryReadback
    from investment_analyst.workspace.workspace_v2_lifecycle_models import (
        PreparatorySlotArtifact,
        PreparatorySlotRetirementReceipt,
    )
    from investment_analyst.workspace.workspace_v2_migration_models import (
        GenerationContentProof,
        HistoricalSealSnapshot,
        ProgressReconciliation,
        RecoveredProgressInventory,
    )

    now = datetime(2026, 10, 17, tzinfo=UTC)
    source, slot, controls = tmp_path / "retained", tmp_path / "slot", tmp_path / "controls"
    controls.mkdir()
    service = WorkspaceService(environ={}, home=tmp_path / "home")
    initialized = service.initialize(source, format_version=2)
    raw = RawRecord(
        record_id=uuid4(),
        source=SourceReference(source_id="fixture:source", record_key="one", retrieved_at=now),
        available_at=now,
        received_at=now,
        payload={"exact": "1.00"},
        schema_version="fixture-v1",
    )
    with service.open_storage(initialized.paths, WorkspaceAccessMode.READ_WRITE) as storage:
        storage.raw_records.save(raw)
    for name in ("aapl_daily_run.lock", "aapl_local_service.lock"):
        (source / "state" / name).touch()
    shutil.copytree(source, slot)
    ref = inventory_module.reconciliation_file_reference
    empty = controls / "empty.jsonl"
    empty.write_bytes(b"")
    evidence = controls / "old-proof.json"
    evidence.write_bytes(b"{}")
    control = ref(evidence, "control")
    old_id, generation_id, retire_id, parent_id = uuid4(), uuid4(), uuid4(), uuid4()
    photo_epoch = generation_id if w2_photo else old_id
    database = ref(source / "storage/v2/index.duckdb", "database")
    parent_snapshot = HistoricalSealSnapshot(
        source_fingerprint="b" * 64,
        metric_count=0,
        metric_digest=hashlib.sha256(b"").hexdigest(),
        diagnostic_count=0,
        diagnostic_digest=hashlib.sha256(b"").hexdigest(),
        sealed_at=now,
    )
    w_inventory = RecoveredProgressInventory(
        raw_count=1,
        observation_count=0,
        metric_count=0,
        diagnostic_count=0,
        metric_digest=parent_snapshot.metric_digest,
        diagnostic_digest=parent_snapshot.diagnostic_digest,
        dataset_sha256="a" * 64,
        delta_membership=ref(empty, "inventory"),
        relationships=control,
        verified_at=now,
    )
    w = ProgressReconciliation(
        operation_id=old_id,
        predecessor_operation_id=uuid4(),
        phase="BASELINE_VERIFIED",
        predecessor=control,
        predecessor_proofs=(control,),
        parent_membership=ref(empty, "inventory"),
        parent_seal=parent_snapshot,
        source_workspace_id=uuid4(),
        source_created_at=now,
        source_root=tmp_path / "v1",
        destination_root=source,
        destination_workspace_id=initialized.manifest.workspace_id,
        database_before=database,
        wal_before=None,
        database_after=database,
        recovered_at=now,
        predecessor_code=ref(evidence, "code"),
        predecessor_tree_fingerprint="a" * 64,
        predecessor_environment_fingerprint="b" * 64,
        current_tree_fingerprint="c" * 64,
        current_environment_fingerprint="d" * 64,
        resource_receipt=ref(evidence, "resource"),
        inventory=w_inventory,
        prepared_at=now,
        updated_at=now,
    )
    w_path = controls / "original-W.json"
    w_path.write_text(w.model_dump_json())
    content = GenerationContentProof(
        generation_id=generation_id,
        workspace_id=initialized.manifest.workspace_id,
        baseline_certificate=ref(w_path, "control"),
        baseline_database=database,
        database=database,
        resource_receipt=ref(evidence, "resource"),
        verifier_sha256="a" * 64,
        policy_sha256="b" * 64,
        preserved_tables=(),
        raw_count=1,
        raw_digest="a" * 64,
        observation_count=0,
        observation_digest="b" * 64,
        metric_count=0,
        metric_digest="c" * 64,
        diagnostic_count=0,
        diagnostic_digest="d" * 64,
        validated_additions=(),
        verified_at=now,
    )
    content_path = controls / "W2-content.json"
    content_path.write_text(content.model_dump_json())
    certificate = controls / "W2-certificate.json"
    certificate.write_text('{"fixture":"certified-W2"}')
    head = SimpleNamespace(certificate=ref(certificate, "control"))
    generation = SimpleNamespace(
        core_proof=ref(content_path, "control"),
        inventory=w_inventory,
        verified_w=ref(w_path, "control"),
        phase="BASELINE_VERIFIED",
        reserved_successor=None,
        generation_id=generation_id,
        database=database,
        destination_workspace_id=initialized.manifest.workspace_id,
    )
    monkeypatch.setattr(
        inventory_module.WorkspaceV2GenerationLedger, "resolve", lambda _: (head, generation)
    )
    release_root = tmp_path / "releases"
    release_sha = "dbbdfd021b0fee20be31b2d2a845874b0e861067"
    reader = release_root / release_sha / "src/investment_analyst/storage/compact_analytical_v2.py"
    reader.parent.mkdir(parents=True)
    reader.write_text("# fixture integrated reader\n")
    bindings = SimpleNamespace(current=None, previous=None)
    monkeypatch.setattr(
        local_release,
        "LocalReleaseService",
        lambda: SimpleNamespace(
            paths=SimpleNamespace(releases=release_root), load_workspace_bindings=lambda: bindings
        ),
    )
    files = controls / "files.jsonl"
    if w2_photo:
        # The new photo adds a control before raw files. The preceding C4
        # inventory/ordinals remain historical and must not be relabelled.
        for root in (source, slot):
            (root / "state/generation-fixture.json").write_text('{"fixture":"new photo"}')
    items = []
    for relative in physical._source_files(slot):
        path = slot / relative
        data = path.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        items.append(
            physical.PhysicalDeltaFile(
                path=relative,
                size_bytes=len(data),
                sha256=digest,
                blocks=()
                if not data
                else (
                    physical.PhysicalDeltaBlock(
                        offset_bytes=0, length_bytes=len(data), sha256=digest, origin="parent"
                    ),
                ),
            )
        )
    files.write_text("".join(item.model_dump_json() + "\n" for item in items))
    link_inventory = files
    if w2_photo:
        link_inventory = controls / "old-link-inventory.jsonl"
        link_inventory.write_text(
            "".join(
                item.model_dump_json() + "\n"
                for item in items
                if item.path != "state/generation-fixture.json"
            )
        )
    remote_manifest = controls / "remote-parent.json"
    remote_manifest.write_bytes(b'{"fixture":"remote-parent"}')
    parent_restore = RemoteBackupRestoreReceipt(
        backup_id=parent_id,
        remote_manifest_sha256=ref(remote_manifest, "control").sha256,
        restored_workspace_id=initialized.manifest.workspace_id,
        volume_count=1,
        downloaded_volumes=1,
        downloaded_bytes=1,
        completed_at=now,
    )
    parent_restore_path = controls / "remote-parent-restore.json"
    parent_restore_path.write_text(parent_restore.model_dump_json())
    manifest = physical.PhysicalDeltaManifest(
        historical_state="APPLYING",
        epoch=photo_epoch,
        source_root=source,
        source_workspace_id=initialized.manifest.workspace_id,
        parent_backup_id=parent_id,
        parent_manifest_sha256=ref(remote_manifest, "control").sha256,
        parent_index_sha256=ref(empty, "inventory").sha256,
        files_sha256=ref(files, "inventory").sha256,
        file_count=len(items),
        deleted_count=0,
        logical_bytes=sum(item.size_bytes for item in items),
        new_bytes=0,
        reused_bytes=sum(item.size_bytes for item in items),
    )
    manifest_path = controls / "delta-manifest.json"
    manifest_path.write_text(manifest.model_dump_json())
    header = physical.ImmutableLinkHeader(
        operation_id=generation_id,
        action="LINK",
        retained_root=source,
        retained_workspace_id=initialized.manifest.workspace_id,
        slot=slot,
        inventory=ref(link_inventory, "inventory"),
        parent_manifest=ref(remote_manifest, "control"),
        parent_index=ref(empty, "inventory"),
        parent_proof=control,
        verifier_sha256=physical._immutable_link_verifier_sha256(),
        byte_budget=1_000_000,
        created_at=now,
    )
    journal = physical.ImmutableLinkJournal(controls / "links", header).open()
    link_items = tuple(physical._iter_delta_files(link_inventory))
    ordinal, item = next(
        (i, item)
        for i, item in enumerate(link_items)
        if item.path.startswith("storage/v2/raw/raw/sha256/")
    )
    (slot / item.path).unlink()
    journal.apply_page(((ordinal, item),))
    link = ref(journal.root / "page-00000000.trailer.json", "control")
    restore = physical.PhysicalDeltaReceipt(
        kind="restore",
        restored_root=slot,
        epoch=photo_epoch,
        historical_state="APPLYING",
        manifest_sha256=ref(manifest_path, "control").sha256,
        files_sha256=manifest.files_sha256,
        confirmed_files=manifest.file_count,
        parent_linked_files=1,
        parent_linked_bytes=item.size_bytes,
        immutable_link_journals=(link,) if w2_photo else (),
        completed_at=now,
    )
    restore_path = controls / "restore.json"
    restore_path.write_text(restore.model_dump_json())
    upload = physical.PhysicalDeltaReceipt(
        kind="upload",
        epoch=photo_epoch,
        historical_state="APPLYING",
        manifest_sha256=restore.manifest_sha256,
        files_sha256=manifest.files_sha256,
        confirmed_files=manifest.file_count,
        completed_at=now,
    )
    upload_path = controls / "upload.json"
    upload_path.write_text(upload.model_dump_json())
    readback = CompatibleRecoveryReadback(
        operation_id=photo_epoch,
        workspace_id=initialized.manifest.workspace_id,
        restored_root=slot,
        manifest_sha256=restore.manifest_sha256,
        recovery_receipt_sha256=hashlib.sha256(restore.model_dump_json().encode()).hexdigest(),
        reader_sha=release_sha,
        reader_module_path=reader,
        raw_count=1,
        observation_count=0,
        metric_count=0,
        diagnostic_count=0,
        raw_sample_sha256="a" * 64,
        observation_sample_sha256="b" * 64,
        metric_sample_sha256="c" * 64,
        diagnostic_sample_sha256="d" * 64,
        checked_at=now,
    )
    readback_path = controls / "readback.json"
    readback_path.write_text(readback.model_dump_json())
    coverage_path = controls / "coverage.json"
    coverage_path.write_text(
        json.dumps(
            {
                "action": "recovery-readback",
                "operation_id": str(photo_epoch),
                "result": {
                    "readback": readback.model_dump(mode="json"),
                    "coverage": {
                        "physical_files": manifest.file_count,
                        "physical_bytes": manifest.logical_bytes,
                        "raw_indexed": 1,
                    },
                },
            }
        )
    )
    config = DriveTransportConfig(
        rclone_path=Path("/usr/bin/rclone"), encrypted_remote="fixture_crypt:", folder_id="fixture"
    )
    config_path = controls / "transport.json"
    config_path.write_text(config.model_dump_json())
    paths = {
        "delta_manifest": manifest_path,
        "files_inventory": files,
        "remote_upload": upload_path,
        "remote_parent_restore": parent_restore_path,
        "remote_parent_manifest": remote_manifest,
        "remote_parent_index": empty,
        "remote_locator": config_path,
        "restore_receipt": restore_path,
        "complete_coverage": coverage_path,
        "compatible_readback": readback_path,
        "retained_certificate": certificate,
        "link_witness": link.path,
    }
    artifacts = tuple(
        PreparatorySlotArtifact(
            kind=kind,
            path=p,
            size_bytes=p.stat().st_size,
            sha256=hashlib.sha256(p.read_bytes()).hexdigest(),
        )
        for kind, p in paths.items()
    )
    proposal = PreparatorySlotRetirementReceipt(
        operation_id=retire_id,
        phase="retiring",
        source_root=slot,
        retained_root=source,
        workspace_id=initialized.manifest.workspace_id,
        retained_generation_id=generation_id,
        recovery_epoch=photo_epoch,
        evidence=artifacts,
        configuration_sha256=hashlib.sha256(config.model_dump_json().encode()).hexdigest(),
        files_confirmed=manifest.file_count,
        bytes_confirmed=manifest.logical_bytes,
        verified_at=now,
    )
    budget = MaintenanceResourceBudget(
        operation_id=retire_id,
        profile="verification",
        assignments=(
            DatabaseResourceAssignment(
                database=source / "storage/v2/index.duckdb",
                policy=DuckDBResourcePolicy(
                    memory_limit_bytes=1_000_000_000,
                    threads=1,
                    spill_limit_bytes=0,
                    temp_directory=controls / "temp",
                ),
            ),
        ),
        cache_and_control_bytes=1_000_000,
    )
    admission = MaintenanceResourceReceipt(
        budget=budget,
        pid=os.getpid(),
        process_start_ticks=1,
        boot_id=uuid4(),
        cgroup_path=Path("/sys/fs/cgroup/fixture"),
        observed_memory_high_bytes=budget.memory_high_bytes,
        observed_memory_max_bytes=budget.memory_max_bytes,
        observed_swap_max_bytes=0,
        host_free_bytes=30_000_000_000,
        effective_settings=(),
        recorded_at=now,
    )
    admission_path = controls / "admission.json"
    admission_path.write_text(admission.model_dump_json())
    options = dict(
        admission_path=admission_path,
        checkpoint_path=controls / "retirement-checkpoint.json",
        proof_path=controls / "retirement-proof.json",
        unlink_root=controls / "unlinks",
        journal_byte_budget=1_000_000,
    )
    return WorkspaceV2LifecycleService(service), proposal, budget, options, generation, bindings


@pytest.mark.parametrize(
    "fault",
    [
        None,
        "unique",
        "missing",
        "ctime",
        "database",
        "linked_database",
        "epoch",
        "namespace",
        "reserved",
        "active",
        "writer",
    ],
)
def test_preparatory_applying_retirement_requires_every_gate_and_preserves_retained_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str | None
) -> None:
    import fcntl
    from types import SimpleNamespace

    from investment_analyst.storage.resource_policy import (
        MaintenanceExecution,
        maintenance_execution,
        maintenance_resource_budget,
    )

    lifecycle, proposal, budget, options, generation, bindings = _preparatory_slot_fixture(
        tmp_path, monkeypatch
    )
    source, slot = proposal.retained_root, proposal.source_root
    raw = next((source / "storage/v2/raw/raw/sha256").rglob("?" * 64))
    original = raw.read_bytes()
    database = (source / "storage/v2/index.duckdb").read_bytes()
    lock = None
    if fault == "unique":
        (slot / "unique.bin").write_bytes(b"never delete")
    elif fault == "missing":
        (slot / "manifest.json").unlink()
    elif fault == "ctime":
        raw.touch()
    elif fault == "database":
        with (slot / "storage/v2/index.duckdb").open("ab") as handle:
            handle.write(b"changed local database")
    elif fault == "linked_database":
        (slot / "storage/v2/index.duckdb").unlink()
        (slot / "storage/v2/index.duckdb").hardlink_to(source / "storage/v2/index.duckdb")
    elif fault == "epoch":
        proposal = proposal.model_copy(update={"recovery_epoch": uuid4()})
    elif fault == "namespace":
        proposal = proposal.model_copy(update={"configuration_sha256": "f" * 64})
    elif fault == "reserved":
        generation.phase = "SUCCESSOR_BOUND"
    elif fault == "active":
        bindings.current = SimpleNamespace(workspace_root=slot)
    elif fault == "writer":
        lock = (slot / "storage/v2/raw-v2-staging.lock").open("rb")
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        with (
            maintenance_resource_budget(budget),
            maintenance_execution(MaintenanceExecution(budget, lambda *_: None)),
        ):
            if fault is None:
                result = lifecycle.retire_preparatory_slot(proposal, **options)
                assert result.phase == "retired" and result.historical_state == "APPLYING"
                assert not slot.exists()
                assert result.unlink_witness.path.is_file()
                generation.phase = "SUCCESSOR_BOUND"  # Terminal evidence remains reusable later.
                assert lifecycle.retire_preparatory_slot(proposal, **options) == result
            else:
                with pytest.raises((WorkspaceV2LifecycleError, ValueError, RuntimeError)):
                    lifecycle.retire_preparatory_slot(proposal, **options)
                assert slot.exists()
                assert not options["proof_path"].exists()
    finally:
        if lock is not None:
            lock.close()
    assert raw.read_bytes() == original
    assert (source / "storage/v2/index.duckdb").read_bytes() == database


@pytest.mark.parametrize(
    "crash", ["unlink_tail", "rename", "partial_remove", "after_remove", "terminal"]
)
def test_preparatory_retirement_resumes_its_own_exact_unlink_and_removal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, crash: str
) -> None:
    from investment_analyst.storage.resource_policy import (
        MaintenanceExecution,
        maintenance_execution,
        maintenance_resource_budget,
    )
    from investment_analyst.workspace import workspace_v2_physical_delta as physical

    lifecycle, proposal, budget, options, _, _ = _preparatory_slot_fixture(tmp_path, monkeypatch)
    source = proposal.retained_root
    raw = next((source / "storage/v2/raw/raw/sha256").rglob("?" * 64))
    before = raw.read_bytes()
    with (
        maintenance_resource_budget(budget),
        maintenance_execution(MaintenanceExecution(budget, lambda *_: None)),
    ):
        with monkeypatch.context() as patch:
            if crash == "unlink_tail":
                seal = physical.ImmutableLinkJournal._seal

                def crash_trailer(self, path, document):
                    if self.header.action == "UNLINK" and path.name.endswith(".trailer.json"):
                        raise OSError("interrupted before trailer")
                    return seal(self, path, document)

                patch.setattr(physical.ImmutableLinkJournal, "_seal", crash_trailer)
            elif crash == "rename":
                replace = lifecycle_module.os.replace

                def crash_rename(left, right):
                    replace(left, right)
                    if left == proposal.source_root:
                        raise OSError("interrupted after rename")

                patch.setattr(lifecycle_module.os, "replace", crash_rename)
            elif crash in {"partial_remove", "after_remove"}:
                remove = shutil.rmtree

                def crash_remove(path, *args, **kwargs):
                    if crash == "partial_remove":
                        (path / "storage/v2/index.duckdb").unlink()
                        (path / "storage/v2/raw-v2-staging.lock").unlink()
                    else:
                        remove(path, *args, **kwargs)
                    raise OSError("interrupted during removal")

                patch.setattr(lifecycle_module.shutil, "rmtree", crash_remove)
            else:
                from investment_analyst.storage.workspace_v2_inventory import (
                    WorkspaceV2ProgressReconciler,
                )

                write = WorkspaceV2ProgressReconciler._write_once

                def crash_terminal(path, data):
                    if path == options["proof_path"]:
                        raise OSError("interrupted before terminal receipt")
                    return write(path, data)

                patch.setattr(
                    WorkspaceV2ProgressReconciler, "_write_once", staticmethod(crash_terminal)
                )
            with pytest.raises(OSError):
                lifecycle.retire_preparatory_slot(proposal, **options)
        result = lifecycle.retire_preparatory_slot(proposal, **options)
        assert result.phase == "retired" and not proposal.source_root.exists()
        assert lifecycle.retire_preparatory_slot(proposal, **options) == result
    assert raw.read_bytes() == before
    assert raw.stat().st_nlink == 1


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


def _owned_payload_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from investment_analyst.workspace.workspace_v2_acceptance import CompatibleRecoveryReadback
    from investment_analyst.workspace.workspace_v2_lifecycle_models import (
        OwnedPayloadRetirementInput,
        OwnedPayloadRole,
        PreparatorySlotArtifact,
    )
    from investment_analyst.workspace.workspace_v2_physical_delta import (
        PhysicalDeltaBlock,
        PhysicalDeltaFile,
        PhysicalDeltaManifest,
        PhysicalDeltaReceipt,
    )

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    ops = tmp_path / ".local/share/investment-analyst/ops/data-chassis-45"
    delta = ops / "physical-delta-W-20261017"
    root = delta / "physical-delta/objects"
    root.mkdir(parents=True)
    payload = b"independently recovered payload"
    digest = hashlib.sha256(payload).hexdigest()
    (root / f"{digest}.bin").write_bytes(payload)
    entry = PhysicalDeltaFile(
        path="raw/test.json",
        size_bytes=len(payload),
        sha256=digest,
        blocks=(
            PhysicalDeltaBlock(
                offset_bytes=0,
                length_bytes=len(payload),
                sha256=digest,
                origin="delta",
                object_key=f"physical-delta/objects/{digest}.bin",
            ),
        ),
    )
    inventory = delta / "files.jsonl"
    inventory.write_text(entry.model_dump_json() + "\n")
    epoch, workspace = uuid4(), uuid4()
    manifest = PhysicalDeltaManifest(
        historical_state="APPLYING",
        epoch=epoch,
        source_root=tmp_path
        / ".local/share/investment-analyst/workspaces/data-chassis-43-rehearsal/restore-v2",
        source_workspace_id=workspace,
        parent_backup_id=uuid4(),
        parent_manifest_sha256="a" * 64,
        parent_index_sha256="b" * 64,
        files_sha256=hashlib.sha256(inventory.read_bytes()).hexdigest(),
        file_count=1,
        deleted_count=0,
        logical_bytes=len(payload),
        new_bytes=len(payload),
        reused_bytes=0,
    )
    manifest_path = delta / "delta-manifest.json"
    manifest_path.write_text(manifest.model_dump_json())
    manifest_sha = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    refs = []
    for kind, path in (("delta_manifest", manifest_path), ("files_inventory", inventory)):
        data = path.read_bytes()
        refs.append(
            PreparatorySlotArtifact(
                kind=kind, path=path, size_bytes=len(data), sha256=hashlib.sha256(data).hexdigest()
            )
        )
    restore = None
    for kind in ("upload", "restore"):
        receipt = PhysicalDeltaReceipt(
            kind=kind,
            restored_root=ops / "recovery-W-restore-rehearsal" if kind == "restore" else None,
            epoch=epoch,
            historical_state="APPLYING",
            manifest_sha256=manifest_sha,
            files_sha256=manifest.files_sha256,
            confirmed_files=1,
            completed_at=datetime.now(UTC),
        )
        path = delta / f"{kind}-receipt.json"
        data = receipt.model_dump_json().encode()
        path.write_bytes(data)
        refs.append(
            PreparatorySlotArtifact(
                kind="remote_upload" if kind == "upload" else "restore_receipt",
                path=path,
                size_bytes=len(data),
                sha256=hashlib.sha256(data).hexdigest(),
            )
        )
        if kind == "restore":
            restore = receipt
    assert restore is not None
    reader = CompatibleRecoveryReadback(
        operation_id=epoch,
        workspace_id=workspace,
        restored_root=restore.restored_root,
        manifest_sha256=manifest_sha,
        recovery_receipt_sha256=hashlib.sha256(restore.model_dump_json().encode()).hexdigest(),
        reader_sha="dbbdfd021b0fee20be31b2d2a845874b0e861067",
        reader_module_path=tmp_path / "release44/reader.py",
        raw_count=1,
        observation_count=1,
        metric_count=1,
        diagnostic_count=1,
        raw_sample_sha256=digest,
        observation_sample_sha256=digest,
        metric_sample_sha256=digest,
        diagnostic_sample_sha256=digest,
        checked_at=datetime.now(UTC),
    )
    path = ops / "readback.json"
    data = reader.model_dump_json().encode()
    path.write_bytes(data)
    refs.append(
        PreparatorySlotArtifact(
            kind="compatible_readback",
            path=path,
            size_bytes=len(data),
            sha256=hashlib.sha256(data).hexdigest(),
        )
    )
    request = OwnedPayloadRetirementInput(
        operation_id=uuid4(),
        role=OwnedPayloadRole.DELTA_STAGING_OBJECTS,
        source_root=root,
        retained_controls_root=ops / "cleanup-E0",
        consuming_generation=epoch,
        releasing_stage="E0",
        evidence=tuple(refs),
        no_users=True,
        no_unique_data=True,
    )
    return WorkspaceV2LifecycleService(WorkspaceService(environ={}, home=tmp_path)), request


def test_owned_payload_retirement_preserves_controls_and_replays(tmp_path, monkeypatch):
    lifecycle, request = _owned_payload_fixture(tmp_path, monkeypatch)
    receipt = lifecycle.retire_owned_payload(request)
    assert receipt.checkpoint.phase == "retired"
    assert receipt.checkpoint.files_confirmed == 1
    assert receipt.checkpoint.bytes_confirmed == len(b"independently recovered payload")
    assert not request.source_root.exists()
    assert all(ref.path.exists() for ref in request.evidence)
    assert lifecycle.retire_owned_payload(request) == receipt


@pytest.mark.parametrize(
    "failure", ["hash", "root", "workspace", "owner", "unique", "foreign", "symlink", "protected"]
)
def test_owned_payload_retirement_rejects_missing_authority(tmp_path, monkeypatch, failure):
    from investment_analyst.workspace.workspace_v2_lifecycle_models import OwnedPayloadRole

    lifecycle, request = _owned_payload_fixture(tmp_path, monkeypatch)
    if failure == "hash":
        next(request.source_root.iterdir()).write_bytes(b"corrupt")
    elif failure == "root":
        request = request.model_copy(update={"source_root": request.source_root.parent})
    elif failure == "workspace":
        ref = next(ref for ref in request.evidence if ref.kind == "delta_manifest")
        document = json.loads(ref.path.read_bytes())
        document["source_root"] = str(tmp_path / "foreign-workspace")
        ref.path.write_text(json.dumps(document))
        data = ref.path.read_bytes()
        replacement = ref.model_copy(
            update={"size_bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
        )
        request = request.model_copy(
            update={
                "evidence": tuple(replacement if item == ref else item for item in request.evidence)
            }
        )
    elif failure == "owner":

        def active(root):
            raise WorkspaceV2LifecycleError("active owner")

        monkeypatch.setattr(lifecycle_module, "_require_no_directory_users", active)
    elif failure == "unique":
        (request.source_root / "unique.bin").write_bytes(b"unique")
    elif failure == "foreign":
        request = request.model_copy(update={"consuming_generation": uuid4()})
    elif failure == "symlink":
        (request.source_root / "unknown").symlink_to(request.evidence[0].path)
    else:
        request = request.model_copy(update={"role": OwnedPayloadRole.INDEPENDENT_READ_CACHE})
    with pytest.raises(WorkspaceV2LifecycleError):
        lifecycle.retire_owned_payload(request)
    assert request.source_root.exists()
    assert not list(request.retained_controls_root.glob("*.checkpoint.json"))


@pytest.mark.parametrize("point", ["detach", "remove", "terminal"])
def test_owned_payload_retirement_resumes_exact_cursor(tmp_path, monkeypatch, point):
    lifecycle, request = _owned_payload_fixture(tmp_path, monkeypatch)
    replace, remove, write = (
        lifecycle_module.os.replace,
        lifecycle_module.shutil.rmtree,
        lifecycle_module._replace_json,
    )
    fired = False

    def interrupted_replace(source, destination):
        nonlocal fired
        replace(source, destination)
        if point == "detach" and source == request.source_root and not fired:
            fired = True
            raise OSError("lost after durable detach")

    def interrupted_remove(root):
        nonlocal fired
        if point == "remove" and not fired:
            fired = True
            next(root.iterdir()).unlink()
            raise OSError("lost halfway through removal")
        remove(root)

    def interrupted_write(path, document):
        nonlocal fired
        write(path, document)
        if (
            point == "terminal"
            and str(path).endswith(".checkpoint.json")
            and document["phase"] == "retired"
            and not fired
        ):
            fired = True
            raise OSError("lost before terminal receipt")

    monkeypatch.setattr(lifecycle_module.os, "replace", interrupted_replace)
    monkeypatch.setattr(lifecycle_module.shutil, "rmtree", interrupted_remove)
    monkeypatch.setattr(lifecycle_module, "_replace_json", interrupted_write)
    with pytest.raises(OSError):
        lifecycle.retire_owned_payload(request)
    assert fired
    result = lifecycle.retire_owned_payload(request)
    assert result.checkpoint.phase == "retired"
    assert lifecycle.retire_owned_payload(request) == result


@pytest.mark.parametrize("writer", [False, True])
@pytest.mark.parametrize("name", [".pytest_cache", ".ruff_cache"])
def test_owned_payload_regenerable_matrix_roots(tmp_path, monkeypatch, writer, name):
    from investment_analyst.workspace.workspace_v2_lifecycle_models import (
        OwnedPayloadRetirementInput,
        OwnedPayloadRole,
    )

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    repository = tmp_path / "projects/investment-analyst"
    root = (repository / ".worktrees/data-chassis-45" if writer else repository) / name
    root.mkdir(parents=True)
    (root / "CACHEDIR.TAG").write_text("regenerable")
    protected = root.parent / "protected-untracked.md"
    protected.write_text("preserve user work")
    request = OwnedPayloadRetirementInput(
        operation_id=uuid4(),
        role=OwnedPayloadRole.REGENERABLE_LOCAL_CACHE,
        source_root=root,
        retained_controls_root=tmp_path
        / ".local/share/investment-analyst/ops/data-chassis-45/cleanup-E0",
        consuming_generation=None,
        releasing_stage="E0",
        no_users=True,
        no_unique_data=True,
    )
    lifecycle = WorkspaceV2LifecycleService(WorkspaceService(environ={}, home=tmp_path))
    result = lifecycle.retire_owned_payload(request)
    assert result.checkpoint.files_confirmed == 1
    assert protected.read_text() == "preserve user work"
    assert lifecycle.retire_owned_payload(request) == result


def test_owned_payload_resume_detects_unknown_writes(tmp_path, monkeypatch):
    lifecycle, request = _owned_payload_fixture(tmp_path, monkeypatch)
    original = lifecycle_module.shutil.rmtree

    def interrupt(root):
        (root / "new-unique.bin").write_bytes(b"preserve")
        raise OSError("client disappeared")

    monkeypatch.setattr(lifecycle_module.shutil, "rmtree", interrupt)
    with pytest.raises(OSError):
        lifecycle.retire_owned_payload(request)
    monkeypatch.setattr(lifecycle_module.shutil, "rmtree", original)
    with pytest.raises(WorkspaceV2LifecycleError, match="new or missing"):
        lifecycle.retire_owned_payload(request)
    # The exact detached root remains discoverable from its durable cursor.
    document = json.loads(
        next(request.retained_controls_root.glob("*.checkpoint.json")).read_bytes()
    )
    assert (Path(document["retiring_root"]) / "new-unique.bin").read_bytes() == b"preserve"


@pytest.mark.parametrize("fault", [None, "database", "counts"])
def test_preparatory_w2_photo_uses_its_actual_epoch_and_same_retained_certificate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str | None
):
    from investment_analyst.storage.resource_policy import (
        MaintenanceExecution,
        maintenance_execution,
        maintenance_resource_budget,
    )

    lifecycle, proposal, budget, options, generation, _ = _preparatory_slot_fixture(
        tmp_path, monkeypatch, w2_photo=True
    )
    assert proposal.recovery_epoch == generation.generation_id
    database = proposal.retained_root / "storage/v2/index.duckdb"
    original = database.read_bytes()
    if fault == "database":
        generation.database = generation.database.model_copy(update={"sha256": "f" * 64})
    if fault == "counts":
        generation.inventory = generation.inventory.model_copy(update={"raw_count": 2})
    with (
        maintenance_resource_budget(budget),
        maintenance_execution(MaintenanceExecution(budget, lambda *_: None)),
    ):
        if fault:
            with pytest.raises(WorkspaceV2LifecycleError):
                lifecycle.retire_preparatory_slot(proposal, **options)
            assert proposal.source_root.exists()
        else:
            receipt = lifecycle.retire_preparatory_slot(proposal, **options)
            assert receipt.phase == "retired" and receipt.historical_state == "APPLYING"
            assert receipt.recovery_epoch == proposal.recovery_epoch
            assert not proposal.source_root.exists()
    assert database.read_bytes() == original
