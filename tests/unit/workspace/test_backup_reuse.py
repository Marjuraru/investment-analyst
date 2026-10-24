"""Unit coverage for verified hardlink reuse of backup generations."""

from __future__ import annotations

import errno
import hashlib
import os
from datetime import UTC, datetime
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

import pytest

from investment_analyst.core.models import RawRecord, SourceReference
from investment_analyst.workspace.backup_reuse import (
    BackupReuseError,
    BackupReuseService,
    inspect_backup_parent,
)
from investment_analyst.workspace.models import WorkspaceAccessMode
from investment_analyst.workspace.service import WorkspaceService
from investment_analyst.workspace.workspace_v2_backup import WorkspaceV2BackupService

_MOMENT = datetime(2026, 10, 22, 12, tzinfo=UTC)


def _raw(identity: str) -> RawRecord:
    return RawRecord(
        record_id=uuid5(NAMESPACE_URL, f"backup-reuse-raw-{identity}"),
        asset_id="equity:us:aapl",
        source=SourceReference(
            source_id="fixture:backup-reuse",
            record_key=identity,
            retrieved_at=_MOMENT,
        ),
        event_time=_MOMENT,
        available_at=_MOMENT,
        received_at=_MOMENT,
        payload={"identity": identity},
        schema_version="backup-reuse-v1",
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _seed(root: Path) -> tuple[WorkspaceService, Path, Path]:
    service = WorkspaceService(environ={}, home=root / "home")
    source = service.initialize(root / "v2", format_version=2).paths.root
    with service.open_storage(service.resolve(source), WorkspaceAccessMode.READ_WRITE) as storage:
        storage.raw_records.save(_raw("first"))
    parent = root / "parent-backup"
    WorkspaceV2BackupService(service).create(source, parent)
    with service.open_storage(service.resolve(source), WorkspaceAccessMode.READ_WRITE) as storage:
        storage.raw_records.save(_raw("second"))
    return service, source, parent


def test_successor_reuses_parent_files_and_preserves_parent(tmp_path: Path) -> None:
    service, source, parent = _seed(tmp_path)
    parent_manifest_before = _sha256(parent / "backup_manifest.json")
    successor = tmp_path / "successor"
    receipt = BackupReuseService(service).create_successor(source, successor, parent)
    assert receipt.files_reused >= 1
    assert receipt.files_copied >= 1
    assert receipt.parent_unchanged is True
    assert receipt.verified is True
    assert receipt.parent_manifest_sha256 == parent_manifest_before
    assert _sha256(parent / "backup_manifest.json") == parent_manifest_before
    parent_entry = "manifest.json"
    successor_entry = successor / parent_entry
    source_entry = source / parent_entry
    assert os.stat(successor_entry).st_ino == os.stat(parent / parent_entry).st_ino
    assert os.stat(successor_entry).st_ino != os.stat(source_entry).st_ino
    inspection = WorkspaceV2BackupService(service).restore(successor, tmp_path / "restored")
    assert inspection.raw_record_count == 2


def test_parent_must_be_a_verified_backup(tmp_path: Path) -> None:
    service, source, parent = _seed(tmp_path)
    with pytest.raises(BackupReuseError, match="disjoint"):
        BackupReuseService(service).create_successor(source, tmp_path / "x", source)
    parent_file = next(item for item in _manifest_files(parent) if (parent / item).is_file())
    with (parent / parent_file).open("ab") as stream:
        stream.write(b"tampered")
    with pytest.raises(BackupReuseError, match="verification"):
        BackupReuseService(service).create_successor(source, tmp_path / "x", parent)


def test_exdev_requires_explicit_copy_admission(tmp_path: Path, monkeypatch) -> None:
    service, source, parent = _seed(tmp_path)

    def _cross_device(*args, **kwargs):
        raise OSError(errno.EXDEV, "cross-device link")

    monkeypatch.setattr(os, "link", _cross_device)
    with pytest.raises(BackupReuseError, match="crossed filesystems"):
        BackupReuseService(service).create_successor(
            source, tmp_path / "no-fallback", parent, allow_copy_fallback=False
        )
    receipt = BackupReuseService(service).create_successor(
        source,
        tmp_path / "fallback",
        parent,
        available_bytes=lambda: 10_000_000_000,
        allow_copy_fallback=True,
    )
    assert receipt.files_copied >= 1
    assert receipt.verified is True
    with pytest.raises(BackupReuseError, match="capacity"):
        BackupReuseService(service).create_successor(
            source,
            tmp_path / "no-capacity",
            parent,
            available_bytes=lambda: 1,
            allow_copy_fallback=True,
        )


def test_parent_inspection_and_symlink_rejection(tmp_path: Path) -> None:
    service, source, parent = _seed(tmp_path)
    inspection = inspect_backup_parent(parent)
    assert inspection.files >= 1
    assert inspection.byte_length > 0
    os.symlink(source / "manifest.json", source / "unsafe-link")
    with pytest.raises(BackupReuseError, match="could not be completed"):
        BackupReuseService(service).create_successor(source, tmp_path / "symlinked", parent)


def _manifest_files(root: Path) -> list[str]:
    import json

    payload = json.loads((root / "backup_manifest.json").read_text(encoding="utf-8"))
    return [item["path"] for item in payload["files"]]
