#!/usr/bin/env python3
"""Inspect, migrate, and verify a v1 workspace with a durable supervisor receipt."""

from __future__ import annotations

import argparse
import errno
import json
import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import TypedDict
from uuid import NAMESPACE_URL, uuid5

from investment_analyst.storage.compact_analytical_v2 import CompactAnalyticalStore
from investment_analyst.storage.historical_analytical_import import (
    DirectHistoricalAnalyticalSource,
)
from investment_analyst.storage.workspace_v2 import WorkspaceV2Store
from investment_analyst.storage.workspace_v2_inventory import (
    WorkspaceV2AnalyticalImporter,
    WorkspaceV2InventoryState,
)
from investment_analyst.workspace.backup import verify_v1_migration_source_snapshot
from investment_analyst.workspace.models import WorkspaceAccessMode
from investment_analyst.workspace.service import WorkspaceError, WorkspaceService
from investment_analyst.workspace.workspace_v2_backup_inventory import write_inventory
from investment_analyst.workspace.workspace_v2_migration import (
    WorkspaceV2MigrationError,
    WorkspaceV2MigrationPhase,
    WorkspaceV2MigrationProgress,
    create_v1_migration_source_snapshot,
    load_v1_migration_source_snapshot,
    migrate_workspace_v1_to_v2,
    source_fingerprint,
)
from investment_analyst.workspace.workspace_v2_migration_models import (
    WorkspaceV2MigrationWorkCheckpoint,
    WorkspaceV2MigrationWorkPhase,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    snapshot = commands.add_parser("snapshot")
    snapshot.add_argument("--source", required=True, type=Path)
    snapshot.add_argument("--destination", required=True, type=Path)
    snapshot.add_argument("--report", required=True, type=Path)
    snapshot.add_argument("--reserve-bytes", required=True, type=int)
    inventory = commands.add_parser("inventory")
    inventory.add_argument("--source", required=True, type=Path)
    inventory.add_argument("--report", required=True, type=Path)
    status = commands.add_parser("status")
    status.add_argument("--destination", required=True, type=Path)
    status.add_argument("--report", required=True, type=Path)
    budget = commands.add_parser("budget")
    budget.add_argument("--destination", required=True, type=Path)
    budget.add_argument("--source-snapshot", required=True, type=Path)
    budget.add_argument("--report", required=True, type=Path)
    verify = commands.add_parser("verify")
    verify.add_argument("--source", required=True, type=Path)
    verify.add_argument("--destination", required=True, type=Path)
    verify.add_argument("--inventory", required=True, type=Path)
    verify.add_argument("--report", required=True, type=Path)
    for command in ("migrate", "migrate-worker", "analytical", "analytical-worker"):
        child = commands.add_parser(command)
        child.add_argument("--source", required=True, type=Path)
        child.add_argument("--report", required=True, type=Path)
        child.add_argument("--destination", required=True, type=Path)
        child.add_argument("--inventory", required=True, type=Path)
        child.add_argument("--page-limit", required=True, type=int)
        child.add_argument("--reserve-bytes", required=True, type=int)
        child.add_argument("--reconcile-source", action="store_true")
    return parser


def _new_json(path: Path, payload: dict[str, object]) -> None:
    if not path.is_absolute() or path.exists() or path.is_symlink():
        raise WorkspaceV2MigrationError("report path must be a new absolute regular path")
    _replace_json(path, payload)


def _replace_json(path: Path, payload: dict[str, object]) -> None:
    """Atomically refresh a regular supervisor receipt without following links."""
    if not path.is_absolute() or path.is_symlink() or (path.exists() and not path.is_file()):
        raise WorkspaceV2MigrationError("report path must be an absolute regular path")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            json.dump(payload, stream, sort_keys=True, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


_HOST_CAPACITY_TIMEOUT_SECONDS = 15
_HOST_CAPACITY_ATTEMPTS = 2
_TRANSIENT_HOST_ERRNOS = frozenset({errno.EAGAIN, errno.EINTR, errno.EMFILE, errno.ENFILE})


def _host_free_bytes(attempt_observer: Callable[[dict[str, object]], None] | None = None) -> int:
    command = [
        "powershell.exe",
        "-NoProfile",
        "-Command",
        "[System.IO.DriveInfo]::new('C').AvailableFreeSpace",
    ]
    last_error: OSError | subprocess.SubprocessError | None = None
    for ordinal in range(1, _HOST_CAPACITY_ATTEMPTS + 1):
        started = time.monotonic_ns()
        try:
            completed = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                timeout=_HOST_CAPACITY_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired as error:
            last_error = error
            _record_capacity_attempt(attempt_observer, ordinal, started, "timeout", None, "timeout")
            if ordinal < _HOST_CAPACITY_ATTEMPTS:
                continue
            break
        except (OSError, subprocess.SubprocessError) as error:
            last_error = error
            transient = isinstance(error, OSError) and error.errno in _TRANSIENT_HOST_ERRNOS
            _record_capacity_attempt(
                attempt_observer,
                ordinal,
                started,
                "os_error" if isinstance(error, OSError) else "subprocess_error",
                None,
                "transient" if transient else "process_error",
            )
            if transient and ordinal < _HOST_CAPACITY_ATTEMPTS:
                continue
            break
        if completed.returncode != 0:
            transient = _is_transient_host_capacity_exit(completed.stderr)
            _record_capacity_attempt(
                attempt_observer,
                ordinal,
                started,
                "nonzero_exit",
                completed.returncode,
                "transient_wsl_vsock" if transient else "nonzero_exit",
            )
            if transient and ordinal < _HOST_CAPACITY_ATTEMPTS:
                continue
            if transient:
                break
            raise WorkspaceV2MigrationError("host capacity API returned a nonzero exit status")
        value = completed.stdout.strip()
        if not value.isdecimal():
            _record_capacity_attempt(
                attempt_observer,
                ordinal,
                started,
                "invalid_output",
                completed.returncode,
                "invalid_output",
            )
            raise WorkspaceV2MigrationError("host capacity API returned an invalid value")
        _record_capacity_attempt(
            attempt_observer,
            ordinal,
            started,
            "success",
            completed.returncode,
            "measured",
        )
        return int(value)
    raise WorkspaceV2MigrationError("host capacity API invocation failed") from last_error


def _is_transient_host_capacity_exit(stderr: str) -> bool:
    """Classify the known WSL host-bridge failure without retaining command output."""
    normalized = stderr.casefold()
    return ("utilbindvsockanyport" in normalized and "socket failed" in normalized) or (
        "utilacceptvsock" in normalized and "accept4 failed 110" in normalized
    )


def _record_capacity_attempt(
    observer: Callable[[dict[str, object]], None] | None,
    ordinal: int,
    started: int,
    outcome: str,
    returncode: int | None,
    diagnostic: str,
) -> None:
    if observer is not None:
        observer(
            {
                "schema_version": "workspace-v2-host-capacity-attempt-v1",
                "ordinal": ordinal,
                "duration_ms": (time.monotonic_ns() - started) // 1_000_000,
                "outcome": outcome,
                "returncode": returncode,
                "diagnostic": diagnostic,
            }
        )


class _PhysicalMeasurement(TypedDict):
    allocated_bytes: int
    apparent_bytes: int


def _du_bytes(root: Path, *, apparent: bool) -> int:
    """Read Linux physical allocation through the same tool used by the runbook."""
    command = ["du", "-s", "-B1"]
    if apparent:
        command.append("--apparent-size")
    command.append(str(root))
    completed = subprocess.run(command, check=True, capture_output=True, text=True)
    fields = completed.stdout.strip().split("\t", 1)
    if len(fields) != 2 or not fields[0].isdecimal():
        raise WorkspaceV2MigrationError("physical capacity tool returned an invalid value")
    return int(fields[0])


def _measure_physical_root(root: Path) -> _PhysicalMeasurement:
    if not root.is_absolute() or root.is_symlink() or not root.is_dir():
        raise WorkspaceV2MigrationError("capacity root is unavailable or unsafe")
    return {
        "allocated_bytes": _du_bytes(root, apparent=False),
        "apparent_bytes": _du_bytes(root, apparent=True),
    }


def _capacity_probe() -> dict[str, object]:
    """Return the host C: probe without treating an unavailable API as free space."""
    try:
        return {"status": "MEASURED", "available_bytes": _host_free_bytes()}
    except (OSError, subprocess.SubprocessError, WorkspaceV2MigrationError) as error:
        return {
            "status": "UNKNOWN",
            "reason_code": "HOST_CAPACITY_API_UNAVAILABLE",
            "error_type": error.__class__.__name__,
        }


def _phase_budget(
    *,
    host: dict[str, object],
    existing_bytes: int,
    new_write_bytes: int | None,
    auxiliary_bytes: int | None,
    evidence_bytes: int | None,
    reserve_bytes: int,
) -> dict[str, object]:
    """Keep measured present bytes separate from unmeasured future write quantities."""
    pending = (new_write_bytes, auxiliary_bytes, evidence_bytes)
    payload: dict[str, object] = {
        "existing_assigned_bytes": existing_bytes,
        "new_write_bytes": new_write_bytes if new_write_bytes is not None else "UNKNOWN",
        "auxiliary_bytes": auxiliary_bytes if auxiliary_bytes is not None else "UNKNOWN",
        "evidence_bytes": evidence_bytes if evidence_bytes is not None else "UNKNOWN",
        "reserve_bytes": reserve_bytes,
    }
    available = host.get("available_bytes")
    if not isinstance(available, int) or any(value is None for value in pending):
        payload.update(
            {
                "required_incremental_bytes": "UNKNOWN",
                "margin_bytes": "UNKNOWN",
                "deficit_bytes": "UNKNOWN",
            }
        )
        return payload
    required = sum(value for value in pending if value is not None) + reserve_bytes
    payload.update(
        {
            "required_incremental_bytes": required,
            "margin_bytes": available - required,
            "deficit_bytes": max(0, required - available),
        }
    )
    return payload


def _append_progress(path: Path, event: WorkspaceV2MigrationProgress) -> None:
    payload = {"at": datetime.now(UTC).isoformat(), **event.to_json_dict()}
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def _append_capacity_attempt(path: Path, attempt: dict[str, object]) -> None:
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(attempt, sort_keys=True, separators=(",", ":")) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def _safe_error_detail(error: BaseException) -> str:
    """Return a compact diagnostic without exposing subprocess output or paths."""
    if isinstance(error, (WorkspaceError, WorkspaceV2MigrationError, ValueError)):
        return str(error)
    if isinstance(error, subprocess.CalledProcessError):
        return "external command failed"
    if isinstance(error, OSError):
        return "filesystem operation failed"
    return "operation failed"


def _load_direct_analytical_state(path: Path) -> WorkspaceV2InventoryState:
    if path.is_symlink() or not path.is_file():
        raise WorkspaceV2MigrationError("direct analytical checkpoint is missing or unsafe")
    try:
        state = WorkspaceV2InventoryState.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as error:
        raise WorkspaceV2MigrationError("direct analytical checkpoint is incompatible") from error
    if state.policy_version != "direct-historical-import-v1" or state.phase != "complete":
        raise WorkspaceV2MigrationError("direct analytical checkpoint is incomplete")
    return state


def _verify_pairwise_checkpoint(
    *,
    path: Path,
    state: WorkspaceV2InventoryState,
    destination_root: Path,
    destination_workspace_id: object,
    family: str,
    count: int,
    digest: str,
) -> WorkspaceV2MigrationWorkCheckpoint:
    if path.is_symlink() or not path.is_file():
        raise WorkspaceV2MigrationError("paired analytical verification checkpoint is missing")
    try:
        checkpoint = WorkspaceV2MigrationWorkCheckpoint.model_validate_json(
            path.read_text(encoding="utf-8")
        )
    except (OSError, UnicodeError, ValueError) as error:
        raise WorkspaceV2MigrationError(
            "paired analytical verification checkpoint is incompatible"
        ) from error
    cursor = state.metric_cursor if family == "metrics" else state.diagnostic_cursor
    expected_cursor = (
        f"{cursor.available_at.isoformat()}|{cursor.identifier}" if cursor is not None else None
    )
    if (
        checkpoint.phase is not WorkspaceV2MigrationWorkPhase.VERIFY
        or checkpoint.source_fingerprint != state.source_fingerprint
        or checkpoint.destination_root != destination_root
        or checkpoint.destination_workspace_id != destination_workspace_id
        or checkpoint.policy_version != state.policy_version
        or checkpoint.verifier_policy != f"paired-{family}-inventory-v1"
        or checkpoint.input_references != (family,)
        or checkpoint.cursor != expected_cursor
        or checkpoint.confirmed_count != count
        or checkpoint.accumulated_digest != digest
    ):
        raise WorkspaceV2MigrationError("paired analytical verification checkpoint changed")
    return checkpoint


def _child_environment() -> dict[str, str]:
    """Make the supervisor's child import this worktree from every cwd."""
    source = str(Path(__file__).resolve().parents[1] / "src")
    inherited = os.environ.get("PYTHONPATH", "")
    return {
        **os.environ,
        "PYTHONPATH": source if not inherited else os.pathsep.join((source, inherited)),
    }


def _safe_child_error(path: Path) -> str | None:
    """Persist the CLI's bounded safe error line in the supervisor receipt."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        if line.startswith("workspace-v2-migration: "):
            return line[:512]
    return None


def _process_identity(pid: int) -> dict[str, object]:
    """Capture the Linux process identity needed to distinguish a reused PID."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        closing = stat.rfind(")")
        fields = stat[closing + 2 :].split()
        start_ticks = fields[19]
        boot_id = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="utf-8").strip()
    except (IndexError, OSError):
        return {"pid": pid, "start_ticks": None, "boot_id": None}
    return {"pid": pid, "start_ticks": start_ticks, "boot_id": boot_id}


