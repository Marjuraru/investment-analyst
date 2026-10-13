"""Recovery checks for complete workspace-v2 backups."""

import json
import shutil
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from investment_analyst.workspace.backup import WorkspaceBackupCounts
from investment_analyst.workspace.service import WorkspaceService
from investment_analyst.workspace.workspace_v2_backup import (
    WorkspaceV2BackupError,
    WorkspaceV2BackupService,
    WorkspaceV2PackedFragment,
    WorkspaceV2PackedVolume,
    _iter_packed_volumes,
    _packed_journal_binding,
    _stream_manifest,
)
from investment_analyst.workspace.workspace_v2_backup_inventory import write_inventory
from investment_analyst.workspace.workspace_v2_recovery import (
    WorkspaceV2RecoveryError,
    backup_and_restore_workspace_v2,
)


def _host_free_bytes() -> int:
    return 10_000_000_000


def _seal_as_stream_manifest(backup_root: Path, manifest: object) -> None:
    stream_manifest = _stream_manifest(
        workspace_id=manifest.source_workspace_id,
        counts=manifest.counts,
        inventory_path=backup_root / "source-inventory.jsonl",
        journal_path=backup_root / "packed-backup-volumes.jsonl",
        created_at=datetime.now(UTC),
    )
    (backup_root / "backup_manifest.json").write_text(
        json.dumps(stream_manifest.model_dump(mode="json"), sort_keys=True), encoding="utf-8"
    )


def test_recovery_restores_a_ready_v2_workspace(tmp_path: Path) -> None:
    service = WorkspaceService(environ={}, home=tmp_path / "home")
    source = tmp_path / "source"
    service.initialize(source, format_version=2)

    receipt = backup_and_restore_workspace_v2(
        service=service,
        source_root=source,
        backup_root=tmp_path / "backup",
        restore_root=tmp_path / "restore",
        available_bytes=_host_free_bytes,
    )

    assert receipt.raw_record_count == 0
    assert service.inspect(receipt.restore_root).format_version == 2
    manifest = json.loads((tmp_path / "backup" / "backup_manifest.json").read_text())
    assert manifest["schema_version"] == "workspace-v2-backup-manifest-v3"
    assert manifest["transport"] == "packed-gzip-volumes-v1"
    assert manifest["volume_count"] > 0
    reused = backup_and_restore_workspace_v2(
        service=service,
        source_root=source,
        backup_root=tmp_path / "backup",
        restore_root=tmp_path / "restore-reused",
        available_bytes=_host_free_bytes,
    )
    assert reused.restore_root == tmp_path / "restore-reused"


def test_recovery_rejects_overlapping_paths(tmp_path: Path) -> None:
    service = WorkspaceService(environ={}, home=tmp_path / "home")
    source = tmp_path / "source"
    service.initialize(source, format_version=2)

    with pytest.raises(WorkspaceV2RecoveryError, match="disjoint"):
        backup_and_restore_workspace_v2(
            service=service,
            source_root=source,
            backup_root=source / "backup",
            restore_root=tmp_path / "restore",
        )


def test_packed_backup_restores_multiple_sealed_volumes_without_source(tmp_path: Path) -> None:
    service = WorkspaceService(environ={}, home=tmp_path / "home")
    source = tmp_path / "source"
    service.initialize(source, format_version=2)
    (source / "state" / "payload.bin").write_bytes(b"retained-by-packed-backup" * 32)
    backup = WorkspaceV2BackupService(service)

    manifest = backup.create_packed(
        source,
        tmp_path / "backup",
        volume_bytes=1_000_000,
        fragment_bytes=128_000,
        available_bytes=_host_free_bytes,
    )

    assert manifest.volume_count > 1
    shutil.rmtree(source)
    restored = backup.restore_packed(
        tmp_path / "backup", tmp_path / "restore", available_bytes=_host_free_bytes
    )
    assert restored.format_version == 2
    assert (tmp_path / "restore" / "state" / "payload.bin").read_bytes() == (
        b"retained-by-packed-backup" * 32
    )


def test_stream_manifest_restores_without_materializing_inventory(tmp_path: Path) -> None:
    service = WorkspaceService(environ={}, home=tmp_path / "home")
    source = tmp_path / "source"
    service.initialize(source, format_version=2)
    payload = b"streamed-packed-restore" * 65_536
    (source / "state" / "payload.bin").write_bytes(payload)
    backup = WorkspaceV2BackupService(service)
    manifest = backup.create_packed(
        source,
        tmp_path / "backup",
        volume_bytes=128_000,
        fragment_bytes=32_000,
        available_bytes=_host_free_bytes,
    )
    _seal_as_stream_manifest(tmp_path / "backup", manifest)
    shutil.rmtree(source)

    restored = backup.restore_packed(
        tmp_path / "backup", tmp_path / "restore", available_bytes=_host_free_bytes
    )

    assert restored.format_version == 2
    assert (tmp_path / "restore" / "state" / "payload.bin").read_bytes() == payload


