#!/usr/bin/env python3
"""Classify, seal, apply, or verify preserved SEC acquisition provenance."""

import argparse
import json
import sys
from pathlib import Path

from pydantic import ValidationError

from investment_analyst.application.runtime import ApplicationRuntime, ApplicationRuntimeError
from investment_analyst.catalog.provider_configuration import resolve_sec_configuration
from investment_analyst.storage import StorageError
from investment_analyst.workspace.models import WorkspaceAccessMode
from investment_analyst.workspace.sec_provenance_models import (
    SecProvenanceClassification,
    SecProvenanceError,
)
from investment_analyst.workspace.sec_provenance_repair import (
    SecProvenanceEvidencePlan,
    SecProvenanceEvidenceReceipt,
    SecProvenanceEvidenceService,
    SecProvenanceRepairError,
    SecProvenanceRepairService,
    load_evidence_plan,
    load_plan,
    write_evidence_plan,
    write_plan,
)
from investment_analyst.workspace.service import WorkspaceError, WorkspaceService

_SEC_CAPABILITY = "fundamentals.company_facts"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="mode", required=True)
    for name in (
        "classify",
        "evidence-plan",
        "evidence-apply",
        "evidence-verify",
        "plan",
        "apply",
        "verify",
    ):
        command = subparsers.add_parser(name)
        command.add_argument("--workspace", type=Path, required=True)
        command.add_argument("--executor-sha", required=True)
        command.add_argument("--asset", action="append", default=[])
        if name in ("classify", "evidence-plan", "plan", "apply", "verify"):
            command.add_argument("--source-workspace", type=Path, required=True)
        if name == "evidence-plan":
            command.add_argument("--base-sha", required=True)
            command.add_argument("--output", type=Path, required=True)
        if name in ("evidence-apply", "evidence-verify"):
            command.add_argument("--plan", type=Path, required=True)
        if name == "evidence-apply":
            command.add_argument("--receipt", type=Path, required=True)
        if name == "plan":
            command.add_argument("--output", type=Path, required=True)
        if name in ("apply", "verify"):
            command.add_argument("--plan", type=Path, required=True)
        if name == "apply":
            command.add_argument("--receipt", type=Path, required=True)
    return parser


def _configurations(runtime: ApplicationRuntime, asset_ids: list[str]):
    declared = tuple(sorted(set(asset_ids)))
    candidates = declared or tuple(
        asset.asset_id for asset in runtime.catalog.list_assets(capability=_SEC_CAPABILITY)
    )
    if not candidates:
        raise SecProvenanceRepairError("no catalog-backed SEC issuer was resolved")
    return tuple(
        resolve_sec_configuration(runtime.provider_resolver, asset_id=asset_id)
        for asset_id in candidates
    )


def _destination_id(arguments: argparse.Namespace, service: WorkspaceService):
    return service.manifest(service.resolve(arguments.workspace).root).workspace_id


def _open(arguments: argparse.Namespace, service: WorkspaceService, *, write: bool):
    storage = service.open_storage(
        service.resolve(arguments.workspace),
        WorkspaceAccessMode.READ_WRITE if write else WorkspaceAccessMode.READ_ONLY,
    )
    source = None
    if getattr(arguments, "source_workspace", None) is not None:
        source = service.open_storage(
            service.resolve(arguments.source_workspace),
            WorkspaceAccessMode.READ_ONLY,
        )
    return storage, source


def _evidence_service(
    arguments: argparse.Namespace,
    runtime: ApplicationRuntime,
    service: WorkspaceService,
    storage,
    source,
    *,
    source_workspace_id,
    base_sha: str,
) -> SecProvenanceEvidenceService:
    return SecProvenanceEvidenceService(
        storage,
        source=source,
        configurations=_configurations(runtime, list(arguments.asset)),
        destination_workspace_id=_destination_id(arguments, service),
        source_workspace_id=source_workspace_id,
        executor_sha=arguments.executor_sha,
        base_sha=base_sha,
    )


