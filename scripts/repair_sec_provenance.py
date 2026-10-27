#!/usr/bin/env python3
"""Seal, apply, or verify the narrow SEC provenance repair of one workspace."""

import argparse
import json
import sys
from pathlib import Path

from pydantic import ValidationError

from investment_analyst.application.runtime import ApplicationRuntime, ApplicationRuntimeError
from investment_analyst.catalog.provider_configuration import resolve_sec_configuration
from investment_analyst.storage import StorageError
from investment_analyst.workspace.models import WorkspaceAccessMode
from investment_analyst.workspace.sec_provenance_repair import (
    SecProvenanceRepairError,
    SecProvenanceRepairReceipt,
    SecProvenanceRepairService,
    load_plan,
    write_plan,
)
from investment_analyst.workspace.service import WorkspaceError, WorkspaceService

_SEC_CAPABILITY = "fundamentals.company_facts"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="mode", required=True)
    for name in ("plan", "apply", "verify"):
        command = subparsers.add_parser(name)
        command.add_argument("--workspace", type=Path, required=True)
        command.add_argument("--source-workspace", type=Path, required=True)
        command.add_argument("--executor-sha", required=True)
        command.add_argument("--asset", action="append", default=[])
        if name == "plan":
            command.add_argument("--output", type=Path, required=True)
        else:
            command.add_argument("--plan", type=Path, required=True)
        if name == "apply":
            command.add_argument("--receipt", type=Path, required=True)
    return parser


def _configurations(runtime: ApplicationRuntime, asset_ids: list[str]):
    declared = tuple(sorted(set(asset_ids)))
    if declared:
        candidates = declared
    else:
        candidates = tuple(
            asset.asset_id for asset in runtime.catalog.list_assets(capability=_SEC_CAPABILITY)
        )
    if not candidates:
        raise SecProvenanceRepairError("no catalog-backed SEC issuer was resolved")
    return tuple(
        resolve_sec_configuration(runtime.provider_resolver, asset_id=asset_id)
        for asset_id in candidates
    )


def _service(
    arguments: argparse.Namespace,
    runtime: ApplicationRuntime,
    *,
    write: bool,
):
    service = WorkspaceService()
    destination_paths = service.resolve(arguments.workspace)
    source_paths = service.resolve(arguments.source_workspace)
    destination_manifest = service.manifest(destination_paths.root)
    source_manifest = service.manifest(source_paths.root)
    storage = service.open_storage(
        destination_paths,
        WorkspaceAccessMode.READ_WRITE if write else WorkspaceAccessMode.READ_ONLY,
    )
    source = service.open_storage(source_paths, WorkspaceAccessMode.READ_ONLY)
    repair = SecProvenanceRepairService(
        storage,
        source=source,
        configurations=_configurations(runtime, list(arguments.asset)),
        destination_workspace_id=destination_manifest.workspace_id,
        source_workspace_id=source_manifest.workspace_id,
        executor_sha=arguments.executor_sha,
    )
    return repair, storage, source


def _plan(arguments: argparse.Namespace, runtime: ApplicationRuntime) -> int:
    repair, storage, source = _service(arguments, runtime, write=False)
    with storage, source:
        plan = repair.build_plan()
    output = write_plan(arguments.output, plan)
    print(
        json.dumps(
            {
                "mode": "plan",
                "schema_version": plan.schema_version,
                "plan_digest": plan.digest(),
                "plan": str(output),
                "references": len(plan.references),
                "entries": len(plan.entries),
                "conflicts": len(plan.conflicts),
                "entry_ids": [str(item.raw_record_id) for item in plan.entries],
                "conflict_ids": [str(item.raw_record_id) for item in plan.conflicts],
            },
            separators=(",", ":"),
            sort_keys=True,
        )
    )
    return 0


def _apply(arguments: argparse.Namespace, runtime: ApplicationRuntime) -> int:
    plan = load_plan(arguments.plan)
    receipt_path = Path(arguments.receipt).expanduser().resolve()
    if receipt_path.exists() or receipt_path.is_symlink():
        existing = SecProvenanceRepairReceipt.model_validate_json(
            receipt_path.read_text(encoding="utf-8")
        )
        if existing.plan_digest != plan.digest() or existing.executor_sha != arguments.executor_sha:
            raise SecProvenanceRepairError("existing receipt binds a different plan or executor")
        print(
            json.dumps(
                {
                    "mode": "apply",
                    "changed": 0,
                    "resumed": True,
                    "receipt": str(receipt_path),
                    "plan_digest": plan.digest(),
                },
                separators=(",", ":"),
                sort_keys=True,
            )
        )
        return 0
    repair, storage, source = _service(arguments, runtime, write=True)
    with storage, source:
        receipt = repair.apply(plan)
        verification = repair.verify(plan)
    if verification.verified is not True:
        raise SecProvenanceRepairError("post-repair verification did not reproduce every dependent")
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    receipt_path.write_text(
        json.dumps(
            receipt.model_dump(mode="json"),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "mode": "apply",
                "changed": receipt.changed(),
                "resumed": False,
                "receipt": str(receipt_path),
                "plan_digest": plan.digest(),
                "verification": verification.model_dump(mode="json"),
            },
            separators=(",", ":"),
            sort_keys=True,
        )
    )
    return 0


def _verify(arguments: argparse.Namespace, runtime: ApplicationRuntime) -> int:
    plan = load_plan(arguments.plan)
    repair, storage, source = _service(arguments, runtime, write=False)
    with storage, source:
        verification = repair.verify(plan)
    print(
        json.dumps(
            {
                "mode": "verify",
                "plan_digest": plan.digest(),
                "verification": verification.model_dump(mode="json"),
            },
            separators=(",", ":"),
            sort_keys=True,
        )
    )
    return 0 if verification.verified else 1


def main() -> int:
    """Execute one maintenance mode and print compact JSON evidence."""
    arguments = _parser().parse_args()
    try:
        runtime = ApplicationRuntime.create_default()
        if arguments.mode == "plan":
            return _plan(arguments, runtime)
        if arguments.mode == "apply":
            return _apply(arguments, runtime)
        return _verify(arguments, runtime)
    except (
        ApplicationRuntimeError,
        SecProvenanceRepairError,
        StorageError,
        WorkspaceError,
        ValidationError,
        OSError,
        ValueError,
    ) as error:
        print(f"SEC provenance repair failed: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
