"""Operate the historical continuity archive and deterministic pilot selection."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import UTC, date, datetime
from pathlib import Path
from uuid import UUID

from investment_analyst.storage.compact_analytical_v2 import CompactAnalyticalStore
from investment_analyst.workspace.backup_reuse import inspect_backup_parent
from investment_analyst.workspace.continuity_archive import (
    ContinuityArchiveError,
    ContinuityArchiveWriter,
    load_continuity_archive_manifest,
    restore_continuity_archive,
    upload_continuity_archive,
    verify_continuity_archive,
    verify_remote_continuity_archive,
)
from investment_analyst.workspace.drive_transport import DriveTransport, DriveTransportConfig
from investment_analyst.workspace.historical_recovery import (
    HistoricalRecoveryError,
    HistoricalRecoveryImporter,
    HistoricalRecoveryPause,
    HistoricalRecoveryRepairService,
    HistoricalRecoverySelectionBuilder,
    create_historical_recovery_admission,
    load_historical_recovery_admission,
    load_historical_recovery_manifest,
    require_historical_recovery_budget,
    verify_historical_recovery_selection,
    write_historical_recovery_admission,
)
from investment_analyst.workspace.historical_recovery_budget import (
    HistoricalRecoveryBudget,
    HistoricalRecoveryBudgetError,
    measure_historical_recovery_budget,
)
from investment_analyst.workspace.historical_selection import (
    HistoricalSelectionError,
    HistoricalSelectionImporter,
    HistoricalSelectionRequest,
    MarketFamilySelection,
    build_historical_selection,
    load_selection_manifest,
    workspace_fingerprint,
    write_selection_manifest,
)
from investment_analyst.workspace.models import WorkspaceAccessMode
from investment_analyst.workspace.service import WorkspaceError, WorkspaceService

_DEPLOYMENT_BINDING = (
    Path.home() / ".local/share/investment-analyst/runtime/deployment_workspace_binding.json"
)
_INVENTORY_CLASSIFICATIONS = (
    "KEEP_ACTIVE",
    "IMPORT_LATER",
    "ARCHIVE_ONLY",
    "CONFLICT",
    "UNKNOWN",
)


def _now() -> datetime:
    return datetime.now(UTC)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while True:
            data = stream.read(1024 * 1024)
            if not data:
                break
            digest.update(data)
    return digest.hexdigest()


def _new_file(path: Path) -> Path:
    destination = path.expanduser().resolve()
    if destination.exists() or destination.is_symlink():
        raise ValueError("output path must be new")
    destination.parent.mkdir(parents=True, exist_ok=True)
    return destination


def _workspace(service: WorkspaceService, root: Path):
    paths = service.resolve(root)
    inspection = service.inspect(paths.root)
    manifest = service.manifest(paths.root)
    fingerprint = workspace_fingerprint(
        workspace_id=manifest.workspace_id,
        format_version=manifest.format_version,
        manifest_sha256=hashlib.sha256(paths.manifest_path.read_bytes()).hexdigest(),
        raw_count=inspection.raw_record_count,
        observation_count=inspection.observation_count,
        metric_count=inspection.metric_result_count,
        diagnostic_count=inspection.diagnostic_result_count,
    )
    return paths, inspection, manifest, fingerprint


def _bound_workspace_id() -> UUID | None:
    if not _DEPLOYMENT_BINDING.is_file():
        return None
    try:
        payload = json.loads(_DEPLOYMENT_BINDING.read_bytes())
        current = payload.get("current") or {}
        value = current.get("workspace_id")
        return UUID(value) if isinstance(value, str) else None
    except (OSError, ValueError, TypeError):
        return None


def _require_scratch_destination(service: WorkspaceService, root: Path) -> None:
    paths = service.resolve(root)
    if (paths.state_root / "aapl_local_service.lock").exists():
        raise ValueError("destination has an active local service lock")
    manifest = service.manifest(paths.root)
    if manifest.format_version != 2:
        raise ValueError("pilot destination must use workspace format 2")
    if manifest.workspace_id == _bound_workspace_id():
        raise ValueError("pilot destination is the deployed production workspace")


def _parse_date(value: str) -> date:
    return date.fromisoformat(value)


def _parse_instant(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("known-at must include timezone information")
    return parsed.astimezone(UTC)


def _transport(args: argparse.Namespace) -> DriveTransport | None:
    if not args.rclone_path or not args.encrypted_remote or not args.folder_id:
        return None
    config = DriveTransportConfig(
        rclone_path=Path(args.rclone_path),
        encrypted_remote=args.encrypted_remote,
        folder_id=args.folder_id,
    )
    return DriveTransport(config)


def _inventory(args: argparse.Namespace) -> int:
    previous: dict[str, dict[str, object]] = {}
    if args.previous_inventory:
        for line in (
            Path(args.previous_inventory).expanduser().read_text(encoding="utf-8").splitlines()
        ):
            if not line.strip():
                continue
            row = json.loads(line)
            if isinstance(row, dict) and isinstance(row.get("path"), str):
                previous[row["path"]] = row
    destination = _new_file(Path(args.output))
    seen: dict[str, str] = {}
    with destination.open("xb") as stream:
        for raw_root in args.root:
            root = Path(raw_root).expanduser().resolve()
            consumer = args.consumer or str(root)
            if root.is_dir():
                candidates = (
                    path for path in root.rglob("*") if path.is_file() and not path.is_symlink()
                )
            elif root.is_file() and not root.is_symlink():
                candidates = iter((root,))
            else:
                stream.write(
                    json.dumps(
                        {
                            "path": str(root),
                            "classification": "UNKNOWN",
                            "consumer": consumer,
                            "reason": "root is missing or unsafe",
                        },
                        separators=(",", ":"),
                        sort_keys=True,
                    )
                    + "\n"
                )
                continue
            for path in candidates:
                try:
                    size = path.stat().st_size
                    prior = previous.get(str(path))
                    checksum = (
                        prior["sha256"]
                        if prior
                        and prior.get("size_bytes") == size
                        and isinstance(prior.get("sha256"), str)
                        else _sha256_file(path)
                    )
                    classification = args.classification
                    existing = seen.get(str(path))
                    if existing is not None and existing != checksum:
                        classification = "CONFLICT"
                    seen[str(path)] = checksum
                    if classification not in _INVENTORY_CLASSIFICATIONS:
                        raise ValueError("inventory classification is invalid")
                    stream.write(
                        json.dumps(
                            {
                                "path": str(path),
                                "size_bytes": size,
                                "sha256": checksum,
                                "classification": classification,
                                "consumer": consumer,
                            },
                            separators=(",", ":"),
                            sort_keys=True,
                        )
                        + "\n"
                    )
                except OSError:
                    stream.write(
                        json.dumps(
                            {
                                "path": str(path),
                                "classification": "UNKNOWN",
                                "consumer": consumer,
                                "reason": "path is not readable",
                            },
                            separators=(",", ":"),
                            sort_keys=True,
                        )
                        + "\n"
                    )
        stream.flush()
    print(json.dumps({"schema_version": "continuity-inventory-v1", "output": str(destination)}))
    return 0


def _archive(args: argparse.Namespace) -> int:
    if not args.writer_closed:
        raise ValueError("archive requires a stopped source (--writer-closed)")
    service = WorkspaceService()
    paths, inspection, manifest, fingerprint = _workspace(service, Path(args.source_workspace))
    writer = ContinuityArchiveWriter(
        Path(args.destination),
        source_workspace_id=manifest.workspace_id,
        source_fingerprint=fingerprint,
        code_sha=args.code_sha,
    )
    root = paths.root
    files = list(args.file or ())
    if not files:
        files = [
            str(path.relative_to(root))
            for path in root.rglob("*")
            if path.is_file() and not path.is_symlink()
        ]
    for relative in sorted(files):
        writer.add_file(root / relative, relative)
    archive = writer.finalize()
    transport = _transport(args)
    if transport is not None:
        upload_continuity_archive(transport, writer.root, archive)
        if args.verify_remote_staging:
            verify_remote_continuity_archive(
                transport,
                archive,
                Path(args.verify_remote_staging),
            )
    output = _new_file(Path(args.output))
    output.write_text(
        json.dumps(archive.model_dump(mode="json"), separators=(",", ":"), sort_keys=True),
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "schema_version": "continuity-archive-receipt-v1",
                "archive_id": str(archive.archive_id),
                "manifest_sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
                "source_workspace_id": str(manifest.workspace_id),
                "source_fingerprint": fingerprint,
                "inspection_status": inspection.status,
                "files": len(archive.files),
                "total_bytes": archive.total_bytes,
                "uploaded": transport is not None,
                "manifest": str(output),
            },
            separators=(",", ":"),
            sort_keys=True,
        )
    )
    return 0


def _verify(args: argparse.Namespace) -> int:
    root = Path(args.archive).expanduser().resolve()
    manifest = load_continuity_archive_manifest(root)
    verify_continuity_archive(root, manifest)
    remote_verified = False
    transport = _transport(args)
    if transport is not None:
        if not args.verify_remote_staging:
            raise ValueError("remote verification requires --verify-remote-staging")
        verify_remote_continuity_archive(
            transport,
            manifest,
            Path(args.verify_remote_staging),
        )
        remote_verified = True
    print(
        json.dumps(
            {
                "schema_version": "continuity-archive-verification-v1",
                "archive_id": str(manifest.archive_id),
                "files": len(manifest.files),
                "total_bytes": manifest.total_bytes,
                "local_verified": True,
                "remote_verified": remote_verified,
            },
            separators=(",", ":"),
            sort_keys=True,
        )
    )
    return 0


def _restore(args: argparse.Namespace) -> int:
    root = Path(args.archive).expanduser().resolve()
    manifest = load_continuity_archive_manifest(root)
    restored = restore_continuity_archive(
        root,
        manifest,
        Path(args.destination),
        paths=args.path or None,
    )
    print(
        json.dumps(
            {
                "schema_version": "continuity-archive-restore-v1",
                "archive_id": str(manifest.archive_id),
                "restored": list(restored),
            },
            separators=(",", ":"),
            sort_keys=True,
        )
    )
    return 0


def _select(args: argparse.Namespace) -> int:
    service = WorkspaceService()
    source_paths, source_inspection, source_manifest, source_fingerprint = _workspace(
        service, Path(args.source_workspace)
    )
    destination_paths, _, destination_manifest, _ = _workspace(
        service, Path(args.destination_workspace)
    )
    _require_scratch_destination(service, Path(args.destination_workspace))
    market_selections = []
    for raw in args.market_selection:
        asset_id, source_id, start, end = raw.split("|", 3)
        market_selections.append(
            MarketFamilySelection(
                asset_id=asset_id,
                source_id=source_id,
                start=_parse_date(start),
                end=_parse_date(end),
            )
        )
    request = HistoricalSelectionRequest(
        source_workspace_id=source_manifest.workspace_id,
        source_fingerprint=source_fingerprint,
        destination_workspace_id=destination_manifest.workspace_id,
        known_at=_parse_instant(args.known_at),
        created_at=_now(),
        market_selections=tuple(market_selections),
        warmup_observations=args.warmup_observations,
        fundamental_asset_id=args.fundamental_asset,
        fundamental_source_id=args.fundamental_source,
        fundamental_start=_parse_date(args.fundamental_start),
        document_asset_id=args.document_asset,
        document_source_id=args.document_source,
    )
    storage = service.open_storage(source_paths, WorkspaceAccessMode.READ_ONLY)
    with storage:
        selection = build_historical_selection(storage, request)
    write_selection_manifest(Path(args.output), selection)
    print(
        json.dumps(
            {
                "schema_version": "historical-selection-receipt-v1",
                "selection_id": str(selection.selection_id),
                "digest": selection.digest,
                "source_workspace_id": str(selection.source_workspace_id),
                "destination_workspace_id": str(selection.destination_workspace_id),
                "source_inspection": source_inspection.to_json_dict(),
                "destination_inspection": destination_paths.to_json_dict(),
                "items": len(selection.items),
                "documents": len(selection.document_checksums),
                "exclusions": len(selection.exclusions),
            },
            separators=(",", ":"),
            sort_keys=True,
        )
    )
    return 0


def _import(args: argparse.Namespace) -> int:
    service = WorkspaceService()
    source_paths, _, source_manifest, source_fingerprint = _workspace(
        service, Path(args.source_workspace)
    )
    destination_paths, _, destination_manifest, _ = _workspace(
        service, Path(args.destination_workspace)
    )
    _require_scratch_destination(service, Path(args.destination_workspace))
    manifest = load_selection_manifest(Path(args.manifest))
    if manifest.destination_workspace_id != destination_manifest.workspace_id:
        raise ValueError("selection destination does not match the scratch workspace")
    if manifest.source_workspace_id != source_manifest.workspace_id:
        raise ValueError("selection source does not match the read-only workspace")
    source = service.open_storage(source_paths, WorkspaceAccessMode.READ_ONLY)
    destination = service.open_storage(destination_paths, WorkspaceAccessMode.READ_WRITE)
    with source, destination:
        summary = HistoricalSelectionImporter(
            source=source,
            destination=destination,
            manifest=manifest,
            checkpoint_path=Path(args.checkpoint),
            source_fingerprint=source_fingerprint,
        ).run()
    print(json.dumps(summary.model_dump(mode="json"), separators=(",", ":"), sort_keys=True))
    return 0


def _recovery_select(args: argparse.Namespace) -> int:
    service = WorkspaceService()
    source_paths = service.resolve(Path(args.source_workspace))
    destination_paths = service.resolve(Path(args.destination_workspace))
    source_inspection = service.inspect(source_paths.root)
    destination_inspection = service.inspect(destination_paths.root)
    output_root = Path(args.output_root).expanduser()
    if not output_root.is_absolute():
        raise ValueError("recovery output root must be absolute")
    backup_reused_bytes = 0
    if args.backup_parent:
        backup_reused_bytes = inspect_backup_parent(Path(args.backup_parent)).byte_length
    builder = HistoricalRecoverySelectionBuilder(
        service=service,
        source_paths=source_paths,
        destination_paths=destination_paths,
        source_inspection=source_inspection,
        destination_inspection=destination_inspection,
    )
    print(
        json.dumps(
            {
                "schema_version": "historical-recovery-selection-notice-v1",
                "output_root": str(output_root),
                "source_workspace_id": str(source_inspection.workspace_id),
                "destination_workspace_id": str(destination_inspection.workspace_id),
                "backup_reused_bytes": backup_reused_bytes,
                "reserve_bytes": args.reserve_bytes,
                "workspace_writes": "none",
            },
            separators=(",", ":"),
            sort_keys=True,
        ),
        file=sys.stderr,
        flush=True,
    )
    manifest, matrix, budget = builder.build(
        known_at=_parse_instant(args.known_at),
        output_root=output_root,
        backup_reused_bytes=backup_reused_bytes,
        reserve_bytes=args.reserve_bytes,
    )
    print(
        json.dumps(
            {
                "schema_version": "historical-recovery-selection-receipt-v1",
                "selection_id": str(manifest.selection_id),
                "selection_digest": manifest.digest,
                "source_workspace_id": str(manifest.source_workspace_id),
                "destination_workspace_id": str(manifest.destination_workspace_id),
                "totals": manifest.totals.model_dump(mode="json"),
                "pages": len(manifest.pages),
                "exclusions": len(manifest.exclusions),
                "matrix_assets": len(matrix["assets"]),
                "budget": budget.model_dump(mode="json"),
                "output_root": str(output_root),
            },
            separators=(",", ":"),
            sort_keys=True,
        )
    )
    return 0 if budget.admitted else 3


def _recovery_admit(args: argparse.Namespace) -> int:
    service = WorkspaceService()
    manifest_path = Path(args.manifest)
    manifest = load_historical_recovery_manifest(manifest_path)
    source_paths, _, source_manifest, _ = _workspace(service, Path(args.source_workspace))
    if source_manifest.workspace_id != manifest.source_workspace_id:
        raise ValueError("admission source does not match the selection")
    source = service.open_storage(source_paths, WorkspaceAccessMode.READ_ONLY)
    with source:
        admission = create_historical_recovery_admission(
            manifest=manifest,
            manifest_root=manifest_path.parent,
            source=source,
        )
    write_historical_recovery_admission(Path(args.output), admission)
    print(json.dumps(admission.model_dump(mode="json"), separators=(",", ":"), sort_keys=True))
    return 0


def _recovery_import(args: argparse.Namespace) -> int:
    service = WorkspaceService()
    manifest_path = Path(args.manifest)
    manifest = load_historical_recovery_manifest(manifest_path)
    verify_historical_recovery_selection(manifest, manifest_path.parent)
    budget = HistoricalRecoveryBudget.model_validate_json(Path(args.budget).read_bytes())
    require_historical_recovery_budget(budget)
    admission = load_historical_recovery_admission(Path(args.admission)) if args.admission else None
    source_paths, _, source_manifest, source_fingerprint = _workspace(
        service, Path(args.source_workspace)
    )
    if (
        str(source_manifest.workspace_id) != str(manifest.source_workspace_id)
        or source_fingerprint != manifest.source_fingerprint
    ):
        raise ValueError("recovery source changed after selection")
    destination_paths = service.resolve(Path(args.destination_workspace))
    destination_inspection = service.inspect(destination_paths.root)
    if destination_inspection.workspace_id != manifest.destination_workspace_id:
        raise ValueError("recovery destination does not match the selection")
    fresh_budget = measure_historical_recovery_budget(
        selected_payload_bytes=budget.selected_payload_bytes,
        present_payload_bytes=budget.present_payload_bytes,
        import_peak_bytes=budget.import_peak_bytes,
        backup_new_bytes=budget.backup_new_bytes,
        backup_reused_bytes=budget.backup_reused_bytes,
        restore_peak_bytes=budget.restore_peak_bytes,
        reserve_bytes=budget.reserve_bytes,
    )
    require_historical_recovery_budget(fresh_budget)
    print(
        json.dumps(
            {
                "schema_version": "historical-recovery-import-notice-v1",
                "selection_id": str(manifest.selection_id),
                "destination_workspace_id": str(manifest.destination_workspace_id),
                "import_required": manifest.totals.import_required,
                "new_payload_bytes": fresh_budget.import_new_bytes,
                "host_free_bytes": fresh_budget.host_free_bytes,
                "wsl_free_bytes": fresh_budget.wsl_free_bytes,
                "writer_window": "exclusive",
            },
            separators=(",", ":"),
            sort_keys=True,
        ),
        file=sys.stderr,
        flush=True,
    )
    source = service.open_storage(source_paths, WorkspaceAccessMode.READ_ONLY)
    destination = service.open_storage(destination_paths, WorkspaceAccessMode.READ_WRITE)
    try:
        with source, destination:
            summary = HistoricalRecoveryImporter(
                source=source,
                destination=destination,
                manifest=manifest,
                manifest_root=manifest_path.parent,
                checkpoint_path=Path(args.checkpoint),
                source_fingerprint=source_fingerprint,
                admission=admission,
                page_limit=args.page_limit,
            ).run(max_pages=args.max_pages)
    except HistoricalRecoveryPause as pause:
        print(
            json.dumps(
                {
                    "schema_version": "historical-recovery-paused-v1",
                    "reason": str(pause),
                    "checkpoint": args.checkpoint,
                },
                separators=(",", ":"),
                sort_keys=True,
            )
        )
        return 0
    print(json.dumps(summary.model_dump(mode="json"), separators=(",", ":"), sort_keys=True))
    return 0


def _recovery_repair(args: argparse.Namespace) -> int:
    service = WorkspaceService()
    manifest_path = Path(args.manifest)
    selection = load_historical_recovery_manifest(manifest_path)
    source_paths = service.resolve(Path(args.source_workspace))
    destination_paths = service.resolve(Path(args.destination_workspace))
    print(
        json.dumps(
            {
                "schema_version": "historical-recovery-repair-notice-v1",
                "selection_id": str(selection.selection_id),
                "selection_digest": selection.digest,
                "manifest_sha256": args.manifest_sha256,
                "intent": args.intent,
                "receipt": args.receipt,
                "writer_window": "exclusive",
            },
            separators=(",", ":"),
            sort_keys=True,
        ),
        file=sys.stderr,
        flush=True,
    )
    source = service.open_storage(source_paths, WorkspaceAccessMode.READ_ONLY)
    destination = service.open_storage(destination_paths, WorkspaceAccessMode.READ_WRITE)
    with source, destination:
        receipt = HistoricalRecoveryRepairService(
            source=source,
            destination=destination,
            selection=selection,
            selection_root=manifest_path.parent,
            repair_manifest_path=Path(args.repair_manifest),
            expected_manifest_sha256=args.manifest_sha256,
            intent_path=Path(args.intent),
            receipt_path=Path(args.receipt),
        ).run()
    print(json.dumps(receipt.model_dump(mode="json"), separators=(",", ":"), sort_keys=True))
    return 0


def _recovery_status(args: argparse.Namespace) -> int:
    service = WorkspaceService()
    manifest_path = Path(args.manifest)
    manifest = load_historical_recovery_manifest(manifest_path)
    verify_historical_recovery_selection(manifest, manifest_path.parent)
    source_paths = service.resolve(Path(args.source_workspace))
    destination_paths = service.resolve(Path(args.destination_workspace))
    destination_inspection = service.inspect(destination_paths.root)
    source = service.open_storage(source_paths, WorkspaceAccessMode.READ_ONLY)
    destination = service.open_storage(destination_paths, WorkspaceAccessMode.READ_ONLY)
    with source, destination:
        seal = CompactAnalyticalStore(destination.store.connection).historical_seal()
    checkpoint = None
    if args.checkpoint and Path(args.checkpoint).is_file():
        checkpoint = json.loads(Path(args.checkpoint).read_bytes())
    print(
        json.dumps(
            {
                "schema_version": "historical-recovery-status-v1",
                "selection_id": str(manifest.selection_id),
                "selection_digest": manifest.digest,
                "source_workspace_id": str(manifest.source_workspace_id),
                "destination_workspace_id": str(manifest.destination_workspace_id),
                "totals": manifest.totals.model_dump(mode="json"),
                "pages": len(manifest.pages),
                "destination_inspection": destination_inspection.to_json_dict(),
                "historical_seal": None if seal is None else seal.model_dump(mode="json"),
                "checkpoint": checkpoint,
            },
            separators=(",", ":"),
            sort_keys=True,
        )
    )
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    inventory = subparsers.add_parser("inventory")
    inventory.add_argument("--root", action="append", required=True)
    inventory.add_argument("--consumer")
    inventory.add_argument(
        "--classification",
        default="ARCHIVE_ONLY",
        choices=_INVENTORY_CLASSIFICATIONS,
    )
    inventory.add_argument("--previous-inventory")
    inventory.add_argument("--output", required=True)
    inventory.set_defaults(handler=_inventory)

    archive = subparsers.add_parser("archive")
    archive.add_argument("--source-workspace", required=True)
    archive.add_argument("--destination", required=True)
    archive.add_argument("--code-sha", required=True)
    archive.add_argument("--writer-closed", action="store_true")
    archive.add_argument("--file", action="append")
    archive.add_argument("--output", required=True)
    archive.add_argument("--rclone-path")
    archive.add_argument("--encrypted-remote")
    archive.add_argument("--folder-id")
    archive.add_argument("--verify-remote-staging")
    archive.set_defaults(handler=_archive)

    verify = subparsers.add_parser("verify")
    verify.add_argument("--archive", required=True)
    verify.add_argument("--rclone-path")
    verify.add_argument("--encrypted-remote")
    verify.add_argument("--folder-id")
    verify.add_argument("--verify-remote-staging")
    verify.set_defaults(handler=_verify)

    restore = subparsers.add_parser("restore")
    restore.add_argument("--archive", required=True)
    restore.add_argument("--destination", required=True)
    restore.add_argument("--path", action="append")
    restore.set_defaults(handler=_restore)

    select = subparsers.add_parser("select")
    select.add_argument("--source-workspace", required=True)
    select.add_argument("--destination-workspace", required=True)
    select.add_argument("--known-at", required=True)
    select.add_argument("--market-selection", action="append", required=True)
    select.add_argument("--warmup-observations", type=int, default=200)
    select.add_argument("--fundamental-asset", required=True)
    select.add_argument("--fundamental-source", required=True)
    select.add_argument("--fundamental-start", required=True)
    select.add_argument("--document-asset", required=True)
    select.add_argument("--document-source", required=True)
    select.add_argument("--output", required=True)
    select.set_defaults(handler=_select)

    import_pilot = subparsers.add_parser("import")
    import_pilot.add_argument("--source-workspace", required=True)
    import_pilot.add_argument("--destination-workspace", required=True)
    import_pilot.add_argument("--manifest", required=True)
    import_pilot.add_argument("--checkpoint", required=True)
    import_pilot.set_defaults(handler=_import)

    recovery_select = subparsers.add_parser("recovery-select")
    recovery_select.add_argument("--source-workspace", required=True)
    recovery_select.add_argument("--destination-workspace", required=True)
    recovery_select.add_argument("--known-at", required=True)
    recovery_select.add_argument("--output-root", required=True)
    recovery_select.add_argument("--backup-parent")
    recovery_select.add_argument("--reserve-bytes", type=int, default=5_000_000_000)
    recovery_select.set_defaults(handler=_recovery_select)

    recovery_import = subparsers.add_parser("recovery-import")
    recovery_import.add_argument("--source-workspace", required=True)
    recovery_import.add_argument("--destination-workspace", required=True)
    recovery_import.add_argument("--manifest", required=True)
    recovery_import.add_argument("--budget", required=True)
    recovery_import.add_argument("--admission")
    recovery_import.add_argument("--checkpoint", required=True)
    recovery_import.add_argument("--page-limit", type=int, default=256)
    recovery_import.add_argument("--max-pages", type=int)
    recovery_import.set_defaults(handler=_recovery_import)

    recovery_admit = subparsers.add_parser("recovery-admit")
    recovery_admit.add_argument("--source-workspace", required=True)
    recovery_admit.add_argument("--manifest", required=True)
    recovery_admit.add_argument("--output", required=True)
    recovery_admit.set_defaults(handler=_recovery_admit)

    recovery_status = subparsers.add_parser("recovery-status")
    recovery_status.add_argument("--source-workspace", required=True)
    recovery_status.add_argument("--destination-workspace", required=True)
    recovery_status.add_argument("--manifest", required=True)
    recovery_status.add_argument("--checkpoint")
    recovery_status.set_defaults(handler=_recovery_status)

    recovery_repair = subparsers.add_parser("recovery-repair")
    recovery_repair.add_argument("--source-workspace", required=True)
    recovery_repair.add_argument("--destination-workspace", required=True)
    recovery_repair.add_argument("--manifest", required=True)
    recovery_repair.add_argument("--repair-manifest", required=True)
    recovery_repair.add_argument("--manifest-sha256", required=True)
    recovery_repair.add_argument("--intent", required=True)
    recovery_repair.add_argument("--receipt", required=True)
    recovery_repair.set_defaults(handler=_recovery_repair)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        return int(args.handler(args))
    except (
        ContinuityArchiveError,
        HistoricalRecoveryBudgetError,
        HistoricalRecoveryError,
        HistoricalSelectionError,
        WorkspaceError,
        OSError,
        ValueError,
    ) as error:
        print(
            json.dumps(
                {
                    "schema_version": "continuity-operation-error-v1",
                    "error": str(error),
                },
                separators=(",", ":"),
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