def _classify(arguments: argparse.Namespace, runtime: ApplicationRuntime) -> int:
    service = WorkspaceService()
    source_id = service.manifest(service.resolve(arguments.source_workspace).root).workspace_id
    storage, source = _open(arguments, service, write=False)
    with storage, source:
        plan = _evidence_service(
            arguments,
            runtime,
            service,
            storage,
            source,
            source_workspace_id=source_id,
            base_sha=arguments.executor_sha,
        ).build_plan()
    counts: dict[str, int] = {
        classification.value: 0 for classification in SecProvenanceClassification
    }
    for group in plan.groups:
        counts[group.classification.value] += 1
    print(
        json.dumps(
            {
                "mode": "classify",
                "destination_workspace_id": str(plan.destination_workspace_id),
                "source_workspace_id": str(plan.source_workspace_id),
                "references": len(plan.groups),
                "classification_counts": counts,
                "current_reconstructing": sum(
                    group.current_reconstructing for group in plan.groups
                ),
                "historical_required": sum(group.historical_required for group in plan.groups),
                "indirect_references": sum(group.indirect_references for group in plan.groups),
                "admissible_artifacts": len(plan.entries),
                "conflicts": [conflict.model_dump(mode="json") for conflict in plan.conflicts],
                "budget": plan.budget.model_dump(mode="json"),
            },
            separators=(",", ":"),
            sort_keys=True,
        )
    )
    return 0


def _evidence_plan(arguments: argparse.Namespace, runtime: ApplicationRuntime) -> int:
    service = WorkspaceService()
    source_id = service.manifest(service.resolve(arguments.source_workspace).root).workspace_id
    storage, source = _open(arguments, service, write=False)
    with storage, source:
        plan = _evidence_service(
            arguments,
            runtime,
            service,
            storage,
            source,
            source_workspace_id=source_id,
            base_sha=arguments.base_sha,
        ).build_plan()
    output = write_evidence_plan(arguments.output, plan)
    print(
        json.dumps(
            {
                "mode": "evidence-plan",
                "schema_version": plan.schema_version,
                "plan_digest": plan.digest(),
                "plan": str(output),
                "references": len(plan.groups),
                "entries": len(plan.entries),
                "conflicts": len(plan.conflicts),
                "bindings": plan.budget.bindings,
                "bytes_new": plan.budget.bytes_new,
                "entry_ids": [str(item.artifact.raw_record_id) for item in plan.entries],
                "conflict_ids": [str(item.raw_record_id) for item in plan.conflicts],
            },
            separators=(",", ":"),
            sort_keys=True,
        )
    )
    return 0


def _evidence_apply(arguments: argparse.Namespace, runtime: ApplicationRuntime) -> int:
    plan = load_evidence_plan(arguments.plan)
    receipt_path = Path(arguments.receipt).expanduser().resolve()
    service = WorkspaceService()
    if _destination_id(arguments, service) != plan.destination_workspace_id:
        raise SecProvenanceError("sealed plan destination workspace does not match")
    if receipt_path.exists() or receipt_path.is_symlink():
        existing = SecProvenanceEvidenceReceipt.model_validate_json(
            receipt_path.read_text(encoding="utf-8")
        )
        if existing.plan_digest != plan.digest() or existing.executor_sha != arguments.executor_sha:
            raise SecProvenanceError("existing receipt binds a different plan or executor")
        print(
            json.dumps(
                {
                    "mode": "evidence-apply",
                    "resumed": True,
                    "files_created": 0,
                    "receipt": str(receipt_path),
                    "plan_digest": plan.digest(),
                },
                separators=(",", ":"),
                sort_keys=True,
            )
        )
        return 0
    storage, _ = _open(arguments, service, write=False)
    with storage:
        evidence = _evidence_service(
            arguments,
            runtime,
            service,
            storage,
            None,
            source_workspace_id=plan.source_workspace_id,
            base_sha=plan.base_sha,
        )
        receipt = evidence.apply(plan)
        verification = evidence.verify(plan)
    if verification.verified is not True:
        raise SecProvenanceError("published evidence did not reproduce every bound observation")
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
                "mode": "evidence-apply",
                "resumed": False,
                "files_created": receipt.files_created,
                "bytes_written": receipt.bytes_written,
                "receipt": str(receipt_path),
                "plan_digest": plan.digest(),
                "verification": verification.model_dump(mode="json"),
            },
            separators=(",", ":"),
            sort_keys=True,
        )
    )
    return 0