def test_stream_restore_resumes_after_capacity_failure(tmp_path: Path) -> None:
    service = WorkspaceService(environ={}, home=tmp_path / "home")
    source = tmp_path / "source"
    service.initialize(source, format_version=2)
    (source / "state" / "payload.bin").write_bytes(b"resume" * 65_536)
    backup = WorkspaceV2BackupService(service)
    manifest = backup.create_packed(
        source,
        tmp_path / "backup",
        volume_bytes=128_000,
        fragment_bytes=32_000,
        available_bytes=_host_free_bytes,
    )
    _seal_as_stream_manifest(tmp_path / "backup", manifest)
    readings = iter((_host_free_bytes(), _host_free_bytes(), 0))

    with pytest.raises(WorkspaceV2BackupError, match="capacity reserve"):
        backup.restore_packed(
            tmp_path / "backup",
            tmp_path / "restore",
            available_bytes=lambda: next(readings),
            reserve_bytes=1,
        )

    assert (tmp_path / ".restore.packed-restore-checkpoint.json").is_file()
    restored = backup.restore_packed(
        tmp_path / "backup", tmp_path / "restore", available_bytes=_host_free_bytes
    )
    assert restored.workspace_id == service.inspect(source).workspace_id


def test_stream_restore_resume_revalidates_confirmed_volume(tmp_path: Path) -> None:
    service = WorkspaceService(environ={}, home=tmp_path / "home")
    source = tmp_path / "source"
    service.initialize(source, format_version=2)
    (source / "state" / "payload.bin").write_bytes(b"resume" * 65_536)
    backup = WorkspaceV2BackupService(service)
    manifest = backup.create_packed(
        source,
        tmp_path / "backup",
        volume_bytes=128_000,
        fragment_bytes=32_000,
        available_bytes=_host_free_bytes,
    )
    _seal_as_stream_manifest(tmp_path / "backup", manifest)
    readings = iter((_host_free_bytes(), _host_free_bytes(), 0))
    with pytest.raises(WorkspaceV2BackupError, match="capacity reserve"):
        backup.restore_packed(
            tmp_path / "backup",
            tmp_path / "restore",
            available_bytes=lambda: next(readings),
            reserve_bytes=1,
        )

    volumes = [
        json.loads(line)
        for line in (tmp_path / "backup" / "packed-backup-volumes.jsonl").read_text().splitlines()
    ]
    (tmp_path / "backup" / volumes[0]["path"]).write_bytes(b"corrupt")

    with pytest.raises(WorkspaceV2BackupError, match="verification failed"):
        backup.restore_packed(
            tmp_path / "backup", tmp_path / "restore", available_bytes=_host_free_bytes
        )
    assert (tmp_path / ".restore.packed-restore-checkpoint.json").is_file()


def test_packed_journal_stream_rejects_missing_or_truncated_volume(tmp_path: Path) -> None:
    journal = tmp_path / "packed-backup-volumes.jsonl"
    journal.write_text('{"volume_id":"volume-00000000"}\n', encoding="utf-8")
    with pytest.raises(WorkspaceV2BackupError, match="malformed"):
        tuple(_iter_packed_volumes(journal))
    journal.write_text("", encoding="utf-8")
    with pytest.raises(WorkspaceV2BackupError, match="incomplete"):
        tuple(_iter_packed_volumes(journal))


def test_packed_journal_binding_is_streamed_and_hash_bound(tmp_path: Path) -> None:
    journal = tmp_path / "packed-backup-volumes.jsonl"
    journal.write_text("", encoding="utf-8")
    with pytest.raises(WorkspaceV2BackupError, match="incomplete"):
        _packed_journal_binding(journal)


def test_stream_manifest_rejects_a_malformed_journal(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "payload").write_bytes(b"payload")
    inventory = tmp_path / "source-inventory.jsonl"
    write_inventory(source, inventory)
    journal = tmp_path / "packed-backup-volumes.jsonl"
    journal.write_text("{}\n", encoding="utf-8")
    with pytest.raises(WorkspaceV2BackupError, match="malformed"):
        _stream_manifest(
            workspace_id=uuid4(),
            counts=WorkspaceBackupCounts(
                raw_records=0, observations=0, metric_results=0, diagnostic_results=0
            ),
            inventory_path=inventory,
            journal_path=journal,
            created_at=datetime.now(UTC),
        )