def _supervisor_payload(
    *,
    started_at: str,
    started_monotonic: int,
    status: str,
    stdout: Path,
    stderr: Path,
    process: dict[str, object] | None,
    progress: dict[str, object] | None = None,
    returncode: int | None = None,
    receipt_present: bool = False,
) -> dict[str, object]:
    """Create the one durable supervisor receipt at each lifecycle boundary."""
    return {
        "schema_version": "workspace-v2-migration-supervisor-v1",
        "started_at": started_at,
        "updated_at": datetime.now(UTC).isoformat(),
        "monotonic_elapsed_ns": time.monotonic_ns() - started_monotonic,
        "status": status,
        "process": process,
        "progress": progress,
        "returncode": returncode,
        "signal": -returncode if returncode is not None and returncode < 0 else None,
        "receipt_present": receipt_present,
        "stdout_path": str(stdout),
        "stderr_path": str(stderr),
        "safe_error": _safe_child_error(stderr) if status == "error" else None,
    }


def _safe_progress(destination: Path) -> dict[str, object] | None:
    """Expose only the latest owned checkpoint fields; never inspect workspace data."""
    state_root = destination / "state"
    if state_root.is_symlink() or not state_root.is_dir():
        return {"status": "UNKNOWN", "reason_code": "STATE_ROOT_UNAVAILABLE"}
    candidates: list[Path] = []
    for pattern in (
        "*-verification-work.json",
        "*-import-state*.json",
        "workspace-v2-source-transition.json",
    ):
        candidates = sorted(
            path for path in state_root.glob(pattern) if path.is_file() and not path.is_symlink()
        )
        if candidates:
            break
    if not candidates:
        return {"status": "UNKNOWN", "reason_code": "CHECKPOINT_NOT_FOUND"}
    path = candidates[-1]
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        return {
            "status": "UNKNOWN",
            "checkpoint": path.name,
            "reason_code": "CHECKPOINT_UNREADABLE",
        }
    if not isinstance(document, dict):
        return {
            "status": "UNKNOWN",
            "checkpoint": path.name,
            "reason_code": "CHECKPOINT_UNREADABLE",
        }
    progress: dict[str, object] = {"status": "OBSERVED", "checkpoint": path.name}
    for field in ("phase", "confirmed_count", "metric_count", "diagnostic_count", "ordinal"):
        value = document.get(field)
        if isinstance(value, (str, int)) and not isinstance(value, bool):
            progress[field] = value
    return progress