def _evidence_verify(arguments: argparse.Namespace, runtime: ApplicationRuntime) -> int:
    plan: SecProvenanceEvidencePlan = load_evidence_plan(arguments.plan)
    service = WorkspaceService()
    storage, _ = _open(arguments, service, write=False)
    with storage:
        verification = _evidence_service(
            arguments,
            runtime,
            service,
            storage,
            None,
            source_workspace_id=plan.source_workspace_id,
            base_sha=plan.base_sha,
        ).verify(plan)
    print(
        json.dumps(
            {
                "mode": "evidence-verify",
                "plan_digest": plan.digest(),
                "verification": verification.model_dump(mode="json"),
            },
            separators=(",", ":"),
            sort_keys=True,
        )
    )
    return 0 if verification.verified else 1


def _legacy_service(arguments: argparse.Namespace, runtime: ApplicationRuntime, *, write: bool):
    service = WorkspaceService()
    destination_paths = service.resolve(arguments.workspace)
    source_paths = service.resolve(arguments.source_workspace)
    storage = service.open_storage(
        destination_paths,
        WorkspaceAccessMode.READ_WRITE if write else WorkspaceAccessMode.READ_ONLY,
    )
    source = service.open_storage(source_paths, WorkspaceAccessMode.READ_ONLY)
    repair = SecProvenanceRepairService(
        storage,
        source=source,
        configurations=_configurations(runtime, list(arguments.asset)),
        destination_workspace_id=service.manifest(destination_paths.root).workspace_id,
        source_workspace_id=service.manifest(source_paths.root).workspace_id,
        executor_sha=arguments.executor_sha,
    )
    return repair, storage, source


def _legacy_plan(arguments: argparse.Namespace, runtime: ApplicationRuntime) -> int:
    repair, storage, source = _legacy_service(arguments, runtime, write=False)
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


def _legacy_apply(arguments: argparse.Namespace, runtime: ApplicationRuntime) -> int:
    plan = load_plan(arguments.plan)
    receipt_path = Path(arguments.receipt).expanduser().resolve()
    if receipt_path.exists() or receipt_path.is_symlink():
        raise SecProvenanceRepairError("legacy repair receipt already exists for this path")
    repair, storage, source = _legacy_service(arguments, runtime, write=True)
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
                "receipt": str(receipt_path),
                "plan_digest": plan.digest(),
                "verification": verification.model_dump(mode="json"),
            },
            separators=(",", ":"),
            sort_keys=True,
        )
    )
    return 0


def _legacy_verify(arguments: argparse.Namespace, runtime: ApplicationRuntime) -> int:
    plan = load_plan(arguments.plan)
    repair, storage, source = _legacy_service(arguments, runtime, write=False)
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
        handlers = {
            "classify": _classify,
            "evidence-plan": _evidence_plan,
            "evidence-apply": _evidence_apply,
            "evidence-verify": _evidence_verify,
            "plan": _legacy_plan,
            "apply": _legacy_apply,
            "verify": _legacy_verify,
        }
        return handlers[arguments.mode](arguments, runtime)
    except (
        ApplicationRuntimeError,
        SecProvenanceError,
        SecProvenanceRepairError,
        StorageError,
        WorkspaceError,
        ValidationError,
        OSError,
        ValueError,
    ) as error:
        print(f"SEC provenance operation failed: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
