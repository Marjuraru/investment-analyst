"""Focused streaming coverage for the packed physical parent index."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from investment_analyst.workspace.service import WorkspaceService
from investment_analyst.workspace.workspace_v2_backup import WorkspaceV2BackupService
from investment_analyst.workspace.workspace_v2_physical_delta import (
    WorkspaceV2PhysicalDeltaError,
    WorkspaceV2PhysicalDeltaIndexService,
    _iter_entries,
    verify_physical_delta_index,
)


def _backup(tmp_path: Path) -> tuple[WorkspaceV2BackupService, Path]:
    service = WorkspaceService(environ={}, home=tmp_path / "home")
    source = tmp_path / "source"
    service.initialize(source, format_version=2)
    (source / "state" / "empty.bin").touch()
    (source / "state" / "payload.bin").write_bytes(b"physical-delta" * 20_000)
    backup = WorkspaceV2BackupService(service)
    root = tmp_path / "backup"
    backup.create_packed(
        source,
        root,
        volume_bytes=32_000,
        fragment_bytes=4_000,
        available_bytes=lambda: 10_000_000_000,
    )
    return backup, root


def _builder(backup: WorkspaceV2BackupService) -> WorkspaceV2PhysicalDeltaIndexService:
    return WorkspaceV2PhysicalDeltaIndexService(
        backup,
        clock=lambda: datetime(2026, 10, 15, tzinfo=UTC),
        checkpoint_interval=1,
    )


def test_physical_delta_index_streams_complete_parent_coverage(tmp_path: Path) -> None:
    backup, root = _backup(tmp_path)

    manifest = _builder(backup).build(root, tmp_path / "index")
    trailer = verify_physical_delta_index(tmp_path / "index" / manifest.index_path)

    assert trailer.parent_backup_id == manifest.parent_backup_id
    assert trailer.file_count > 1
    assert trailer.entry_count >= trailer.file_count
    assert trailer.logical_bytes > 0
    entries = (tmp_path / "index" / manifest.index_path).read_text(encoding="utf-8").splitlines()
    assert any('"path":"state/empty.bin"' in line for line in entries)
    assert any('"parent_object_key":"volumes/' in line for line in entries)


def test_physical_delta_index_resumes_an_exact_partial_prefix(tmp_path: Path) -> None:
    backup, root = _backup(tmp_path)
    destination = tmp_path / "index"
    destination.mkdir()
    inspected_root, packed = backup.inspect_packed_for_restore(root)
    first = next(_iter_entries(inspected_root, packed))
    partial = destination / ".physical-delta-index.jsonl.fixture.tmp"
    partial.write_text(
        json.dumps(first.model_dump(mode="json"), sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )

    manifest = _builder(backup).build(root, destination)

    assert verify_physical_delta_index(destination / manifest.index_path).entry_count > 1
    assert not partial.exists()


def test_physical_delta_index_rejects_missing_parent_volume(tmp_path: Path) -> None:
    backup, root = _backup(tmp_path)
    _, packed = backup.inspect_packed_for_restore(root)
    journal = (root / "packed-backup-volumes.jsonl").read_text(encoding="utf-8").splitlines()
    first_volume = json.loads(journal[0])["path"]
    (root / first_volume).unlink()

    with pytest.raises(WorkspaceV2PhysicalDeltaError, match="absent or corrupt"):
        _builder(backup).build(root, tmp_path / "index")


def test_physical_delta_index_rejects_corrupt_sealed_index(tmp_path: Path) -> None:
    backup, root = _backup(tmp_path)
    destination = tmp_path / "index"
    manifest = _builder(backup).build(root, destination)
    index = destination / manifest.index_path
    content = index.read_text(encoding="utf-8")
    index.write_text(content.replace("parent_object_key", "parent_object_kez", 1), encoding="utf-8")

    with pytest.raises(WorkspaceV2PhysicalDeltaError, match="malformed|bind"):
        verify_physical_delta_index(index)
