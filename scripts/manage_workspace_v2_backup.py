#!/usr/bin/env python3
"""Operate workspace-v2 preservation and sealed remote-plan preparation safely."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
from pathlib import Path
from uuid import UUID, uuid4

from investment_analyst.workspace.backup_reuse import BackupReuseError, BackupReuseService
from investment_analyst.workspace.drive_transport import (
    DriveTransport,
    DriveTransportConfig,
    DriveTransportError,
)
from investment_analyst.workspace.service import WorkspaceService
from investment_analyst.workspace.workspace_v2_backup import WorkspaceV2BackupService
from investment_analyst.workspace.workspace_v2_lifecycle import (
    WorkspaceV2LifecycleError,
    WorkspaceV2LifecycleService,
)
from investment_analyst.workspace.workspace_v2_lifecycle_models import (
    LifecycleCheckpoint,
    LifecycleInventory,
    LifecyclePhase,
)
from investment_analyst.workspace.workspace_v2_remote import (
    RemoteBackupManifest,
    RemoteBackupManifestReconciliationReceipt,
    RemoteBackupRestoreReceipt,
    RemoteBackupTransferReceipt,
    RemoteComplementTransferReceipt,
    WorkspaceV2RemoteError,
    WorkspaceV2RemoteService,
)


def _new_file(value: str) -> Path:
    candidate = Path(value).expanduser()
    if candidate.is_symlink() or not candidate.is_absolute():
        raise argparse.ArgumentTypeError("output must be a new absolute regular path")
    path = candidate.resolve(strict=False)
    if path.exists():
        raise argparse.ArgumentTypeError("output must be a new absolute regular path")
    return path


def _existing_file(value: str) -> Path:
    candidate = Path(value).expanduser()
    if candidate.is_symlink() or not candidate.is_absolute():
        raise argparse.ArgumentTypeError("input must be an existing regular file")
    path = candidate.resolve(strict=False)
    if not path.is_file():
        raise argparse.ArgumentTypeError("input must be an existing regular file")
    return path


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="manage_workspace_v2_backup")
    commands = parser.add_subparsers(dest="command", required=True)

    inventory = commands.add_parser("inventory", help="create one frozen-source metadata receipt")
    inventory.add_argument("--source", type=Path, required=True)
    inventory.add_argument("--operation-id", type=UUID, required=True)
    inventory.add_argument("--source-fingerprint", required=True)
    inventory.add_argument("--writer-closed", action="store_true", required=True)
    inventory.add_argument("--recoverable-prefix", action="append", default=[])
    inventory.add_argument("--output", type=_new_file, required=True)

    preserve = commands.add_parser("preserve", help="publish and verify the small local complement")
    preserve.add_argument("--inventory", type=_existing_file, required=True)
    preserve.add_argument("--complement", type=Path, required=True)
    preserve.add_argument("--checkpoint", type=Path, required=True)
    preserve.add_argument("--output", type=_new_file, required=True)

    retire = commands.add_parser("retire", help="retire only a fully classified inactive source")
    retire.add_argument("--inventory", type=_existing_file, required=True)
    retire.add_argument("--complement", type=Path, required=True)
    retire.add_argument("--checkpoint", type=Path, required=True)
    retire.add_argument("--output", type=_new_file, required=True)

    remote = commands.add_parser(
        "plan-remote", help="prepare a content-addressed remote object plan"
    )
    remote.add_argument("--backup", type=Path, required=True)
    remote.add_argument("--configuration-fingerprint", required=True)
    remote.add_argument("--parent-manifest", type=_existing_file)
    remote.add_argument("--output", type=_new_file, required=True)

    retention = commands.add_parser(
        "plan-retention", help="produce a fail-closed preview of logical backup retention"
    )
    retention.add_argument("--manifest", type=_existing_file, action="append", required=True)
    retention.add_argument("--recovery-receipt", type=_existing_file, required=True)
    retention.add_argument("--output", type=_new_file, required=True)

    upload = commands.add_parser(
        "upload-remote", help="upload a sealed remote delta and publish its manifest last"
    )
    upload.add_argument("--backup", type=Path, required=True)
    upload.add_argument("--configuration-fingerprint", required=True)
    upload.add_argument("--parent-manifest", type=_existing_file)
    upload.add_argument("--rclone-path", type=Path, required=True)
    upload.add_argument("--encrypted-remote", required=True)
    upload.add_argument("--folder-id", required=True)
    upload.add_argument("--receipt", type=_new_file, required=True)

    upload_complement = commands.add_parser(
        "upload-complement-remote", help="upload the sealed B complement with durable resume"
    )
    upload_complement.add_argument("--complement", type=Path, required=True)
    upload_complement.add_argument("--rclone-path", type=Path, required=True)
    upload_complement.add_argument("--encrypted-remote", required=True)
    upload_complement.add_argument("--folder-id", required=True)
    upload_complement.add_argument("--checkpoint", type=Path, required=True)
    upload_complement.add_argument("--receipt", type=_new_file, required=True)

    verify_complement = commands.add_parser(
        "verify-complement-remote", help="read and verify every sealed B complement object"
    )
    verify_complement.add_argument("--transfer-receipt", type=_existing_file, required=True)
    verify_complement.add_argument("--rclone-path", type=Path, required=True)
    verify_complement.add_argument("--encrypted-remote", required=True)
    verify_complement.add_argument("--folder-id", required=True)
    verify_complement.add_argument("--staging", type=Path, required=True)
    verify_complement.add_argument("--checkpoint", type=Path, required=True)
    verify_complement.add_argument("--receipt", type=_new_file, required=True)

    reconcile = commands.add_parser(
        "reconcile-remote", help="bind a legacy transfer receipt to its readable remote manifest"
    )
    reconcile.add_argument("--transfer-receipt", type=_existing_file, required=True)
    reconcile.add_argument("--configuration-fingerprint", required=True)
    reconcile.add_argument("--rclone-path", type=Path, required=True)
    reconcile.add_argument("--encrypted-remote", required=True)
    reconcile.add_argument("--folder-id", required=True)
    reconcile.add_argument("--staging", type=Path, required=True)
    reconcile.add_argument("--receipt", type=_new_file, required=True)

    restore_remote = commands.add_parser(
        "restore-remote", help="restore a sealed stream backup directly from the encrypted remote"
    )
    restore_remote.add_argument("--transfer-receipt", type=_existing_file, required=True)
    restore_remote.add_argument("--manifest-reconciliation-receipt", type=_existing_file)
    restore_remote.add_argument("--configuration-fingerprint", required=True)
    restore_remote.add_argument("--rclone-path", type=Path, required=True)
    restore_remote.add_argument("--encrypted-remote", required=True)
    restore_remote.add_argument("--folder-id", required=True)
    restore_remote.add_argument("--staging", type=Path, required=True)
    restore_remote.add_argument("--destination", type=Path, required=True)
    restore_remote.add_argument("--reserve-bytes", type=int, default=5_000_000_000)
    restore_remote.add_argument("--minimum-host-free-bytes", type=int, default=25_000_000_000)
    restore_remote.add_argument("--receipt", type=_new_file, required=True)

    create_reuse = commands.add_parser(
        "create-reuse", help="create a verified successor reusing identical parent files"
    )
    create_reuse.add_argument("--source", type=Path, required=True)
    create_reuse.add_argument("--destination", type=Path, required=True)
    create_reuse.add_argument("--parent", type=Path, required=True)
    create_reuse.add_argument("--reserve-bytes", type=int, default=5_000_000_000)
    create_reuse.add_argument("--allow-copy-fallback", action="store_true")
    create_reuse.add_argument("--receipt", type=_new_file, required=True)

    create_packed = commands.add_parser(
        "create-packed", help="create a resumable packed backup from a verified source"
    )
    create_packed.add_argument("--source", type=Path, required=True)
    create_packed.add_argument("--destination", type=Path, required=True)
    create_packed.add_argument("--volume-bytes", type=int, default=8 * 1024 * 1024)
    create_packed.add_argument("--fragment-bytes", type=int, default=1024 * 1024)
    create_packed.add_argument("--reserve-bytes", type=int, default=5_000_000_000)
    create_packed.add_argument("--writer-closed", action="store_true")
    create_packed.add_argument("--receipt", type=_new_file, required=True)
    return parser


def _write_output(path: Path, document: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(document, stream, sort_keys=True, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        temporary.unlink(missing_ok=True)


def _load_inventory(path: Path) -> LifecycleInventory:
    try:
        return LifecycleInventory.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ValueError("lifecycle inventory is malformed") from error


def _host_free_bytes() -> int:
    return int(shutil.disk_usage(Path("/mnt/c")).free)


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    workspace_service = WorkspaceService()
    lifecycle = WorkspaceV2LifecycleService(workspace_service)
    try:
        if args.command == "inventory":
            source = lifecycle.freeze_source(
                args.source,
                operation_id=args.operation_id,
                source_fingerprint=args.source_fingerprint,
                writer_closed=args.writer_closed,
            )
            result = lifecycle.inventory_source(
                source, recoverable_prefixes=tuple(args.recoverable_prefix)
            )
            _write_output(args.output, result.model_dump(mode="json"))
            return 0
        if args.command == "preserve":
            inventory = _load_inventory(args.inventory)
            source = inventory.frozen_source
            complement = lifecycle.publish_complement(source, args.complement)
            checkpoint = LifecycleCheckpoint(
                operation_id=source.operation_id,
                phase=LifecyclePhase.COMPLEMENT_PUBLISHED,
                source=source,
                complement_sha256=hashlib.sha256(
                    (args.complement / "local-complement-manifest.json").read_bytes()
                ).hexdigest(),
                files_confirmed=len(complement.files),
                bytes_confirmed=complement.total_bytes,
            )
            lifecycle.write_checkpoint(args.checkpoint, checkpoint)
            _write_output(args.output, complement.model_dump(mode="json"))
            return 0
        if args.command == "retire":
            inventory = _load_inventory(args.inventory)
            receipt = lifecycle.retire_source(
                inventory.frozen_source,
                inventory,
                args.complement,
                checkpoint_path=args.checkpoint,
            )
            _write_output(args.output, receipt.model_dump(mode="json"))
            return 0
        if args.command == "plan-remote":
            parent = (
                None
                if args.parent_manifest is None
                else RemoteBackupManifest.model_validate_json(
                    args.parent_manifest.read_text(encoding="utf-8")
                )
            )
            remote = WorkspaceV2RemoteService(WorkspaceV2BackupService(workspace_service))
            plan = remote.plan_base(
                args.backup,
                configuration_fingerprint=args.configuration_fingerprint,
                parent=parent,
            )
            remote.publish_manifest(plan, args.output)
            return 0
        if args.command == "plan-retention":
            manifests = tuple(
                RemoteBackupManifest.model_validate_json(path.read_text(encoding="utf-8"))
                for path in args.manifest
            )
            recovery = RemoteBackupRestoreReceipt.model_validate_json(
                args.recovery_receipt.read_text(encoding="utf-8")
            )
            remote = WorkspaceV2RemoteService(WorkspaceV2BackupService(workspace_service))
            preview = remote.plan_retention_preview(manifests, recovery_receipt=recovery)
            _write_output(args.output, preview.model_dump(mode="json"))
            return 0
        if args.command == "upload-remote":
            parent = (
                None
                if args.parent_manifest is None
                else RemoteBackupManifest.model_validate_json(
                    args.parent_manifest.read_text(encoding="utf-8")
                )
            )
            remote = WorkspaceV2RemoteService(WorkspaceV2BackupService(workspace_service))
            transport = DriveTransport(
                DriveTransportConfig(
                    rclone_path=args.rclone_path,
                    encrypted_remote=args.encrypted_remote,
                    folder_id=args.folder_id,
                )
            )
            transport.probe()
            plan = remote.plan_base(
                args.backup,
                configuration_fingerprint=args.configuration_fingerprint,
                parent=parent,
            )
            remote.upload_base(plan, args.backup, transport, args.receipt)
            return 0
        if args.command == "upload-complement-remote":
            remote = WorkspaceV2RemoteService(WorkspaceV2BackupService(workspace_service))
            transport = DriveTransport(
                DriveTransportConfig(
                    rclone_path=args.rclone_path,
                    encrypted_remote=args.encrypted_remote,
                    folder_id=args.folder_id,
                )
            )
            transport.probe()
            remote.upload_complement(
                args.complement,
                transport=transport,
                checkpoint_path=args.checkpoint,
                receipt_path=args.receipt,
            )
            return 0
        if args.command == "verify-complement-remote":
            transfer = RemoteComplementTransferReceipt.model_validate_json(
                args.transfer_receipt.read_text(encoding="utf-8")
            )
            remote = WorkspaceV2RemoteService(WorkspaceV2BackupService(workspace_service))
            transport = DriveTransport(
                DriveTransportConfig(
                    rclone_path=args.rclone_path,
                    encrypted_remote=args.encrypted_remote,
                    folder_id=args.folder_id,
                )
            )
            transport.probe()
            remote.verify_complement_from_remote(
                transfer,
                transport=transport,
                staging_root=args.staging,
                checkpoint_path=args.checkpoint,
                receipt_path=args.receipt,
            )
            return 0
        if args.command == "create-reuse":
            service = BackupReuseService(workspace_service)
            receipt = service.create_successor(
                args.source,
                args.destination,
                args.parent,
                available_bytes=_host_free_bytes if args.allow_copy_fallback else None,
                reserve_bytes=args.reserve_bytes,
                allow_copy_fallback=args.allow_copy_fallback,
            )
            _write_output(args.receipt, receipt.model_dump(mode="json"))
            return 0
        if args.command == "create-packed":
            service = WorkspaceV2BackupService(workspace_service)
            manifest = service.create_packed(
                args.source,
                args.destination,
                volume_bytes=args.volume_bytes,
                fragment_bytes=args.fragment_bytes,
                available_bytes=_host_free_bytes,
                reserve_bytes=args.reserve_bytes,
                writer_closed=args.writer_closed,
            )
            _write_output(
                args.receipt,
                {
                    "schema_version": "packed-backup-receipt-v1",
                    "writer_closed": args.writer_closed,
                    "manifest": manifest.model_dump(mode="json"),
                },
            )
            return 0
        if args.command == "restore-remote":
            if _host_free_bytes() < args.minimum_host_free_bytes:
                raise ValueError("remote restore host capacity is below the declared floor")
            transfer = RemoteBackupTransferReceipt.model_validate_json(
                args.transfer_receipt.read_text(encoding="utf-8")
            )
            reconciliation = (
                None
                if args.manifest_reconciliation_receipt is None
                else RemoteBackupManifestReconciliationReceipt.model_validate_json(
                    args.manifest_reconciliation_receipt.read_text(encoding="utf-8")
                )
            )
            if reconciliation is not None and reconciliation.backup_id != transfer.backup_id:
                raise ValueError("remote manifest reconciliation binds a different backup")
            remote = WorkspaceV2RemoteService(WorkspaceV2BackupService(workspace_service))
            transport = DriveTransport(
                DriveTransportConfig(
                    rclone_path=args.rclone_path,
                    encrypted_remote=args.encrypted_remote,
                    folder_id=args.folder_id,
                )
            )
            transport.probe()
            remote.restore_from_remote(
                transfer,
                configuration_fingerprint=args.configuration_fingerprint,
                transport=transport,
                staging_root=args.staging,
                destination=args.destination,
                available_bytes=_host_free_bytes,
                reserve_bytes=args.reserve_bytes,
                receipt_path=args.receipt,
                expected_manifest_sha256=(
                    None if reconciliation is None else reconciliation.observed_manifest_sha256
                ),
            )
            return 0
        if args.command == "reconcile-remote":
            transfer = RemoteBackupTransferReceipt.model_validate_json(
                args.transfer_receipt.read_text(encoding="utf-8")
            )
            remote = WorkspaceV2RemoteService(WorkspaceV2BackupService(workspace_service))
            transport = DriveTransport(
                DriveTransportConfig(
                    rclone_path=args.rclone_path,
                    encrypted_remote=args.encrypted_remote,
                    folder_id=args.folder_id,
                )
            )
            transport.probe()
            remote.reconcile_remote_manifest(
                transfer,
                transfer_receipt_path=args.transfer_receipt,
                configuration_fingerprint=args.configuration_fingerprint,
                transport=transport,
                staging_root=args.staging,
                receipt_path=args.receipt,
            )
            return 0
    except KeyboardInterrupt:
        print(
            "manage_workspace_v2_backup: interrupted; confirmed remote objects remain reusable",
            file=sys.stderr,
        )
        return 130
    except (
        BackupReuseError,
        DriveTransportError,
        OSError,
        ValueError,
        WorkspaceV2LifecycleError,
        WorkspaceV2RemoteError,
    ) as error:
        print(f"manage_workspace_v2_backup: {error}", file=sys.stderr)
        return 2
    raise AssertionError("unreachable command")


if __name__ == "__main__":
    raise SystemExit(main())