def _worker(args: argparse.Namespace) -> int:
    report = args.report.resolve(strict=False)
    progress = report.with_suffix(report.suffix + ".progress.jsonl")
    capacity_attempts = report.with_suffix(report.suffix + ".capacity-attempts.jsonl")
    if (
        report.exists()
        or report.is_symlink()
        or progress.is_symlink()
        or capacity_attempts.exists()
        or capacity_attempts.is_symlink()
    ):
        raise WorkspaceV2MigrationError("migration report already exists or is unsafe")
    service = WorkspaceService()
    destination_paths = service.resolve(args.destination.resolve(strict=False))
    direct_state = destination_paths.state_root / "direct-historical-import-state.json"
    transition = service.v2_source_transition(destination_paths)
    if (
        (direct_state.exists() or direct_state.is_symlink())
        and transition is not None
        and transition.phase is WorkspaceV2MigrationPhase.COMPLETE
    ):
        # A direct analytical state is the durable handoff from raw and
        # observation import.  Its worker revalidates the B binding before it
        # touches the destination, so this is never a trust-based skip.
        return _analytical_worker(args)
    receipt = migrate_workspace_v1_to_v2(
        service=service,
        source_root=args.source.resolve(strict=False),
        destination_root=args.destination.resolve(strict=False),
        page_limit=args.page_limit,
        reserve_bytes=args.reserve_bytes,
        host_free_bytes=lambda: _host_free_bytes(
            attempt_observer=lambda attempt: _append_capacity_attempt(capacity_attempts, attempt)
        ),
        reconcile_source=args.reconcile_source,
        progress=lambda event: _append_progress(progress, event),
    )
    _new_json(report, receipt.to_json_dict())
    return 0


