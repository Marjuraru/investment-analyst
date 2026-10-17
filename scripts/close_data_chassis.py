#!/usr/bin/env python3
"""Explicit DATA-CHASSIS maintenance over existing typed services and receipts."""

from __future__ import annotations

import argparse
import cProfile
import errno
import hashlib
import inspect
import json
import os
import platform
import resource
import stat
import subprocess
import sys
import threading
import time
import traceback
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import duckdb
from migrate_workspace_v2 import _host_free_bytes

from investment_analyst.application.local_release import LocalReleaseService, ReleaseManifest
from investment_analyst.storage.resource_policy import (
    MaintenanceExecution,
    MaintenanceResourceBudget,
    MaintenanceResourceReceipt,
    MaintenanceResourceSample,
    ResourcePolicyError,
    maintenance_execution,
    maintenance_resource_budget,
    resource_stop_reason,
    sample_cgroup_resources,
)
from investment_analyst.storage.workspace_v2_inventory import (
    WorkspaceV2GenerationLedger,
    WorkspaceV2ProgressReconciler,
)
from investment_analyst.workspace.drive_transport import DriveTransport, DriveTransportConfig
from investment_analyst.workspace.models import WorkspaceAccessMode
from investment_analyst.workspace.service import WorkspaceService
from investment_analyst.workspace.workspace_v2_acceptance import (
    CompatibleRecoveryReadback,
    FinalAcceptanceInput,
    build_final_acceptance,
)
from investment_analyst.workspace.workspace_v2_cutover import (
    WorkspaceV2CutoverCheckpoint,
    WorkspaceV2CutoverPhase,
    WorkspaceV2CutoverService,
    WorkspaceV2CutWindowService,
)
from investment_analyst.workspace.workspace_v2_lifecycle import (
    WorkspaceV2LifecycleService,
    _replace_json,
    _write_new_json,
)
from investment_analyst.workspace.workspace_v2_lifecycle_models import (
    FrozenSourceCut,
    LifecycleCheckpoint,
    LifecycleInventory,
    LifecyclePhase,
)
from investment_analyst.workspace.workspace_v2_migration import (
    WorkspaceV2MigrationError,
    _RecoveredCoreBaseline,
    quiescent_source_cut,
    source_fingerprint,
    verify_recovered_core_baseline,
)
from investment_analyst.workspace.workspace_v2_migration_models import (
    ProgressReconciliation,
    ProgressReconciliationV2,
)
from investment_analyst.workspace.workspace_v2_operational_state import compare_operational_models
from investment_analyst.workspace.workspace_v2_physical_delta import (
    PhysicalDeltaManifest,
    PhysicalDeltaReceipt,
    WorkspaceV2PhysicalDeltaService,
    _iter_delta_files,
    _verify_full_recovery_coverage,
    verify_physical_delta_index,
)

INTEGRATED_RELEASE = "dbbdfd021b0fee20be31b2d2a845874b0e861067"
PHASES = (
    "preclean",
    "prepare",
    "catchup",
    "delta",
    "activate",
    "report",
    "retire-final",
)


