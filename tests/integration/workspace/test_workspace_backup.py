"""Integration coverage for verified local workspace backup and restore."""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from investment_analyst.application.asset_preferences import (
    AssetPreferenceEntry,
    AssetPreferencesStore,
    AssetPreferencesUpdate,
    EffectiveAssetPreferences,
    asset_preferences_fingerprint,
)
from investment_analyst.workspace.backup import (
    BACKUP_MANIFEST_NAME,
    WorkspaceBackupError,
    WorkspaceBackupService,
)
from investment_analyst.workspace.service import WorkspaceService


def _service(tmp_path: Path) -> tuple[WorkspaceService, WorkspaceBackupService, Path]:
    workspace_service = WorkspaceService(
        environ={},
        home=tmp_path / "home",
        clock=lambda: datetime(2026, 8, 1, tzinfo=UTC),
    )
    workspace = tmp_path / "source"
    initialized = workspace_service.initialize(workspace)
    (initialized.paths.state_root / "scheduler.json").write_text(
        '{"schema_version":"test-state-v1"}\n',
        encoding="utf-8",
    )
    return (
        workspace_service,
        WorkspaceBackupService(
            workspace_service,
            clock=lambda: datetime(2026, 8, 1, hour=1, tzinfo=UTC),
        ),
        workspace,
    )


def test_backup_is_repeatable_and_restore_preserves_identity_counts_and_hashes(
    tmp_path: Path,
) -> None:
    workspace_service, service, source = _service(tmp_path)

    first = service.create(source, tmp_path / "backup-1")
    second = service.create(source, tmp_path / "backup-2")
    restored = service.restore(tmp_path / "backup-1", tmp_path / "restored")
    restored_manifest = workspace_service.inspect(tmp_path / "restored")

    assert first.backup_id == second.backup_id
    assert first.files == second.files
    assert first.counts == second.counts
    assert restored.workspace_id == first.source_workspace_id
    assert restored_manifest.workspace_id == first.source_workspace_id
    assert restored.status == "ready"
    assert restored_manifest.status == "ready"
    assert restored.raw_record_count == first.counts.raw_records
    assert restored.observation_count == first.counts.observations
    assert restored.metric_result_count == first.counts.metric_results
    assert restored.diagnostic_result_count == first.counts.diagnostic_results
    assert (tmp_path / "restored/state/scheduler.json").is_file()


def test_restore_rejects_corrupt_or_missing_files_without_publishing_destination(
    tmp_path: Path,
) -> None:
    _, service, source = _service(tmp_path)
    backup = tmp_path / "backup"
    service.create(source, backup)
    manifest = json.loads((backup / BACKUP_MANIFEST_NAME).read_text(encoding="utf-8"))
    target = backup / manifest["files"][0]["path"]
    target.write_bytes(target.read_bytes() + b"corrupt")

    with pytest.raises(WorkspaceBackupError, match="hash verification"):
        service.restore(backup, tmp_path / "not-published")
    assert not (tmp_path / "not-published").exists()

    clean = tmp_path / "clean-backup"
    service.create(source, clean)
    missing = clean / json.loads((clean / BACKUP_MANIFEST_NAME).read_text())["files"][0]["path"]
    missing.unlink()
    with pytest.raises(WorkspaceBackupError, match="inventory"):
        service.restore(clean, tmp_path / "also-not-published")
    assert not (tmp_path / "also-not-published").exists()


def test_backup_and_restore_reject_existing_or_nonempty_destinations(tmp_path: Path) -> None:
    _, service, source = _service(tmp_path)
    existing_backup = tmp_path / "existing-backup"
    existing_backup.mkdir()
    with pytest.raises(WorkspaceBackupError, match="already exists"):
        service.create(source, existing_backup)

    backup = tmp_path / "backup"
    service.create(source, backup)
    destination = tmp_path / "destination"
    destination.mkdir()
    preserved = destination / "preserved.txt"
    preserved.write_text("keep", encoding="utf-8")
    with pytest.raises(WorkspaceBackupError, match="new or empty"):
        service.restore(backup, destination)
    assert preserved.read_text(encoding="utf-8") == "keep"


def test_backup_rejects_internal_symlink_without_copying_external_file(tmp_path: Path) -> None:
    _, service, source = _service(tmp_path)
    external = tmp_path / "external-secret.txt"
    external.write_text("must stay outside the backup", encoding="utf-8")
    (source / "state/external-link.txt").symlink_to(external)
    destination = tmp_path / "backup"

    with pytest.raises(WorkspaceBackupError, match="symbolic links"):
        service.create(source, destination)

    assert not destination.exists()


def test_restore_verification_rejects_symlinked_inventory_file(tmp_path: Path) -> None:
    _, service, source = _service(tmp_path)
    backup = tmp_path / "backup"
    manifest = service.create(source, backup)
    target = backup / manifest.files[0].path
    external = tmp_path / "external-replacement.bin"
    external.write_bytes(target.read_bytes())
    target.unlink()
    target.symlink_to(external)
    destination = tmp_path / "restored"

    with pytest.raises(WorkspaceBackupError, match="symbolic links"):
        service.restore(backup, destination)

    assert not destination.exists()


def test_backup_restore_preserves_preference_revision_and_supports_absent_state(
    tmp_path: Path,
) -> None:
    _, service, source = _service(tmp_path)
    absent_manifest = service.create(source, tmp_path / "without-preferences")
    assert all(
        item.path != "state/asset_preferences_state_v1.json" for item in absent_manifest.files
    )

    entry = AssetPreferenceEntry(
        asset_id="equity:us:aapl",
        watchlist=True,
        favorite=True,
        scheduled_refresh=True,
    )
    entries = (entry,)
    fingerprint = asset_preferences_fingerprint(entries)
    seed = EffectiveAssetPreferences(
        source="cli_seed",
        revision_id=None,
        created_at=None,
        fingerprint=fingerprint,
        entries=entries,
    )
    preference_path = source / "state/asset_preferences_state_v1.json"
    store = AssetPreferencesStore(
        preference_path,
        clock=lambda: datetime(2026, 8, 1, hour=2, tzinfo=UTC),
    )
    persisted, changed = store.apply(
        AssetPreferencesUpdate(
            expected_revision_id=None,
            expected_fingerprint=fingerprint,
            entries=entries,
        ),
        seed,
    )
    manifest = service.create(source, tmp_path / "with-preferences")
    service.restore(tmp_path / "with-preferences", tmp_path / "restored-preferences")
    restored_store = AssetPreferencesStore(
        tmp_path / "restored-preferences/state/asset_preferences_state_v1.json"
    )

    restored = restored_store.load()
    assert changed is True
    assert any(item.path == "state/asset_preferences_state_v1.json" for item in manifest.files)
    assert restored is not None
    assert restored.current.revision_id == persisted.revision_id
    assert restored.current.fingerprint == persisted.fingerprint
    assert preference_path.read_bytes() == restored_store.path.read_bytes()