def _analytical_worker(args: argparse.Namespace) -> int:
    """Resume only the durable analytical verification, never raw or observations."""
    report = args.report.resolve(strict=False)
    if report.exists() or report.is_symlink():
        raise WorkspaceV2MigrationError("analytical report already exists or is unsafe")
    service = WorkspaceService()
    source_root = args.source.resolve(strict=False)
    destination_root = args.destination.resolve(strict=False)
    snapshot = load_v1_migration_source_snapshot(service=service, snapshot_root=source_root)
    destination_paths = service.resolve(destination_root)
    destination = service.inspect(destination_root)
    if destination.format_version != 2 or destination.errors:
        raise WorkspaceV2MigrationError("analytical target must be a ready workspace v2")
    state_path = destination_paths.state_root / "direct-historical-import-state.json"
    if state_path.is_symlink() or not state_path.is_file():
        raise WorkspaceV2MigrationError("analytical checkpoint is missing or unsafe")
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise WorkspaceV2MigrationError("analytical checkpoint is incompatible") from error
    fingerprint = state.get("source_fingerprint")
    if not isinstance(fingerprint, str) or fingerprint != snapshot.source_fingerprint:
        raise WorkspaceV2MigrationError("analytical checkpoint does not bind to snapshot B")
    source_paths = service.resolve(source_root)
    with (
        service.open_storage(source_paths, WorkspaceAccessMode.READ_ONLY) as source,
        service.open_storage(destination_paths, WorkspaceAccessMode.READ_WRITE) as target,
    ):
        if not isinstance(target.store, WorkspaceV2Store):
            raise WorkspaceV2MigrationError("analytical target did not open workspace v2")
        receipt = WorkspaceV2AnalyticalImporter(
            DirectHistoricalAnalyticalSource(source),
            CompactAnalyticalStore(target.store.connection),
            source_root=source_root,
            destination_root=destination_root,
            state_root=destination_paths.state_root,
            source_workspace_id=str(service.manifest(source_root).workspace_id),
            source_fingerprint=fingerprint,
            page_limit=args.page_limit,
            allow_source_transition=True,
            direct_transport=True,
        ).run()
    _new_json(report, receipt.model_dump(mode="json"))
    return 0


