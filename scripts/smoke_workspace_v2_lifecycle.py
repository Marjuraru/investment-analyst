#!/usr/bin/env python3
"""Bind existing v2 backup and recovery evidence to the exact candidate SHA."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from investment_analyst.workspace.service import WorkspaceService
from investment_analyst.workspace.workspace_v2_backup import (
    WorkspaceV2BackupService,
    WorkspaceV2PackedStreamManifest,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", type=Path, required=True)
    parser.add_argument("--restore", type=Path, required=True)
    parser.add_argument("--backup", type=Path, required=True)
    parser.add_argument("--recovery-receipt", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def _sha() -> str:
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    )
    value = completed.stdout.strip()
    if len(value) != 40:
        raise ValueError("candidate SHA is invalid")
    return value


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        output = args.output.expanduser().resolve(strict=False)
        if not output.is_absolute() or output.exists() or output.is_symlink():
            raise ValueError("output must be a new absolute regular path")
        receipt_path = args.recovery_receipt.expanduser().resolve(strict=False)
        if receipt_path.is_symlink() or not receipt_path.is_file():
            raise ValueError("recovery receipt is unavailable")
        document = json.loads(receipt_path.read_text(encoding="utf-8"))
        receipt = document.get("recovery", document)
        if not isinstance(receipt, dict):
            raise ValueError("recovery receipt is malformed")
        service = WorkspaceService()
        target = service.inspect(args.target)
        restored = service.inspect(args.restore)
        if target.format_version != 2 or restored.format_version != 2:
            raise ValueError("target and restore must be workspace v2")
        _, backup = WorkspaceV2BackupService(service).inspect_packed_for_restore(args.backup)
        if not isinstance(backup, WorkspaceV2PackedStreamManifest):
            raise ValueError("backup is not a sealed stream manifest")
        if receipt.get("backup_id") not in {None, str(backup.backup_id)}:
            raise ValueError("recovery receipt does not bind the backup")
        if receipt.get("backup_root") not in {None, str(args.backup.expanduser().resolve())}:
            raise ValueError("recovery receipt does not bind the backup root")
        if receipt.get("restored_workspace_id") != str(restored.workspace_id):
            raise ValueError("recovery receipt does not bind the restore")
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps(
                {
                    "schema_version": "workspace-v2-lifecycle-smoke-v1",
                    "candidate_sha": _sha(),
                    "backup_id": str(backup.backup_id),
                    "target_workspace_id": str(target.workspace_id),
                    "restore_workspace_id": str(restored.workspace_id),
                    "volume_count": backup.volume_count,
                    "recovery_receipt": str(receipt_path),
                },
                sort_keys=True,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        return 0
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print(f"workspace-v2-lifecycle-smoke: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
