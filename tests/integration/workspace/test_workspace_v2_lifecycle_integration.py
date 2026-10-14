"""End-to-end lifecycle check using real workspace and packed-recovery services."""

from pathlib import Path
from uuid import uuid4

from investment_analyst.workspace.service import WorkspaceService
from investment_analyst.workspace.workspace_v2_lifecycle import WorkspaceV2LifecycleService
from investment_analyst.workspace.workspace_v2_recovery import backup_and_restore_workspace_v2


def test_recovery_slot_retirement_keeps_the_verified_backup(tmp_path: Path) -> None:
    """A proven restore slot is retired without recopying its packed backup."""
    service = WorkspaceService(environ={}, home=tmp_path / "home")
    source = tmp_path / "source"
    service.initialize(source, format_version=2)
    (source / "state" / "integrated.json").write_text("{}\n", encoding="utf-8")
    recovered = backup_and_restore_workspace_v2(
        service=service,
        source_root=source,
        backup_root=tmp_path / "backup",
        restore_root=tmp_path / "restore",
        available_bytes=lambda: 10_000_000_000,
    )

    receipt = WorkspaceV2LifecycleService(service).retire_recovery_slot(
        recovered,
        operation_id=uuid4(),
        checkpoint_path=tmp_path / "retire-checkpoint.json",
    )

    assert receipt.phase.value == "retired"
    assert (tmp_path / "backup" / "backup_manifest.json").is_file()
    assert not (tmp_path / "restore").exists()