def _supervise(args: argparse.Namespace) -> int:
    report = args.report.resolve(strict=False)
    supervisor = report.with_suffix(report.suffix + ".supervisor.json")
    stdout = report.with_suffix(report.suffix + ".stdout.log")
    stderr = report.with_suffix(report.suffix + ".stderr.log")
    if any(path.exists() or path.is_symlink() for path in (report, supervisor, stdout, stderr)):
        raise WorkspaceV2MigrationError("migration report paths must be new and safe")
    report.parent.mkdir(parents=True, exist_ok=True)
    started_wall = datetime.now(UTC).isoformat()
    started_monotonic = time.monotonic_ns()
    _new_json(
        supervisor,
        _supervisor_payload(
            started_at=started_wall,
            started_monotonic=started_monotonic,
            status="starting",
            stdout=stdout,
            stderr=stderr,
            process=None,
        ),
    )
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "analytical-worker" if args.command == "analytical" else "migrate-worker",
        "--source",
        str(args.source),
        "--destination",
        str(args.destination),
        "--inventory",
        str(args.inventory),
        "--report",
        str(report),
        "--page-limit",
        str(args.page_limit),
        "--reserve-bytes",
        str(args.reserve_bytes),
    ]
    if args.reconcile_source:
        command.append("--reconcile-source")
    child_pid: int | None = None
    process: dict[str, object] | None = None
    interrupted = False
    with stdout.open("x", encoding="utf-8") as out, stderr.open("x", encoding="utf-8") as err:
        child = subprocess.Popen(
            command,
            stdout=out,
            stderr=err,
            env=_child_environment(),
        )
        child_pid = child.pid
        process = _process_identity(child_pid)
        _replace_json(
            supervisor,
            _supervisor_payload(
                started_at=started_wall,
                started_monotonic=started_monotonic,
                status="running",
                stdout=stdout,
                stderr=stderr,
                process=process,
                progress=_safe_progress(args.destination.resolve(strict=False)),
            ),
        )
        try:
            while True:
                try:
                    code = child.wait(timeout=5)
                    break
                except subprocess.TimeoutExpired:
                    _replace_json(
                        supervisor,
                        _supervisor_payload(
                            started_at=started_wall,
                            started_monotonic=started_monotonic,
                            status="running",
                            stdout=stdout,
                            stderr=stderr,
                            process=process,
                            progress=_safe_progress(args.destination.resolve(strict=False)),
                        ),
                    )
        except KeyboardInterrupt:
            interrupted = True
            child.send_signal(signal.SIGINT)
            code = child.wait()
    if code == 0 and report.is_file():
        status = "completed"
    elif interrupted:
        status = "interrupted"
    else:
        status = "error"
    _replace_json(
        supervisor,
        _supervisor_payload(
            started_at=started_wall,
            started_monotonic=started_monotonic,
            status=status,
            stdout=stdout,
            stderr=stderr,
            process=process,
            progress=_safe_progress(args.destination.resolve(strict=False)),
            returncode=code,
            receipt_present=report.is_file(),
        ),
    )
    return 0 if status == "completed" else 130 if status == "interrupted" else 2


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "snapshot":
            service = WorkspaceService()
            receipt = create_v1_migration_source_snapshot(
                service=service,
                source_root=args.source.resolve(strict=False),
                snapshot_root=args.destination.resolve(strict=False),
                host_free_bytes=_host_free_bytes,
                reserve_bytes=args.reserve_bytes,
            )
            _new_json(args.report.resolve(strict=False), receipt.to_json_dict())
            return 0
        if args.command in {"migrate", "analytical"}:
            return _supervise(args)
        if args.command == "migrate-worker":
            return _worker(args)
        if args.command == "analytical-worker":
            return _analytical_worker(args)
        if args.command == "inventory":
            write_inventory(
                args.source.resolve(strict=False),
                args.report.resolve(strict=False),
            )
            return 0
        if args.command == "budget":
            destination = args.destination.resolve(strict=False)
            snapshot = args.source_snapshot.resolve(strict=False)
            target_measurement = _measure_physical_root(destination)
            snapshot_measurement = _measure_physical_root(snapshot)
            host = _capacity_probe()
            reserve = 5_000_000_000
            _new_json(
                args.report.resolve(strict=False),
                {
                    "schema_version": "workspace-v2-migration-capacity-budget-v2",
                    "as_of": datetime.now(UTC).isoformat(),
                    "destination": str(destination),
                    "source_snapshot": str(snapshot),
                    "host_capacity": host,
                    "measurements": {
                        "destination": target_measurement,
                        "source_snapshot": snapshot_measurement,
                    },
                    "reserve_bytes": reserve,
                    "historical_intermediate_bytes": 0,
                    "historical_intermediate_status": "NOT_PLANNED_DIRECT_TRANSPORT",
                    "phases": {
                        "direct_transport": {
                            **_phase_budget(
                                host=host,
                                existing_bytes=target_measurement["allocated_bytes"],
                                new_write_bytes=None,
                                auxiliary_bytes=None,
                                evidence_bytes=None,
                                reserve_bytes=reserve,
                            ),
                            "reason": (
                                "remaining pages, WAL/spill and evidence are measured "
                                "during transport"
                            ),
                        },
                        "backup": {
                            **_phase_budget(
                                host=host,
                                existing_bytes=target_measurement["allocated_bytes"],
                                new_write_bytes=None,
                                auxiliary_bytes=None,
                                evidence_bytes=None,
                                reserve_bytes=reserve,
                            ),
                            "reason": (
                                "packed backup bytes remain UNKNOWN until closed volumes "
                                "are measured"
                            ),
                        },
                        "serial_restore": {
                            **_phase_budget(
                                host=host,
                                existing_bytes=0,
                                new_write_bytes=None,
                                auxiliary_bytes=None,
                                evidence_bytes=None,
                                reserve_bytes=reserve,
                            ),
                            "reason": (
                                "serial restore counts only new restore units and active "
                                "buffers after backup"
                            ),
                        },
                    },
                },
            )
            return 0
        service = WorkspaceService()
        if args.command == "status":
            paths = service.resolve(args.destination.resolve(strict=False))
            roots = (paths.state_root, paths.storage_root / "v2")
            checkpoints = tuple(
                sorted(
                    path.relative_to(paths.root).as_posix()
                    for root in roots
                    for path in root.glob("*-import-state*.json")
                    if path.is_file() and not path.is_symlink()
                )
            )
            transition = paths.state_root / "workspace-v2-source-transition.json"
            if transition.is_file() and not transition.is_symlink():
                transition_name = transition.relative_to(paths.root).as_posix()
                checkpoints = tuple(sorted((*checkpoints, transition_name)))
            direct_state = paths.state_root / "direct-historical-import-state.json"
            completion = "UNKNOWN"
            complete = False
            if direct_state.is_file() and not direct_state.is_symlink():
                try:
                    document = json.loads(direct_state.read_text(encoding="utf-8"))
                except (OSError, UnicodeError, ValueError):
                    completion = "UNKNOWN"
                else:
                    if isinstance(document, dict) and document.get("phase") == "complete":
                        complete = True
                        completion = "CHECKPOINT_COMPLETE"
                    elif isinstance(document, dict):
                        completion = "CHECKPOINT_IN_PROGRESS"
            _new_json(
                args.report.resolve(strict=False),
                {
                    "schema_version": "workspace-v2-migration-status-v1",
                    "destination": str(paths.root),
                    "checkpoint_files": checkpoints,
                    "complete": complete,
                    "completion_status": completion,
                },
            )
            return 0
        source = args.source.resolve(strict=False)
        if args.command == "verify":
            inventory = args.inventory.resolve(strict=False)
            if (
                not args.inventory.is_absolute()
                or inventory != source / "backup_manifest.json"
                or inventory.is_symlink()
                or not inventory.is_file()
            ):
                raise WorkspaceV2MigrationError("verification inventory is missing or unsafe")
            verified_snapshot = verify_v1_migration_source_snapshot(source)
            snapshot = load_v1_migration_source_snapshot(service=service, snapshot_root=source)
            if snapshot != verified_snapshot:
                raise WorkspaceV2MigrationError("source snapshot descriptor changed")
            destination_paths = service.resolve(args.destination.resolve(strict=False))
            destination = service.inspect(destination_paths.root)
            if destination.format_version != 2 or destination.errors:
                raise WorkspaceV2MigrationError("verification target must be workspace format v2")
            source_inspection = service.inspect(source)
            if (
                source_inspection.raw_record_count != destination.raw_record_count
                or source_inspection.observation_count != destination.observation_count
            ):
                raise WorkspaceV2MigrationError(
                    "raw or observation counts differ from source snapshot"
                )
            transition = service.v2_source_transition(destination_paths)
            if (
                transition is None
                or transition.phase is not WorkspaceV2MigrationPhase.COMPLETE
                or transition.snapshot_id != snapshot.snapshot_id
                or transition.snapshot_source_fingerprint != snapshot.source_fingerprint
                or transition.destination_workspace_id != destination.workspace_id
                or transition.confirmed_equivalent_count != source_inspection.raw_record_count
                or transition.unconfirmed_equivalent_count != 0
                or transition.missing_count != 0
                or transition.conflicting_count != 0
            ):
                raise WorkspaceV2MigrationError("raw transition proof is incomplete")
            state = _load_direct_analytical_state(
                destination_paths.state_root / "direct-historical-import-state.json"
            )
            if (
                state.source_workspace_id != str(snapshot.source_workspace_id)
                or state.source_fingerprint != snapshot.source_fingerprint
            ):
                raise WorkspaceV2MigrationError("analytical checkpoint does not bind B")
            metrics_checkpoint = _verify_pairwise_checkpoint(
                path=(
                    destination_paths.state_root
                    / "direct-historical-import-v1-metrics-verification-work.json"
                ),
                state=state,
                destination_root=destination_paths.root,
                destination_workspace_id=uuid5(NAMESPACE_URL, str(destination_paths.root)),
                family="metrics",
                count=state.metric_count,
                digest=state.metric_digest,
            )
            diagnostics_checkpoint = _verify_pairwise_checkpoint(
                path=(
                    destination_paths.state_root
                    / "direct-historical-import-v1-diagnostics-verification-work.json"
                ),
                state=state,
                destination_root=destination_paths.root,
                destination_workspace_id=uuid5(NAMESPACE_URL, str(destination_paths.root)),
                family="diagnostics",
                count=state.diagnostic_count,
                digest=state.diagnostic_digest,
            )
            with service.open_storage(destination_paths, WorkspaceAccessMode.READ_ONLY) as storage:
                if not isinstance(storage.store, WorkspaceV2Store):
                    raise WorkspaceV2MigrationError("verification target did not open workspace v2")
                analytical = CompactAnalyticalStore(storage.store.connection)
                seal = analytical.historical_seal()
                analytical.verify_historical_relationships()
                if (
                    seal is None
                    or seal.source_fingerprint != state.source_fingerprint
                    or seal.metric_count != state.metric_count
                    or seal.metric_digest != state.metric_digest
                    or seal.diagnostic_count != state.diagnostic_count
                    or seal.diagnostic_digest != state.diagnostic_digest
                    or analytical.count_metrics(origin="HISTORICAL") != state.metric_count
                    or analytical.count_diagnostics(origin="HISTORICAL") != state.diagnostic_count
                ):
                    raise WorkspaceV2MigrationError("historical analytical seal does not match")
            _new_json(
                args.report.resolve(strict=False),
                {
                    "schema_version": "workspace-v2-migration-verify-v2",
                    "source_snapshot": snapshot.to_json_dict(),
                    "destination": destination.to_json_dict(),
                    "complete": True,
                    "reused_pairwise_comparison": True,
                    "raw_observation_equivalence": {
                        "raw_records": source_inspection.raw_record_count,
                        "observations": source_inspection.observation_count,
                        "transition_confirmed": transition.confirmed_equivalent_count,
                    },
                    "analytical_equivalence": {
                        "metric_count": state.metric_count,
                        "metric_digest": state.metric_digest,
                        "diagnostic_count": state.diagnostic_count,
                        "diagnostic_digest": state.diagnostic_digest,
                        "metrics_checkpoint": metrics_checkpoint.to_json_dict(),
                        "diagnostics_checkpoint": diagnostics_checkpoint.to_json_dict(),
                        "sealed_at": seal.sealed_at.isoformat(),
                    },
                },
            )
            return 0
        inspection = service.inspect(source)
        payload: dict[str, object] = {
            "schema_version": "workspace-v2-migration-inventory-v1",
            "source_fingerprint": source_fingerprint(service, source),
            "inspection": inspection.to_json_dict(),
        }
        _new_json(args.report.resolve(strict=False), payload)
        return 0
    except (
        OSError,
        ValueError,
        subprocess.SubprocessError,
        WorkspaceError,
        WorkspaceV2MigrationError,
    ) as error:
        print(
            f"workspace-v2-migration: {error.__class__.__name__}: {_safe_error_detail(error)}",
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
