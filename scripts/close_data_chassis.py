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
import shlex
import signal
import stat
import subprocess
import sys
import threading
import time
import traceback
import urllib.request
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager, suppress
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4, uuid5

import duckdb
from migrate_workspace_v2 import _host_free_bytes

from investment_analyst.application.local_release import LocalReleaseService, ReleaseManifest
from investment_analyst.application.storage_observability import measure_cut_analytical_payloads
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
    historical_transition_fingerprints,
    reconciliation_file_reference,
    verify_generation_contents,
    verify_generation_contents_typed_baseline,
    verify_generation_raw_files,
)
from investment_analyst.workspace.drive_transport import DriveTransport, DriveTransportConfig
from investment_analyst.workspace.models import WorkspaceAccessMode, WorkspaceManifest
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
    OwnedPayloadRetirementInput,
    PreparatorySlotRetirementReceipt,
)
from investment_analyst.workspace.workspace_v2_migration import (
    WorkspaceV2MigrationError,
    _live_quiescent_source,
    _QuiescentSourceIntent,
    _RecoveredCoreBaseline,
    _reject_conflicting_successor_reservation,
    _SuccessorReservation,
    close_quiescent_native_reader,
    measure_recovered_comparison,
    quiescent_source_cut,
    source_fingerprint,
    verify_recovered_core_baseline,
)
from investment_analyst.workspace.workspace_v2_migration_models import (
    CUT_WINDOW_EXCEPTION_SECONDS,
    CutInvalidation,
    CutOwner,
    CutWindow,
    CutWindowGo,
    CutWindowReceipt,
    GenerationContentProof,
    HistoricalTransition,
    HistoricalTransitionReceipt,
    ProgressReconciliation,
    ProgressReconciliationV2,
    ReconciliationFileReference,
    ReconciliationGenerationReference,
    ReconciliationHead,
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
GENERATION_CODE_PATHS = (
    "scripts/close_data_chassis.py",
    "src/investment_analyst/application/local_release.py",
    "src/investment_analyst/application/storage_observability.py",
    "src/investment_analyst/storage/workspace_v2_inventory.py",
    "src/investment_analyst/storage/compact_analytical_v2.py",
    "src/investment_analyst/storage/historical_analytical_import.py",
    "src/investment_analyst/workspace/workspace_v2_migration_models.py",
    "src/investment_analyst/storage/resource_policy.py",
    "src/investment_analyst/storage/serialization.py",
    "src/investment_analyst/storage/raw_v2.py",
    "src/investment_analyst/storage/raw_records.py",
    "src/investment_analyst/workspace/workspace_v2_migration.py",
    "src/investment_analyst/workspace/workspace_v2_physical_delta.py",
    "src/investment_analyst/workspace/workspace_v2_lifecycle.py",
    "src/investment_analyst/workspace/workspace_v2_lifecycle_models.py",
    "src/investment_analyst/workspace/workspace_v2_cutover.py",
    "src/investment_analyst/workspace/workspace_v2_operational_state.py",
)


class MaintenancePhaseError(ValueError):
    """One controlled reason code; never interpolate upstream error payloads."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class _CutMonitor:
    """Unit-owned R1/deadline supervision, independent of the client and SQL."""

    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.root = _required(args.resource_run_root, "resource_run_root")
        read_args = argparse.Namespace(**vars(args))
        read_args.resource_budget = _required(args.cut_readback_budget, "cut_readback_budget")
        self.budgets = (_load_resource_budget(args), _load_resource_budget(read_args))
        base, reader = self.budgets
        if (
            base.profile != "verification"
            or {a.database for a in base.assignments}
            != {
                args.source / "storage/data/processed/investment_analyst.duckdb",
                args.destination / "storage/v2/index.duckdb",
            }
            or len(base.assignments) != 2
            or base.reader_worker is not None
            or reader.profile != "compatible_44"
            or len(reader.assignments) != 1
            or reader.assignments[0].database
            != _required(args.recovery_root, "recovery_root") / "storage/v2/index.duckdb"
            or reader.assignments[0].policy.memory_limit_bytes != 2_000_000_000
        ):
            raise MaintenancePhaseError("cut_requires_verification_and_single_reader_profiles")
        directories = [a.policy.temp_directory for b in self.budgets for a in b.assignments]
        if len(directories) != len(set(directories)):
            raise MaintenancePhaseError("cut_profiles_share_temporary_directories")
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._execution: MaintenanceExecution | None = None
        self._run: Path | None = None
        self._bound_at = time.monotonic()
        self._capacity_success: int | None = None
        self._capacity_success_at: float | None = None
        self._capacity_low = False
        self._capacity_error: str | None = None
        self._capacity_attempt_at: float | None = None
        self._host_updated = threading.Event()
        self.reason: str | None = None
        self.recovering = False
        self._stopped_at: float | None = None
        self._events: tuple[int, int, int] | None = None
        self._cgroup = _process_identity(os.getpid())[2]
        self._host = threading.Thread(target=self._host_probe, daemon=True)
        self._watcher = threading.Thread(target=self._watch, daemon=True)

    def start_ready(self, timeout_seconds: float = 15.0) -> None:
        """Start supervision only after a fresh valid host/R1 observation."""
        if self._host.ident is None:
            self._host.start()
        deadline = time.monotonic() + max(0.1, timeout_seconds)
        while True:
            if self._stop.is_set():
                raise MaintenancePhaseError("cut_monitor_stopped_before_ready")
            try:
                reason = self._ready_sample()
            except Exception as error:
                raise MaintenancePhaseError("cut_monitor_readiness_failed") from error
            if reason is None:
                if self._watcher.ident is None:
                    self._watcher.start()
                return
            if reason != "host_capacity_unavailable_or_below_floor":
                raise MaintenancePhaseError(reason)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise MaintenancePhaseError("host_capacity_unavailable_or_below_floor")
            self._host_updated.wait(min(0.5, remaining))
            self._host_updated.clear()

    def bind(self, execution: MaintenanceExecution, run: Path) -> None:
        with self._lock:
            self._execution, self._run, self._bound_at = execution, run, time.monotonic()

    def unbind(self) -> None:
        with self._lock:
            self._execution, self._run = None, None

    def require_continue(self) -> None:
        if self.reason is not None:
            raise MaintenancePhaseError(self.reason)

    def _host_probe(self) -> None:
        required = max(budget.required_host_bytes for budget in self.budgets)
        previous: tuple[str, str | None] | None = None
        while not self._stop.is_set():
            state: tuple[str, int | None, str | None]
            try:
                value = _capacity()
            except MaintenancePhaseError as failure:
                if failure.code == "host_capacity_below_floor":
                    state = ("below_floor", None, failure.code)
                    with self._lock:
                        self._capacity_low = True
                        self._capacity_success = None
                        self._capacity_success_at = None
                        self._capacity_error = failure.code
                        self._capacity_attempt_at = time.monotonic()
                else:
                    state = ("transient", None, "host_capacity_probe_failed")
                    with self._lock:
                        self._capacity_error = "host_capacity_probe_failed"
                        self._capacity_attempt_at = time.monotonic()
            except Exception:
                state = ("transient", None, "host_capacity_probe_failed")
                with self._lock:
                    self._capacity_error = "host_capacity_probe_failed"
                    self._capacity_attempt_at = time.monotonic()
            else:
                if value < required:
                    state = ("below_floor", value, "host_capacity_below_floor")
                    with self._lock:
                        self._capacity_low = True
                        self._capacity_success = None
                        self._capacity_success_at = None
                        self._capacity_error = "host_capacity_below_floor"
                        self._capacity_attempt_at = time.monotonic()
                else:
                    state = ("valid", value, None)
                    with self._lock:
                        self._capacity_success = value
                        self._capacity_success_at = time.monotonic()
                        self._capacity_error = (
                            "host_capacity_below_floor" if self._capacity_low else None
                        )
                        self._capacity_attempt_at = self._capacity_success_at
            key = (state[0], state[2])
            if key != previous:
                previous = key
                with suppress(OSError):
                    _replace_json(
                        self.root / "cut-host-probe.json",
                        {
                            "state": state[0],
                            "host_free_bytes": state[1],
                            "diagnostic": state[2],
                            "at": datetime.now(UTC).isoformat(),
                        },
                    )
            self._host_updated.set()
            self._stop.wait(1)

    def _resource_sample(
        self, capacity: int, *, heartbeat_age_seconds: int
    ) -> MaintenanceResourceSample:
        samples = tuple(
            sample_cgroup_resources(
                budget,
                self._cgroup,
                host_free_bytes=capacity,
                log_bytes=(self.root / "worker.log").stat().st_size
                if (self.root / "worker.log").exists()
                else 0,
                heartbeat_age_seconds=heartbeat_age_seconds,
            )
            for budget in self.budgets
        )
        return samples[0].model_copy(
            update={
                "temp_allocated_bytes": sum(s.temp_allocated_bytes for s in samples),
                "temp_logical_bytes": sum(s.temp_logical_bytes for s in samples),
                "memory_current_bytes": max(s.memory_current_bytes for s in samples),
                "available_memory_bytes": min(s.available_memory_bytes for s in samples),
                "events": samples[-1].events,
            }
        )

    def _ready_sample(self) -> str | None:
        now = time.monotonic()
        with self._lock:
            low = self._capacity_low
            success = self._capacity_success
            success_at = self._capacity_success_at
        if low:
            return "host_capacity_below_floor"
        if success is None or success_at is None or now - success_at > 15:
            return "host_capacity_unavailable_or_below_floor"
        sample = self._resource_sample(success, heartbeat_age_seconds=0)
        for budget in self.budgets:
            reason = resource_stop_reason(budget, sample, initial_events=None)
            if reason is not None:
                return reason
        self._events = sample.events
        _write_new_json(self.root / "cut-resource-origin.json", sample.model_dump(mode="json"))
        _replace_json(self.root / "cut-last-sample.json", sample.model_dump(mode="json"))
        return None

    def _observe(self) -> str | None:
        now = time.monotonic()
        with self._lock:
            execution, run, bound = self._execution, self._run, self._bound_at
            low = self._capacity_low
            success = self._capacity_success
            success_at = self._capacity_success_at
        if low:
            return "host_capacity_below_floor"
        if success is None or success_at is None or now - success_at > 15:
            return "host_capacity_unavailable_or_below_floor"
        age = 0
        if execution is not None and run is not None:
            path = run / "heartbeat.json"
            if path.exists():
                heartbeat = json.loads(path.read_bytes())
                if heartbeat.get("pid") != os.getpid() or heartbeat.get("operation_id") != str(
                    self.args.operation_id
                ):
                    return "cut_heartbeat_owner_differs"
                age = int(now - heartbeat["heartbeat_monotonic"])
            elif now - bound > 5:
                return "cut_heartbeat_missing"
        sample = self._resource_sample(success, heartbeat_age_seconds=age)
        if self._events is None:
            self._events = sample.events
            _write_new_json(self.root / "cut-resource-origin.json", sample.model_dump(mode="json"))
        _replace_json(self.root / "cut-last-sample.json", sample.model_dump(mode="json"))
        for budget in self.budgets:
            reason = resource_stop_reason(budget, sample, initial_events=self._events)
            if reason is not None:
                return reason
        window_path = _required(self.args.cut_window, "cut_window")
        if window_path.exists():
            window_service = WorkspaceV2CutWindowService(window_path)
            window = window_service.load()
            if window.phase not in {"HEALTHY_V1", "HEALTHY_V2", "ABORTED", "RETURNING_V1"}:
                boot = _process_identity(os.getpid())[1]
                if (
                    window_service.remaining_work_ns(boot_id=boot, monotonic_ns=time.monotonic_ns())
                    <= 0
                ):
                    return "cut_global_recovery_reserve_reached"
        return None

    def _watch(self) -> None:
        while not self._stop.is_set():
            try:
                reason = self._observe()
                if reason is not None and not self.recovering:
                    self.reason = self.reason or reason
                    with self._lock:
                        execution = self._execution
                    if execution is not None:
                        execution.cancel(self.reason)
                    if self._stopped_at is None:
                        self._stopped_at = time.monotonic()
                        _replace_json(
                            self.root / "cut-stop-request.json",
                            {
                                "reason": self.reason,
                                "at": datetime.now(UTC).isoformat(),
                            },
                        )
                    if time.monotonic() - self._stopped_at >= 5:
                        # systemd runs the mandatory source-aware ExecStopPost.
                        os.kill(os.getpid(), signal.SIGTERM)
            except Exception:
                # A dead observer cannot leave a stopped source unattended.
                os.kill(os.getpid(), signal.SIGTERM)
                return
            self._stop.wait(1)

    def close(self) -> None:
        self._stop.set()
        self._host_updated.set()
        if self._watcher.ident is not None:
            self._watcher.join(timeout=2)
        if self._host.ident is not None:
            self._host.join(timeout=2)


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
            "reconcile-generation",
            "invalidate-reservation",
            "verify-generation",
            "verify-generation-raw",
            "certify-generation",
            "retire-preparatory",
            "retire-owned-payload",
            "verify-relationships",
            "verify-financial-baseline",
            "verify-core-baseline",
            "prepare-parent-reuse",
            "prepare-links",
            "prepare-delta",
            "recovery-readback",
            "profile-relationships",
            "cut",
            "repair-publication",
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
    parser.add_argument("--prepared-delta", type=_absolute)
    parser.add_argument(
        "--delta-action",
        choices=("plan", "upload", "restore", "measure-plan", "measure-comparison"),
        default="plan",
    )
    parser.add_argument("--coverage-only", action="store_true")
    parser.add_argument("--recovery-root", type=_absolute)
    parser.add_argument("--reuse-parent-immutables", action="store_true")
    parser.add_argument("--immutable-parent-proof", type=_absolute)
    parser.add_argument("--immutable-link-journal", type=_absolute)
    parser.add_argument("--immutable-link-witness", type=_absolute)
    parser.add_argument("--link-witness-receipt", type=_absolute)
    parser.add_argument("--journal-byte-budget", type=int, default=1_000_000_000)
    parser.add_argument("--transport-config", type=_absolute)
    parser.add_argument("--independent-read-root", type=_absolute)
    parser.add_argument("--recovery-receipt", type=_absolute)
    parser.add_argument("--cut-window", type=_absolute)
    parser.add_argument("--cut-go", type=_absolute)
    parser.add_argument("--cut-readback-budget", type=_absolute)
    parser.add_argument("--cut-recover", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--acceptance-input", type=_absolute)
    parser.add_argument("--lifecycle-inventory", type=_absolute)
    parser.add_argument("--complement-root", type=_absolute)
    parser.add_argument(
        "--preclean-kind",
        choices=("recovery_slot", "packed_backup", "restore_staging", "unused_release"),
        default="recovery_slot",
    )
    parser.add_argument("--retire-root", type=_absolute)
    parser.add_argument("--owned-payload-input", type=_absolute)
    parser.add_argument("--publication-repair-input", type=_absolute)
    parser.add_argument("--retained-controls", type=_absolute)
    parser.add_argument("--retire-release-sha", type=_sha)
    parser.add_argument("--retained-release-path", type=_absolute, action="append")
    parser.add_argument("--remote-restore-receipt", type=_absolute)
    parser.add_argument("--output", type=_absolute)
    parser.add_argument("--resource-budget", type=_absolute)
    parser.add_argument("--predecessor-transition", type=_absolute)
    parser.add_argument("--predecessor-proof", type=_absolute, action="append")
    parser.add_argument("--predecessor-code", type=_absolute)
    parser.add_argument("--relationships-proof", type=_absolute)
    parser.add_argument("--reconciliation-id", type=UUID)
    parser.add_argument("--reservation-path", type=_absolute)
    parser.add_argument("--baseline-code-root", type=_absolute)
    parser.add_argument("--core-baseline-path", type=_absolute)
    parser.add_argument("--generation-content-proof", type=_absolute)
    parser.add_argument("--preparatory-retirement-input", type=_absolute)
    parser.add_argument("--profile-financial-reader", action="store_true")
    parser.add_argument("--supervised-worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--resource-run-root", type=_absolute, help=argparse.SUPPRESS)
    return parser


MAINTENANCE_UNIT = "investment-analyst-dc45-cut-f.service"
CUT_MONITOR_READINESS_SECONDS = 15.0


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
        [
            "systemctl",
            "--user",
            "show",
            MAINTENANCE_UNIT,
            "--property=ActiveState,MainPID,ControlPID,Result,ActiveEnterTimestampMonotonic",
        ],
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
    is_stop_hook = (
        getattr(args, "cut_recover", False)
        and args.phase == "cut"
        and properties.get("ActiveState") in {"deactivating", "failed"}
        and properties.get("ControlPID") == str(pid)
    )
    if not is_stop_hook and (
        properties.get("ActiveState") != "active" or properties.get("MainPID") != str(pid)
    ):
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
    monitor = getattr(args, "cut_monitor", None)
    if monitor is not None:
        with monitor._lock:
            active_execution = monitor._execution
        if active_execution is not None:
            # Freeze and catch-up share the real source instance. Replacing its
            # execution registry would try to ATTACH that database twice.
            from investment_analyst.storage.resource_policy import require_maintenance_admission

            require_maintenance_admission(receipt)
            yield
            return
    execution: MaintenanceExecution
    cache = _PhysicalCacheReclaimer(
        receipt,
        enabled=args.phase
        in {
            "preclean",
            "cut",
            "prepare-delta",
            "delta",
            "catchup",
            "reconcile-generation",
            "verify-generation",
            "verify-generation-raw",
            "certify-generation",
            "retire-preparatory",
            "prepare-links",
            "retire-owned-payload",
            "recovery-readback",
        },
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
    if monitor is not None:
        monitor.bind(execution, run)
    cache.start()
    try:
        with maintenance_resource_budget(budget), maintenance_execution(execution):
            yield
    finally:
        if monitor is not None:
            monitor.unbind()
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


def _cut_unit_options(args: argparse.Namespace, worker_args: list[str], run: Path) -> list[str]:
    """A unit-owned recovery hook must outlive both the client and the worker."""
    go = CutWindowGo.model_validate_json(_required(args.cut_go, "cut_go").read_bytes())
    reserve = next(b.upper_seconds for b in go.bounds if b.step == "cancel_close_restart")
    recovery = list(worker_args)
    if "--output" in recovery:
        index = recovery.index("--output")
        recovery[index + 1] = str(run / "cut-recovery-phase.json")
    recovery.append("--cut-recover")
    hook = shlex.join([sys.executable, str(Path(__file__).resolve()), *recovery])
    # systemd performs specifier/environment expansion even without a shell.
    hook = hook.replace("%", "%%").replace("$", "$$")
    return [
        f"--property=RuntimeMaxSec={go.window_limit_seconds - reserve}",
        f"--property=TimeoutStopSec={reserve}",
        "--property=ExecStopPost=" + hook,
    ]


def _wait_cut_unit(output: Path, run: Path) -> dict[str, object]:
    # Client loss must not stop this unit. Resource/deadline observation belongs
    # to its MainPID, and systemd owns recovery after that PID disappears.
    while True:
        properties = _unit_properties()
        if (
            properties.get("MainPID", "0") == "0"
            and properties.get("ControlPID", "0") == "0"
            and properties.get("ActiveState") not in {"active", "activating", "deactivating"}
        ):
            if output.is_file():
                return json.loads(output.read_bytes())
            if (run / "cut-recovery-phase.json").is_file():
                raise MaintenancePhaseError("cut_interrupted_recovery_recorded")
            raise MaintenancePhaseError("cut_exited_without_recovery_receipt")
        time.sleep(1)


def _supervise(args: argparse.Namespace, argv: list[str]) -> dict[str, object]:
    budget = _load_resource_budget(args)
    properties = _unit_properties()
    if (
        properties.get("MainPID", "0") != "0"
        or properties.get("ControlPID", "0") != "0"
        or properties.get("ActiveState") in {"active", "activating", "deactivating"}
    ):
        raise MaintenancePhaseError("maintenance_unit_already_in_use")
    initial_capacity = _capacity()
    if initial_capacity < budget.required_host_bytes:
        raise MaintenancePhaseError("next_unit_host_reserve_unavailable")
    run = args.controls_root / ("resource-run-" + uuid4().hex)
    run.mkdir()
    # Preserve the bytes actually admitted for this worker. Later edits must
    # never relabel a completed receipt or force reconstruction of old code.
    repository = Path(__file__).resolve().parents[1]
    bindings = {}
    for relative in GENERATION_CODE_PATHS:
        document = (repository / relative).read_bytes()
        archived = run / "code" / relative
        archived.parent.mkdir(parents=True, exist_ok=True)
        WorkspaceV2ProgressReconciler._write_once(archived, document)
        bindings[relative] = {
            "sha256": hashlib.sha256(document).hexdigest(),
            "size_bytes": len(document),
        }
    _write_new_json(
        run / "source-bindings.json",
        {
            "code_sha": args.code_sha,
            "files": bindings,
            "recorded_at": datetime.now(UTC).isoformat(),
        },
    )
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
    if args.phase == "cut":
        command = [part for part in command if not part.startswith("--property=TimeoutStopSec=")]
        command[3:3] = _cut_unit_options(args, worker_args, run)
    subprocess.run(command, check=True, capture_output=True, timeout=10)
    if args.phase == "cut":
        return _wait_cut_unit(output, run)
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
        or budget.assignments[0].policy.memory_limit_bytes != 2_000_000_000
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
        or budget.assignments[0].policy.memory_limit_bytes != 2_000_000_000
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


def _prepare_generation_from_revoked_head(
    args: argparse.Namespace,
    service: WorkspaceService,
    ledger: WorkspaceV2GenerationLedger,
    admission: MaintenanceResourceReceipt,
    head: ReconciliationHead,
    previous: ProgressReconciliationV2,
    source: WorkspaceManifest,
) -> dict[str, object]:
    """Publish the next generation from a revoked SUCCESSOR_BOUND head.

    Partial writes stay in the current image. The revoked attempt, its dead
    owner and the historical W certificate remain immutable ancestors; this
    function never synthesizes COMPLETE or reuses the dead session.
    """
    attempt = previous.reserved_successor.operation_id if previous.reserved_successor else None
    if (
        previous.phase != "SUCCESSOR_BOUND"
        or attempt is None
        or previous.successor_owner is None
        or previous.successor_admission is None
        or previous.coverage is None
    ):
        raise MaintenancePhaseError("generation_continuation_requires_revoked_successor")
    reservation_path = ledger.state / f"progress-successor-reservation.{attempt}.json"
    reservation = _SuccessorReservation.model_validate_json(reservation_path.read_bytes())
    if (
        reservation.reconciliation_id != previous.generation_id
        or reservation.successor != previous.reserved_successor
        or reservation.resource_receipt != previous.successor_admission
    ):
        raise MaintenancePhaseError("generation_continuation_reservation_differs")
    invalidations = []
    for path in sorted(ledger.state.glob(f"cut-invalidation.{attempt}.*.json")):
        if path.is_symlink():
            raise MaintenancePhaseError("generation_continuation_invalidation_unsafe")
        event = CutInvalidation.model_validate_json(path.read_bytes())
        if (
            event.attempt_id == attempt
            and event.generation_id == args.operation_id
            and event.certificate
            == ReconciliationGenerationReference(
                schema_type="workspace-v2-progress-reconciliation-v2",
                artifact=head.certificate,
            )
            and event.reservation == reconciliation_file_reference(reservation_path, "control")
            and event.owner == previous.successor_owner
            and event.destination_root == args.destination
            and event.destination_workspace_id == ledger.identity.workspace_id
            and event.source_root == args.source
            and event.source_workspace_id == source.workspace_id
            and event.reason == "SESSION_ENDED"
        ):
            # Own continuation revocation from a crash before head publication.
            # The intent/publish path revalidates it and records it as the new
            # generation invalidation; it is never the revoked predecessor.
            continue
        if (
            event.attempt_id != attempt
            or event.generation_id != previous.generation_id
            or event.certificate
            != ReconciliationGenerationReference(
                schema_type="workspace-v2-progress-reconciliation-v2",
                artifact=head.certificate,
            )
            or event.reservation != reconciliation_file_reference(reservation_path, "control")
            or event.owner != previous.successor_owner
            or event.destination_root != args.destination
            or event.destination_workspace_id != ledger.identity.workspace_id
            or event.source_root != args.source
            or event.source_workspace_id != source.workspace_id
            or event.reason != "SESSION_ENDED"
        ):
            raise MaintenancePhaseError("generation_continuation_invalidation_differs")
        WorkspaceV2ProgressReconciler._verify(event.database)
        if event.wal is not None:
            WorkspaceV2ProgressReconciler._verify(event.wal)
        invalidations.append((event, reconciliation_file_reference(path, "control")))
    if len(invalidations) != 1:
        raise MaintenancePhaseError("generation_continuation_invalidation_not_unique")
    revoked, revoked_reference = invalidations[0]
    database = args.destination / "storage/v2/index.duckdb"
    if database.with_suffix(".duckdb.wal").exists():
        raise MaintenancePhaseError("generation_continuation_requires_closed_image")
    image = reconciliation_file_reference(database, "database")
    if image != revoked.database:
        raise MaintenancePhaseError("generation_continuation_database_changed")
    core = _RecoveredCoreBaseline.model_validate_json(
        _required(args.core_baseline_path, "core_baseline_path").read_bytes()
    )
    WorkspaceV2ProgressReconciler._verify(core.reconciliation)
    if (
        core.reconciliation != previous.verified_w
        or core.database.role != "database"
        or previous.inventory is None
        or core.raw_count > previous.inventory.raw_count
        or core.observation_count > previous.inventory.observation_count
        or (source.workspace_id, source.created_at)
        != (previous.source_workspace_id, previous.source_created_at)
    ):
        raise MaintenancePhaseError("generation_continuation_core_baseline_differs")
    expected_head = ledger._head_digest()
    intent_path = args.controls_root / f"continuation-invalidation.{args.operation_id}.intent.json"
    if intent_path.exists():
        event = CutInvalidation.model_validate_json(intent_path.read_bytes())
    else:
        event = CutInvalidation(
            event_id=uuid5(args.operation_id, "SESSION_ENDED"),
            generation_id=args.operation_id,
            attempt_id=attempt,
            previous_event=None,
            certificate=ReconciliationGenerationReference(
                schema_type="workspace-v2-progress-reconciliation-v2",
                artifact=head.certificate,
            ),
            reservation=reconciliation_file_reference(reservation_path, "control"),
            owner=previous.successor_owner,
            source_root=args.source,
            source_workspace_id=source.workspace_id,
            destination_root=args.destination,
            destination_workspace_id=ledger.identity.workspace_id,
            database=image,
            wal=None,
            write_generation=revoked.write_generation,
            historical_seal=None,
            historical_receipt=None,
            reason="SESSION_ENDED",
            owner_absence=revoked.owner_absence,
            exclusivity=revoked.exclusivity,
            no_activation=revoked.no_activation,
            code_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            policy_sha256=admission.resource_policy_sha256,
            recorded_at=datetime.now(UTC),
            expected_head_sha256=expected_head,
        )
        WorkspaceV2ProgressReconciler._write_once(
            intent_path, event.model_dump_json().encode() + b"\n"
        )
    if (
        event.generation_id != args.operation_id
        or event.attempt_id != attempt
        or event.certificate.artifact != head.certificate
        or event.reservation != reconciliation_file_reference(reservation_path, "control")
        or event.owner != previous.successor_owner
        or event.database != image
        or event.expected_head_sha256 != expected_head
    ):
        raise MaintenancePhaseError("generation_continuation_intent_differs")
    invalidation = ledger.invalidate(event, admission=admission)
    prepared_path = args.controls_root / f"prepared-generation.{args.operation_id}.intent.json"
    if prepared_path.exists():
        generation = ProgressReconciliationV2.model_validate_json(prepared_path.read_bytes())
    else:
        code = []
        repository = Path(__file__).resolve().parents[1]
        for relative in GENERATION_CODE_PATHS:
            target = args.controls_root / "generation-code" / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            WorkspaceV2ProgressReconciler._write_once(target, (repository / relative).read_bytes())
            code.append(reconciliation_file_reference(target, "code"))
        tree, environment = historical_transition_fingerprints(256, "shared-reference-verifier-v1")
        now = datetime.now(UTC)
        receipts = [*previous.previous_receipts, revoked_reference]
        cut_window_receipt = args.controls_root / "cut-window-receipt.json"
        if cut_window_receipt.exists() and not cut_window_receipt.is_symlink():
            receipts.append(reconciliation_file_reference(cut_window_receipt, "control"))
        generation = ProgressReconciliationV2(
            generation_id=args.operation_id,
            ordinal=previous.ordinal + 1,
            phase="PREPARED",
            previous_generation=ReconciliationGenerationReference(
                schema_type="workspace-v2-progress-reconciliation-v2",
                artifact=head.certificate,
            ),
            previous_certificate=None,
            invalidation=invalidation,
            source_root=args.source,
            source_workspace_id=source.workspace_id,
            source_created_at=source.created_at,
            destination_root=args.destination,
            destination_workspace_id=ledger.identity.workspace_id,
            predecessor=previous.predecessor,
            previous_attempt=previous.previous_attempt,
            previous_reservation=reconciliation_file_reference(reservation_path, "control"),
            previous_receipts=tuple(receipts),
            parent_membership=previous.parent_membership,
            parent_seal=previous.parent_seal,
            verified_w=previous.verified_w,
            database=image,
            wal=None,
            inventory=None,
            core_proof=None,
            corrective_proofs=(),
            code=tuple(code),
            tree_fingerprint=tree,
            environment_fingerprint=environment,
            resource_receipt=reconciliation_file_reference(
                args.resource_run_root / "admission.json", "resource"
            ),
            prepared_at=now,
            recorded_at=now,
        )
        WorkspaceV2ProgressReconciler._write_once(
            prepared_path, generation.model_dump_json().encode() + b"\n"
        )
    if (
        generation.generation_id != args.operation_id
        or generation.phase != "PREPARED"
        or generation.ordinal != previous.ordinal + 1
        or generation.previous_generation.artifact != head.certificate
        or generation.invalidation != invalidation
        or generation.verified_w != previous.verified_w
        or generation.database != image
    ):
        raise MaintenancePhaseError("generation_continuation_prepared_differs")
    reference = ledger.publish(generation, expected_head_sha256=expected_head, admission=admission)
    return {
        "generation": generation.model_dump(mode="json"),
        "certificate": reference.model_dump(mode="json"),
        "reused": False,
        "continuation_of": str(previous.generation_id),
        "revoked_attempt_id": str(attempt),
    }


def _prepare_generation(args: argparse.Namespace, service: WorkspaceService) -> dict[str, object]:
    """Publish C1 through the typed ledger, preserving the exact legacy objects.

    The small operational intents make retries byte-exact. They are never a
    substitute for the ledger's locks, live owner/binding checks or CAS.
    """
    from investment_analyst.workspace.workspace_v2_backup import _v2_writer_guard

    ledger = WorkspaceV2GenerationLedger(service, args.destination)
    admission = MaintenanceResourceReceipt.model_validate_json(
        (_required(args.resource_run_root, "resource_run_root") / "admission.json").read_bytes()
    )
    ledger._admit(admission)
    if admission.budget.operation_id != args.operation_id:
        raise MaintenancePhaseError("generation_admission_identity_differs")
    release = LocalReleaseService()
    bindings = release.load_workspace_bindings()
    properties = release.systemctl.show_properties(
        "investment-analyst.service", ("ActiveState", "MainPID", "ExecStart")
    )
    if (
        any(
            b is not None and b.workspace_format_version == 2
            for b in (bindings.current, bindings.previous)
        )
        or properties.get("ActiveState") != "active"
        or properties.get("MainPID") in {None, "0"}
        or str(args.source) not in properties.get("ExecStart", "")
        or str(args.destination) in properties.get("ExecStart", "")
    ):
        raise MaintenancePhaseError("generation_requires_active_v1_and_no_v2_activation")
    resolved = ledger.resolve()
    if resolved is not None:
        head, generation = resolved
        if generation.generation_id == args.operation_id:
            return {
                "generation": generation.model_dump(mode="json"),
                "certificate": head.certificate.model_dump(mode="json"),
                "reused": True,
            }
        return _prepare_generation_from_revoked_head(
            args,
            service,
            ledger,
            admission,
            head,
            generation,
            service.manifest(args.source),
        )
    reconciliation_id = args.reconciliation_id
    if reconciliation_id is None or not args.predecessor_proof:
        raise MaintenancePhaseError("generation_requires_legacy_identity_and_invalidity_evidence")
    legacy_path = ledger.state / f"progress-reconciliation.{reconciliation_id}.json"
    legacy = ProgressReconciliation.model_validate_json(legacy_path.read_bytes())
    if (
        legacy.phase != "SUCCESSOR_BOUND"
        or legacy.reserved_successor is None
        or legacy.source_root != args.source
        or legacy.destination_root != args.destination
    ):
        raise MaintenancePhaseError("generation_requires_exact_preserved_legacy_successor")
    WorkspaceV2ProgressReconciler._verify(legacy.parent_membership)
    source = service.manifest(args.source)
    if (source.workspace_id, source.created_at) != (
        legacy.source_workspace_id,
        legacy.source_created_at,
    ):
        raise MaintenancePhaseError("generation_source_identity_differs")
    transition_path = _required(args.predecessor_transition, "predecessor_transition")
    transition = HistoricalTransition.model_validate_json(transition_path.read_bytes())
    attempt = legacy.reserved_successor.operation_id
    receipt_path = ledger.state / f"historical-transition-receipt.{attempt}.json"
    receipt = HistoricalTransitionReceipt.model_validate_json(receipt_path.read_bytes())
    if (
        transition.operation_id != attempt
        or receipt.transition.operation_id != attempt
        or transition.destination_workspace_id != ledger.identity.workspace_id
        or receipt.transition.destination_workspace_id != ledger.identity.workspace_id
        or transition.phase != "COMPLETE"
    ):
        raise MaintenancePhaseError("generation_predecessor_seal_binding_differs")
    reservation_path = ledger.state / f"progress-successor-reservation.{attempt}.json"
    reservation = _SuccessorReservation.model_validate_json(reservation_path.read_bytes())
    WorkspaceV2ProgressReconciler._verify(reservation.resource_receipt)
    old_admission = MaintenanceResourceReceipt.model_validate_json(
        reservation.resource_receipt.path.read_bytes()
    )
    owner = CutOwner(
        source_session=reservation.source_session,
        pid=old_admission.pid,
        start_ticks=old_admission.process_start_ticks,
        boot_id=old_admission.boot_id,
        cgroup=old_admission.cgroup_path,
    )
    core = _RecoveredCoreBaseline.model_validate_json(
        _required(args.core_baseline_path, "core_baseline_path").read_bytes()
    )
    WorkspaceV2ProgressReconciler._verify(core.reconciliation)
    original_w = ProgressReconciliation.model_validate_json(core.reconciliation.path.read_bytes())
    if original_w.phase != "BASELINE_VERIFIED" or original_w.operation_id != reconciliation_id:
        raise MaintenancePhaseError("generation_original_w_certificate_differs")
    legacy_document = legacy_path.read_bytes()
    legacy_digest = hashlib.sha256(legacy_document).hexdigest()
    archived = (
        ledger.state
        / f"progress-reconciliation-generation.{reconciliation_id}.{legacy_digest}.json"
    )
    WorkspaceV2ProgressReconciler._write_once(archived, legacy_document)
    previous = ReconciliationGenerationReference(
        schema_type="workspace-v2-progress-reconciliation-v1",
        artifact=reconciliation_file_reference(archived, "control"),
    )
    proofs = tuple(reconciliation_file_reference(p, "control") for p in args.predecessor_proof)
    evidence_path = args.controls_root / "generation-invalidation-evidence.json"
    with _v2_writer_guard(args.destination):
        database = args.destination / "storage/v2/index.duckdb"
        if database.with_suffix(".duckdb.wal").exists():
            raise MaintenancePhaseError("generation_requires_closed_image_no_new_wal")
        image = reconciliation_file_reference(database, "database")
        if not evidence_path.exists():
            _write_new_json(
                evidence_path,
                {
                    "operation_id": str(args.operation_id),
                    "observed_at": datetime.now(UTC).isoformat(),
                    "destination": str(args.destination),
                    "workspace_id": str(ledger.identity.workspace_id),
                    "database": image.model_dump(mode="json"),
                    "wal_present": False,
                    "active_v1_pid": int(properties["MainPID"]),
                    "active_source": str(args.source),
                    "v2_binding_present": False,
                    "historical_owner": owner.model_dump(mode="json"),
                    "invalidity_evidence": [p.model_dump(mode="json") for p in proofs],
                },
            )
    evidence = reconciliation_file_reference(evidence_path, "control")
    prior_event = None
    for reason in ("EVIDENCE_INVALID", "SOURCE_REOPENED"):
        intent = args.controls_root / f"invalidation-{reason.lower()}.intent.json"
        if intent.exists():
            event = CutInvalidation.model_validate_json(intent.read_bytes())
        else:
            event = CutInvalidation(
                event_id=uuid5(args.operation_id, reason),
                generation_id=args.operation_id,
                attempt_id=attempt,
                previous_event=prior_event,
                certificate=previous,
                reservation=reconciliation_file_reference(reservation_path, "control"),
                owner=owner,
                source_root=args.source,
                source_workspace_id=source.workspace_id,
                destination_root=args.destination,
                destination_workspace_id=ledger.identity.workspace_id,
                database=image,
                wal=None,
                write_generation=evidence,
                historical_seal=reconciliation_file_reference(transition_path, "control"),
                historical_receipt=reconciliation_file_reference(receipt_path, "control"),
                reason=reason,
                owner_absence=evidence,
                exclusivity=evidence,
                no_activation=evidence,
                code_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                policy_sha256=admission.resource_policy_sha256,
                recorded_at=datetime.now(UTC),
            )
            WorkspaceV2ProgressReconciler._write_once(
                intent, event.model_dump_json().encode() + b"\n"
            )
        if event.generation_id != args.operation_id or event.previous_event != prior_event:
            raise MaintenancePhaseError("generation_invalidation_intent_differs")
        prior_event = ledger.invalidate(event, admission=admission)
    prepared_path = args.controls_root / "prepared-generation.intent.json"
    if prepared_path.exists():
        generation = ProgressReconciliationV2.model_validate_json(prepared_path.read_bytes())
    else:
        code = []
        repository = Path(__file__).resolve().parents[1]
        for relative in GENERATION_CODE_PATHS:
            target = args.controls_root / "generation-code" / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            WorkspaceV2ProgressReconciler._write_once(target, (repository / relative).read_bytes())
            code.append(reconciliation_file_reference(target, "code"))
        tree, environment = historical_transition_fingerprints(256, "shared-reference-verifier-v1")
        now = datetime.now(UTC)
        generation = ProgressReconciliationV2(
            generation_id=args.operation_id,
            ordinal=1,
            previous_generation=previous,
            invalidation=prior_event,
            source_root=args.source,
            source_workspace_id=source.workspace_id,
            source_created_at=source.created_at,
            destination_root=args.destination,
            destination_workspace_id=ledger.identity.workspace_id,
            predecessor=legacy.predecessor,
            previous_attempt=reconciliation_file_reference(transition_path, "control"),
            previous_reservation=reconciliation_file_reference(reservation_path, "control"),
            previous_receipts=(reconciliation_file_reference(receipt_path, "control"), *proofs),
            parent_membership=legacy.parent_membership,
            parent_seal=legacy.parent_seal,
            verified_w=core.reconciliation,
            database=image,
            wal=None,
            code=tuple(code),
            tree_fingerprint=tree,
            environment_fingerprint=environment,
            resource_receipt=reconciliation_file_reference(
                args.resource_run_root / "admission.json", "resource"
            ),
            prepared_at=now,
            recorded_at=now,
        )
        WorkspaceV2ProgressReconciler._write_once(
            prepared_path, generation.model_dump_json().encode() + b"\n"
        )
    if generation.generation_id != args.operation_id or generation.invalidation != prior_event:
        raise MaintenancePhaseError("generation_preparation_intent_differs")
    reference = ledger.publish(generation, expected_head_sha256=None, admission=admission)
    return {
        "generation": generation.model_dump(mode="json"),
        "certificate": reference.model_dump(mode="json"),
        "reused": False,
    }


def _invalidate_legacy_reservation(
    args: argparse.Namespace, service: WorkspaceService
) -> dict[str, object]:
    """Close a historical pre-head reservation using C1; never clear its file."""
    from investment_analyst.workspace.workspace_v2_backup import _v2_writer_guard

    ledger = WorkspaceV2GenerationLedger(service, args.destination)
    resolved = ledger.resolve()
    if (
        resolved is None
        or resolved[1].generation_id != args.reconciliation_id
        or resolved[1].phase != "BASELINE_VERIFIED"
        or resolved[1].reserved_successor is not None
    ):
        raise MaintenancePhaseError("orphan_invalidation_requires_unreserved_certified_generation")
    path = _required(args.reservation_path, "reservation_path")
    reservation = _SuccessorReservation.model_validate_json(path.read_bytes())
    baseline = ProgressReconciliation.model_validate_json(ledger._read(reservation.baseline))
    cut = reservation.successor
    if (
        path != ledger.state / f"progress-successor-reservation.{cut.operation_id}.json"
        or reservation.reconciliation_id != args.operation_id
        or baseline.operation_id != args.operation_id
        or reservation.baseline != resolved[1].verified_w
        or baseline.phase != "BASELINE_VERIFIED"
        or baseline.reserved_successor is not None
        or cut.source_root != args.source
        or (service.manifest(args.source).workspace_id, service.manifest(args.source).created_at)
        != (cut.workspace_id, cut.created_at)
    ):
        raise MaintenancePhaseError("orphan_invalidation_reservation_binding_differs")
    old = MaintenanceResourceReceipt.model_validate_json(ledger._read(reservation.resource_receipt))
    owner = CutOwner(
        source_session=reservation.source_session,
        pid=old.pid,
        start_ticks=old.process_start_ticks,
        boot_id=old.boot_id,
        cgroup=old.cgroup_path,
    )
    boot = UUID(Path("/proc/sys/kernel/random/boot_id").read_text().strip())
    absent = old.boot_id != boot
    if not absent:
        try:
            absent = _process_identity(old.pid)[0] != old.process_start_ticks
        except (FileNotFoundError, ProcessLookupError):
            absent = True
    if not absent:
        raise MaintenancePhaseError("orphan_invalidation_owner_still_alive")
    release = LocalReleaseService()
    bindings = release.load_workspace_bindings()
    properties = release.systemctl.show_properties(
        "investment-analyst.service", ("ActiveState", "MainPID", "ExecStart")
    )
    if (
        any(
            b is not None and b.workspace_format_version == 2
            for b in (bindings.current, bindings.previous)
        )
        or properties.get("ActiveState") != "active"
        or properties.get("MainPID") in {None, "0"}
        or str(args.source) not in properties.get("ExecStart", "")
        or str(args.destination) in properties.get("ExecStart", "")
    ):
        raise MaintenancePhaseError("orphan_invalidation_requires_active_v1_no_activation")
    admission = MaintenanceResourceReceipt.model_validate_json(
        (_required(args.resource_run_root, "resource_run_root") / "admission.json").read_bytes()
    )
    ledger._admit(admission)
    if admission.budget.operation_id != args.operation_id:
        raise MaintenancePhaseError("orphan_invalidation_admission_differs")
    evidence_path = args.controls_root / f"orphan-invalidation.{cut.operation_id}.evidence.json"
    intent_path = args.controls_root / f"orphan-invalidation.{cut.operation_id}.intent.json"
    if intent_path.exists():
        event = CutInvalidation.model_validate_json(intent_path.read_bytes())
        if (
            event.attempt_id != cut.operation_id
            or event.certificate.artifact != reservation.baseline
            or event.owner != owner
            or event.expected_head_sha256 != ledger._head_digest()
        ):
            raise MaintenancePhaseError("orphan_invalidation_intent_differs")
    else:
        with _v2_writer_guard(args.destination):
            database = args.destination / "storage/v2/index.duckdb"
            if database.with_suffix(".duckdb.wal").exists():
                raise MaintenancePhaseError("orphan_invalidation_requires_closed_image")
            image = reconciliation_file_reference(database, "database")
            if not evidence_path.exists():
                _write_new_json(
                    evidence_path,
                    {
                        "observed_at": datetime.now(UTC).isoformat(),
                        "owner": owner.model_dump(mode="json"),
                        "owner_absent": absent,
                        "observed_boot_id": str(boot),
                        "active_v1_pid": int(properties["MainPID"]),
                        "database": image.model_dump(mode="json"),
                        "wal_present": False,
                        "v2_binding_present": False,
                        "head": resolved[0].model_dump(mode="json"),
                    },
                )
            observed = json.loads(evidence_path.read_bytes())
            if (
                observed.get("database") != image.model_dump(mode="json")
                or observed.get("owner") != owner.model_dump(mode="json")
                or observed.get("head") != resolved[0].model_dump(mode="json")
            ):
                raise MaintenancePhaseError("orphan_invalidation_evidence_changed")
        evidence = reconciliation_file_reference(evidence_path, "control")
        event = CutInvalidation(
            event_id=uuid5(cut.operation_id, "SESSION_ENDED"),
            generation_id=args.operation_id,
            attempt_id=cut.operation_id,
            expected_head_sha256=ledger._head_digest(),
            certificate=ReconciliationGenerationReference(
                schema_type="workspace-v2-progress-reconciliation-v1", artifact=reservation.baseline
            ),
            reservation=reconciliation_file_reference(path, "control"),
            owner=owner,
            source_root=args.source,
            source_workspace_id=cut.workspace_id,
            destination_root=args.destination,
            destination_workspace_id=ledger.identity.workspace_id,
            database=image,
            wal=None,
            write_generation=evidence,
            historical_seal=None,
            historical_receipt=None,
            reason="SESSION_ENDED",
            owner_absence=evidence,
            exclusivity=evidence,
            no_activation=evidence,
            code_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            policy_sha256=admission.resource_policy_sha256,
            recorded_at=datetime.now(UTC),
        )
        WorkspaceV2ProgressReconciler._write_once(
            intent_path, event.model_dump_json().encode() + b"\n"
        )
    reference = ledger.invalidate(event, admission=admission)
    return {"invalidation": reference.model_dump(mode="json"), "attempt_id": str(cut.operation_id)}


def _verify_generation(args: argparse.Namespace, service: WorkspaceService) -> dict[str, object]:
    ledger = WorkspaceV2GenerationLedger(service, args.destination)
    resolved = ledger.resolve()
    if resolved is None or resolved[1].generation_id != args.operation_id:
        raise MaintenancePhaseError("corrective_verification_requires_prepared_generation")
    generation = resolved[1]
    core = _RecoveredCoreBaseline.model_validate_json(
        _required(args.core_baseline_path, "core_baseline_path").read_bytes()
    )
    if generation.verified_w != core.reconciliation:
        raise MaintenancePhaseError("corrective_verification_original_w_binding_differs")
    continuation = (
        generation.previous_generation.schema_type == "workspace-v2-progress-reconciliation-v2"
    )
    previous_certificate = None
    if continuation:
        previous_certificate = ProgressReconciliationV2.model_validate_json(
            ledger._read(generation.previous_generation.artifact)
        )
        if (
            previous_certificate.phase != "SUCCESSOR_BOUND"
            or previous_certificate.core_proof is None
            or previous_certificate.reserved_successor is None
        ):
            raise MaintenancePhaseError("continuation_verification_requires_revoked_head")
    allowed_verifiers = {
        hashlib.sha256(inspect.getsource(verify_generation_contents).encode()).hexdigest(),
        hashlib.sha256(
            inspect.getsource(verify_generation_contents_typed_baseline).encode()
        ).hexdigest(),
    }
    proof_path = getattr(args, "generation_content_proof", None) or (
        args.controls_root / "corrective-content-proof.json"
    )
    if proof_path.exists():
        proof = GenerationContentProof.model_validate_json(proof_path.read_bytes())
        WorkspaceV2ProgressReconciler._verify(proof.database)
        if (
            proof.generation_id != args.operation_id
            or proof.baseline_certificate != core.reconciliation
            or proof.verifier_sha256 not in allowed_verifiers
            or (continuation and proof.database != generation.database)
        ):
            raise MaintenancePhaseError("corrective_content_proof_binding_differs")
    else:
        if continuation:
            assert previous_certificate is not None
            assert previous_certificate.core_proof is not None
            proof = verify_generation_contents_typed_baseline(
                service,
                generation_id=args.operation_id,
                destination=args.destination,
                expected_database=generation.database,
                baseline_certificate=core.reconciliation,
                baseline_content_proof=previous_certificate.core_proof,
                admission_path=_required(args.resource_run_root, "resource_run_root")
                / "admission.json",
                relationships_path=args.relationships_proof,
            )
        else:
            proof = verify_generation_contents(
                service,
                generation_id=args.operation_id,
                destination=args.destination,
                baseline_root=_required(args.recovery_root, "recovery_root"),
                baseline_certificate=core.reconciliation,
                baseline_database_sha256=core.database.sha256,
                admission_path=_required(args.resource_run_root, "resource_run_root")
                / "admission.json",
                relationships_path=args.relationships_proof,
            )
        WorkspaceV2ProgressReconciler._write_once(
            proof_path, proof.model_dump_json().encode() + b"\n"
        )
    # Content proof alone does not certify raw files or controlled links. The
    # baseline remains PREPARED until those independent gates are integrated.
    return {
        "content_proof": proof.model_dump(mode="json"),
        "proof_path": str(proof_path),
        "baseline_published": False,
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


def _catchup_live(
    args: argparse.Namespace, service: WorkspaceService, current: FrozenSourceCut
) -> dict[str, object]:
    core_path = args.core_baseline_path
    if core_path is None and args.reconciliation_id is not None:
        resolved = WorkspaceV2GenerationLedger(service, args.destination).resolve()
        if resolved is not None and resolved[1].core_proof is not None:
            core_path = resolved[1].core_proof.path
    summary = WorkspaceV2CutoverService(service).catch_up(
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
        core_baseline_path=core_path,
    )
    if getattr(args, "cut_monitor", None) is not None:
        _capture_cut_bytes(args, service, current)
    return summary.model_dump(mode="json")


def _cut_workspace_bytes(root: Path) -> dict[str, int]:
    """Stat the active root once, without hashing/hydrating its historical data."""
    logical = allocated = files = 0
    seen: set[tuple[int, int]] = set()
    for parent, directories, names in os.walk(root, followlinks=False):
        for name in directories:
            if (Path(parent) / name).is_symlink():
                raise MaintenancePhaseError("cut_byte_capture_unsafe_directory")
        for name in names:
            metadata = (Path(parent) / name).lstat()
            if not stat.S_ISREG(metadata.st_mode):
                raise MaintenancePhaseError("cut_byte_capture_unsafe_file")
            identity = (metadata.st_dev, metadata.st_ino)
            if identity in seen:
                continue
            seen.add(identity)
            files += 1
            logical += metadata.st_size
            allocated += metadata.st_blocks * 512
    return {"files": files, "logical_bytes": logical, "linux_allocated_bytes": allocated}


def _capture_cut_bytes(
    args: argparse.Namespace, service: WorkspaceService, cut: FrozenSourceCut
) -> None:
    """Preserve paired measurements before releasing this actual F source reader."""
    session = _live_quiescent_source(args.source, cut)
    if session is None:
        raise MaintenancePhaseError("cut_byte_capture_requires_live_source")
    started = time.monotonic()
    measured: list[dict[str, object]] = []
    for root, version in ((args.source, 1), (args.destination, 2)):
        args.cut_monitor.require_continue()
        with service.open_storage(service.resolve(root), WorkspaceAccessMode.READ_ONLY) as storage:
            payloads = measure_cut_analytical_payloads(
                storage.store.connection, format_version=version
            )
        measured.append(
            {
                "workspace_root": str(root),
                "workspace_id": str(service.manifest(root).workspace_id),
                "format_version": version,
                "workspace_bytes": _cut_workspace_bytes(root),
                "analytical_payloads": [item.to_json_dict() for item in payloads],
            }
        )
    before, after = measured
    if [item["row_count"] for item in before["analytical_payloads"]] != [
        item["row_count"] for item in after["analytical_payloads"]
    ]:
        raise MaintenancePhaseError("cut_byte_capture_counts_differ")
    # A stat-only size capture is not an independent content proof. Retain the
    # same complete catchup proof and live session, rather than invent a scope
    # from equal counts or reuse W2 as the final F denominator.
    _live_quiescent_source(args.source, cut)
    args.cut_monitor.require_continue()
    _write_new_json(
        args.controls_root / "paired-cut-byte-capture.json",
        {
            "operation_id": str(args.operation_id),
            "source_cut": cut.model_dump(mode="json"),
            "source_session": str(session.writer_generation),
            "cutover_checkpoint": reconciliation_file_reference(
                args.controls_root / "cutover-checkpoint.json", "control"
            ).model_dump(mode="json"),
            "collector_code": reconciliation_file_reference(
                Path(inspect.getfile(measure_cut_analytical_payloads)), "code"
            ).model_dump(mode="json"),
            "pairs": measured,
            "collector_elapsed_seconds": time.monotonic() - started,
            "measured_at": datetime.now(UTC).isoformat(),
        },
    )


def _cut_advance(
    args: argparse.Namespace, phase: str, step: str, **references: ReconciliationFileReference
) -> None:
    from typing import cast

    from investment_analyst.workspace.workspace_v2_migration_models import CutWindowPhase

    path = _required(args.cut_window, "cut_window")
    WorkspaceV2CutWindowService(path).advance(
        cast(CutWindowPhase, phase),
        expected=reconciliation_file_reference(path, "control"),
        boot_id=_process_identity(os.getpid())[1],
        monotonic_ns=time.monotonic_ns(),
        at=datetime.now(UTC),
        step=step,
        **references,
    )


def _cut_service_identity(args: argparse.Namespace, *, v2: bool) -> dict[str, object]:
    """Observe the actual process and HTTP identity, not just deployment intent."""
    release = LocalReleaseService()
    root = args.destination if v2 else args.source
    identity = WorkspaceService().manifest(root)
    properties = release.systemctl.show_properties(
        "investment-analyst.service", ("ActiveState", "MainPID", "ExecStart", "WorkingDirectory")
    )
    pid = int(properties.get("MainPID", "0"))
    if properties.get("ActiveState") != "active" or not pid:
        raise MaintenancePhaseError("cut_service_not_active")
    start, boot, cgroup = _process_identity(pid)
    command = (Path("/proc") / str(pid) / "cmdline").read_bytes().split(b"\0")
    arguments = [value.decode() for value in command if value]
    if "--workspace" not in arguments or arguments[arguments.index("--workspace") + 1] != str(root):
        raise MaintenancePhaseError("cut_live_workspace_root_differs")
    if v2:
        bindings = release.load_workspace_bindings()
        binding = bindings.current
        if (
            binding is None
            or binding.workspace_root != root
            or binding.workspace_id != identity.workspace_id
            or binding.workspace_format_version != 2
            or binding.release_sha != INTEGRATED_RELEASE
            or identity.format_version != 2
            or properties.get("WorkingDirectory")
            != str(release.paths.releases / INTEGRATED_RELEASE)
        ):
            raise MaintenancePhaseError("cut_live_v2_binding_differs")
    statuses: dict[str, int] = {}
    for endpoint in ("/api/v1/overview", "/api/v1/candidate-notifications"):
        with urllib.request.urlopen("http://127.0.0.1:8765" + endpoint, timeout=5) as response:
            statuses[endpoint] = response.status
            if response.status != 200:
                raise MaintenancePhaseError("cut_http_health_failed")
            if endpoint.endswith("overview"):
                overview = json.load(response)
                if overview.get("workspace_id") != str(identity.workspace_id):
                    raise MaintenancePhaseError("cut_http_workspace_identity_differs")
    if _process_identity(pid) != (start, boot, cgroup):
        raise MaintenancePhaseError("cut_service_restarted_during_health")
    return {
        "workspace_root": str(root),
        "workspace_id": str(identity.workspace_id),
        "format_version": identity.format_version,
        "pid": pid,
        "start_ticks": start,
        "boot_id": str(boot),
        "cgroup": str(cgroup),
        "unit": properties,
        "http": statuses,
        "observed_at": datetime.now(UTC).isoformat(),
    }


def _cut_terminal(args: argparse.Namespace, *, v2: bool) -> dict[str, object]:
    observed = _cut_service_identity(args, v2=v2)
    health_document = json.dumps(observed, sort_keys=True).encode() + b"\n"
    path = args.controls_root / (
        ("cut-v2-health." if v2 else "cut-v1-health.")
        + hashlib.sha256(health_document).hexdigest()
        + ".json"
    )
    WorkspaceV2ProgressReconciler._write_once(path, health_document)
    evidence = reconciliation_file_reference(path, "control")
    binding_path = args.controls_root / "cut-v2-binding.json"
    if v2:
        WorkspaceV2ProgressReconciler._write_once(
            binding_path, LocalReleaseService().paths.workspace_binding_file.read_bytes()
        )
    phase = "HEALTHY_V2" if v2 else "HEALTHY_V1"
    window_path = _required(args.cut_window, "cut_window")
    window = WorkspaceV2CutWindowService(window_path).load()
    if (
        window.boot_id != _process_identity(os.getpid())[1]
        or time.monotonic_ns() > window.deadline_monotonic_ns
    ):
        phase = "ABORTED"
    if window.phase != phase:
        _cut_advance(
            args,
            phase,
            "verified_http_and_runtime",
            **({"binding": reconciliation_file_reference(binding_path, "control")} if v2 else {}),
        )
    receipt = CutWindowReceipt(
        window=reconciliation_file_reference(window_path, "control"),
        phase=phase,
        completed_monotonic_ns=time.monotonic_ns(),
        completed_at=datetime.now(UTC),
        identity=evidence,
        health=evidence,
    )
    terminal = args.controls_root / "cut-window-receipt.json"
    WorkspaceV2ProgressReconciler._write_once(terminal, receipt.model_dump_json().encode() + b"\n")
    return {"window_receipt": receipt.model_dump(mode="json"), "health": observed}


def _cut_step(
    args: argparse.Namespace, phase: str, *, action: str = "plan", readback: bool = False
) -> dict[str, object]:
    monitor = args.cut_monitor
    monitor.require_continue()
    child = argparse.Namespace(**vars(args))
    child.phase, child.delta_action = phase, action
    child.resource_run_root = _required(args.resource_run_root, "resource_run_root") / (
        phase + "-" + action
    )
    child.resource_run_root.mkdir()
    if readback:
        child.resource_budget = args.cut_readback_budget
    started = time.monotonic()
    with _worker_resources(child):
        result = _execute(child)
    monitor.require_continue()
    _write_new_json(
        child.resource_run_root / "phase-result.json",
        {
            "phase": phase,
            "action": action,
            "elapsed_seconds": time.monotonic() - started,
            "result": result,
        },
    )
    return result


def _run_global_cut(args: argparse.Namespace) -> dict[str, object]:
    """One MainPID owns stop, the fresh source session and every phase's clock."""
    args.cut_monitor = monitor = _CutMonitor(args)
    service = WorkspaceService()
    release = LocalReleaseService()
    window_path = _required(args.cut_window, "cut_window")
    if window_path.exists():
        raise MaintenancePhaseError("cut_window_cannot_be_restarted")
    run = _required(args.resource_run_root, "resource_run_root")
    preflight = argparse.Namespace(**vars(args))
    preflight.resource_run_root = run / "preflight"
    preflight.resource_run_root.mkdir()
    monitor.start_ready(CUT_MONITOR_READINESS_SECONDS)
    # All admission and fresh authority checks happen with v1 still active.
    with _worker_resources(preflight):
        go_path = _required(args.cut_go, "cut_go")
        go = CutWindowGo.model_validate_json(go_path.read_bytes())
        if (
            go.window_limit_seconds == CUT_WINDOW_EXCEPTION_SECONDS
            and go.attempt_id != args.operation_id
        ):
            raise MaintenancePhaseError("cut_window_authorization_attempt_differs")
        ledger = WorkspaceV2GenerationLedger(service, args.destination)
        resolved = ledger.resolve()
        if resolved is None:
            raise MaintenancePhaseError("cut_requires_certified_baseline")
        head, baseline = resolved
        if (
            baseline.phase != "BASELINE_VERIFIED"
            or baseline.reserved_successor is not None
            or head.certificate != go.baseline
            or baseline.generation_id != args.reconciliation_id
            or baseline.source_workspace_id != go.source_workspace_id
            or baseline.destination_workspace_id != go.destination_workspace_id
        ):
            raise MaintenancePhaseError("cut_go_baseline_differs")
        # Fail before the window/stop when a same-baseline reservation still
        # lacks durable revocation. The real reservation rechecks this under
        # its own guards; this preflight never opens a DB or writes a receipt.
        _reject_conflicting_successor_reservation(ledger, head)
        prepared_root = _required(args.prepared_delta, "prepared_delta")
        prepared_path = prepared_root / "delta-manifest.json"
        prepared = PhysicalDeltaManifest.model_validate_json(prepared_path.read_bytes())
        prepared_reference = reconciliation_file_reference(prepared_path, "control")
        if (
            prepared.epoch != baseline.generation_id
            or prepared.source_root != args.destination
            or prepared.source_workspace_id != baseline.destination_workspace_id
            or prepared_reference not in go.prerequisites
        ):
            raise MaintenancePhaseError("cut_prepared_delta_baseline_differs")
        database = next(
            (
                item
                for item in _iter_delta_files(prepared_root / "files.jsonl")
                if item.path == "storage/v2/index.duckdb"
            ),
            None,
        )
        if database is None or (database.sha256, database.size_bytes) != (
            baseline.database.sha256,
            baseline.database.size_bytes,
        ):
            raise MaintenancePhaseError("cut_prepared_database_differs")
        _cut_service_identity(args, v2=False)
        guard = subprocess.run(
            [
                sys.executable,
                str(Path(__file__).with_name("check_workflow_guards.py")),
                "--live",
                "--phase",
                "build",
                "--repo",
                "Marjuraru/investment-analyst",
                "--issue",
                "345",
            ],
            capture_output=True,
            text=True,
            timeout=60,
            check=True,
        )
        authority = json.loads(guard.stdout)
        if authority.get("authoritative") is not True or authority.get("decision") != "CONTINUE":
            raise MaintenancePhaseError("cut_live_guard_did_not_continue")
        guard_path = run / "cut-live-guard.json"
        _write_new_json(guard_path, authority)
        capacity_path = run / "cut-live-capacity.json"
        capacity = _capacity()
        if capacity < _load_resource_budget(args).required_host_bytes:
            raise MaintenancePhaseError("cut_host_reserve_unavailable")
        _write_new_json(
            capacity_path, {"host_free_bytes": capacity, "at": datetime.now(UTC).isoformat()}
        )
        monitor.require_continue()
        go = go.model_copy(
            update={
                "guard": reconciliation_file_reference(guard_path, "control"),
                "capacity": reconciliation_file_reference(capacity_path, "control"),
                "admitted_at": datetime.now(UTC),
            }
        )
        admitted = run / "cut-admitted-go.json"
        _write_new_json(admitted, go.model_dump(mode="json"))
        intent = _QuiescentSourceIntent(args.source, args.operation_id)
        start, boot, cgroup = _process_identity(os.getpid())
        now = time.monotonic_ns()
        unit_started = int(_unit_properties().get("ActiveEnterTimestampMonotonic", "0")) * 1000
        if unit_started <= 0 or unit_started > now:
            raise MaintenancePhaseError("cut_unit_runtime_origin_unavailable")
        prefix_seconds = (now - unit_started + 999_999_999) // 1_000_000_000
        if not go.admits_prefix(prefix_seconds):
            raise MaintenancePhaseError("cut_unit_prefix_exceeds_measured_go")
        window = CutWindow(
            window_id=uuid4(),
            attempt_id=args.operation_id,
            source_root=args.source,
            source_workspace_id=go.source_workspace_id,
            destination_root=args.destination,
            destination_workspace_id=go.destination_workspace_id,
            release_sha=INTEGRATED_RELEASE,
            owner=CutOwner(
                source_session=intent.writer_generation,
                pid=os.getpid(),
                start_ticks=start,
                boot_id=boot,
                cgroup=cgroup,
            ),
            boot_id=boot,
            started_monotonic_ns=now,
            deadline_monotonic_ns=now + go.window_limit_seconds * 1_000_000_000,
            stop_started_at=datetime.now(UTC),
            recovery_reserve_seconds=next(
                b.upper_seconds for b in go.bounds if b.step == "cancel_close_restart"
            ),
            window_limit_seconds=go.window_limit_seconds,
            window_authorization=go.window_authorization,
            go=reconciliation_file_reference(admitted, "control"),
            baseline=head.certificate,
            code=reconciliation_file_reference(run / "code/scripts/close_data_chassis.py", "code"),
            environment_sha256=baseline.environment_fingerprint,
            resource_receipt=reconciliation_file_reference(
                preflight.resource_run_root / "admission.json", "resource"
            ),
            step="ready_to_stop",
            updated_at=datetime.now(UTC),
        )
        WorkspaceV2CutWindowService(window_path).prepare(window, go)
    try:
        monitor.require_continue()
        _cut_advance(args, "STOPPING", "normal_service_stop")
        release.systemctl.stop("investment-analyst.service")
        with ExitStack() as source_lease:
            with _worker_resources_for_freeze(args):
                cut = source_lease.enter_context(
                    quiescent_source_cut(
                        service, args.source, operation_id=args.operation_id, _session_intent=intent
                    )
                )
                _write_new_json(
                    args.controls_root / "frozen-source-cut.json", cut.model_dump(mode="json")
                )
                args.live_source_cut = cut
                _cut_advance(args, "QUIESCENT", "fresh_source_session")
                _cut_advance(args, "APPLYING", "catchup")
                _cut_step(args, "catchup")
                # End the source instance before switching executions/profiles;
                # the original source lease and writer exclusion remain held.
                close_quiescent_native_reader(args.source, cut)
            _cut_step(args, "delta", action="plan")
            _cut_step(args, "delta", action="upload")
            _cut_step(args, "delta", action="restore", readback=True)
            recovery = reconciliation_file_reference(
                _required(args.delta_root, "delta_root") / "restore-receipt.json", "control"
            )
            args.recovery_receipt = recovery.path
            _cut_advance(
                args,
                "RECOVERY_VERIFIED",
                "same_epoch_external_recovery",
                recovery_receipt=recovery,
            )
            intention_path = args.controls_root / "activation-intent.json"
            _write_new_json(
                intention_path,
                {
                    "operation_id": str(args.operation_id),
                    "release_sha": INTEGRATED_RELEASE,
                    "workspace_root": str(args.destination),
                    "recovery": recovery.model_dump(mode="json"),
                    "previous_state": release.load_deployment_state().model_dump(mode="json"),
                    "previous_bindings": release.load_workspace_bindings().model_dump(mode="json"),
                },
            )
            _cut_advance(
                args,
                "ACTIVATING",
                "durable_activation_intention",
                activation_intent=reconciliation_file_reference(intention_path, "control"),
            )
            _cut_step(args, "activate")
            return _cut_terminal(args, v2=True)
    finally:
        monitor.close()


@contextmanager
def _worker_resources_for_freeze(args: argparse.Namespace) -> Iterator[None]:
    # Admission stays live while entering the quiescent native reader; child
    # phases install their own measurements without replacing the source lease.
    freeze = argparse.Namespace(**vars(args))
    freeze.resource_run_root = _required(args.resource_run_root, "resource_run_root") / "freeze"
    freeze.resource_run_root.mkdir()
    with _worker_resources(freeze):
        yield


def _invalidate_cut_owner(args: argparse.Namespace, window: CutWindow) -> dict[str, object]:
    """C1 keeps the reservation, the current DB and any valid WAL after owner loss."""
    from investment_analyst.workspace.workspace_v2_backup import _v2_writer_guard

    service = WorkspaceService()
    ledger = WorkspaceV2GenerationLedger(service, args.destination)
    if ledger.attempt_invalidated(window.attempt_id):
        return {"invalidation": "already_recorded"}
    path = args.destination / "state" / f"progress-successor-reservation.{window.attempt_id}.json"
    if not path.exists():
        return {"invalidation": "no_reservation_created"}
    reservation = _SuccessorReservation.model_validate_json(path.read_bytes())
    if reservation.source_session != window.owner.source_session:
        raise MaintenancePhaseError("cut_recovery_reservation_owner_differs")
    resolved = ledger.resolve()
    if resolved is None:
        raise MaintenancePhaseError("cut_recovery_head_missing")
    head, generation = resolved
    admission = MaintenanceResourceReceipt.model_validate_json(
        (_required(args.resource_run_root, "resource_run_root") / "admission.json").read_bytes()
    )
    with _v2_writer_guard(args.destination):
        database = reconciliation_file_reference(
            args.destination / "storage/v2/index.duckdb", "database"
        )
        wal_path = args.destination / "storage/v2/index.duckdb.wal"
        wal = reconciliation_file_reference(wal_path, "wal") if wal_path.exists() else None
        evidence_path = args.resource_run_root / "owner-loss-evidence.json"
        _write_new_json(
            evidence_path,
            {
                "window": reconciliation_file_reference(
                    _required(args.cut_window, "cut_window"), "control"
                ).model_dump(mode="json"),
                "database": database.model_dump(mode="json"),
                "wal": None if wal is None else wal.model_dump(mode="json"),
                "health": _cut_service_identity(args, v2=False),
                "owner": window.owner.model_dump(mode="json"),
                "head": head.model_dump(mode="json"),
            },
        )
    evidence = reconciliation_file_reference(evidence_path, "control")
    event = CutInvalidation(
        event_id=uuid4(),
        generation_id=generation.generation_id,
        attempt_id=window.attempt_id,
        expected_head_sha256=ledger._head_digest(),
        certificate=ReconciliationGenerationReference(
            schema_type="workspace-v2-progress-reconciliation-v2", artifact=head.certificate
        ),
        reservation=reconciliation_file_reference(path, "control"),
        owner=window.owner,
        source_root=args.source,
        source_workspace_id=window.source_workspace_id,
        destination_root=args.destination,
        destination_workspace_id=window.destination_workspace_id,
        database=database,
        wal=wal,
        write_generation=evidence,
        historical_seal=None,
        historical_receipt=None,
        reason="SESSION_ENDED",
        owner_absence=evidence,
        exclusivity=evidence,
        no_activation=evidence,
        code_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        policy_sha256=admission.resource_policy_sha256,
        recorded_at=datetime.now(UTC),
    )
    reference = ledger.invalidate(event, admission=admission)
    return {"invalidation": reference.model_dump(mode="json")}


def _recover_v2_activation(args: argparse.Namespace, window: CutWindow) -> dict[str, object]:
    from investment_analyst.application.local_release import (
        DeploymentState,
        DeploymentWorkspaceBindings,
    )
    from investment_analyst.storage.workspace_v2_inventory import _reconciliation_lock

    if window.activation_intent is None or window.recovery_receipt is None:
        raise MaintenancePhaseError("cut_v2_recovery_intention_missing")
    for reference in (window.activation_intent, window.recovery_receipt, window.code):
        WorkspaceV2ProgressReconciler._verify(reference)
    intention = json.loads(window.activation_intent.path.read_bytes())
    if (
        intention.get("operation_id") != str(args.operation_id)
        or intention.get("workspace_root") != str(args.destination)
        or intention.get("release_sha") != INTEGRATED_RELEASE
        or intention.get("recovery") != window.recovery_receipt.model_dump(mode="json")
    ):
        raise MaintenancePhaseError("cut_v2_recovery_intention_differs")
    # No maintenance DB is opened here: compatible runtime recovery owns its
    # own writer. Never replay preactivation rollback once this phase exists.
    with _reconciliation_lock(args.controls_root):
        LocalReleaseService().recover_interrupted_v2_activation(
            INTEGRATED_RELEASE,
            workspace_root=args.destination,
            previous_state=DeploymentState.model_validate(intention["previous_state"]),
            previous_bindings=DeploymentWorkspaceBindings.model_validate_json(
                json.dumps(intention["previous_bindings"])
            ),
            deadline_seconds=(
                min(
                    window.recovery_reserve_seconds,
                    max(0, (window.deadline_monotonic_ns - time.monotonic_ns()) / 1e9),
                )
                if window.boot_id == _process_identity(os.getpid())[1]
                else window.recovery_reserve_seconds
            ),
        )
    return _cut_terminal(args, v2=True)


def _recover_global_cut(args: argparse.Namespace) -> dict[str, object]:
    """ExecStopPost runs after the MainPID and all its native handles are gone."""
    properties = _unit_properties()
    if (
        not args.supervised_worker
        or properties.get("ControlPID") != str(os.getpid())
        or properties.get("ActiveState") not in {"deactivating", "failed"}
        or properties.get("MainPID", "0") != "0"
    ):
        raise MaintenancePhaseError("cut_recovery_is_not_owned_stop_hook")
    window_path = _required(args.cut_window, "cut_window")
    if not window_path.exists():
        return {"recovery": "preflight_ended_before_stop"}
    window = WorkspaceV2CutWindowService(window_path).load()
    if (
        window.attempt_id != args.operation_id
        or window.source_root != args.source
        or window.destination_root != args.destination
    ):
        raise MaintenancePhaseError("cut_recovery_window_binding_differs")
    if window.owner.boot_id == _process_identity(os.getpid())[1]:
        try:
            alive = _process_identity(window.owner.pid)[0] == window.owner.start_ticks
        except (FileNotFoundError, ProcessLookupError):
            alive = False
        if alive:
            raise MaintenancePhaseError("cut_recovery_owner_still_alive")
    terminal = args.controls_root / "cut-window-receipt.json"
    if window.phase in {"HEALTHY_V1", "HEALTHY_V2"} and terminal.exists():
        result = {"recovery": "terminal_reused", "receipt": str(terminal)}
        if window.phase == "HEALTHY_V2":
            _cut_service_identity(args, v2=True)
            return result
    if window.phase in {"ACTIVATING", "HEALTHY_V2"}:
        # An activation may already have accepted writes. A missing final flag
        # is never permission to restore an older root or restart v1.
        return _recover_v2_activation(args, window)
    release = LocalReleaseService()
    bindings = release.load_workspace_bindings()
    unit = release.systemctl.show_properties("investment-analyst.service", ("ExecStart",))
    if (
        any(
            b is not None and b.workspace_format_version == 2
            for b in (bindings.current, bindings.previous)
        )
        or str(args.destination) in unit.get("ExecStart", "")
        or str(args.source) not in unit.get("ExecStart", "")
    ):
        raise MaintenancePhaseError("cut_return_v1_actual_binding_is_ambiguous")
    changed_boot = window.boot_id != _process_identity(os.getpid())[1]
    if not changed_boot and window.phase not in {"RETURNING_V1", "HEALTHY_V1", "ABORTED"}:
        if window.phase == "PREPARED":
            _cut_advance(args, "ABORTED", "stop_was_not_requested")
            return {
                "recovery": "aborted_before_stop",
                "health": _cut_service_identity(args, v2=False),
            }
        _cut_advance(args, "RETURNING_V1", "owner_lost_return_source")
    if not release.systemctl.is_active("investment-analyst.service"):
        release.systemctl.restart("investment-analyst.service")
    healthy, _ = release.health_checker.check_health_readiness(
        port=8765,
        endpoints=("/api/v1/overview", "/api/v1/candidate-notifications"),
        deadline_seconds=max(
            1,
            min(
                window.recovery_reserve_seconds,
                (window.deadline_monotonic_ns - time.monotonic_ns()) / 1e9,
            ),
        ),
    )
    if not healthy:
        raise MaintenancePhaseError("cut_return_v1_health_failed")
    # Restore availability before resource admission. Low disk/memory must not
    # prevent returning the old service when no activation was attempted.
    result = _cut_terminal(args, v2=False) if not terminal.exists() else {"receipt": str(terminal)}
    recovery = argparse.Namespace(**vars(args))
    recovery.resource_run_root = _required(args.resource_run_root, "resource_run_root") / (
        "recovery-" + uuid4().hex
    )
    recovery.resource_run_root.mkdir()
    with _worker_resources(recovery):
        result.update(_invalidate_cut_owner(recovery, window))
    return result


def _execute(args: argparse.Namespace) -> dict[str, object]:
    if args.phase == "resource-check":
        # Let the parent observe a live bounded worker; no database is opened.
        time.sleep(2)
        return {"resource_admission": "verified", "database_opened": False}
    service = WorkspaceService()
    if args.phase == "repair-publication":
        from investment_analyst.workspace.workspace_v2_migration_models import (
            LedgerPublicationRepairInput,
        )

        request_path = _required(args.publication_repair_input, "publication_repair_input")
        request = LedgerPublicationRepairInput.model_validate_json(request_path.read_bytes())
        if (
            request.repair_id != args.operation_id
            or request.source_root != args.source
            or request.destination_root != args.destination
            or request.generation_id != args.reconciliation_id
            or request.source_workspace_id != service.manifest(args.source).workspace_id
            or request.destination_workspace_id != service.manifest(args.destination).workspace_id
        ):
            raise MaintenancePhaseError("publication_repair_command_binding_differs")
        admission_path = _required(args.resource_run_root, "resource_run_root") / "admission.json"
        admission = MaintenanceResourceReceipt.model_validate_json(admission_path.read_bytes())
        health = _cut_service_identity(args, v2=False)
        result = WorkspaceV2GenerationLedger(service, args.destination).repair_publication(
            request,
            intent_path=request_path,
            admission=admission,
            dry_run=args.dry_run,
        )
        return {"publication_repair": result, "v1_health": health}
    if args.phase == "retire-owned-payload":
        request = OwnedPayloadRetirementInput.model_validate_json(
            _required(args.owned_payload_input, "owned_payload_input").read_bytes()
        )
        if (
            request.operation_id != args.operation_id
            or request.retained_controls_root != args.controls_root
        ):
            raise MaintenancePhaseError("owned_payload_operation_binding_differs")
        receipt = WorkspaceV2LifecycleService(service).retire_owned_payload(request)
        return {"owned_payload_retirement": receipt.model_dump(mode="json")}
    if args.phase == "reconcile-generation":
        return _prepare_generation(args, service)
    if args.phase == "invalidate-reservation":
        return _invalidate_legacy_reservation(args, service)
    if args.phase == "verify-generation":
        return _verify_generation(args, service)
    if args.phase == "retire-preparatory":
        proposal = PreparatorySlotRetirementReceipt.model_validate_json(
            _required(
                args.preparatory_retirement_input, "preparatory_retirement_input"
            ).read_bytes()
        )
        if (
            proposal.operation_id != args.operation_id
            or proposal.retained_root != args.destination
            or proposal.source_root == args.source
            or proposal.retained_generation_id != args.reconciliation_id
        ):
            raise MaintenancePhaseError("preparatory_retirement_command_binding_differs")
        return (
            WorkspaceV2LifecycleService(service)
            .retire_preparatory_slot(
                proposal,
                admission_path=_required(args.resource_run_root, "resource_run_root")
                / "admission.json",
                checkpoint_path=args.controls_root / "preparatory-retirement-checkpoint.json",
                proof_path=args.controls_root / "preparatory-retirement-proof.json",
                unlink_root=_required(args.immutable_link_journal, "immutable_link_journal"),
                journal_byte_budget=args.journal_byte_budget,
            )
            .model_dump(mode="json")
        )
    if args.phase == "certify-generation":
        ledger = WorkspaceV2GenerationLedger(service, args.destination)
        resolved = ledger.resolve()
        if resolved is None or resolved[1].generation_id != args.operation_id:
            raise MaintenancePhaseError("baseline_generation_owner_differs")
        code = []
        if resolved[1].phase == "PREPARED":
            repository = Path(__file__).resolve().parents[1]
            for relative in GENERATION_CODE_PATHS:
                document = (repository / relative).read_bytes()
                digest = hashlib.sha256(document).hexdigest()
                path = args.controls_root / "certified-code" / digest / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                WorkspaceV2ProgressReconciler._write_once(path, document)
                code.append(reconciliation_file_reference(path, "code"))
        else:
            code.extend(resolved[1].code)
        certificate = ledger.certify_baseline(
            content_path=args.generation_content_proof
            or (args.controls_root / "corrective-content-proof.json"),
            raw_coverage_path=args.controls_root / "generation-raw-coverage.json",
            relationships_path=_required(args.relationships_proof, "relationships_proof"),
            admission_path=_required(args.resource_run_root, "resource_run_root")
            / "admission.json",
            code=tuple(code),
        )
        return {
            "certificate": certificate.model_dump(mode="json"),
            "generation": ledger.resolve()[1].model_dump(mode="json"),
        }
    if args.phase == "verify-generation-raw":
        document = json.loads(
            _required(args.link_witness_receipt, "link_witness_receipt").read_bytes()
        )
        if (
            document.get("schema_version") != "data-chassis-maintenance-phase-receipt-v1"
            or document.get("action") != "prepare-links"
            or document.get("operation_id") != str(args.operation_id)
            or document.get("destination_root") != str(args.destination)
        ):
            raise MaintenancePhaseError("generation_link_witness_receipt_binding_differs")
        witness = ReconciliationFileReference.model_validate_json(
            json.dumps(document["result"]["immutable_link_journal"])
        )
        proof = verify_generation_raw_files(
            service,
            destination=args.destination,
            content_path=args.generation_content_proof
            or (args.controls_root / "corrective-content-proof.json"),
            link_witness=witness,
            admission_path=_required(args.resource_run_root, "resource_run_root")
            / "admission.json",
        )
        path = args.controls_root / "generation-raw-coverage.json"
        WorkspaceV2ProgressReconciler._write_once(path, proof.model_dump_json().encode() + b"\n")
        return {"raw_coverage": proof.model_dump(mode="json"), "proof_path": str(path)}
    if args.phase == "prepare-links":
        proof, count = WorkspaceV2PhysicalDeltaService(service).prepare_existing_link_witness(
            _required(args.delta_root, "delta_root"),
            _required(args.recovery_root, "recovery_root"),
            _required(args.immutable_link_journal, "immutable_link_journal"),
            operation_id=args.operation_id,
            parent_manifest=_required(args.parent_manifest, "parent_manifest"),
            parent_index=_required(args.parent_index, "parent_index"),
            parent_proof=_required(args.immutable_parent_proof, "immutable_parent_proof"),
            admission_path=_required(args.resource_run_root, "resource_run_root")
            / "admission.json",
            byte_budget=args.journal_byte_budget,
        )
        return {"immutable_link_journal": proof.model_dump(mode="json"), "confirmed_links": count}
    if args.phase in {"catchup", "delta", "activate"}:
        _admit_cut_command(args, service)
    if args.phase == "recovery-readback":
        _, manifest = _delta(args)
        if args.coverage_only:
            # Measure the changed coverage insertion on the retained W2 photo,
            # without recreating its retired slot, re-reading Drive or calling
            # the installed reader again. This is explicitly NOT a new restore.
            resolved = WorkspaceV2GenerationLedger(service, args.destination).resolve()
            if (
                resolved is None
                or resolved[1].generation_id != manifest.epoch
                or resolved[1].phase != "BASELINE_VERIFIED"
                or resolved[1].reserved_successor is not None
                or manifest.historical_state != "APPLYING"
                or args.recovery_root != args.destination
                or resolved[1].destination_workspace_id != manifest.source_workspace_id
            ):
                raise MaintenancePhaseError("coverage_measurement_requires_retained_w2")
            budget = _load_resource_budget(args)
            if (
                len(budget.assignments) != 1
                or budget.assignments[0].database != args.destination / "storage/v2/index.duckdb"
            ):
                raise MaintenancePhaseError("coverage_measurement_requires_single_candidate")
            service = WorkspaceService(
                resource_policies={budget.assignments[0].database: budget.assignments[0].policy}
            )
            coverage_started = time.monotonic()
            with service.open_storage(
                service.resolve(args.destination), WorkspaceAccessMode.READ_ONLY
            ) as storage:
                coverage = _verify_full_recovery_coverage(
                    args.destination,
                    args.delta_root / "files.jsonl",
                    manifest,
                    storage.store.connection,
                )
                coverage_seconds = time.monotonic() - coverage_started
                payload_started = time.monotonic()
                payloads = measure_cut_analytical_payloads(
                    storage.store.connection, format_version=2
                )
                payload_seconds = time.monotonic() - payload_started
            return {
                "coverage_only": True,
                "new_restore": False,
                "generation_certificate": resolved[0].certificate.model_dump(mode="json"),
                "coverage": coverage,
                "coverage_elapsed_seconds": coverage_seconds,
                "payload_capture_elapsed_seconds": payload_seconds,
                "analytical_payloads": [item.to_json_dict() for item in payloads],
                "verifier_sha256": hashlib.sha256(
                    inspect.getsource(_verify_full_recovery_coverage).encode()
                ).hexdigest(),
            }
        receipt = PhysicalDeltaReceipt.model_validate_json(
            _required(args.recovery_receipt, "recovery_receipt").read_bytes()
        )
        return _complete_recovery(args, receipt, manifest)
    if args.phase == "prepare-delta":
        preparation_binding = {}
        # W remains immutable. A C1 generation prepares a distinct W2 epoch;
        # the legacy path below continues to read only its preserved W photo.
        generation_id = getattr(args, "reconciliation_id", None)
        if generation_id is not None:
            ledger = WorkspaceV2GenerationLedger(service, args.destination)
            resolved = ledger.resolve()
            if resolved is None:
                raise MaintenancePhaseError("prepared_delta_generation_head_missing")
            baseline_ref, baseline = resolved[0].certificate, resolved[1]
            if (
                baseline.generation_id != generation_id
                or args.operation_id != generation_id
                or baseline.phase != "BASELINE_VERIFIED"
                or baseline.reserved_successor is not None
                or baseline.source_root != args.source
                or baseline.destination_root != args.destination
                or baseline.core_proof is None
            ):
                raise MaintenancePhaseError("prepared_delta_requires_certified_unreserved_w2")
            content = GenerationContentProof.model_validate_json(ledger._read(baseline.core_proof))
            if (
                content.database != baseline.database
                or content.generation_id != generation_id
                or content.verifier_sha256
                != hashlib.sha256(
                    inspect.getsource(verify_generation_contents).encode()
                ).hexdigest()
            ):
                raise MaintenancePhaseError("prepared_delta_corrective_verifier_differs")
            for reference in (baseline.database, *baseline.corrective_proofs):
                WorkspaceV2ProgressReconciler._verify(reference)
            baseline_database = baseline.database
            preparation_binding = {"generation_certificate": baseline_ref.model_dump(mode="json")}
        else:
            proof_path = _required(args.core_baseline_path, "core_baseline_path")
            core = _RecoveredCoreBaseline.model_validate_json(proof_path.read_bytes())
            WorkspaceV2ProgressReconciler._verify(core.reconciliation)
            baseline = ProgressReconciliation.model_validate_json(
                core.reconciliation.path.read_bytes()
            )
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
            baseline_database = core.database
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
            if args.delta_action == "measure-comparison":
                if generation_id is None:
                    raise MaintenancePhaseError("comparison_measurement_requires_certified_w2")
                return {
                    "preparation_only": True,
                    **measure_recovered_comparison(
                        service,
                        args.source,
                        args.destination,
                        generation_id,
                        _required(args.resource_run_root, "resource_run_root") / "admission.json",
                    ),
                }
            if args.delta_action == "measure-plan":
                return {
                    "preparation_only": True,
                    **preparation_binding,
                    "measurement": physical.measure_prepared_plan(
                        args.destination,
                        _required(args.parent_index, "parent_index"),
                        root,
                        link_witness=reconciliation_file_reference(
                            _required(args.immutable_link_witness, "immutable_link_witness"),
                            "control",
                        ),
                    ),
                }
            if args.delta_action == "upload":
                transfer = physical.upload(
                    root,
                    _transport(args),
                    checkpoint_path=root / "upload-checkpoint.json",
                    receipt_path=root / "upload-receipt.json",
                    batch_transfer=getattr(args, "independent_read_root", None) is not None,
                    **_independent_read_options(args),
                )
                return {
                    "preparation_only": True,
                    **preparation_binding,
                    "transfer": transfer.model_dump(mode="json"),
                }
            controlled_options: dict[str, object] = {}
            if generation_id is not None:
                if not args.reuse_parent_immutables:
                    raise MaintenancePhaseError("generation_restore_requires_controlled_links")
                if args.journal_byte_budget > _load_resource_budget(args).cache_and_control_bytes:
                    raise MaintenancePhaseError("generation_restore_journal_exceeds_admission")
                controlled_options = {
                    "immutable_link_journal": _required(
                        args.immutable_link_journal, "immutable_link_journal"
                    ),
                    "immutable_link_witness": reconciliation_file_reference(
                        _required(args.immutable_link_witness, "immutable_link_witness"), "control"
                    ),
                    "journal_byte_budget": args.journal_byte_budget,
                }
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
                **controlled_options,
                **_independent_read_options(args),
            )
            return {
                "preparation_only": True,
                **preparation_binding,
                "restore": restored.model_dump(mode="json"),
            }
        result = physical.plan(
            args.destination,
            _required(args.parent_index, "parent_index"),
            root,
            parent_manifest_sha256=args.parent_manifest_sha256,
            epoch=args.operation_id,
            allow_incomplete_historical=True,
            immutable_parent_proof=_required(args.immutable_parent_proof, "immutable_parent_proof"),
            immutable_link_witness=(
                reconciliation_file_reference(args.immutable_link_witness, "control")
                if getattr(args, "immutable_link_witness", None) is not None
                else None
            ),
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
            != (baseline_database.size_bytes, baseline_database.sha256)
        ):
            raise MaintenancePhaseError("prepared_delta_w_database_binding_differs")
        return {
            "preparation_only": True,
            **preparation_binding,
            "manifest": result.model_dump(mode="json"),
        }
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
        if (
            args.phase == "verify-relationships"
            and (args.destination / "state/reconciliation-head.json").exists()
        ):
            path = args.controls_root / "generation-relationships.json"
            ledger = WorkspaceV2GenerationLedger(service, args.destination)
            admission_path = (
                _required(args.resource_run_root, "resource_run_root") / "admission.json"
            )
            if path.exists():
                proof = ledger.reuse_relationships(path, admission_path=admission_path)
            else:
                proof = ledger.verify_relationships(admission_path=admission_path)
                WorkspaceV2ProgressReconciler._write_once(
                    path, proof.model_dump_json().encode() + b"\n"
                )
            return {"relationships": proof.model_dump(mode="json"), "proof_path": str(path)}
        if (
            args.phase == "profile-relationships"
            and (args.destination / "state/reconciliation-head.json").exists()
        ):
            from investment_analyst.storage.resource_policy import execute_bounded

            resolved = WorkspaceV2GenerationLedger(service, args.destination).resolve()
            if resolved is None or resolved[1].generation_id != args.operation_id:
                raise MaintenancePhaseError("relationship_profile_generation_differs")
            with service.open_storage(
                service.resolve(args.destination), WorkspaceAccessMode.READ_ONLY
            ) as storage:
                connection = storage.store.connection
                counts = execute_bounded(
                    connection,
                    "SELECT count(*) FILTER (WHERE r.record_id IS NULL),"
                    "count(*) FILTER (WHERE r.asset_id<>o.asset_id),"
                    "count(*) FILTER (WHERE r.source_id<>o.source_id) "
                    "FROM normalized_observations_v2 o LEFT JOIN raw_v2_index r "
                    "ON r.record_id=o.raw_record_id",
                ).fetchone()
                pairs = execute_bounded(
                    connection,
                    "SELECT r.source_id,o.source_id,count(*) FROM normalized_observations_v2 o "
                    "JOIN raw_v2_index r ON r.record_id=o.raw_record_id "
                    "WHERE r.source_id<>o.source_id GROUP BY r.source_id,o.source_id "
                    "ORDER BY r.source_id,o.source_id LIMIT 16",
                ).fetchall()
            return {
                "profile_only": True,
                "generation_id": str(args.operation_id),
                "missing_raw": counts[0],
                "different_asset": counts[1],
                "different_source": counts[2],
                "source_pairs": pairs,
            }
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
        held_cut = getattr(args, "live_source_cut", None)
        if held_cut is not None:
            from investment_analyst.workspace.workspace_v2_migration import _live_quiescent_source

            if _live_quiescent_source(args.source, held_cut) is None:
                raise MaintenancePhaseError("catchup_requires_live_source_session")
            return _catchup_live(args, service, held_cut)
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
            return _catchup_live(args, service, current)

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
                immutable_link_witness=(
                    reconciliation_file_reference(args.immutable_link_witness, "control")
                    if args.immutable_link_witness is not None
                    else None
                ),
                prepared_delta=args.prepared_delta,
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
            immutable_link_journal=args.immutable_link_journal,
            immutable_link_witness=(
                reconciliation_file_reference(args.immutable_link_witness, "control")
                if args.immutable_link_witness is not None
                else None
            ),
            journal_byte_budget=args.journal_byte_budget,
            **_independent_read_options(args),
        )
        result = _complete_recovery(args, restored, final_manifest)
        projection = compare_operational_models(args.source, restored.restored_root)
        return {**result, "operational_models": projection}
    if args.phase == "preclean":
        if args.preclean_kind == "unused_release":
            if args.retire_release_sha is None:
                raise MaintenancePhaseError("missing_retire_release_sha")
            return (
                LocalReleaseService()
                .retire_unused_release(
                    args.retire_release_sha,
                    operation_id=args.operation_id,
                    retained_controls=_required(args.retained_controls, "retained_controls"),
                    retained_release_paths=tuple(args.retained_release_path or ()),
                )
                .model_dump(mode="json")
            )
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
    tool_sha256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    started = datetime.now(UTC)
    timer = time.monotonic()
    try:
        if (
            args.source == args.destination
            or args.controls_root.is_relative_to(args.source)
            or args.controls_root.is_relative_to(args.destination)
        ):
            raise MaintenancePhaseError("roots_must_be_disjoint")
        if args.phase == "status" or (args.dry_run and args.phase != "repair-publication"):
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
        if args.phase == "cut":
            result = _recover_global_cut(args) if args.cut_recover else _run_global_cut(args)
        else:
            if args.cut_recover:
                raise MaintenancePhaseError("cut_recovery_requires_cut_phase")
            with _worker_resources(args):
                result = _execute(args)
        io_after = _process_io()
        try:
            cgroup_peak = int((_process_identity(os.getpid())[2] / "memory.peak").read_text())
        except (OSError, ValueError):
            cgroup_peak = None
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
            "maintenance_cgroup_peak_bytes": cgroup_peak,
            **_io_measurement(io_before, io_after, args, result),
            "tool_sha256": tool_sha256,
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