class MaintenancePhaseError(ValueError):
    """One controlled reason code; never interpolate upstream error payloads."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _absolute(value: str) -> Path:
    path = Path(value)
    if not path.is_absolute() or any(item.is_symlink() for item in (path, *path.parents)):
        raise argparse.ArgumentTypeError("path must be absolute without symlink components")
    return path


def _sha(value: str) -> str:
    if len(value) != 40 or any(item not in "0123456789abcdef" for item in value):
        raise argparse.ArgumentTypeError("code-sha must be a full commit SHA")
    return value


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "phase",
        choices=(
            "status",
            *PHASES,
            "resource-check",
            "recover-progress",
            "verify-relationships",
            "verify-financial-baseline",
            "verify-core-baseline",
            "prepare-parent-reuse",
            "prepare-delta",
            "recovery-readback",
            "profile-relationships",
        ),
    )
    parser.add_argument("--source", type=_absolute, required=True)
    parser.add_argument("--destination", type=_absolute, required=True)
    parser.add_argument("--controls-root", type=_absolute, required=True)
    parser.add_argument("--operation-id", type=UUID, required=True)
    parser.add_argument("--code-sha", type=_sha, required=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--parent-index", type=_absolute)
    parser.add_argument("--parent-manifest", type=_absolute)
    parser.add_argument("--parent-manifest-sha256")
    parser.add_argument("--delta-root", type=_absolute)
    parser.add_argument("--delta-action", choices=("plan", "upload", "restore"), default="plan")
    parser.add_argument("--recovery-root", type=_absolute)
    parser.add_argument("--reuse-parent-immutables", action="store_true")
    parser.add_argument("--immutable-parent-proof", type=_absolute)
    parser.add_argument("--transport-config", type=_absolute)
    parser.add_argument("--independent-read-root", type=_absolute)
    parser.add_argument("--recovery-receipt", type=_absolute)
    parser.add_argument("--cut-window", type=_absolute)
    parser.add_argument("--acceptance-input", type=_absolute)
    parser.add_argument("--lifecycle-inventory", type=_absolute)
    parser.add_argument("--complement-root", type=_absolute)
    parser.add_argument(
        "--preclean-kind",
        choices=("recovery_slot", "packed_backup", "restore_staging"),
        default="recovery_slot",
    )
    parser.add_argument("--retire-root", type=_absolute)
    parser.add_argument("--retained-controls", type=_absolute)
    parser.add_argument("--remote-restore-receipt", type=_absolute)
    parser.add_argument("--output", type=_absolute)
    parser.add_argument("--resource-budget", type=_absolute)
    parser.add_argument("--predecessor-transition", type=_absolute)
    parser.add_argument("--predecessor-proof", type=_absolute, action="append")
    parser.add_argument("--predecessor-code", type=_absolute)
    parser.add_argument("--relationships-proof", type=_absolute)
    parser.add_argument("--reconciliation-id", type=UUID)
    parser.add_argument("--baseline-code-root", type=_absolute)
    parser.add_argument("--core-baseline-path", type=_absolute)
    parser.add_argument("--profile-financial-reader", action="store_true")
    parser.add_argument("--supervised-worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--resource-run-root", type=_absolute, help=argparse.SUPPRESS)
    return parser


MAINTENANCE_UNIT = "investment-analyst-dc45-cut-f.service"


def _memory_composition(cgroup: Path) -> dict[str, int] | None:
    """Read diagnostic counters only; never infer missing memory as zero."""
    fields = {
        "anon",
        "file",
        "kernel",
        "file_mapped",
        "inactive_file",
        "active_file",
        "slab",
        "pagetables",
    }
    try:
        values = {
            parts[0]: int(parts[1])
            for line in (cgroup / "memory.stat").read_text().splitlines()
            if len(parts := line.split()) == 2 and parts[0] in fields
        }
    except (OSError, ValueError):
        return None
    if not {"anon", "file"}.issubset(values) or any(value < 0 for value in values.values()):
        return None
    return values


class _PhysicalCacheReclaimer:
    """Bound file/slab cache in this worker's cgroup without changing limits.

    Kernel reclaim can block, so it runs in one daemon thread while the regular
    heartbeat and parent supervisor remain responsive. It never targets an
    ancestor, v1, another service or the machine-wide cache.
    """

    def __init__(self, receipt: MaintenanceResourceReceipt, *, enabled: bool) -> None:
        self.receipt = receipt
        self.enabled = enabled
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._monitor: threading.Thread | None = None
        self._stop = threading.Event()
        self._started = 0.0
        self._finished = 0.0
        self._requests = 0
        self._completed = 0
        self._short = False
        self._before: int | None = None
        self._after: int | None = None
        self._error: str | None = None
        self._history: list[dict[str, object]] = []

    def start(self) -> None:
        if not self.enabled:
            return

        def monitor() -> None:
            while not self._stop.wait(0.05):
                self.poll(_memory_composition(self.receipt.cgroup_path))

        self._monitor = threading.Thread(target=monitor, daemon=True)
        self._monitor.start()

    def close(self) -> None:
        self._stop.set()
        if self._monitor is not None:
            self._monitor.join(timeout=0.1)
        if self._thread is not None:
            self._thread.join(timeout=0.1)

    def _reclaim(self) -> None:
        error = None
        short = False
        cgroup = self.receipt.cgroup_path
        try:
            identity = _process_identity(os.getpid())
            if (
                os.getpid() != self.receipt.pid
                or identity != (self.receipt.process_start_ticks, self.receipt.boot_id, cgroup)
                or cgroup.name != MAINTENANCE_UNIT
            ):
                raise MaintenancePhaseError("cache_reclaim_owner_mismatch")
            path = cgroup / "memory.reclaim"
            if path.is_symlink():
                raise MaintenancePhaseError("cache_reclaim_controller_unsafe")
            try:
                path.write_text(str(512 * 1024 * 1024))
            except OSError as failure:
                if failure.errno != errno.EAGAIN:
                    raise
                # The kernel documents EAGAIN as a short reclaim. It is useful
                # only if measured memory actually fell; never retry blindly.
                short = True
            after = int((cgroup / "memory.current").read_text())
            if short and self._before is not None and after >= self._before:
                error = "cache_reclaim_no_progress"
        except MaintenancePhaseError as failure:
            error, after = failure.code, None
        except (OSError, ValueError):
            error, after = "cache_reclaim_controller_unavailable", None
        with self._lock:
            self._error, self._after, self._short = self._error or error, after, short
            self._completed += 1
            self._finished = time.monotonic()
            self._history.append(
                {
                    "memory_before": self._before,
                    "memory_after": after,
                    "elapsed_seconds": self._finished - self._started,
                    "short_reclaim": short,
                    "error": self._error,
                }
            )
            self._history = self._history[-16:]

    def poll(self, memory: dict[str, int] | None) -> dict[str, object]:
        with self._lock:
            active = self._thread is not None and self._thread.is_alive()
            now = time.monotonic()
            if active and now - self._started > 2:
                self._error = "cache_reclaim_timeout"
            if self.enabled and not self._stop.is_set() and not active and self._error is None:
                try:
                    current = int((self.receipt.cgroup_path / "memory.current").read_text())
                except (OSError, ValueError):
                    current = 0
                    self._error = "cache_reclaim_measurement_unavailable"
                if (
                    self._error is None
                    and memory is not None
                    and memory.get("file", 0) >= 256 * 1024 * 1024
                    and current >= self.receipt.budget.memory_high_bytes - 768 * 1024 * 1024
                    and current < self.receipt.budget.memory_high_bytes
                ):
                    self._before, self._after = current, None
                    self._started = now
                    self._requests += 1
                    self._thread = threading.Thread(target=self._reclaim, daemon=True)
                    self._thread.start()
                    active = True
            return {
                "enabled": self.enabled,
                "requests": self._requests,
                "completed": self._completed,
                "active": active,
                "short_reclaim": self._short,
                "error": self._error,
                "memory_before": self._before,
                "memory_after": self._after,
                "recent_requests": list(self._history),
            }


def _load_resource_budget(args: argparse.Namespace) -> MaintenanceResourceBudget:
    path = _required(args.resource_budget, "resource_budget")
    budget = MaintenanceResourceBudget.model_validate_json(path.read_bytes())
    if budget.operation_id != args.operation_id:
        raise MaintenancePhaseError("resource_operation_mismatch")
    temporary_root = args.controls_root / "resource-temporaries" / str(args.operation_id)
    for allocation in budget.assignments + (
        () if budget.reader_worker is None else (budget.reader_worker,)
    ):
        directory = allocation.policy.temp_directory
        if directory is None or not directory.is_relative_to(temporary_root):
            raise MaintenancePhaseError("resource_temporary_boundary_mismatch")
        if not (
            allocation.database.is_relative_to(args.source)
            or allocation.database.is_relative_to(args.destination)
            or (
                args.recovery_root is not None
                and allocation.database.is_relative_to(args.recovery_root)
            )
        ):
            raise MaintenancePhaseError("resource_database_boundary_mismatch")
    return budget


def _unit_properties() -> dict[str, str]:
    completed = subprocess.run(
        ["systemctl", "--user", "show", MAINTENANCE_UNIT, "--property=ActiveState,MainPID,Result"],
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )
    if completed.returncode != 0:
        raise MaintenancePhaseError("maintenance_unit_state_unavailable")
    return dict(line.split("=", 1) for line in completed.stdout.splitlines() if "=" in line)


def _process_identity(pid: int) -> tuple[int, UUID, Path]:
    process = Path("/proc") / str(pid)
    start_ticks = int((process / "stat").read_text().rsplit(") ", 1)[1].split()[19])
    boot_id = UUID(Path("/proc/sys/kernel/random/boot_id").read_text().strip())
    lines = (process / "cgroup").read_text().splitlines()
    unified = [line[3:] for line in lines if line.startswith("0::")]
    if len(unified) != 1 or ".." in Path(unified[0]).parts:
        raise MaintenancePhaseError("maintenance_cgroup_unavailable")
    return start_ticks, boot_id, Path("/sys/fs/cgroup") / unified[0].lstrip("/")


def _resource_receipt(
    budget: MaintenanceResourceBudget,
    pid: int,
    sample: MaintenanceResourceSample,
    *,
    reason: str | None = None,
) -> MaintenanceResourceReceipt:
    start_ticks, boot_id, cgroup = _process_identity(pid)
    return MaintenanceResourceReceipt(
        budget=budget,
        pid=pid,
        process_start_ticks=start_ticks,
        boot_id=boot_id,
        cgroup_path=cgroup,
        resource_policy_sha256=hashlib.sha256(
            Path(inspect.getfile(MaintenanceResourceBudget)).read_bytes()
        ).hexdigest(),
        coordinator_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        python_version=platform.python_version(),
        duckdb_version=duckdb.__version__,
        stage="admitted" if reason is None else "stopped",
        observed_memory_high_bytes=sample.memory_high_bytes,
        observed_memory_max_bytes=sample.memory_max_bytes,
        observed_swap_max_bytes=sample.swap_max_bytes,
        host_free_bytes=sample.host_free_bytes,
        effective_settings=(),
        stop_reason=reason,
        recorded_at=datetime.now(UTC),
    )


@contextmanager
def _worker_resources(args: argparse.Namespace) -> Iterator[None]:
    budget = _load_resource_budget(args)
    run = _required(args.resource_run_root, "resource_run_root")
    if not run.is_relative_to(args.controls_root):
        raise MaintenancePhaseError("resource_run_boundary_mismatch")
    pid = os.getpid()
    properties = _unit_properties()
    if properties.get("ActiveState") != "active" or properties.get("MainPID") != str(pid):
        raise MaintenancePhaseError("worker_is_not_owned_maintenance_unit")
    _, _, cgroup = _process_identity(pid)
    # This admission precedes any service/backend construction or WAL replay.
    sample = sample_cgroup_resources(
        budget, cgroup, host_free_bytes=_capacity(), log_bytes=0, heartbeat_age_seconds=0
    )
    reason = resource_stop_reason(budget, sample)
    receipt = _resource_receipt(budget, pid, sample, reason=reason)
    _write_new_json(run / "admission.json", receipt.model_dump(mode="json"))
    if reason is not None:
        raise MaintenancePhaseError(reason)
    execution: MaintenanceExecution
    cache = _PhysicalCacheReclaimer(
        receipt, enabled=args.phase in {"prepare-delta", "delta", "catchup"}
    )

    def observe(phase: str, step_started: float, deadline: int) -> None:
        stop = run / "stop-request.json"
        if stop.exists():
            execution.cancel("supervisor_stop_requested")
        memory = _memory_composition(cgroup)
        _replace_json(
            run / "heartbeat.json",
            {
                "operation_id": str(args.operation_id),
                "pid": pid,
                "process_start_ticks": receipt.process_start_ticks,
                "boot_id": str(receipt.boot_id),
                "phase": phase,
                "step_started_monotonic": step_started,
                "deadline_seconds": deadline,
                "heartbeat_monotonic": time.monotonic(),
                "effective_native_settings": execution.settings_snapshot(),
                "active_query_sha256": execution.active_query_sha256,
                "memory_stat": memory,
                "physical_cache_reclaim": cache.poll(memory),
            },
        )

    execution = MaintenanceExecution(budget, observe)
    cache.start()
    try:
        with maintenance_resource_budget(budget), maintenance_execution(execution):
            yield
    finally:
        cache.close()
        _replace_json(
            run / "native-settings.json",
            {
                "operation_id": str(args.operation_id),
                "settings_observed": execution.settings_snapshot(),
                "cancel_reason": execution.cancel_reason,
                "query_profile_columns": ["executions", "total_seconds", "max_seconds"],
                "query_profile_by_sql_sha256": execution.query_profile(),
                "completed_measurements": execution.measurements(),
                "query_plans": execution.query_plans(),
                "physical_cache_reclaim": cache.poll(None),
            },
        )


def _stop_owned_worker(run: Path, reason: str, grace: int) -> None:
    _replace_json(
        run / "stop-request.json", {"reason": reason, "at": datetime.now(UTC).isoformat()}
    )
    # Cancellation and systemd termination share one finite stop budget.
    interrupt_grace = min(5, max(0, grace - 1))
    deadline = time.monotonic() + interrupt_grace
    while time.monotonic() < deadline:
        properties = _unit_properties()
        if properties.get("MainPID", "0") == "0":
            return
        time.sleep(0.25)
    # Only the explicitly named transient worker and its descendants; never v1/WSL.
    subprocess.run(
        ["systemctl", "--user", "stop", MAINTENANCE_UNIT],
        check=True,
        capture_output=True,
        timeout=grace - interrupt_grace + 1,
    )


def _supervise(args: argparse.Namespace, argv: list[str]) -> dict[str, object]:
    budget = _load_resource_budget(args)
    properties = _unit_properties()
    if properties.get("MainPID", "0") != "0" or properties.get("ActiveState") == "active":
        raise MaintenancePhaseError("maintenance_unit_already_in_use")
    initial_capacity = _capacity()
    if initial_capacity < budget.required_host_bytes:
        raise MaintenancePhaseError("next_unit_host_reserve_unavailable")
    run = args.controls_root / ("resource-run-" + uuid4().hex)
    run.mkdir()
    output = args.output or run / "phase-result.json"
    if output.exists():
        raise MaintenancePhaseError("phase_output_already_exists")
    worker_args = [*argv, "--supervised-worker", "--resource-run-root", str(run)]
    if args.output is None:
        worker_args.extend(["--output", str(output)])
    script = Path(__file__).resolve()
    environment = str(script.parent.parent / "src") + ":" + str(script.parent)
    unit_stop_timeout = budget.stop_grace_seconds - min(5, max(0, budget.stop_grace_seconds - 1))
    command = [
        "systemd-run",
        "--user",
        "--unit=" + MAINTENANCE_UNIT,
        "--collect",
        f"--property=MemoryHigh={budget.memory_high_bytes}",
        f"--property=MemoryMax={budget.memory_max_bytes}",
        "--property=MemorySwapMax=0",
        "--property=KillMode=control-group",
        f"--property=TimeoutStopSec={unit_stop_timeout}",
        "--property=Restart=no",
        "--property=WorkingDirectory=" + str(script.parent.parent),
        "--property=StandardOutput=append:" + str(run / "worker.log"),
        "--property=StandardError=append:" + str(run / "worker.log"),
        "--setenv=PYTHONPATH=" + environment,
    ]
    # Preserve the bridge and executable search path without persisting/printing secrets.
    if "WSL_INTEROP" in os.environ:
        command.append("--setenv=WSL_INTEROP=" + os.environ["WSL_INTEROP"])
    command.append("--setenv=PATH=" + os.environ.get("PATH", "/usr/bin:/bin"))
    command.extend([sys.executable, str(script), *worker_args])
    if args.phase in {"catchup", "activate"}:
        command.insert(3, "--property=RuntimeMaxSec=1800")
    subprocess.run(command, check=True, capture_output=True, timeout=10)
    stop_sampling = threading.Event()
    host_lock = threading.Lock()
    host_value: int | None = initial_capacity
    host_measured = time.monotonic()

    def host_probe() -> None:
        nonlocal host_value, host_measured
        while not stop_sampling.is_set():
            try:
                value: int | None = _capacity()
            except Exception:
                value = None
            with host_lock:
                host_value, host_measured = value, time.monotonic()
            stop_sampling.wait(1)

    thread = threading.Thread(target=host_probe, daemon=True)
    thread.start()
    initial_events = None
    reason: str | None = None
    started = time.monotonic()
    try:
        while True:
            properties = _unit_properties()
            if properties.get("MainPID", "0") == "0":
                if not output.is_file():
                    failure = run / "worker-failure.json"
                    if failure.is_file():
                        raise MaintenancePhaseError(json.loads(failure.read_text())["reason"])
                    raise MaintenancePhaseError("worker_exited_without_terminal_receipt")
                return json.loads(output.read_text())
            pid = int(properties["MainPID"])
            start_ticks, boot_id, cgroup = _process_identity(pid)
            heartbeat_path = run / "heartbeat.json"
            heartbeat = json.loads(heartbeat_path.read_text()) if heartbeat_path.exists() else None
            if heartbeat is not None and heartbeat.get("phase") not in {"ready", "open", "sql"}:
                raise MaintenancePhaseError("worker_heartbeat_phase_invalid")
            reclaim = heartbeat.get("physical_cache_reclaim") if heartbeat is not None else None
            if reclaim is not None and (
                not isinstance(reclaim, dict) or reclaim.get("error") is not None
            ):
                reason = "physical_cache_reclaim_failed"
            if heartbeat is None and time.monotonic() - started <= 5:
                time.sleep(0.1)
                continue
            if (
                heartbeat is None
                or heartbeat.get("pid") != pid
                or heartbeat.get("operation_id") != str(args.operation_id)
                or heartbeat.get("process_start_ticks") != start_ticks
                or heartbeat.get("boot_id") != str(boot_id)
            ):
                reason = "worker_heartbeat_binding_missing"
            with host_lock:
                capacity = host_value if time.monotonic() - host_measured <= 15 else None
            age = int(time.monotonic() - heartbeat["heartbeat_monotonic"]) if heartbeat else 6
            log_path = run / "worker.log"
            sample = sample_cgroup_resources(
                budget,
                cgroup,
                host_free_bytes=capacity,
                log_bytes=log_path.stat().st_size if log_path.exists() else 0,
                heartbeat_age_seconds=age,
            )
            initial_events = sample.events if initial_events is None else initial_events
            reason = reason or resource_stop_reason(budget, sample, initial_events=initial_events)
            if (
                heartbeat
                and heartbeat["deadline_seconds"]
                and time.monotonic() - heartbeat["step_started_monotonic"]
                > heartbeat["deadline_seconds"]
            ):
                reason = heartbeat["phase"] + "_timeout"
            _replace_json(run / "last-sample.json", sample.model_dump(mode="json"))
            if reason:
                _write_new_json(
                    run / "stop.json", {"reason": reason, "at": datetime.now(UTC).isoformat()}
                )
                _stop_owned_worker(run, reason, budget.stop_grace_seconds)
                raise MaintenancePhaseError(reason)
            time.sleep(1)
    except BaseException:
        if _unit_properties().get("MainPID", "0") != "0":
            _stop_owned_worker(run, reason or "supervisor_interrupted", budget.stop_grace_seconds)
        raise
    finally:
        stop_sampling.set()
        thread.join(timeout=0.1)


def _required(path: Path | None, name: str) -> Path:
    if path is None:
        raise MaintenancePhaseError(f"missing_{name}")
    return path


def _process_io() -> dict[str, int] | None:
    """Read Linux syscall byte counters; these exclude subprocesses and remote verification."""
    try:
        values = dict(line.split(":", 1) for line in Path("/proc/self/io").read_text().splitlines())
        return {key: int(values[key]) for key in ("rchar", "wchar", "read_bytes", "write_bytes")}
    except (OSError, ValueError, KeyError):
        return None


def _io_measurement(
    before: dict[str, int] | None,
    after: dict[str, int] | None,
    args: argparse.Namespace,
    result: dict[str, object],
) -> dict[str, object]:
    counters = (
        {key: max(0, after[key] - before[key]) for key in before}
        if before is not None and after is not None
        else None
    )
    sent: int | None = None
    reused: int | None = None
    transfers = getattr(args, "transfer_counters", None)
    if transfers is not None:
        payload, controls = transfers["upload_payload"], transfers["upload_controls"]
        if payload is not None and controls is not None:
            sent = payload + controls
    if args.phase == "delta":
        if args.delta_action == "plan":
            reused = int(result["reused_bytes"])
            sent = 0
        elif args.delta_action == "restore":
            sent = 0
            reused = int(result["restore"]["parent_linked_bytes"])
    return {
        "bytes_read": counters["rchar"] if counters is not None else None,
        "bytes_written": counters["wchar"] if counters is not None else None,
        "bytes_sent": sent,
        "bytes_reused": reused,
        "io_scope": "process_syscalls_including_controls_excluding_children",
        "io_counters": counters,
        "transfer_scope": (
            "rclone_accounting_bytes_including_retries; payload_and_controls_separate"
        ),
        "transfer_counters": transfers,
        "bytes_reused_scope": "parent_references_for_plan; hardlinked_parent_bytes_for_restore",
    }


def _cut(args: argparse.Namespace) -> WorkspaceV2CutoverCheckpoint:
    cut = WorkspaceV2CutoverCheckpoint.model_validate_json(
        (args.controls_root / "cutover-checkpoint.json").read_bytes()
    )
    if (
        cut.operation_id != args.operation_id
        or cut.destination_root != args.destination
        or cut.source.source_root != args.source
    ):
        raise MaintenancePhaseError("cutover_binding_mismatch")
    return cut


def _capacity() -> int:
    inherited = os.environ.get("WSL_INTEROP")
    init_socket = _wsl_init_socket()
    repair = bool(inherited and not Path(inherited).exists() and init_socket is not None)
    attempts: list[dict[str, object]] = []
    try:
        if repair:
            os.environ["WSL_INTEROP"] = str(init_socket)
        try:
            value = _host_free_bytes(attempts.append)
        except WorkspaceV2MigrationError:
            # One bounded failover only for the diagnosed WSL socket failure.
            # Every successful return still comes from a fresh host API call.
            if not attempts or any(
                attempt.get("diagnostic") != "transient_wsl_vsock" for attempt in attempts
            ):
                raise
            fresh = _wsl_init_socket()
            if fresh is None:
                raise
            os.environ["WSL_INTEROP"] = str(fresh)
            repair = True
            value = _fresh_host_capacity()
    finally:
        if repair:
            if inherited is None:
                os.environ.pop("WSL_INTEROP", None)
            else:
                os.environ["WSL_INTEROP"] = inherited
    if value < 25_000_000_000:
        raise MaintenancePhaseError("host_capacity_below_floor")
    return value


def _fresh_host_capacity() -> int:
    """Call the existing API reader after a clean Linux exec, outside DuckDB's native state."""
    scripts = Path(__file__).resolve().parent
    try:
        completed = subprocess.run(
            [
                sys.executable,
                "-c",
                "import sys; sys.path.insert(0, sys.argv[1]); "
                "from migrate_workspace_v2 import _host_free_bytes; print(_host_free_bytes())",
                str(scripts),
            ],
            env={**os.environ, "PYTHONPATH": str(scripts.parent / "src")},
            capture_output=True,
            text=True,
            check=False,
            timeout=35,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise WorkspaceV2MigrationError("fresh host API reader failed") from error
    if completed.returncode != 0 or not completed.stdout.strip().isdecimal():
        raise WorkspaceV2MigrationError("fresh host API reader failed")
    return int(completed.stdout.strip())


def _wsl_init_socket() -> Path | None:
    """Recover only the root-owned persistent WSL bridge, never substitute df for C:."""
    path = Path("/run/WSL/1_interop")
    try:
        metadata = path.lstat()
        if metadata.st_uid != 0:
            return None
        if stat.S_ISLNK(metadata.st_mode):
            path = path.resolve(strict=True)
            if path.parent != Path("/run/WSL"):
                return None
            metadata = path.lstat()
    except OSError:
        return None
    return path if stat.S_ISSOCK(metadata.st_mode) and metadata.st_uid == 0 else None


def _delta(args: argparse.Namespace) -> tuple[Path, PhysicalDeltaManifest]:
    root = _required(args.delta_root, "delta_root")
    manifest = PhysicalDeltaManifest.model_validate_json(
        (root / "delta-manifest.json").read_bytes()
    )
    if manifest.epoch != args.operation_id or manifest.source_root != args.destination:
        raise MaintenancePhaseError("delta_binding_mismatch")
    return root, manifest


def _transport(args: argparse.Namespace) -> DriveTransport:

    config = DriveTransportConfig.model_validate_json(
        _required(args.transport_config, "transport_config").read_bytes()
    )
    counters: dict[str, int | None] = {
        "upload_payload": 0,
        "upload_controls": 0,
        "download_payload": 0,
        "download_controls": 0,
    }
    args.transfer_counters = counters

    def transferred(direction: str, key: str, value: int | None) -> None:
        category = "payload" if key.startswith("physical-delta/objects/") else "controls"
        name = f"{direction}_{category}"
        previous = counters[name]
        counters[name] = previous + value if previous is not None and value is not None else None

    return DriveTransport(config, transfer_observer=transferred)


def _independent_read_options(args: argparse.Namespace) -> dict[str, Path | str | None]:
    root = getattr(args, "independent_read_root", None)
    if root is None:
        return {"independent_cache_root": None, "configuration_sha256": None}
    config = DriveTransportConfig.model_validate_json(
        _required(args.transport_config, "transport_config").read_bytes()
    )
    return {
        "independent_cache_root": root,
        "configuration_sha256": hashlib.sha256(config.model_dump_json().encode()).hexdigest(),
    }


_READBACK = r"""
import hashlib,json,sys,importlib.util
from pathlib import Path
from investment_analyst.workspace.service import WorkspaceService
from investment_analyst.workspace.models import WorkspaceAccessMode
from investment_analyst.storage import compact_analytical_v2 as module
from investment_analyst.storage.compact_analytical_v2 import CompactAnalyticalStore
from investment_analyst.storage.serialization import canonical_json_bytes
root=Path(sys.argv[1]); service=WorkspaceService(); identity=service.manifest(root)
spec=importlib.util.spec_from_file_location('maintenance_reader_policy',sys.argv[2])
policy_module=importlib.util.module_from_spec(spec)
sys.modules[spec.name]=policy_module; spec.loader.exec_module(policy_module)
policy=policy_module.DuckDBResourcePolicy.model_validate_json(sys.argv[3])
with policy_module.compatible_reader_instance(
    root/'storage/v2/index.duckdb',read_only=True,policy=policy):
    with service.open_storage(service.resolve(root),WorkspaceAccessMode.READ_ONLY) as storage:
        compact=CompactAnalyticalStore(storage.store.connection)
        seal=compact.historical_seal()
        if seal is None: raise RuntimeError('historical seal absent')
        raw=storage.raw_records.get_many(storage.raw_records._staging.list_inventory_page(limit=8))
        observations=storage.observations.get_many(
            storage.observations._staging.list_observation_inventory_page(limit=8))
        metrics=compact.get_metrics(compact.list_metric_ids_page(limit=8))
        diagnostics=compact.get_diagnostics(compact.list_diagnostic_ids_page(limit=8))
        def digest(rows):
            result=hashlib.sha256()
            for key in sorted(rows,key=str): result.update(canonical_json_bytes(rows[key]))
            return result.hexdigest()
        print(json.dumps({'workspace_id':str(identity.workspace_id),
            'reader_module_path':str(Path(module.__file__).resolve()),
            'raw_count':storage.raw_records.count(),'observation_count':storage.observations.count(),
            'metric_count':storage.metric_results.count(),'diagnostic_count':storage.diagnostics.count(),
            'raw_sample_sha256':digest(raw),'observation_sample_sha256':digest(observations),
            'metric_sample_sha256':digest(metrics),'diagnostic_sample_sha256':digest(diagnostics),
            'effective_settings':dict(policy_module.verify_connection_policy(
                storage.store.connection,policy))}))
"""


def _readback(
    args: argparse.Namespace,
    receipt: PhysicalDeltaReceipt,
    *,
    expected_counts: tuple[int, int, int, int],
    expected_workspace_id: UUID,
) -> CompatibleRecoveryReadback:
    if receipt.kind != "restore" or receipt.restored_root is None:
        raise MaintenancePhaseError("readback_requires_independent_restore")
    release = LocalReleaseService().paths.releases / INTEGRATED_RELEASE
    manifest = ReleaseManifest.model_validate_json((release / "manifest.json").read_bytes())
    if manifest.commit_sha != INTEGRATED_RELEASE:
        raise MaintenancePhaseError("readback_release_mismatch")
    budget = _load_resource_budget(args)
    if (
        budget.profile != "compatible_44"
        or len(budget.assignments) != 1
        or budget.assignments[0].database != receipt.restored_root / "storage/v2/index.duckdb"
    ):
        raise MaintenancePhaseError("readback_requires_single_compatible_44_allocation")
    policy = budget.assignments[0].policy
    helper = Path(inspect.getfile(MaintenanceResourceBudget)).resolve()
    completed = subprocess.run(
        [
            str(release / ".venv/bin/python"),
            "-c",
            _READBACK,
            str(receipt.restored_root),
            str(helper),
            policy.model_dump_json(),
        ],
        cwd=release,
        env={**os.environ, "PYTHONPATH": str(release / "src")},
        capture_output=True,
        text=True,
        check=False,
        timeout=budget.sql_timeout_seconds,
    )
    if completed.returncode != 0:
        raise MaintenancePhaseError("compatible_recovery_reader_failed")
    data = json.loads(completed.stdout)
    args.compatible_reader_settings = data.pop("effective_settings")
    result = CompatibleRecoveryReadback.model_validate_json(
        json.dumps(
            {
                **data,
                "operation_id": str(args.operation_id),
                "restored_root": str(receipt.restored_root),
                "manifest_sha256": receipt.manifest_sha256,
                "recovery_receipt_sha256": hashlib.sha256(
                    receipt.model_dump_json().encode()
                ).hexdigest(),
                "reader_sha": INTEGRATED_RELEASE,
                "checked_at": datetime.now(UTC).isoformat(),
            }
        )
    )
    if (
        result.reader_module_path
        != release / "src/investment_analyst/storage/compact_analytical_v2.py"
        or result.workspace_id != expected_workspace_id
        or (
            result.raw_count,
            result.observation_count,
            result.metric_count,
            result.diagnostic_count,
        )
        != expected_counts
    ):
        raise MaintenancePhaseError("compatible_recovery_readback_mismatch")
    return result


def _complete_recovery(
    args: argparse.Namespace,
    receipt: PhysicalDeltaReceipt,
    manifest: PhysicalDeltaManifest,
) -> dict[str, object]:
    root = receipt.restored_root
    delta = _required(args.delta_root, "delta_root")
    if (
        receipt.kind != "restore"
        or root is None
        or root != args.recovery_root
        or receipt.epoch != args.operation_id
        or receipt.epoch != manifest.epoch
        or receipt.files_sha256 != manifest.files_sha256
        or receipt.historical_state != manifest.historical_state
        or receipt.manifest_sha256
        != hashlib.sha256((delta / "delta-manifest.json").read_bytes()).hexdigest()
    ):
        raise MaintenancePhaseError("recovery_coverage_binding_mismatch")
    budget = _load_resource_budget(args)
    if (
        budget.profile != "compatible_44"
        or len(budget.assignments) != 1
        or budget.assignments[0].database != root / "storage/v2/index.duckdb"
    ):
        raise MaintenancePhaseError("recovery_requires_single_compatible_44_allocation")
    policy = budget.assignments[0].policy
    service = WorkspaceService(resource_policies={budget.assignments[0].database: policy})
    identity = service.manifest(root)
    if identity.workspace_id != manifest.source_workspace_id:
        raise MaintenancePhaseError("recovery_workspace_identity_mismatch")
    # Close this instance before spawning the real release 44 reader. There is
    # one admitted database, and the candidate is never opened for comparison.
    with service.open_storage(service.resolve(root), WorkspaceAccessMode.READ_ONLY) as storage:
        coverage = _verify_full_recovery_coverage(
            root, delta / "files.jsonl", manifest, storage.store.connection
        )
        counts = (
            storage.raw_records.count(),
            storage.observations.count(),
            storage.metric_results.count(),
            storage.diagnostics.count(),
        )
    if args.core_baseline_path is not None:
        core = _RecoveredCoreBaseline.model_validate_json(args.core_baseline_path.read_bytes())
        database = next(
            item
            for item in _iter_delta_files(delta / "files.jsonl")
            if item.path == "storage/v2/index.duckdb"
        )
        if (
            core.database.sha256 != database.sha256
            or (core.raw_count, core.observation_count) != counts[:2]
            or core.raw_digest != coverage["raw_corpus_digest"]
        ):
            raise MaintenancePhaseError("recovery_historical_core_binding_mismatch")
    readback = _readback(
        args, receipt, expected_counts=counts, expected_workspace_id=identity.workspace_id
    )
    document = readback.model_dump_json().encode() + b"\n"
    archived = args.controls_root / (
        "compatible-recovery-readback." + hashlib.sha256(document).hexdigest() + ".json"
    )
    WorkspaceV2ProgressReconciler._write_once(archived, document)
    current = args.controls_root / "compatible-recovery-readback.json"
    if current.exists():
        previous = CompatibleRecoveryReadback.model_validate_json(current.read_bytes())
        if previous.model_copy(update={"checked_at": readback.checked_at}) != readback:
            raise MaintenancePhaseError("recovery_readback_changed")
    else:
        _write_new_json(current, readback.model_dump(mode="json"))
    return {
        "restore": receipt.model_dump(mode="json"),
        "coverage": coverage,
        "readback": readback.model_dump(mode="json"),
        "readback_archive": str(archived),
        "compatible_reader_settings": args.compatible_reader_settings,
    }


def _status(args: argparse.Namespace) -> dict[str, object]:
    """Read existing controls only; do not probe, create roots or open an engine."""
    steps: list[dict[str, str]] = []
    cut_path = args.controls_root / "cutover-checkpoint.json"
    complete = False
    if cut_path.exists():
        complete = _cut(args).phase == WorkspaceV2CutoverPhase.CATCHUP_COMPLETE
    for phase in PHASES:
        state = "pending"
        if phase == "catchup" and complete:
            state = "reusable"
        if phase == "delta" and args.delta_root is not None:
            receipt_path = args.delta_root / "upload-receipt.json"
            if receipt_path.exists():
                _, manifest = _delta(args)
                receipt = PhysicalDeltaReceipt.model_validate_json(receipt_path.read_bytes())
                if (
                    receipt.epoch != args.operation_id
                    or receipt.files_sha256 != manifest.files_sha256
                    or receipt.manifest_sha256
                    != hashlib.sha256(
                        (args.delta_root / "delta-manifest.json").read_bytes()
                    ).hexdigest()
                ):
                    raise MaintenancePhaseError("delta_receipt_binding_mismatch")
                state = "reusable"
        steps.append({"phase": phase, "state": state})
    return {
        "schema_version": "data-chassis-maintenance-status-v1",
        "operation_id": str(args.operation_id),
        "code_sha": args.code_sha,
        "steps": steps,
    }


def _admit_cut_command(args: argparse.Namespace, service: WorkspaceService) -> None:
    """Fail before data access for legacy commands outside the C1/global window.

    The unit-owned supervisor must create the window and activation intention.
    These phase commands can consume that authority, never manufacture it.
    """
    path = _required(getattr(args, "cut_window", None), "cut_window")
    ledger = WorkspaceV2GenerationLedger(service, args.destination)
    if ledger.attempt_invalidated(args.operation_id):
        raise MaintenancePhaseError("cut_attempt_invalidated")
    operation = args.delta_action if args.phase == "delta" else args.phase
    boot = UUID(Path("/proc/sys/kernel/random/boot_id").read_text().strip())
    window = WorkspaceV2CutWindowService(path).admit_operation(
        operation=operation, boot_id=boot, monotonic_ns=time.monotonic_ns()
    )
    if (
        window.attempt_id != args.operation_id
        or window.source_root != args.source
        or window.destination_root != args.destination
        or window.release_sha != INTEGRATED_RELEASE
        or window.source_workspace_id != service.manifest(args.source).workspace_id
        or window.destination_workspace_id != ledger.identity.workspace_id
        or window.code.sha256 != hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    ):
        raise MaintenancePhaseError("cut_window_command_binding_differs")
    owner = window.owner
    assert owner.pid is not None
    if _process_identity(owner.pid) != (owner.start_ticks, owner.boot_id, owner.cgroup):
        raise MaintenancePhaseError("cut_window_owner_changed")
    if (
        _unit_properties().get("MainPID") != str(owner.pid)
        or _process_identity(os.getpid())[2] != owner.cgroup
    ):
        raise MaintenancePhaseError("cut_window_requires_owning_maintenance_unit")
    resolved = ledger.resolve()
    if resolved is None:
        raise MaintenancePhaseError("cut_window_requires_certified_generation")
    head, generation = resolved
    if (
        generation.generation_id != getattr(args, "reconciliation_id", None)
        or generation.phase == "PREPARED"
        or generation.source_root != window.source_root
        or generation.source_workspace_id != window.source_workspace_id
        or generation.destination_workspace_id != window.destination_workspace_id
        or generation.environment_fingerprint != window.environment_sha256
    ):
        raise MaintenancePhaseError("cut_window_generation_binding_differs")
    reference = head.certificate
    seen: set[str] = set()
    while reference != window.baseline:
        if reference.sha256 in seen:
            raise MaintenancePhaseError("cut_window_baseline_chain_invalid")
        seen.add(reference.sha256)
        WorkspaceV2ProgressReconciler._verify(reference)
        ancestor = ProgressReconciliationV2.model_validate_json(reference.path.read_bytes())
        if (
            ancestor.generation_id != generation.generation_id
            or ancestor.previous_certificate is None
        ):
            raise MaintenancePhaseError("cut_window_baseline_not_current_ancestor")
        reference = ancestor.previous_certificate
    baseline = ProgressReconciliationV2.model_validate_json(reference.path.read_bytes())
    if baseline.phase != "BASELINE_VERIFIED" or baseline.reserved_successor is not None:
        raise MaintenancePhaseError("cut_window_baseline_was_not_unreserved")
    if generation.reserved_successor is not None and (
        generation.reserved_successor.operation_id != args.operation_id
        or generation.successor_owner != window.owner
    ):
        raise MaintenancePhaseError("cut_window_successor_binding_differs")
    if args.phase != "catchup" and generation.phase != "SUCCESSOR_BOUND":
        raise MaintenancePhaseError("cut_window_requires_complete_successor")


def _execute(args: argparse.Namespace) -> dict[str, object]:
    if args.phase == "resource-check":
        # Let the parent observe a live bounded worker; no database is opened.
        time.sleep(2)
        return {"resource_admission": "verified", "database_opened": False}
    service = WorkspaceService()
    if args.phase in {"catchup", "delta", "activate"}:
        _admit_cut_command(args, service)
    if args.phase == "recovery-readback":
        _, manifest = _delta(args)
        receipt = PhysicalDeltaReceipt.model_validate_json(
            _required(args.recovery_receipt, "recovery_receipt").read_bytes()
        )
        return _complete_recovery(args, receipt, manifest)
    if args.phase == "prepare-delta":
        # Prepare only the preserved W photo while v1 stays active. This epoch
        # cannot be presented as the operational F1 or used for activation.
        proof_path = _required(args.core_baseline_path, "core_baseline_path")
        core = _RecoveredCoreBaseline.model_validate_json(proof_path.read_bytes())
        WorkspaceV2ProgressReconciler._verify(core.reconciliation)
        baseline = ProgressReconciliation.model_validate_json(core.reconciliation.path.read_bytes())
        owner = WorkspaceV2ProgressReconciler(service, args.destination, args.operation_id)
        current = ProgressReconciliation.model_validate_json(owner.path.read_bytes())
        owner._controls(current)
        if (
            current != baseline
            or current.phase != "BASELINE_VERIFIED"
            or current.reserved_successor is not None
            or current.source_root != args.source
            or current.destination_root != args.destination
            or current.database_after != core.database
            or core.verifier_module_sha256
            != hashlib.sha256(
                Path(inspect.getfile(verify_recovered_core_baseline)).read_bytes()
            ).hexdigest()
        ):
            raise MaintenancePhaseError("prepared_delta_requires_preserved_verified_w")
        root = _required(args.delta_root, "delta_root")
        physical = WorkspaceV2PhysicalDeltaService(service)
        if args.delta_action != "plan":
            _, manifest = _delta(args)
            if (
                manifest.historical_state != "APPLYING"
                or manifest.source_workspace_id != baseline.destination_workspace_id
                or manifest.parent_manifest_sha256 != args.parent_manifest_sha256
            ):
                raise MaintenancePhaseError("prepared_delta_manifest_binding_differs")
            _capacity()
            if args.delta_action == "upload":
                transfer = physical.upload(
                    root,
                    _transport(args),
                    checkpoint_path=root / "upload-checkpoint.json",
                    receipt_path=root / "upload-receipt.json",
                    batch_transfer=getattr(args, "independent_read_root", None) is not None,
                    **_independent_read_options(args),
                )
                return {"preparation_only": True, "transfer": transfer.model_dump(mode="json")}
            restored = physical.restore(
                root,
                parent_index=_required(args.parent_index, "parent_index"),
                parent_root=args.destination,
                parent_manifest_sha256=args.parent_manifest_sha256,
                destination=_required(args.recovery_root, "recovery_root"),
                transport=_transport(args),
                checkpoint_path=root / "restore-checkpoint.json",
                receipt_path=root / "restore-receipt.json",
                reuse_parent_immutables=args.reuse_parent_immutables,
                **_independent_read_options(args),
            )
            return {"preparation_only": True, "restore": restored.model_dump(mode="json")}
        result = physical.plan(
            args.destination,
            _required(args.parent_index, "parent_index"),
            root,
            parent_manifest_sha256=args.parent_manifest_sha256,
            epoch=args.operation_id,
            allow_incomplete_historical=True,
            immutable_parent_proof=_required(args.immutable_parent_proof, "immutable_parent_proof"),
        )
        database = next(
            (
                item
                for item in _iter_delta_files(root / "files.jsonl")
                if item.path == "storage/v2/index.duckdb"
            ),
            None,
        )
        if (
            result.historical_state != "APPLYING"
            or database is None
            or (database.size_bytes, database.sha256)
            != (core.database.size_bytes, core.database.sha256)
        ):
            raise MaintenancePhaseError("prepared_delta_w_database_binding_differs")
        return {"preparation_only": True, "manifest": result.model_dump(mode="json")}
    if args.phase == "prepare-parent-reuse":
        return {
            "immutable_parent_proof": WorkspaceV2PhysicalDeltaService(service)
            .prepare_immutable_parent_reuse(
                args.destination,
                _required(args.parent_index, "parent_index"),
                _required(args.remote_restore_receipt, "remote_restore_receipt"),
                args.controls_root / "immutable-parent-proof.json",
                parent_manifest_sha256=args.parent_manifest_sha256,
            )
            .model_dump(mode="json")
        }
    if args.phase == "verify-core-baseline":
        return {
            "core_baseline": verify_recovered_core_baseline(
                service=service,
                destination_root=args.destination,
                reconciliation_id=args.operation_id,
                admission_path=_required(args.resource_run_root, "resource_run_root")
                / "admission.json",
            ).model_dump(mode="json")
        }
    if args.phase == "verify-financial-baseline":
        profiler = cProfile.Profile() if args.profile_financial_reader else None
        try:
            if profiler is not None:
                profiler.enable()
            return (
                WorkspaceV2ProgressReconciler(service, args.destination, args.operation_id)
                .verify_financial_baseline(
                    admission_path=_required(args.resource_run_root, "resource_run_root")
                    / "admission.json",
                    relationships_path=_required(args.relationships_proof, "relationships_proof"),
                )
                .model_dump(mode="json")
            )
        finally:
            if profiler is not None:
                profiler.disable()
                profiler.dump_stats(
                    str(
                        _required(args.resource_run_root, "resource_run_root")
                        / "financial-reader.pstats"
                    )
                )
    if args.phase in {"verify-relationships", "profile-relationships"}:
        return (
            WorkspaceV2ProgressReconciler(service, args.destination, args.operation_id)
            .verify_relationships(
                admission_path=_required(args.resource_run_root, "resource_run_root")
                / "admission.json",
                profile_only=args.phase == "profile-relationships",
            )
            .model_dump(mode="json")
        )
    if args.phase == "recover-progress":
        proofs = args.predecessor_proof
        if not proofs:
            raise MaintenancePhaseError("predecessor_proofs_missing")
        return (
            WorkspaceV2ProgressReconciler(service, args.destination, args.operation_id)
            .recover(
                predecessor_path=_required(args.predecessor_transition, "predecessor_transition"),
                predecessor_proofs=tuple(proofs),
                predecessor_code=_required(args.predecessor_code, "predecessor_code"),
                admission_path=_required(args.resource_run_root, "resource_run_root")
                / "admission.json",
            )
            .model_dump(mode="json")
        )
    cutover = WorkspaceV2CutoverService(service)
    if args.phase == "prepare":
        # The integrated release and physical index are reused; never remigrate or restore base.
        release = LocalReleaseService()
        manifest = ReleaseManifest.model_validate_json(
            (release.paths.releases / INTEGRATED_RELEASE / "manifest.json").read_bytes()
        )
        if manifest.commit_sha != INTEGRATED_RELEASE:
            raise MaintenancePhaseError("integrated_release_mismatch")
        index = verify_physical_delta_index(_required(args.parent_index, "parent_index"))
        return {
            "release": manifest.model_dump(mode="json"),
            "parent": index.model_dump(mode="json"),
            "host_c_free_bytes": _capacity(),
            "state": "reused",
        }
    if args.phase == "catchup":
        release = LocalReleaseService()
        properties = release.systemctl.show_properties(
            "investment-analyst.service", ("ActiveState", "MainPID")
        )
        if properties.get("ActiveState") != "inactive" or properties.get("MainPID") != "0":
            raise MaintenancePhaseError("stop_service_normally_before_catchup")
        frozen_path = args.controls_root / "frozen-source-cut.json"
        frozen = (
            FrozenSourceCut.model_validate_json(frozen_path.read_bytes())
            if frozen_path.exists()
            else None
        )
        with quiescent_source_cut(
            service,
            args.source,
            operation_id=args.operation_id,
            clock=(lambda: frozen.frozen_at) if frozen is not None else lambda: datetime.now(UTC),
        ) as current:
            if frozen is not None and current != frozen:
                raise MaintenancePhaseError("frozen_source_changed")
            if frozen is None:
                _write_new_json(frozen_path, current.model_dump(mode="json"))
            summary = cutover.catch_up(
                current,
                args.destination,
                checkpoint_path=args.controls_root / "cutover-checkpoint.json",
                host_free_bytes=_capacity,
                reserve_bytes=25_000_000_000,
                operational_inventory_path=args.controls_root / "operational-inventory.jsonl",
                operational_seal_path=args.controls_root / "operational-seal.json",
                operational_checkpoint_path=args.controls_root / "operational-checkpoint.json",
                operational_receipt_path=args.controls_root / "operational-receipt.json",
                page_limit=256,
                reconciliation_id=args.reconciliation_id,
                reconciliation_admission_path=(
                    None
                    if args.reconciliation_id is None
                    else _required(args.resource_run_root, "resource_run_root") / "admission.json"
                ),
                baseline_code_root=args.baseline_code_root,
                core_baseline_path=args.core_baseline_path,
            )
        return summary.model_dump(mode="json")
    if args.phase == "delta":
        cut = _cut(args)
        if cut.phase != WorkspaceV2CutoverPhase.CATCHUP_COMPLETE:
            raise MaintenancePhaseError("delta_requires_complete_core_and_operational_f")
        physical = WorkspaceV2PhysicalDeltaService(service)
        root = _required(args.delta_root, "delta_root")
        if args.delta_action == "plan":
            _capacity()
            projection = compare_operational_models(args.source, args.destination)
            parent = _required(args.parent_manifest, "parent_manifest")
            if hashlib.sha256(parent.read_bytes()).hexdigest() != args.parent_manifest_sha256:
                raise MaintenancePhaseError("parent_manifest_digest_mismatch")
            cutover.preserve_recovery_controls(
                checkpoint_path=args.controls_root / "cutover-checkpoint.json",
                operational_inventory_path=args.controls_root / "operational-inventory.jsonl",
                operational_receipt_path=args.controls_root / "operational-receipt.json",
            )
            result = physical.plan(
                args.destination,
                _required(args.parent_index, "parent_index"),
                root,
                parent_manifest_sha256=args.parent_manifest_sha256,
                epoch=args.operation_id,
                immutable_parent_proof=args.immutable_parent_proof,
            )
            return {**result.model_dump(mode="json"), "operational_models": projection}
        _, final_manifest = _delta(args)
        if args.delta_action == "upload":
            _capacity()
            return physical.upload(
                root,
                _transport(args),
                checkpoint_path=root / "upload-checkpoint.json",
                receipt_path=root / "upload-receipt.json",
                batch_transfer=getattr(args, "independent_read_root", None) is not None,
                **_independent_read_options(args),
            ).model_dump(mode="json")
        _capacity()
        restored = physical.restore(
            root,
            parent_index=_required(args.parent_index, "parent_index"),
            parent_root=args.destination,
            parent_manifest_sha256=args.parent_manifest_sha256,
            destination=_required(args.recovery_root, "recovery_root"),
            transport=_transport(args),
            checkpoint_path=root / "restore-checkpoint.json",
            receipt_path=root / "restore-receipt.json",
            reuse_parent_immutables=args.reuse_parent_immutables,
            **_independent_read_options(args),
        )
        result = _complete_recovery(args, restored, final_manifest)
        projection = compare_operational_models(args.source, restored.restored_root)
        return {**result, "operational_models": projection}
    if args.phase == "preclean":
        if args.preclean_kind in {"packed_backup", "restore_staging"}:
            lifecycle = WorkspaceV2LifecycleService(service)
            root = _required(args.retire_root, "retire_root")
            retained = _required(args.retained_controls, "retained_controls")
            manifest_path = _required(args.parent_manifest, "parent_manifest")
            if args.preclean_kind == "restore_staging":
                if root.exists():
                    _, manifest_path = lifecycle.preserve_small_backup_controls(root, retained)
                else:
                    manifest_path = retained / "remote-backup-manifest.json"
            stem = args.preclean_kind.replace("_", "-") + "-retirement"
            return lifecycle.retire_packed_backup(
                root,
                retained_controls=retained,
                parent_index=_required(args.parent_index, "parent_index"),
                remote_manifest_path=manifest_path,
                recovery_receipt_path=_required(
                    args.remote_restore_receipt, "remote_restore_receipt"
                ),
                operation_id=args.operation_id,
                inventory_path=args.controls_root / f"{stem}-inventory.jsonl",
                proof_path=args.controls_root / f"{stem}-proof.json",
                checkpoint_path=args.controls_root / f"{stem}-checkpoint.json",
                role=args.preclean_kind,
            ).model_dump(mode="json")
        root, _ = _delta(args)
        receipt = PhysicalDeltaReceipt.model_validate_json(
            _required(args.recovery_receipt, "recovery_receipt").read_bytes()
        )
        return (
            WorkspaceV2LifecycleService(service)
            .retire_delta_recovery_slot(
                receipt,
                delta_root=root,
                operation_id=args.operation_id,
                checkpoint_path=args.controls_root / "recovery-retirement-checkpoint.json",
                proof_path=args.controls_root / "recovery-retirement-proof.json",
            )
            .model_dump(mode="json")
        )
    if args.phase == "activate":
        cut = _cut(args)
        root, manifest = _delta(args)
        receipt = PhysicalDeltaReceipt.model_validate_json(
            _required(args.recovery_receipt, "recovery_receipt").read_bytes()
        )
        if (
            cut.phase != WorkspaceV2CutoverPhase.CATCHUP_COMPLETE
            or receipt.kind != "restore"
            or receipt.epoch != args.operation_id
            or receipt.historical_state != "COMPLETE"
            or receipt.files_sha256 != manifest.files_sha256
            or receipt.manifest_sha256
            != hashlib.sha256((root / "delta-manifest.json").read_bytes()).hexdigest()
        ):
            raise MaintenancePhaseError("activation_requires_independent_f_recovery")
        readback = CompatibleRecoveryReadback.model_validate_json(
            (args.controls_root / "compatible-recovery-readback.json").read_bytes()
        )
        if (
            readback.operation_id != args.operation_id
            or readback.reader_sha != INTEGRATED_RELEASE
            or readback.workspace_id != cut.destination_workspace_id
            or readback.restored_root != receipt.restored_root
            or readback.manifest_sha256 != receipt.manifest_sha256
            or readback.recovery_receipt_sha256
            != hashlib.sha256(receipt.model_dump_json().encode()).hexdigest()
        ):
            raise MaintenancePhaseError("compatible_readback_binding_mismatch")
        projection = compare_operational_models(args.source, args.destination)
        release = LocalReleaseService()
        expected_reader = (
            release.paths.releases
            / INTEGRATED_RELEASE
            / "src/investment_analyst/storage/compact_analytical_v2.py"
        ).resolve()
        if readback.reader_module_path != expected_reader:
            raise MaintenancePhaseError("compatible_readback_module_mismatch")
        return {
            **(
                release.activate(INTEGRATED_RELEASE, workspace_root=args.destination).model_dump(
                    mode="json"
                )
            ),
            "operational_models_before_activation": projection,
        }
    if args.phase == "report":
        value = FinalAcceptanceInput.model_validate_json(
            _required(args.acceptance_input, "acceptance_input").read_bytes()
        )
        if (
            value.operation_id != args.operation_id
            or value.workspace_root != args.destination
            or value.code_sha != args.code_sha
        ):
            raise MaintenancePhaseError("acceptance_binding_mismatch")
        return build_final_acceptance(value).model_dump(mode="json")
    if args.phase == "retire-final":
        from investment_analyst.workspace.backup import _workspace_process_guard
        from investment_analyst.workspace.workspace_v2_lifecycle import _require_no_directory_users

        cut = _cut(args)
        value = FinalAcceptanceInput.model_validate_json(
            _required(args.acceptance_input, "acceptance_input").read_bytes()
        )
        if (
            value.operation_id != args.operation_id
            or value.workspace_root != args.destination
            or value.workspace_id != cut.destination_workspace_id
            or value.code_sha != args.code_sha
        ):
            raise MaintenancePhaseError("retirement_acceptance_binding_mismatch")
        report = build_final_acceptance(value)
        if any(item.status != "MET" for item in report.objectives[:3]) or any(
            item.status != "MET" for item in value.gates
        ):
            raise MaintenancePhaseError("retirement_requires_q1_q3_and_all_non_numeric_gates")
        release = LocalReleaseService()
        runtime = release.status()
        binding = release.load_workspace_bindings().current
        if (
            not runtime.service_active
            or not runtime.unit_matches_current
            or runtime.overview_status != 200
            or binding is None
            or binding.workspace_root != args.destination
            or binding.workspace_id != cut.destination_workspace_id
            or binding.workspace_format_version != 2
        ):
            raise MaintenancePhaseError("retirement_requires_active_healthy_v2_binding")
        inventory = LifecycleInventory.model_validate_json(
            _required(args.lifecycle_inventory, "lifecycle_inventory").read_bytes()
        )
        if inventory.frozen_source != cut.source or inventory.unknown_file_count:
            raise MaintenancePhaseError("retirement_source_inventory_mismatch_or_unknown")
        lifecycle = WorkspaceV2LifecycleService(service)
        retirement_checkpoint = args.controls_root / "source-retirement-checkpoint.json"
        if not args.source.exists():
            saved = LifecycleCheckpoint.model_validate_json(retirement_checkpoint.read_bytes())
            if saved.source != cut.source or saved.phase not in {
                LifecyclePhase.RETIRING,
                LifecyclePhase.RETIRED,
            }:
                raise MaintenancePhaseError("retirement_resume_checkpoint_mismatch")
            if saved.retirement_root is not None and saved.retirement_root.exists():
                _require_no_directory_users(saved.retirement_root)
            return lifecycle.retire_source(
                cut.source,
                inventory,
                _required(args.complement_root, "complement_root"),
                checkpoint_path=retirement_checkpoint,
            ).model_dump(mode="json")
        with _workspace_process_guard(args.source):
            _require_no_directory_users(args.source)
            if source_fingerprint(service, args.source) != cut.source.source_fingerprint:
                raise MaintenancePhaseError("retirement_source_changed")
            fresh = lifecycle.inventory_current_v1(
                cut.source, _required(args.complement_root, "complement_root")
            )
            if fresh != inventory:
                raise MaintenancePhaseError("retirement_source_inventory_changed")
            return lifecycle.retire_source(
                cut.source,
                inventory,
                _required(args.complement_root, "complement_root"),
                checkpoint_path=args.controls_root / "source-retirement-checkpoint.json",
            ).model_dump(mode="json")
    raise MaintenancePhaseError("unsupported_phase")


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    started = datetime.now(UTC)
    timer = time.monotonic()
    try:
        if (
            args.source == args.destination
            or args.controls_root.is_relative_to(args.source)
            or args.controls_root.is_relative_to(args.destination)
        ):
            raise MaintenancePhaseError("roots_must_be_disjoint")
        if args.phase == "status" or args.dry_run:
            print(json.dumps(_status(args), sort_keys=True))
            return 0
        head = subprocess.check_output(
            ["git", "-C", str(Path(__file__).resolve().parents[1]), "rev-parse", "HEAD"], text=True
        ).strip()
        if head != args.code_sha:
            raise MaintenancePhaseError("maintenance_code_sha_mismatch")
        args.controls_root.mkdir(parents=True, exist_ok=True)
        io_before = _process_io()
        if not args.supervised_worker:
            result = _supervise(args, list(sys.argv[1:] if argv is None else argv))
            print(json.dumps(result, sort_keys=True))
            return 0
        with _worker_resources(args):
            result = _execute(args)
        io_after = _process_io()
        receipt = {
            "schema_version": "data-chassis-maintenance-phase-receipt-v1",
            "operation_id": str(args.operation_id),
            "code_sha": args.code_sha,
            "phase": args.phase,
            "action": args.delta_action if args.phase in {"delta", "prepare-delta"} else args.phase,
            "source_root": str(args.source),
            "destination_root": str(args.destination),
            "started_at": started.isoformat(),
            "completed_at": datetime.now(UTC).isoformat(),
            "elapsed_seconds": time.monotonic() - timer,
            "rss_hwm_bytes": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024,
            **_io_measurement(io_before, io_after, args, result),
            "tool_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "result": result,
        }
        if args.output is not None:
            _write_new_json(args.output, receipt)
        print(json.dumps(receipt, sort_keys=True))
        return 0
    except Exception as error:
        # Do not interpolate upstream exceptions (provider/configuration values may be secret).
        failure = {
            "error": "maintenance_phase_failed",
            "error_type": type(error).__name__,
            "reason": error.code
            if isinstance(error, (MaintenancePhaseError, ResourcePolicyError))
            else "upstream_failure",
            "phase": args.phase,
        }
        if isinstance(error, duckdb.OutOfMemoryException):
            failure["resource_failure"] = (
                "spill_limit" if "max_temp_directory_size" in str(error) else "native_memory"
            )
        if (
            args.supervised_worker
            and args.resource_run_root is not None
            and args.resource_run_root.is_relative_to(args.controls_root)
            and args.resource_run_root.is_dir()
        ):
            _replace_json(args.resource_run_root / "worker-failure.json", failure)
            cause = getattr(error, "__cause__", None)
            _replace_json(
                args.resource_run_root / "failure-frames.json",
                {
                    "error_type": type(error).__name__,
                    "cause_type": type(cause).__name__ if cause is not None else None,
                    "cause_str": str(cause) if cause is not None else None,
                    "frames": [
                        {
                            "file": Path(frame.filename).name,
                            "function": frame.name,
                            "line": frame.lineno,
                        }
                        for frame in traceback.extract_tb((cause or error).__traceback__)[-16:]
                    ],
                },
            )
        print(
            json.dumps(failure),
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
