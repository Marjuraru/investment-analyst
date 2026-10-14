"""CLI coverage for service-owned preservation and retirement on a small fixture."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from investment_analyst.workspace.service import WorkspaceService
from investment_analyst.workspace.workspace_v2_backup import WorkspaceV2BackupService


def _run(repository: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(repository / "scripts" / "manage_workspace_v2_backup.py"),
            *arguments,
        ],
        cwd=Path("/tmp"),
        env={**os.environ, "PYTHONPATH": str(repository / "src")},
        check=False,
        capture_output=True,
        text=True,
    )


def test_cli_preserves_then_retires_a_fully_classified_fixture(tmp_path: Path) -> None:
    repository = Path(__file__).resolve().parents[3]
    source = tmp_path / "source"
    initialized = WorkspaceService(environ={}, home=tmp_path / "home").initialize(source)
    (initialized.paths.state_root / "historic.json").write_text("historic\n", encoding="utf-8")
    processed = source / "storage" / "data" / "processed"
    processed.mkdir(parents=True, exist_ok=True)
    (processed / "event.json").write_text("event\n", encoding="utf-8")
    inventory = tmp_path / "inventory.json"
    operation_id = "21000000-0000-0000-0000-000000000001"

    completed = _run(
        repository,
        "inventory",
        "--source",
        str(source),
        "--operation-id",
        operation_id,
        "--source-fingerprint",
        "f" * 64,
        "--writer-closed",
        "--recoverable-prefix",
        "storage",
        "--output",
        str(inventory),
    )
    assert completed.returncode == 0, completed.stderr
    assert json.loads(inventory.read_text(encoding="utf-8"))["unknown_file_count"] == 0

    complement = tmp_path / "complement"
    checkpoint = tmp_path / "checkpoint.json"
    complement_receipt = tmp_path / "complement-receipt.json"
    completed = _run(
        repository,
        "preserve",
        "--inventory",
        str(inventory),
        "--complement",
        str(complement),
        "--checkpoint",
        str(checkpoint),
        "--output",
        str(complement_receipt),
    )
    assert completed.returncode == 0, completed.stderr
    assert (complement / "state" / "historic.json").is_file()

    retirement = tmp_path / "retirement.json"
    completed = _run(
        repository,
        "retire",
        "--inventory",
        str(inventory),
        "--complement",
        str(complement),
        "--checkpoint",
        str(checkpoint),
        "--output",
        str(retirement),
    )
    assert completed.returncode == 0, completed.stderr
    assert not source.exists()
    assert json.loads(retirement.read_text(encoding="utf-8"))["phase"] == "retired"


def test_cli_rejects_invalid_fingerprint_without_creating_output(tmp_path: Path) -> None:
    repository = Path(__file__).resolve().parents[3]
    source = tmp_path / "source"
    WorkspaceService(environ={}, home=tmp_path / "home").initialize(source)
    output = tmp_path / "inventory.json"
    completed = _run(
        repository,
        "inventory",
        "--source",
        str(source),
        "--operation-id",
        "22000000-0000-0000-0000-000000000002",
        "--source-fingerprint",
        "bad",
        "--writer-closed",
        "--output",
        str(output),
    )
    assert completed.returncode == 2
    assert not output.exists()
    assert "fingerprint" in completed.stderr


def test_cli_uploads_a_sealed_remote_plan_with_a_secret_free_fake_rclone(tmp_path: Path) -> None:
    repository = Path(__file__).resolve().parents[3]
    service = WorkspaceService(environ={}, home=tmp_path / "home")
    source = tmp_path / "source"
    service.initialize(source, format_version=2)
    (source / "state" / "payload.bin").write_bytes(b"payload" * 20_000)
    backup = tmp_path / "backup"
    WorkspaceV2BackupService(service).create_packed(
        source,
        backup,
        volume_bytes=64_000,
        fragment_bytes=16_000,
        available_bytes=lambda: 10_000_000_000,
    )
    rclone = tmp_path / "rclone"
    rclone.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    rclone.chmod(0o700)
    receipt = tmp_path / "remote-receipt.json"

    completed = _run(
        repository,
        "upload-remote",
        "--backup",
        str(backup),
        "--configuration-fingerprint",
        "a" * 64,
        "--rclone-path",
        str(rclone),
        "--encrypted-remote",
        "workspace_crypt",
        "--folder-id",
        "folder-id",
        "--receipt",
        str(receipt),
    )

    assert completed.returncode == 0, completed.stderr
    document = json.loads(receipt.read_text(encoding="utf-8"))
    assert document["objects_uploaded"] > 3
    assert document["objects_reused"] == 0