def test_stream_manifest_binds_valid_sidecars(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "payload").write_bytes(b"payload")
    inventory = tmp_path / "source-inventory.jsonl"
    write_inventory(source, inventory)
    journal = tmp_path / "packed-backup-volumes.jsonl"
    volume = WorkspaceV2PackedVolume(
        volume_id="volume-00000000",
        path="volumes/volume-00000000.gz",
        compressed_bytes=1,
        decompressed_bytes=15,
        compressed_sha256="0" * 64,
        decompressed_sha256="1" * 64,
        fragments=(
            WorkspaceV2PackedFragment(
                path="payload",
                offset_bytes=0,
                length_bytes=7,
                sha256="2" * 64,
                stream_offset_bytes=0,
            ),
        ),
    )
    journal.write_text(json.dumps(volume.model_dump(mode="json")) + "\n", encoding="utf-8")
    manifest = _stream_manifest(
        workspace_id=uuid4(),
        counts=WorkspaceBackupCounts(
            raw_records=0, observations=0, metric_results=0, diagnostic_results=0
        ),
        inventory_path=inventory,
        journal_path=journal,
        created_at=datetime.now(UTC),
    )
    assert manifest.inventory.file_count == 1
    assert manifest.volume_count == 1


def test_packed_backup_keeps_resumable_checkpoint_when_capacity_is_insufficient(
    tmp_path: Path,
) -> None:
    service = WorkspaceService(environ={}, home=tmp_path / "home")
    source = tmp_path / "source"
    service.initialize(source, format_version=2)
    backup = WorkspaceV2BackupService(service)

    with pytest.raises(WorkspaceV2BackupError, match="capacity reserve"):
        backup.create_packed(
            source,
            tmp_path / "backup",
            available_bytes=lambda: 0,
            reserve_bytes=1,
        )

    incomplete = tmp_path / ".backup.incomplete"
    assert (incomplete / "packed-backup-checkpoint.json").is_file()
    assert not (tmp_path / "backup").exists()
    reads = 0

    def resumed_capacity_reader() -> int:
        nonlocal reads
        reads += 1
        return _host_free_bytes()

    resumed = backup.create_packed(
        source,
        tmp_path / "backup",
        available_bytes=resumed_capacity_reader,
    )
    assert resumed.transport == "packed-gzip-volumes-v1"
    assert reads == 1
    assert not incomplete.exists()


def test_packed_backup_preserves_checkpoint_when_capacity_reader_is_unavailable(
    tmp_path: Path,
) -> None:
    service = WorkspaceService(environ={}, home=tmp_path / "home")
    source = tmp_path / "source"
    service.initialize(source, format_version=2)
    backup = WorkspaceV2BackupService(service)

    reads = iter((10_000_000_000, ValueError("unavailable")))

    def unavailable_after_initial_measurement() -> int:
        value = next(reads)
        if isinstance(value, BaseException):
            raise value
        return value

    with pytest.raises(WorkspaceV2BackupError, match="reader is unavailable"):
        backup.create_packed(
            source,
            tmp_path / "backup",
            available_bytes=unavailable_after_initial_measurement,
        )

    assert (tmp_path / ".backup.incomplete" / "packed-backup-checkpoint.json").is_file()


def test_packed_restore_keeps_checkpoint_when_capacity_is_insufficient(tmp_path: Path) -> None:
    service = WorkspaceService(environ={}, home=tmp_path / "home")
    source = tmp_path / "source"
    service.initialize(source, format_version=2)
    backup = WorkspaceV2BackupService(service)
    backup.create_packed(
        source,
        tmp_path / "backup",
        available_bytes=lambda: 10_000_000_000,
    )

    with pytest.raises(WorkspaceV2BackupError, match="capacity reserve"):
        backup.restore_packed(
            tmp_path / "backup",
            tmp_path / "restore",
            available_bytes=lambda: 0,
            reserve_bytes=1,
        )

    assert (tmp_path / ".restore.incomplete").is_dir()
    assert (tmp_path / ".restore.packed-restore-checkpoint.json").is_file()


def test_stream_packed_backup_rejects_a_corrupt_volume(tmp_path: Path) -> None:
    service = WorkspaceService(environ={}, home=tmp_path / "home")
    source = tmp_path / "source"
    service.initialize(source, format_version=2)
    (source / "state" / "payload.bin").write_bytes(b"resumable-packed-restore" * 65_536)
    backup = WorkspaceV2BackupService(service)
    manifest = backup.create_packed(
        source,
        tmp_path / "backup",
        volume_bytes=128_000,
        fragment_bytes=32_000,
        available_bytes=_host_free_bytes,
    )
    assert manifest.volume_count > 1

    backup_root = tmp_path / "backup"
    volumes = [
        json.loads(line)
        for line in (backup_root / "packed-backup-volumes.jsonl").read_text().splitlines()
    ]
    interrupted_path = backup_root / volumes[1]["path"]
    interrupted_path.write_bytes(b"corrupt")

    with pytest.raises(WorkspaceV2BackupError, match="verification failed"):
        backup.restore_packed(backup_root, tmp_path / "restore", available_bytes=_host_free_bytes)

    assert not (tmp_path / ".restore.incomplete").exists()
