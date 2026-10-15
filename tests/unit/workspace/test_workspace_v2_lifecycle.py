"""Focused safety tests for local preservation before snapshot retirement."""

from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

import investment_analyst.workspace.workspace_v2_lifecycle as lifecycle_module
from investment_analyst.workspace.service import WorkspaceService
from investment_analyst.workspace.workspace_v2_lifecycle import (
    WorkspaceV2LifecycleError,
    WorkspaceV2LifecycleService,
)
from investment_analyst.workspace.workspace_v2_lifecycle_models import LifecycleContentRole
from investment_analyst.workspace.workspace_v2_recovery import (
    backup_and_restore_workspace_v2,
)


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


def test_retirement_resumes_after_detach_before_recursive_removal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
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
        raise OSError("interrupted")

    monkeypatch.setattr(lifecycle_module.shutil, "rmtree", fail_before_removal)
    with pytest.raises(WorkspaceV2LifecycleError, match="did not complete"):
        lifecycle.retire_source(frozen, inventory, complement, checkpoint_path=checkpoint)
    assert not source_root.exists()
    assert any(tmp_path.glob(".source.retiring-*"))
    monkeypatch.setattr(lifecycle_module.shutil, "rmtree", original_rmtree)
    resumed = lifecycle.retire_source(frozen, inventory, complement, checkpoint_path=checkpoint)
    assert resumed.phase.value == "retired"


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
