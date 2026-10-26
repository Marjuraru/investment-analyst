#!/usr/bin/env python3
"""Plan, publish and execute exact operational storage retirement."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from uuid import UUID, uuid4

from investment_analyst.application.local_release import LocalReleaseError, LocalReleaseService
from investment_analyst.workspace.drive_transport import (
    DriveTransport,
    DriveTransportConfig,
    DriveTransportError,
)
from investment_analyst.workspace.storage_retirement import (
    RetentionNotAdmitted,
    StorageRetirementError,
    StorageRetirementService,
)
from investment_analyst.workspace.storage_retirement_models import (
    OperationalRetentionPlan,
    RetentionPlanRequest,
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


def _absolute_path(value: str) -> Path:
    candidate = Path(value).expanduser()
    if not candidate.is_absolute() or ".." in candidate.parts:
        raise argparse.ArgumentTypeError("path must be absolute and normalized")
    return candidate


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="manage_storage_retirement")
    parser.add_argument(
        "--allowed-root",
        type=_absolute_path,
        default=Path("/home/marjuraru/.local/share/investment-analyst"),
    )
    commands = parser.add_subparsers(dest="command", required=True)

    plan = commands.add_parser("plan", help="measure exact paths and seal a retention plan")
    plan.add_argument("--request", type=_existing_file, required=True)
    plan.add_argument("--output", type=_new_file, required=True)

    verify = commands.add_parser("verify", help="re-measure a sealed plan without mutating")
    verify.add_argument("--plan", type=_existing_file, required=True)
    verify.add_argument("--plan-sha256", required=True)
    verify.add_argument("--output", type=_new_file, required=True)

    publish = commands.add_parser(
        "publish", help="upload the sealed plan and control inventory with readback"
    )
    publish.add_argument("--plan", type=_existing_file, required=True)
    publish.add_argument("--plan-sha256", required=True)
    publish.add_argument("--rclone-path", type=Path, required=True)
    publish.add_argument("--encrypted-remote", required=True)
    publish.add_argument("--folder-id", required=True)
    publish.add_argument("--remote-prefix", required=True)
    publish.add_argument("--receipt", type=_new_file, required=True)

    retire = commands.add_parser("retire", help="execute a sealed plan with a durable cursor")
    retire.add_argument("--plan", type=_existing_file, required=True)
    retire.add_argument("--plan-sha256", required=True)
    retire.add_argument("--checkpoint", type=_absolute_path, required=True)
    retire.add_argument("--receipt", type=_absolute_path, required=True)
    retire.add_argument("--reserve-bytes", type=int, default=5_000_000_000)

    status = commands.add_parser("status", help="read the durable retention cursor and receipt")
    status.add_argument("--checkpoint", type=_absolute_path, required=True)
    status.add_argument("--receipt", type=_absolute_path, required=True)

    release = commands.add_parser(
        "retire-release",
        help="retire one unreferenced installed release through the release service",
    )
    release.add_argument("--sha", required=True)
    release.add_argument("--operation-id", type=UUID, required=True)
    release.add_argument("--retained-controls", type=_absolute_path, required=True)
    release.add_argument(
        "--retained-release-path", type=_absolute_path, action="append", default=[]
    )
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


def _load_plan(path: Path) -> OperationalRetentionPlan:
    try:
        return OperationalRetentionPlan.model_validate_json(path.read_bytes())
    except (OSError, ValueError) as error:
        raise ValueError("retention plan is malformed") from error


def _print(document: object) -> None:
    print(json.dumps(document, sort_keys=True))


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    service = StorageRetirementService(allowed_root=args.allowed_root)
    try:
        if args.command == "plan":
            request = RetentionPlanRequest.model_validate_json(args.request.read_bytes())
            plan = service.plan(request)
            _write_output(args.output, plan.model_dump(mode="json"))
            _print(
                {
                    "schema_version": "operational-retention-plan-result-v1",
                    "operation_id": str(plan.operation_id),
                    "plan_sha256": service.plan_sha256(plan),
                    "entries": len(plan.entries),
                    "retire_bytes": plan.retire_bytes,
                    "exclusive_bytes": plan.exclusive_bytes,
                    "host_free_bytes": plan.host_free_bytes,
                    "wsl_free_bytes": plan.wsl_free_bytes,
                }
            )
            return 0
        if args.command == "verify":
            plan = _load_plan(args.plan)
            service.verify(plan, plan_sha256=args.plan_sha256)
            result = {
                "schema_version": "operational-retention-verification-v1",
                "operation_id": str(plan.operation_id),
                "plan_sha256": args.plan_sha256,
                "verified": True,
                "entries": len(plan.entries),
                "executable_entries": sum(
                    1
                    for item in plan.entries
                    if item.decision.value in {"REMOTE_ONLY", "RETIRE_AFTER"}
                ),
            }
            _write_output(args.output, result)
            _print(result)
            return 0
        if args.command == "publish":
            plan = _load_plan(args.plan)
            transport = DriveTransport(
                DriveTransportConfig(
                    rclone_path=args.rclone_path,
                    encrypted_remote=args.encrypted_remote,
                    folder_id=args.folder_id,
                )
            )
            transport.probe()
            receipt = service.publish(
                plan,
                plan_sha256=args.plan_sha256,
                transport=transport,
                remote_prefix=args.remote_prefix,
                receipt_path=args.receipt,
            )
            _print(receipt.model_dump(mode="json"))
            return 0
        if args.command == "retire":
            plan = _load_plan(args.plan)
            receipt = service.retire(
                plan,
                plan_sha256=args.plan_sha256,
                checkpoint_path=args.checkpoint,
                receipt_path=args.receipt,
                reserve_bytes=args.reserve_bytes,
            )
            _print(receipt.model_dump(mode="json"))
            return 0
        if args.command == "status":
            _print(
                service.status(
                    checkpoint_path=args.checkpoint,
                    receipt_path=args.receipt,
                )
            )
            return 0
        if args.command == "retire-release":
            if re.fullmatch(r"[0-9a-f]{40}", args.sha) is None:
                raise ValueError("release SHA must be exactly 40 lowercase hex characters")
            receipt = LocalReleaseService().retire_unused_release(
                args.sha,
                operation_id=args.operation_id,
                retained_controls=args.retained_controls,
                retained_release_paths=tuple(args.retained_release_path),
            )
            _print(receipt.model_dump(mode="json"))
            return 0
    except RetentionNotAdmitted as error:
        print(f"manage_storage_retirement: {error}", file=sys.stderr)
        return 3
    except (
        DriveTransportError,
        LocalReleaseError,
        OSError,
        StorageRetirementError,
        ValueError,
    ) as error:
        print(f"manage_storage_retirement: {error}", file=sys.stderr)
        return 2
    raise AssertionError("unreachable command")


if __name__ == "__main__":
    raise SystemExit(main())
