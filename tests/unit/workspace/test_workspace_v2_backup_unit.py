"""Unit coverage for workspace v2 backup and restore recovery."""

from __future__ import annotations

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
from investment_analyst.workspace.backup import (
    WorkspaceBackupError,
    WorkspaceBackupService,
)
from investment_analyst.workspace.models import WorkspaceAccessMode
from investment_analyst.workspace.service import WorkspaceService

_MOMENT = datetime(2026, 8, 1, tzinfo=UTC)


def _seed_workspace(service: WorkspaceService, root: Path) -> None:
    initialized = service.initialize(root, format_version=2)
    source = SourceReference(
        source_id="fixture:backup",
        record_key="backup-record",
        retrieved_at=_MOMENT,
    )
    raw = RawRecord(
        record_id=uuid5(NAMESPACE_URL, "workspace-v2-backup-raw"),
        asset_id="equity:us:aapl",
        source=source,
        event_time=_MOMENT,
        available_at=_MOMENT,
        received_at=_MOMENT,
        payload={"nested": {"checkpoint": 1}},
        schema_version="workspace-v2-backup-v1",
    )
    observation = NormalizedObservation(
        observation_id=uuid5(NAMESPACE_URL, "workspace-v2-backup-observation"),
        raw_record_id=raw.record_id,
        asset_id=raw.asset_id,
        field_name="close",
        value=Decimal("210.5000"),
        unit="USD",
        frequency=DataFrequency.DAY_1,
        observed_at=_MOMENT,
        available_at=_MOMENT,
        normalized_at=_MOMENT,
        source=source,
        quality=DataQuality.VALID,
        transformation_version="1.0.0",
    )
    with service.open_storage(initialized.paths, WorkspaceAccessMode.READ_WRITE) as storage:
        storage.raw_records.save(raw)
        storage.observations.save(observation)
    (root / "state" / "checkpoint.json").write_text('{"cursor": "complete"}\n', encoding="utf-8")


def test_v2_backup_restores_twice_and_preserves_workspace_files(tmp_path: Path) -> None:
    service = WorkspaceService(environ={}, home=tmp_path / "home")
    source = tmp_path / "source"
    _seed_workspace(service, source)
    backup_service = WorkspaceBackupService(service)
    backup = tmp_path / "backup"
    manifest = backup_service.create(source, backup)
    assert manifest.schema_version == "workspace-v2-backup-manifest-v1"
    assert any(item.path == "state/checkpoint.json" for item in manifest.files)

    restored_a = backup_service.restore(backup, tmp_path / "restored-a")
    restored_b = backup_service.restore(backup, tmp_path / "restored-b")
    assert restored_a.status == restored_b.status == "ready"
    assert restored_a.format_version == restored_b.format_version == 2
    assert restored_a.raw_record_count == restored_b.raw_record_count == 1
    assert restored_a.observation_count == restored_b.observation_count == 1
    assert (restored_a.workspace_root / "state/checkpoint.json").read_bytes() == (
        source / "state/checkpoint.json"
    ).read_bytes()
    assert (restored_b.workspace_root / "state/checkpoint.json").read_bytes() == (
        source / "state/checkpoint.json"
    ).read_bytes()


def test_v2_backup_rejects_active_writer_and_tampered_file(tmp_path: Path) -> None:
    service = WorkspaceService(environ={}, home=tmp_path / "home")
    source = tmp_path / "source"
    _seed_workspace(service, source)
    backup_service = WorkspaceBackupService(service)
    initialized = service.resolve(source)
    writer = service.open_storage(initialized, WorkspaceAccessMode.READ_WRITE)
    try:
        with pytest.raises(WorkspaceBackupError, match="writers must close"):
            backup_service.create(source, tmp_path / "blocked-backup")
    finally:
        writer.close()

    backup = tmp_path / "backup"
    manifest = backup_service.create(source, backup)
    target = backup / manifest.files[0].path
    with target.open("ab") as handle:
        handle.write(b"tamper")
    with pytest.raises(WorkspaceBackupError, match="hash verification"):
        backup_service.restore(backup, tmp_path / "tampered-restore")
    assert not (tmp_path / "tampered-restore").exists()
