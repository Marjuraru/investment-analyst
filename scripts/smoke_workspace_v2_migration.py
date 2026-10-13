#!/usr/bin/env python3
"""Run one isolated v1-to-v2 migration and recovery rehearsal."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

from migrate_workspace_v2 import (
    _append_capacity_attempt,
)
from migrate_workspace_v2 import (
    _host_free_bytes as _measured_host_free_bytes,
)

from investment_analyst.workspace.service import WorkspaceService
from investment_analyst.workspace.workspace_v2_migration import (
    WorkspaceV2MigrationError,
    migrate_workspace_v1_to_v2,
)
from investment_analyst.workspace.workspace_v2_recovery import (
    WorkspaceV2RecoveryError,
    backup_and_restore_workspace_v2,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source-snapshot",
        "--source",
        dest="source_snapshot",
        required=True,
        type=Path,
    )
    parser.add_argument("--scratch-root", required=True, type=Path)
    parser.add_argument("--environment-file", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--page-limit", required=True, type=int)
    parser.add_argument("--reserve-bytes", required=True, type=int)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--migration-receipt", type=Path)
    return parser


def _host_free_bytes(
    attempt_observer: Callable[[dict[str, object]], None] | None = None,
) -> int:
    try:
        return _measured_host_free_bytes(attempt_observer)
    except (
        OSError,
        subprocess.SubprocessError,
        ValueError,
        WorkspaceV2MigrationError,
    ) as error:
        raise ValueError("host capacity API is unavailable") from error


def _capacity_attempt_log(path: Path) -> Callable[[dict[str, object]], None]:
    """Persist each fresh host-capacity probe without trusting a prior reading."""
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ValueError("capacity attempt receipt path is unavailable or unsafe")
    path.parent.mkdir(parents=True, exist_ok=True)

    def record(attempt: dict[str, object]) -> None:
        _append_capacity_attempt(path, attempt)

    return record


def _reused_migration_receipt(
    *, service: WorkspaceService, source_snapshot: Path, target: Path, receipt_path: Path
) -> dict[str, object]:
    """Accept a prior complete analytical receipt without replaying migration."""
    if receipt_path.is_symlink() or not receipt_path.is_file():
        raise ValueError("migration receipt is unavailable or unsafe")
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as error:
        raise ValueError("migration receipt is malformed") from error
    analytical = receipt.get("analytical_equivalence")
    if not isinstance(analytical, dict):
        analytical = receipt
    if receipt.get("complete") is not True:
        raise ValueError("migration receipt is not complete")
    inspection = service.inspect(target)
    if inspection.format_version != 2 or inspection.status != "ready":
        raise ValueError("existing migration target is not ready")
    state_path = target / "state" / "direct-historical-import-state.json"
    if state_path.is_symlink() or not state_path.is_file():
        raise ValueError("migration checkpoint is unavailable or unsafe")
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as error:
        raise ValueError("migration checkpoint is malformed") from error
    source_manifest = service.manifest(source_snapshot)
    expected_counts = {
        "metric_count": inspection.metric_result_count,
        "diagnostic_count": inspection.diagnostic_result_count,
    }
    if (
        state.get("phase") != "complete"
        or state.get("source_workspace_id") != str(source_manifest.workspace_id)
        or any(analytical.get(key) != value for key, value in expected_counts.items())
        or any(state.get(key) != analytical.get(key) for key in expected_counts)
        or state.get("source_fingerprint")
        != receipt.get("source_snapshot", {}).get(
            "source_fingerprint", receipt.get("source_fingerprint")
        )
    ):
        raise ValueError("migration receipt does not bind the existing target")
    return {
        "schema_version": "workspace-v2-migration-reused-evidence-v1",
        "reused": True,
        "receipt_path": str(receipt_path.resolve(strict=False)),
        "target": str(target.resolve(strict=False)),
        "source_workspace_id": str(source_manifest.workspace_id),
        "source_fingerprint": state["source_fingerprint"],
        **expected_counts,
    }


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if not args.environment_file.is_file() or args.environment_file.is_symlink():
            raise ValueError("environment file is unavailable or unsafe")
        if args.scratch_root.is_symlink() or (args.scratch_root.exists() and not args.resume):
            raise ValueError("scratch root must be new unless --resume is set")
        if args.output.exists() or args.output.is_symlink():
            raise ValueError("output receipt must be a new regular path")
        capacity_attempts = args.output.with_suffix(args.output.suffix + ".capacity-attempts.jsonl")
        capacity_attempt_observer = _capacity_attempt_log(capacity_attempts)

        def capacity_reader() -> int:
            return _host_free_bytes(capacity_attempt_observer)

        target = args.scratch_root / "target-v2"
        service = WorkspaceService()
        if args.resume and target.exists():
            if args.migration_receipt is None:
                raise ValueError("--resume requires --migration-receipt")
            migration: dict[str, object] = _reused_migration_receipt(
                service=service,
                source_snapshot=args.source_snapshot.resolve(strict=False),
                target=target.resolve(strict=False),
                receipt_path=args.migration_receipt.resolve(strict=False),
            )
        else:
            completed = migrate_workspace_v1_to_v2(
                service=service,
                source_root=args.source_snapshot.resolve(strict=False),
                destination_root=target.resolve(strict=False),
                page_limit=args.page_limit,
                reserve_bytes=args.reserve_bytes,
                host_free_bytes=capacity_reader,
                reconcile_source=True,
            )
            migration = completed.to_json_dict()
        recovery = backup_and_restore_workspace_v2(
            service=service,
            source_root=target,
            backup_root=args.scratch_root / "backup-v2",
            restore_root=args.scratch_root / "restore-v2",
            available_bytes=capacity_reader,
            reserve_bytes=args.reserve_bytes,
        )
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(
                {
                    "schema_version": "workspace-v2-migration-smoke-v1",
                    "migration": migration,
                    "recovery": recovery.model_dump(mode="json"),
                },
                sort_keys=True,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        return 0
    except (
        OSError,
        ValueError,
        subprocess.SubprocessError,
        WorkspaceV2MigrationError,
        WorkspaceV2RecoveryError,
    ) as error:
        print(f"workspace-v2-migration-smoke: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
