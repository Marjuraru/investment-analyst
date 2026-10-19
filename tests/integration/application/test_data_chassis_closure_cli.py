"""Discovery, read-only planning and fail-closed bindings of maintenance CLI."""

from __future__ import annotations

import errno
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType, SimpleNamespace
from uuid import uuid4

import pytest

from investment_analyst.storage.resource_policy import (
    DatabaseResourceAssignment,
    DuckDBResourcePolicy,
    MaintenanceResourceBudget,
    MaintenanceResourceSample,
)
from investment_analyst.workspace.workspace_v2_acceptance import CompatibleRecoveryReadback
from investment_analyst.workspace.workspace_v2_cutover import WorkspaceV2CutoverPhase
from investment_analyst.workspace.workspace_v2_operational_state import (
    OperationalStateTransferError,
)
from investment_analyst.workspace.workspace_v2_physical_delta import PhysicalDeltaReceipt


@pytest.fixture
def resource_cli_module(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    repository = Path(__file__).resolve().parents[3]
    monkeypatch.syspath_prepend(str(repository / "scripts"))
    spec = importlib.util.spec_from_file_location(
        "dc45_resources", repository / "scripts/close_data_chassis.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_integrated_release_44_readback_uses_preconnect_caps_and_never_opens_candidate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    resource_cli_module: ModuleType,
) -> None:
    from investment_analyst.storage.compact_analytical_v2 import CompactAnalyticalStore
    from investment_analyst.workspace.models import WorkspaceAccessMode
    from investment_analyst.workspace.service import WorkspaceService

    module = resource_cli_module
    release = module.LocalReleaseService().paths.releases / module.INTEGRATED_RELEASE
    if not (release / ".venv/bin/python").is_file():
        pytest.skip("prepared integrated release 44 is unavailable")
    operation = uuid4()
    controls = tmp_path / "controls"
    controls.mkdir()
    slot = tmp_path / "slot"
    service = WorkspaceService(environ={}, home=tmp_path / "home")
    initialized = service.initialize(slot, format_version=2)
    empty_digest = hashlib.sha256(b"").hexdigest()
    with service.open_storage(initialized.paths, WorkspaceAccessMode.READ_WRITE) as storage:
        CompactAnalyticalStore(storage.store.connection).seal_historical_inventory(
            source_fingerprint="a" * 64,
            metric_count=0,
            metric_digest=empty_digest,
            diagnostic_count=0,
            diagnostic_digest=empty_digest,
            sealed_at=datetime.now(UTC),
        )
    budget = MaintenanceResourceBudget(
        operation_id=operation,
        profile="compatible_44",
        assignments=(
            DatabaseResourceAssignment(
                database=slot / "storage/v2/index.duckdb",
                policy=DuckDBResourcePolicy(
                    memory_limit_bytes=2_000_000_000,
                    threads=2,
                    temp_directory=controls / "resource-temporaries" / str(operation) / "reader",
                ),
            ),
        ),
    )
    budget_path = controls / "budget.json"
    budget_path.write_text(budget.model_dump_json())
    args = SimpleNamespace(
        resource_budget=budget_path,
        operation_id=operation,
        controls_root=controls,
        source=tmp_path / "source",
        destination=tmp_path / "candidate",
        recovery_root=slot,
    )
    receipt = PhysicalDeltaReceipt(
        kind="restore",
        epoch=operation,
        manifest_sha256="a" * 64,
        files_sha256="b" * 64,
        confirmed_files=0,
        historical_state="COMPLETE",
        restored_root=slot,
        completed_at=datetime.now(UTC),
    )
    monkeypatch.setattr(
        module.WorkspaceService,
        "inspect",
        lambda *args: pytest.fail("candidate inspected outside admitted slot"),
    )
    result = module._readback(
        args,
        receipt,
        expected_counts=(0, 0, 0, 0),
        expected_workspace_id=initialized.manifest.workspace_id,
    )
    assert result.reader_sha == module.INTEGRATED_RELEASE
    assert result.restored_root == slot
    assert args.compatible_reader_settings["threads"] == "2"
    assert args.compatible_reader_settings["max_temp_directory_size"] == "953.6 MiB"
    wrong = MaintenanceResourceBudget(
        operation_id=operation,
        profile="verification",
        assignments=(
            DatabaseResourceAssignment(
                database=budget.assignments[0].database,
                policy=budget.assignments[0].policy.model_copy(
                    update={"memory_limit_bytes": 1_000_000_000, "threads": 1}
                ),
            ),
        ),
    )
    budget_path.write_text(wrong.model_dump_json())
    with pytest.raises(module.MaintenancePhaseError, match="single_compatible_44"):
        module._readback(
            args,
            receipt,
            expected_counts=(0, 0, 0, 0),
            expected_workspace_id=initialized.manifest.workspace_id,
        )


@pytest.mark.parametrize("phase", ["catchup", "delta", "activate"])
def test_cut_commands_require_global_window_before_accessing_data(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    resource_cli_module: ModuleType,
    phase: str,
) -> None:
    module = resource_cli_module
    args, _ = _resource_arguments(tmp_path)
    args.phase = phase
    monkeypatch.setattr(module, "WorkspaceService", object)
    monkeypatch.setattr(
        module,
        "WorkspaceV2GenerationLedger",
        lambda *args: pytest.fail("missing window must fail before ledger/data access"),
    )
    with pytest.raises(module.MaintenancePhaseError, match="missing_cut_window"):
        module._execute(args)


@pytest.mark.parametrize("phase", ["catchup", "delta", "activate"])
def test_invalidation_blocks_cut_even_if_head_publication_crashed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    resource_cli_module: ModuleType,
    phase: str,
) -> None:
    module = resource_cli_module
    args, _ = _resource_arguments(tmp_path)
    args.phase = phase
    args.cut_window = tmp_path / "not-read-window.json"
    monkeypatch.setattr(module, "WorkspaceService", object)
    monkeypatch.setattr(
        module,
        "WorkspaceV2GenerationLedger",
        lambda *unused: SimpleNamespace(
            attempt_invalidated=lambda attempt: attempt == args.operation_id
        ),
    )
    monkeypatch.setattr(
        module,
        "WorkspaceV2CutWindowService",
        lambda *args: pytest.fail("invalidation must block even without window/head publication"),
    )
    with pytest.raises(module.MaintenancePhaseError, match="cut_attempt_invalidated"):
        module._execute(args)


@pytest.mark.parametrize("reserved", [False, True])
def test_catchup_preserves_verifiers_and_never_reuses_dead_session_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    resource_cli_module: ModuleType,
    reserved: bool,
) -> None:
    import investment_analyst.storage.resource_policy as policy
    import investment_analyst.storage.workspace_v2_inventory as inventory
    import investment_analyst.workspace.workspace_v2_migration as migration

    module = resource_cli_module
    args, _ = _resource_arguments(tmp_path)
    # Session semantics below the separately tested window admission boundary.
    monkeypatch.setattr(module, "_admit_cut_command", lambda *args: None)
    reservation = (
        args.destination / "state" / f"progress-successor-reservation.{args.operation_id}.json"
    )
    old_admission = tmp_path / "old-admission.json"
    old_admission.write_bytes(b"original admission")
    document = json.dumps(
        {"source_session": str(uuid4()), "resource_receipt": {"path": str(old_admission)}}
    ).encode()
    if reserved:
        reservation.parent.mkdir(parents=True)
        reservation.write_bytes(document)
    originals = (
        policy.execute_bounded,
        migration.execute_bounded,
        policy.connect_duckdb,
        migration.connect_duckdb,
        migration.uuid4,
        inventory.WorkspaceV2ProgressReconciler._write_once,
        module.ProgressReconciliation.model_validate.__func__,
    )
    # Register restoration too, so a regression cannot contaminate later tests.
    for owner, attribute in (
        (policy, "execute_bounded"),
        (migration, "execute_bounded"),
        (policy, "connect_duckdb"),
        (migration, "connect_duckdb"),
        (migration, "uuid4"),
    ):
        monkeypatch.setattr(owner, attribute, getattr(owner, attribute))
    reconciler = inventory.WorkspaceV2ProgressReconciler
    monkeypatch.setattr(reconciler, "_write_once", staticmethod(reconciler._write_once))
    monkeypatch.setattr(
        module.ProgressReconciliation,
        "model_validate",
        classmethod(module.ProgressReconciliation.model_validate.__func__),
    )
    release = SimpleNamespace(
        systemctl=SimpleNamespace(
            show_properties=lambda *args: {"ActiveState": "inactive", "MainPID": "0"}
        )
    )
    monkeypatch.setattr(module, "LocalReleaseService", lambda: release)

    class CheckedBoundary(Exception):
        pass

    @contextmanager
    def fresh_session(*unused, **kwargs):
        assert kwargs["operation_id"] == args.operation_id
        assert (
            policy.execute_bounded,
            migration.execute_bounded,
            policy.connect_duckdb,
            migration.connect_duckdb,
            migration.uuid4,
            reconciler._write_once,
            module.ProgressReconciliation.model_validate.__func__,
        ) == originals
        if reserved:
            assert reservation.read_bytes() == document
        raise CheckedBoundary
        yield  # pragma: no cover -- stop before opening any database

    monkeypatch.setattr(module, "quiescent_source_cut", fresh_session)
    with pytest.raises(CheckedBoundary):
        module._execute(args)
    if reserved:
        assert reservation.read_bytes() == document


@pytest.mark.parametrize("action", ["upload", "restore"])
def test_prepared_w_transfer_uses_sealed_manifest_without_replanning_or_cut(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    resource_cli_module: ModuleType,
    action: str,
) -> None:
    module = resource_cli_module
    operation = uuid4()
    database = SimpleNamespace(sha256="d" * 64)
    baseline = SimpleNamespace(
        phase="BASELINE_VERIFIED",
        reserved_successor=None,
        source_root=tmp_path / "source",
        destination_root=tmp_path / "destination",
        destination_workspace_id=uuid4(),
        database_after=database,
    )
    proof = tmp_path / "core.json"
    proof.write_bytes(b"{}")
    pointer = tmp_path / "pointer.json"
    pointer.write_bytes(b"{}")
    core = SimpleNamespace(
        reconciliation=SimpleNamespace(path=pointer),
        database=database,
        verifier_module_sha256=hashlib.sha256(
            Path(module.inspect.getfile(module.verify_recovered_core_baseline)).read_bytes()
        ).hexdigest(),
    )
    monkeypatch.setattr(module._RecoveredCoreBaseline, "model_validate_json", lambda _: core)
    monkeypatch.setattr(module.ProgressReconciliation, "model_validate_json", lambda _: baseline)
    monkeypatch.setattr(module.WorkspaceV2ProgressReconciler, "_verify", lambda _: None)

    class Owner:
        path = pointer

        def __init__(self, *args):
            pass

        @staticmethod
        def _verify(value):
            assert value is core.reconciliation

        def _controls(self, value):
            assert value is baseline

    monkeypatch.setattr(module, "WorkspaceV2ProgressReconciler", Owner)
    manifest = SimpleNamespace(
        historical_state="APPLYING",
        source_workspace_id=baseline.destination_workspace_id,
        parent_manifest_sha256="a" * 64,
    )
    root = tmp_path / "sealed-delta"
    monkeypatch.setattr(module, "_delta", lambda _: (root, manifest))
    monkeypatch.setattr(module, "_capacity", lambda: 50_000_000_000)
    transport = object()
    monkeypatch.setattr(module, "_transport", lambda _: transport)
    invoked = []

    class Physical:
        def plan(self, *args, **kwargs):
            raise AssertionError("sealed W must never be regenerated for transfer")

        def upload(self, actual_root, actual_transport, **kwargs):
            assert actual_root == root and actual_transport is transport
            invoked.append("upload")
            return SimpleNamespace(model_dump=lambda **_: {"kind": "upload"})

        def restore(self, actual_root, **kwargs):
            assert actual_root == root and kwargs["parent_root"] == baseline.destination_root
            invoked.append("restore")
            return SimpleNamespace(model_dump=lambda **_: {"kind": "restore"})

    monkeypatch.setattr(module, "WorkspaceV2PhysicalDeltaService", lambda _: Physical())
    args = SimpleNamespace(
        phase="prepare-delta",
        core_baseline_path=proof,
        source=baseline.source_root,
        destination=baseline.destination_root,
        operation_id=operation,
        delta_root=root,
        delta_action=action,
        parent_manifest_sha256="a" * 64,
        parent_index=tmp_path / "parent.jsonl",
        recovery_root=tmp_path / "slot",
        reuse_parent_immutables=False,
    )
    result = module._execute(args)
    assert result["preparation_only"] is True
    assert invoked == [action]


@pytest.mark.parametrize("content", [None, "anon 12\n", "anon -1\nfile 2\n", "anon bad\nfile 2\n"])
def test_memory_composition_keeps_missing_or_invalid_counters_unknown(
    tmp_path: Path, resource_cli_module: ModuleType, content: str | None
) -> None:
    if content is not None:
        (tmp_path / "memory.stat").write_text(content)
    assert resource_cli_module._memory_composition(tmp_path) is None


def test_memory_composition_reports_only_known_numeric_counters(
    tmp_path: Path, resource_cli_module: ModuleType
) -> None:
    (tmp_path / "memory.stat").write_text("anon 12\nfile 34\ninactive_file 30\nunknown private\n")
    assert resource_cli_module._memory_composition(tmp_path) == {
        "anon": 12,
        "file": 34,
        "inactive_file": 30,
    }


@pytest.mark.parametrize("fault", [None, "foreign", "short", "no_progress", "unavailable"])
def test_physical_cache_reclaim_is_owned_bounded_and_preserves_limits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    resource_cli_module: ModuleType,
    fault: str | None,
) -> None:
    module = resource_cli_module
    _, budget = _resource_arguments(tmp_path)
    cgroup = tmp_path / module.MAINTENANCE_UNIT
    cgroup.mkdir()
    current = budget.memory_high_bytes - 700_000_000
    (cgroup / "memory.current").write_text(str(current))
    (cgroup / "memory.reclaim").touch()
    for name, value in (
        ("memory.high", budget.memory_high_bytes),
        ("memory.max", budget.memory_max_bytes),
        ("memory.swap.max", 0),
    ):
        (cgroup / name).write_text(str(value))
    boot = uuid4()
    monkeypatch.setattr(module, "_process_identity", lambda _: (12, boot, cgroup))
    receipt = module._resource_receipt(budget, os.getpid(), _resource_sample(budget))
    if fault == "foreign":
        monkeypatch.setattr(module, "_process_identity", lambda _: (13, boot, cgroup))
    original = Path.write_text

    def kernel_reclaim(path, value, *args, **kwargs):
        if path != cgroup / "memory.reclaim":
            return original(path, value, *args, **kwargs)
        assert value == str(512 * 1024 * 1024)
        if fault == "unavailable":
            raise PermissionError("fixture controller unavailable")
        if fault != "no_progress":
            original(cgroup / "memory.current", str(current - 128_000_000))
        if fault in {"short", "no_progress"}:
            raise OSError(errno.EAGAIN, "fixture short reclaim")
        return original(path, value, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", kernel_reclaim)
    reclaimer = module._PhysicalCacheReclaimer(receipt, enabled=True)
    reclaimer.poll({"file": 1_000_000_000, "anon": 200_000_000})
    assert reclaimer._thread is not None
    reclaimer._thread.join(timeout=1)
    assert not reclaimer._thread.is_alive()
    result = reclaimer.poll({"file": 1_000_000_000})
    assert result["requests"] == result["completed"] == 1
    if fault in {"foreign", "no_progress", "unavailable"}:
        assert result["error"] is not None
    else:
        assert result["error"] is None
        assert result["memory_after"] < result["memory_before"]
        assert result["short_reclaim"] == (fault == "short")
    for name, value in (
        ("memory.high", budget.memory_high_bytes),
        ("memory.max", budget.memory_max_bytes),
        ("memory.swap.max", 0),
    ):
        assert (cgroup / name).read_text() == str(value)


def test_blocked_physical_reclaim_keeps_heartbeat_responsive_and_timeout_sticky(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    resource_cli_module: ModuleType,
) -> None:
    module = resource_cli_module
    _, budget = _resource_arguments(tmp_path)
    cgroup = tmp_path / module.MAINTENANCE_UNIT
    cgroup.mkdir()
    (cgroup / "memory.current").write_text(str(budget.memory_high_bytes - 100_000_000))
    boot = uuid4()
    monkeypatch.setattr(module, "_process_identity", lambda _: (12, boot, cgroup))
    receipt = module._resource_receipt(budget, os.getpid(), _resource_sample(budget))
    entered, release = threading.Event(), threading.Event()
    original = Path.write_text

    def blocked(path, value, *args, **kwargs):
        if path == cgroup / "memory.reclaim":
            entered.set()
            release.wait(timeout=3)
        return original(path, value, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", blocked)
    now = [100.0]
    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: now[0]))
    reclaimer = module._PhysicalCacheReclaimer(receipt, enabled=True)
    try:
        reclaimer.poll({"file": 1_000_000_000})
        assert entered.wait(timeout=1)
        now[0] += 3
        result = reclaimer.poll({"file": 1_000_000_000})
        assert result["active"] and result["error"] == "cache_reclaim_timeout"
        assert result["requests"] == 1
    finally:
        release.set()
        reclaimer._thread.join(timeout=1)
    assert reclaimer.poll({"file": 1_000_000_000})["error"] == "cache_reclaim_timeout"


def test_physical_reclaim_monitors_fast_reads_without_waiting_for_heartbeat(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    resource_cli_module: ModuleType,
) -> None:
    module = resource_cli_module
    _, budget = _resource_arguments(tmp_path)
    cgroup = tmp_path / module.MAINTENANCE_UNIT
    cgroup.mkdir()
    (cgroup / "memory.current").write_text(str(budget.memory_high_bytes - 700_000_000))
    boot = uuid4()
    monkeypatch.setattr(module, "_process_identity", lambda _: (12, boot, cgroup))
    monkeypatch.setattr(module, "_memory_composition", lambda _: {"file": 1_000_000_000})
    receipt = module._resource_receipt(budget, os.getpid(), _resource_sample(budget))
    completed = threading.Event()
    requests = []
    original = Path.write_text

    def continuous_read(path, value, *args, **kwargs):
        if path == cgroup / "memory.reclaim":
            requests.append(value)
            # The next read has already refilled the cache. The monitor must
            # reclaim again within one second, rather than wait two seconds.
            if len(requests) == 2:
                original(cgroup / "memory.current", "100000000")
                completed.set()
            return len(value)
        return original(path, value, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", continuous_read)
    reclaimer = module._PhysicalCacheReclaimer(receipt, enabled=True)
    reclaimer.start()
    try:
        assert completed.wait(timeout=1)
    finally:
        reclaimer.close()
    result = reclaimer.poll(None)
    assert requests == [str(512 * 1024 * 1024)] * 2
    assert result["error"] is None
    assert len(result["recent_requests"]) == 2


def _resource_arguments(tmp_path: Path) -> tuple[SimpleNamespace, MaintenanceResourceBudget]:
    operation = uuid4()
    controls = tmp_path / "controls"
    controls.mkdir()
    destination = tmp_path / "destination"
    budget = MaintenanceResourceBudget(
        operation_id=operation,
        profile="verification",
        assignments=(
            DatabaseResourceAssignment(
                database=destination / "storage/v2/index.duckdb",
                policy=DuckDBResourcePolicy(
                    memory_limit_bytes=1_000_000_000,
                    threads=1,
                    temp_directory=controls
                    / "resource-temporaries"
                    / str(operation)
                    / "destination",
                ),
            ),
        ),
    )
    path = controls / "budget.json"
    path.write_text(budget.model_dump_json())
    run = controls / "worker-run"
    run.mkdir()
    return SimpleNamespace(
        resource_budget=path,
        code_sha="a" * 40,
        operation_id=operation,
        controls_root=controls,
        source=tmp_path / "source",
        destination=destination,
        recovery_root=None,
        output=None,
        phase="catchup",
        resource_run_root=run,
    ), budget


def _resource_sample(budget: MaintenanceResourceBudget) -> MaintenanceResourceSample:
    return MaintenanceResourceSample(
        host_free_bytes=30_000_000_000,
        memory_high_bytes=budget.memory_high_bytes,
        memory_max_bytes=budget.memory_max_bytes,
        swap_max_bytes=0,
        memory_current_bytes=10_000_000,
        available_memory_bytes=6_000_000_000,
        parent_headroom_bytes=(None,),
        temp_allocated_bytes=0,
        temp_logical_bytes=0,
        log_bytes=0,
        events=(0, 0, 0),
        heartbeat_age_seconds=0,
    )


def _cut_monitor_arguments(
    tmp_path: Path,
) -> tuple[SimpleNamespace, MaintenanceResourceBudget, MaintenanceResourceBudget]:
    operation = uuid4()
    controls = tmp_path / "controls"
    controls.mkdir()
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    recovery = tmp_path / "recovery"
    temporary = controls / "resource-temporaries" / str(operation)
    base = MaintenanceResourceBudget(
        operation_id=operation,
        profile="verification",
        assignments=(
            DatabaseResourceAssignment(
                database=source / "storage/data/processed/investment_analyst.duckdb",
                policy=DuckDBResourcePolicy(
                    memory_limit_bytes=1_000_000_000,
                    threads=1,
                    spill_limit_bytes=0,
                    temp_directory=temporary / "source",
                ),
            ),
            DatabaseResourceAssignment(
                database=destination / "storage/v2/index.duckdb",
                policy=DuckDBResourcePolicy(
                    memory_limit_bytes=1_000_000_000,
                    threads=1,
                    spill_limit_bytes=0,
                    temp_directory=temporary / "destination",
                ),
            ),
        ),
    )
    reader = MaintenanceResourceBudget(
        operation_id=operation,
        profile="compatible_44",
        assignments=(
            DatabaseResourceAssignment(
                database=recovery / "storage/v2/index.duckdb",
                policy=DuckDBResourcePolicy(
                    memory_limit_bytes=2_000_000_000,
                    threads=2,
                    spill_limit_bytes=0,
                    temp_directory=temporary / "reader",
                ),
            ),
        ),
    )
    base_path = controls / "cut-budget.json"
    base_path.write_text(base.model_dump_json())
    reader_path = controls / "readback-budget.json"
    reader_path.write_text(reader.model_dump_json())
    run = controls / "run"
    run.mkdir()
    args = SimpleNamespace(
        source=source,
        destination=destination,
        recovery_root=recovery,
        controls_root=controls,
        resource_budget=base_path,
        cut_readback_budget=reader_path,
        resource_run_root=run,
        operation_id=operation,
        cut_window=controls / "cut-window.json",
    )
    return args, base, reader


def _cut_monitor(
    module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    args: SimpleNamespace,
    base: MaintenanceResourceBudget,
) -> object:
    monkeypatch.setattr(
        module,
        "_process_identity",
        lambda pid: (os.getpid(), uuid4(), Path("/sys/fs/cgroup/fixture")),
    )
    monkeypatch.setattr(
        module, "sample_cgroup_resources", lambda *unused, **kwargs: _resource_sample(base)
    )
    return module._CutMonitor(args)


def test_cut_monitor_readiness_accepts_first_transient_then_valid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, resource_cli_module: ModuleType
) -> None:
    module = resource_cli_module
    args, base, _ = _cut_monitor_arguments(tmp_path)
    calls = {"count": 0}

    def capacity() -> int:
        calls["count"] += 1
        if calls["count"] == 1:
            raise RuntimeError("fixture transient host probe")
        return 30_000_000_000

    monkeypatch.setattr(module, "_capacity", capacity)
    monitor = _cut_monitor(module, monkeypatch, args, base)
    try:
        monitor.start_ready(5.0)
        assert monitor.reason is None
        origin = json.loads((args.resource_run_root / "cut-resource-origin.json").read_text())
        assert origin["host_free_bytes"] == 30_000_000_000
        assert calls["count"] >= 2
    finally:
        monitor.close()


@pytest.mark.parametrize("fault", ["no_value", "low", "low_exception"])
def test_cut_monitor_readiness_rejects_unavailable_or_low_capacity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    resource_cli_module: ModuleType,
    fault: str,
) -> None:
    module = resource_cli_module
    args, base, _ = _cut_monitor_arguments(tmp_path)
    if fault == "no_value":

        def capacity() -> int:
            raise RuntimeError("fixture transient host probe")

        expected = "host_capacity_unavailable_or_below_floor"
        timeout = 0.6
    elif fault == "low":

        def capacity() -> int:
            return 24_000_000_000

        expected = "host_capacity_below_floor"
        timeout = 5.0
    else:

        def capacity() -> int:
            raise module.MaintenancePhaseError("host_capacity_below_floor")

        expected = "host_capacity_below_floor"
        timeout = 5.0

    monkeypatch.setattr(module, "_capacity", capacity)
    monitor = _cut_monitor(module, monkeypatch, args, base)
    try:
        with pytest.raises(module.MaintenancePhaseError, match=expected):
            monitor.start_ready(timeout)
        assert not (args.resource_run_root / "cut-resource-origin.json").exists()
    finally:
        monitor.close()


def test_cut_monitor_transient_failure_preserves_last_success_and_stale_stops(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, resource_cli_module: ModuleType
) -> None:
    module = resource_cli_module
    args, base, _ = _cut_monitor_arguments(tmp_path)
    monkeypatch.setattr(module, "_capacity", lambda: 30_000_000_000)
    monitor = _cut_monitor(module, monkeypatch, args, base)
    try:
        monitor.start_ready(5.0)
        success_at = monitor._capacity_success_at
        assert success_at is not None

        def failing() -> int:
            raise RuntimeError("fixture transient host probe")

        monkeypatch.setattr(module, "_capacity", failing)
        time.sleep(1.2)
        assert monitor._capacity_success_at == success_at
        assert monitor._observe() is None
        with monitor._lock:
            monitor._capacity_success_at = success_at - 16
        assert monitor._observe() == "host_capacity_unavailable_or_below_floor"
    finally:
        monitor.close()


def test_unresponsive_worker_stop_targets_only_its_transient_unit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, resource_cli_module: ModuleType
) -> None:
    module = resource_cli_module
    elapsed = [0.0]
    clock = SimpleNamespace(
        monotonic=lambda: elapsed[0],
        sleep=lambda seconds: elapsed.__setitem__(0, elapsed[0] + seconds),
    )
    commands = []
    monkeypatch.setattr(module, "time", clock)
    monkeypatch.setattr(module, "_unit_properties", lambda: {"MainPID": "9999"})
    monkeypatch.setattr(
        module.subprocess, "run", lambda command, **kwargs: commands.append((command, kwargs))
    )
    module._stop_owned_worker(tmp_path, "fixture_unresponsive", 30)
    assert elapsed[0] == 5
    assert commands[0][0] == [
        "systemctl",
        "--user",
        "stop",
        "investment-analyst-dc45-cut-f.service",
    ]
    assert commands[0][1]["timeout"] == 26
    assert (
        json.loads((tmp_path / "stop-request.json").read_text())["reason"] == "fixture_unresponsive"
    )


def test_worker_rejects_missing_cgroup_limits_before_any_storage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, resource_cli_module: ModuleType
) -> None:
    module = resource_cli_module
    args, budget = _resource_arguments(tmp_path)
    monkeypatch.setattr(
        module, "_unit_properties", lambda: {"ActiveState": "active", "MainPID": str(os.getpid())}
    )
    monkeypatch.setattr(
        module, "_process_identity", lambda pid: (12, uuid4(), Path("/sys/fs/cgroup/fixture"))
    )
    monkeypatch.setattr(module, "_capacity", lambda: 30_000_000_000)
    monkeypatch.setattr(
        module,
        "sample_cgroup_resources",
        lambda *a, **k: _resource_sample(budget).model_copy(update={"memory_max_bytes": None}),
    )
    entered = False
    with (
        pytest.raises(module.MaintenancePhaseError, match="cgroup_limits"),
        module._worker_resources(args),
    ):
        entered = True
    assert not entered
    receipt = json.loads((args.resource_run_root / "admission.json").read_text())
    assert receipt["stage"] == "stopped" and receipt["observed_memory_max_bytes"] is None
    assert not args.destination.exists()


@pytest.mark.parametrize(
    "fault,reason",
    [
        ("host", "host_capacity_unavailable_or_below_floor"),
        ("spill", "aggregate_spill_exceeded"),
        ("deadline", "sql_timeout"),
    ],
)
def test_supervisor_stops_only_owned_unit_and_preserves_checkpoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    resource_cli_module: ModuleType,
    fault: str,
    reason: str,
) -> None:
    module = resource_cli_module
    args, budget = _resource_arguments(tmp_path)
    run_id, boot_id = uuid4(), uuid4()
    run = args.controls_root / ("resource-run-" + run_id.hex)
    checkpoint = args.controls_root / "existing-progress.json"
    checkpoint.write_bytes(b"preserve committed progress")
    monkeypatch.setattr(module, "uuid4", lambda: run_id)
    running = False
    commands = []
    stops = []

    def properties():
        return {
            "MainPID": "9999" if running else "0",
            "ActiveState": "active" if running else "inactive",
        }

    def launch(command, **kwargs):
        nonlocal running
        commands.append(command)
        running = True
        now = module.time.monotonic()
        (run / "heartbeat.json").write_text(
            json.dumps(
                {
                    "operation_id": str(args.operation_id),
                    "pid": 9999,
                    "process_start_ticks": 12,
                    "boot_id": str(boot_id),
                    "phase": "sql",
                    "step_started_monotonic": now - 121 if fault == "deadline" else now,
                    "deadline_seconds": 120,
                    "heartbeat_monotonic": now,
                }
            )
        )
        return SimpleNamespace(returncode=0)

    def stop(path, why, grace):
        nonlocal running
        stops.append((path, why, grace))
        running = False

    sample = _resource_sample(budget)
    if fault == "host":
        sample = sample.model_copy(update={"host_free_bytes": None})
    if fault == "spill":
        sample = sample.model_copy(update={"temp_allocated_bytes": 1_000_000_001})
    monkeypatch.setattr(module, "_unit_properties", properties)
    monkeypatch.setattr(
        module, "_process_identity", lambda pid: (12, boot_id, Path("/sys/fs/cgroup/fixture"))
    )
    monkeypatch.setattr(module, "_capacity", lambda: 30_000_000_000)
    monkeypatch.setattr(module, "sample_cgroup_resources", lambda *a, **k: sample)
    monkeypatch.setattr(module.subprocess, "run", launch)
    monkeypatch.setattr(module, "_stop_owned_worker", stop)
    with pytest.raises(module.MaintenancePhaseError, match=reason):
        module._supervise(args, ["catchup"])
    assert stops == [(run, reason, 30)]
    assert checkpoint.read_bytes() == b"preserve committed progress"
    assert not args.destination.exists()
    assert "--unit=investment-analyst-dc45-cut-f.service" in commands[0]
    assert "--property=MemoryMax=3221225472" in commands[0]
    assert "--property=MemorySwapMax=0" in commands[0]
    assert "--property=KillMode=control-group" in commands[0]
    assert "--property=RuntimeMaxSec=1800" in commands[0]
    assert "investment-analyst.service" not in commands[0]


@pytest.mark.parametrize(
    "fault",
    [
        "upload",
        "applying",
        "other_epoch",
        "altered_manifest",
        "wrong_reader",
        "wrong_workspace",
        "invalid_state",
        "wrong_module",
    ],
)
def test_activation_requires_actual_compatible_f_recovery_before_touching_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    repository = Path(__file__).resolve().parents[3]
    monkeypatch.syspath_prepend(str(repository / "scripts"))
    spec = importlib.util.spec_from_file_location(
        "dc45_activation_under_test", repository / "scripts/close_data_chassis.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    operation, workspace_id = uuid4(), uuid4()
    # Exercise recovery/readback negatives after window admission, independently.
    monkeypatch.setattr(module, "_admit_cut_command", lambda *args: None)
    manifest = tmp_path / "delta-manifest.json"
    manifest.write_bytes(b"{}\n")
    manifest_sha = hashlib.sha256(manifest.read_bytes()).hexdigest()
    receipt = PhysicalDeltaReceipt(
        kind="upload" if fault == "upload" else "restore",
        restored_root=None if fault == "upload" else tmp_path / "recovered",
        epoch=uuid4() if fault == "other_epoch" else operation,
        historical_state="APPLYING" if fault == "applying" else "COMPLETE",
        manifest_sha256="0" * 64 if fault == "altered_manifest" else manifest_sha,
        files_sha256="f" * 64,
        confirmed_files=1,
        completed_at=datetime.now(UTC),
    )
    receipt_path = tmp_path / "restore-receipt.json"
    receipt_path.write_text(receipt.model_dump_json())
    if fault in {"wrong_reader", "wrong_workspace", "invalid_state", "wrong_module"}:
        readback = CompatibleRecoveryReadback(
            operation_id=operation,
            workspace_id=uuid4() if fault == "wrong_workspace" else workspace_id,
            restored_root=receipt.restored_root,
            manifest_sha256=manifest_sha,
            recovery_receipt_sha256=hashlib.sha256(receipt.model_dump_json().encode()).hexdigest(),
            reader_sha="a" * 40 if fault == "wrong_reader" else module.INTEGRATED_RELEASE,
            reader_module_path=tmp_path / "reader.py",
            raw_count=0,
            observation_count=0,
            metric_count=0,
            diagnostic_count=0,
            raw_sample_sha256="0" * 64,
            observation_sample_sha256="0" * 64,
            metric_sample_sha256="0" * 64,
            diagnostic_sample_sha256="0" * 64,
            checked_at=datetime.now(UTC),
        )
        (tmp_path / "compatible-recovery-readback.json").write_text(readback.model_dump_json())
    monkeypatch.setattr(
        module,
        "_cut",
        lambda args: SimpleNamespace(
            phase=WorkspaceV2CutoverPhase.CATCHUP_COMPLETE,
            destination_workspace_id=workspace_id,
        ),
    )
    monkeypatch.setattr(
        module, "_delta", lambda args: (tmp_path, SimpleNamespace(files_sha256="f" * 64))
    )

    def unexpected_runtime_mutation():
        raise AssertionError("incomplete recovery must never reach release activation")

    monkeypatch.setattr(module, "LocalReleaseService", unexpected_runtime_mutation)
    if fault in {"invalid_state", "wrong_module"}:
        for root in (tmp_path / "source", tmp_path / "candidate"):
            root.mkdir()
            if fault == "invalid_state":
                (root / "state").mkdir()
                (root / "state/aapl_daily_run_state.json").write_bytes(b"{}\n")
    if fault == "wrong_module":

        class ReadOnlyRelease:
            paths = SimpleNamespace(releases=tmp_path / "releases")

            def activate(self, *args, **kwargs):
                raise AssertionError("wrong reader module must not activate the service")

        monkeypatch.setattr(module, "LocalReleaseService", ReadOnlyRelease)
    args = SimpleNamespace(
        phase="activate",
        operation_id=operation,
        recovery_receipt=receipt_path,
        controls_root=tmp_path,
        source=tmp_path / "source",
        destination=tmp_path / "candidate",
    )
    expected = {
        "wrong_reader": "compatible_readback_binding_mismatch",
        "wrong_workspace": "compatible_readback_binding_mismatch",
        "wrong_module": "compatible_readback_module_mismatch",
        "invalid_state": "model projection is invalid",
    }.get(fault, "activation_requires_independent_f_recovery")
    with pytest.raises(
        (module.MaintenancePhaseError, OperationalStateTransferError), match=expected
    ):
        module._execute(args)


def _run(tmp_path: Path, phase: str, *extra: str) -> subprocess.CompletedProcess[str]:
    repository = Path(__file__).resolve().parents[3]
    return subprocess.run(
        [
            sys.executable,
            str(repository / "scripts/close_data_chassis.py"),
            phase,
            "--source",
            str(tmp_path / "source"),
            "--destination",
            str(tmp_path / "destination"),
            "--controls-root",
            str(tmp_path / "controls"),
            "--operation-id",
            "21000000-0000-0000-0000-000000000001",
            "--code-sha",
            "a" * 40,
            *extra,
        ],
        cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": str(repository / "src")},
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.parametrize("phase", ["status", "catchup", "delta", "activate", "retire-final"])
def test_status_and_dry_run_create_nothing_from_another_cwd(tmp_path: Path, phase: str) -> None:
    completed = _run(tmp_path, phase, *(() if phase == "status" else ("--dry-run",)))
    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout)
    assert [item["phase"] for item in result["steps"]] == [
        "preclean",
        "prepare",
        "catchup",
        "delta",
        "activate",
        "report",
        "retire-final",
    ]
    assert all(item["state"] == "pending" for item in result["steps"])
    assert list(tmp_path.iterdir()) == []


def test_wrong_maintenance_sha_does_not_create_or_operate(tmp_path: Path) -> None:
    output = tmp_path / "receipt.json"
    completed = _run(tmp_path, "catchup", "--output", str(output))
    assert completed.returncode == 2
    assert json.loads(completed.stderr)["error"] == "maintenance_phase_failed"
    assert list(tmp_path.iterdir()) == []


def test_relative_root_is_rejected_before_mutation(tmp_path: Path) -> None:
    completed = _run(tmp_path, "status", "--source", "relative")
    assert completed.returncode == 2
    assert "absolute" in completed.stderr
    assert list(tmp_path.iterdir()) == []


def test_corrupt_cutover_cannot_be_presented_as_reusable(tmp_path: Path) -> None:
    controls = tmp_path / "controls"
    controls.mkdir()
    checkpoint = controls / "cutover-checkpoint.json"
    checkpoint.write_bytes(b'{"phase":"catchup_complete"}')
    completed = _run(tmp_path, "status")
    assert completed.returncode == 2
    assert checkpoint.read_bytes() == b'{"phase":"catchup_complete"}'


@pytest.mark.parametrize("failure", [False, True])
def test_capacity_uses_live_wsl_socket_and_restores_inherited_environment(
    monkeypatch: pytest.MonkeyPatch, failure: bool
) -> None:
    repository = Path(__file__).resolve().parents[3]
    monkeypatch.syspath_prepend(str(repository / "scripts"))
    spec = importlib.util.spec_from_file_location(
        "dc45_closure_under_test", repository / "scripts/close_data_chassis.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    missing = "/run/WSL/nonexistent-fixture-interop"
    monkeypatch.setenv("WSL_INTEROP", missing)
    monkeypatch.setattr(module, "_wsl_init_socket", lambda: Path("/run/WSL/1_interop"))

    def capacity(attempt_observer=None) -> int:
        assert os.environ["WSL_INTEROP"] == "/run/WSL/1_interop"
        if failure:
            raise RuntimeError("fixture host API failure")
        return 30_000_000_000

    monkeypatch.setattr(module, "_host_free_bytes", capacity)
    if failure:
        with pytest.raises(RuntimeError, match="host API"):
            module._capacity()
    else:
        assert module._capacity() == 30_000_000_000
    assert os.environ["WSL_INTEROP"] == missing


@pytest.mark.parametrize("outcome", ["success", "persistent", "unknown"])
def test_capacity_failover_is_bounded_and_only_for_the_diagnosed_socket_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outcome: str
) -> None:
    repository = Path(__file__).resolve().parents[3]
    monkeypatch.syspath_prepend(str(repository / "scripts"))
    spec = importlib.util.spec_from_file_location(
        "dc45_capacity_failover_under_test", repository / "scripts/close_data_chassis.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    inherited = tmp_path / "inherited-socket"
    inherited.touch()
    monkeypatch.setenv("WSL_INTEROP", str(inherited))
    monkeypatch.setattr(module, "_wsl_init_socket", lambda: Path("/run/WSL/2_interop"))
    calls: list[str] = []

    def capacity(observer=None) -> int:
        calls.append(os.environ["WSL_INTEROP"])
        if len(calls) == 1:
            observer(
                {"diagnostic": "nonzero_exit" if outcome == "unknown" else "transient_wsl_vsock"}
            )
            raise module.WorkspaceV2MigrationError("fixture bridge failure")
        if outcome == "persistent":
            raise module.WorkspaceV2MigrationError("fixture bridge remains unavailable")
        return 30_000_000_000

    monkeypatch.setattr(module, "_host_free_bytes", capacity)
    monkeypatch.setattr(module, "_fresh_host_capacity", lambda: capacity())
    if outcome == "success":
        assert module._capacity() == 30_000_000_000
    else:
        with pytest.raises(module.WorkspaceV2MigrationError):
            module._capacity()
    assert calls == [str(inherited)] + ([] if outcome == "unknown" else ["/run/WSL/2_interop"])
    assert os.environ["WSL_INTEROP"] == str(inherited)


@pytest.mark.parametrize("outcome", ["success", "nonzero", "invalid", "timeout"])
def test_fresh_capacity_reader_uses_existing_helper_and_rejects_untrusted_output(
    monkeypatch: pytest.MonkeyPatch, outcome: str
) -> None:
    repository = Path(__file__).resolve().parents[3]
    monkeypatch.syspath_prepend(str(repository / "scripts"))
    spec = importlib.util.spec_from_file_location(
        "dc45_fresh_capacity_under_test", repository / "scripts/close_data_chassis.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    def run(command, **kwargs):
        assert command[0] == sys.executable
        assert "_host_free_bytes" in command[2]
        assert Path(command[3]) == repository / "scripts"
        assert kwargs["env"]["PYTHONPATH"] == str(repository / "src")
        assert kwargs["timeout"] == 35
        if outcome == "timeout":
            raise subprocess.TimeoutExpired(command, 35, stderr="token=fixture-secret")
        return subprocess.CompletedProcess(
            command,
            1 if outcome == "nonzero" else 0,
            stdout="invalid" if outcome == "invalid" else "30000000000\n",
            stderr="token=fixture-secret",
        )

    monkeypatch.setattr(subprocess, "run", run)
    if outcome == "success":
        assert module._fresh_host_capacity() == 30_000_000_000
    else:
        with pytest.raises(
            module.WorkspaceV2MigrationError, match="fresh host API reader failed"
        ) as raised:
            module._fresh_host_capacity()
        assert "fixture-secret" not in str(raised.value)


@pytest.mark.parametrize("changed", [None, "operation", "generation", "destination", "v1"])
def test_preparatory_retirement_dispatch_binds_exact_roots_and_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    resource_cli_module: ModuleType,
    changed: str | None,
) -> None:
    module = resource_cli_module
    args, _ = _resource_arguments(tmp_path)
    args.phase = "retire-preparatory"
    args.reconciliation_id = uuid4()
    args.immutable_link_journal = args.controls_root / "unlinks"
    args.journal_byte_budget = 1_000_000
    args.preparatory_retirement_input = tmp_path / "proposal.json"
    args.preparatory_retirement_input.write_bytes(b"typed proposal checked below")
    proposal = SimpleNamespace(
        operation_id=uuid4() if changed == "operation" else args.operation_id,
        retained_generation_id=uuid4() if changed == "generation" else args.reconciliation_id,
        retained_root=tmp_path / "different" if changed == "destination" else args.destination,
        source_root=args.source if changed == "v1" else tmp_path / "old-W-slot",
    )
    monkeypatch.setattr(
        module,
        "PreparatorySlotRetirementReceipt",
        SimpleNamespace(model_validate_json=lambda document: proposal),
    )
    monkeypatch.setattr(module, "WorkspaceService", object)
    called = []

    def retire(actual, **options):
        assert actual is proposal
        assert options["admission_path"] == args.resource_run_root / "admission.json"
        assert options["checkpoint_path"].parent == args.controls_root
        assert options["proof_path"].parent == args.controls_root
        assert options["unlink_root"] == args.immutable_link_journal
        called.append(actual)
        return SimpleNamespace(model_dump=lambda **unused: {"phase": "retired"})

    monkeypatch.setattr(
        module,
        "WorkspaceV2LifecycleService",
        lambda service: SimpleNamespace(retire_preparatory_slot=retire),
    )
    if changed:
        with pytest.raises(module.MaintenancePhaseError, match="command_binding_differs"):
            module._execute(args)
        assert called == []
    else:
        assert module._execute(args) == {"phase": "retired"}
        assert called == [proposal]


@pytest.mark.parametrize(
    "fault",
    [
        None,
        "prepared",
        "reserved",
        "generation",
        "verifier",
        "database",
        "restore",
        "restore_without_links",
        "restore_over_budget",
        "measure-plan",
        "measure-comparison",
    ],
)
def test_prepare_delta_uses_certified_generation_without_reopening_legacy_w(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    resource_cli_module: ModuleType,
    fault: str | None,
) -> None:
    import inspect

    from investment_analyst.workspace.workspace_v2_migration_models import (
        ReconciliationFileReference,
    )
    from investment_analyst.workspace.workspace_v2_physical_delta import (
        PhysicalDeltaFile,
        PhysicalDeltaManifest,
    )

    module = resource_cli_module
    args, _ = _resource_arguments(tmp_path)
    args.phase, args.delta_action = "prepare-delta", "plan"
    args.reconciliation_id = args.operation_id
    args.delta_root = tmp_path / "new-W2-delta"
    args.parent_index = tmp_path / "parent-index"
    args.parent_manifest_sha256 = "a" * 64
    args.immutable_parent_proof = tmp_path / "historical-parent-proof.json"
    args.immutable_link_witness = tmp_path / "UNLINK-trailer.json"
    args.immutable_link_witness.write_bytes(b"durable controlled unlink witness")
    restoring = fault is not None and fault.startswith("restore")
    if fault in {"measure-plan", "measure-comparison"}:
        args.delta_action = fault
        monkeypatch.setattr(module, "_capacity", lambda: 50_000_000_000)
    if restoring:
        args.delta_action = "restore"
        args.recovery_root = tmp_path / "single-recovery-slot"
        args.reuse_parent_immutables = fault != "restore_without_links"
        args.immutable_link_journal = args.controls_root / "new-links"
        args.journal_byte_budget = 1_000_000
        monkeypatch.setattr(module, "_capacity", lambda: 50_000_000_000)
        monkeypatch.setattr(module, "_transport", lambda _: object())
        monkeypatch.setattr(module, "_independent_read_options", lambda _: {})
        monkeypatch.setattr(
            module,
            "_load_resource_budget",
            lambda _: SimpleNamespace(
                cache_and_control_bytes=1 if fault == "restore_over_budget" else 1_000_000
            ),
        )
    digest = hashlib.sha256(b"").hexdigest()
    database = ReconciliationFileReference(
        path=args.destination / "storage/v2/index.duckdb",
        role="database",
        size_bytes=1 if fault == "database" else 0,
        sha256=digest,
    )
    certificate = ReconciliationFileReference(
        path=args.controls_root / "certified.json",
        role="control",
        size_bytes=1,
        sha256="c" * 64,
    )
    baseline = SimpleNamespace(
        generation_id=uuid4() if fault == "generation" else args.operation_id,
        phase="PREPARED" if fault == "prepared" else "BASELINE_VERIFIED",
        reserved_successor=object() if fault == "reserved" else None,
        source_root=args.source,
        destination_root=args.destination,
        destination_workspace_id=uuid4(),
        core_proof=certificate,
        database=database,
        corrective_proofs=(certificate,),
    )
    verified = []
    content = SimpleNamespace(
        database=database,
        generation_id=args.operation_id,
        verifier_sha256="0" * 64
        if fault == "verifier"
        else hashlib.sha256(
            inspect.getsource(module.verify_generation_contents).encode()
        ).hexdigest(),
    )
    monkeypatch.setattr(module, "WorkspaceService", object)
    monkeypatch.setattr(
        module,
        "WorkspaceV2GenerationLedger",
        lambda *unused: SimpleNamespace(
            resolve=lambda: (SimpleNamespace(certificate=certificate), baseline),
            _read=lambda ref: b"content proof validated by its separate tests",
        ),
    )
    monkeypatch.setattr(
        module, "GenerationContentProof", SimpleNamespace(model_validate_json=lambda _: content)
    )
    monkeypatch.setattr(
        module, "WorkspaceV2ProgressReconciler", SimpleNamespace(_verify=verified.append)
    )
    manifest = PhysicalDeltaManifest(
        historical_state="APPLYING",
        epoch=args.operation_id,
        source_root=args.destination,
        source_workspace_id=baseline.destination_workspace_id,
        parent_backup_id=uuid4(),
        parent_manifest_sha256="a" * 64,
        parent_index_sha256="b" * 64,
        files_sha256=digest,
        file_count=1,
        deleted_count=0,
        logical_bytes=0,
        new_bytes=0,
        reused_bytes=0,
    )
    calls = []

    def measure_comparison(service, source, destination, generation, admission):
        assert source == args.source and destination == args.destination
        assert generation == args.reconciliation_id
        assert admission == args.resource_run_root / "admission.json"
        calls.append({"action": "measure-comparison"})
        return {
            "calibration_only": True,
            "generation_certificate": certificate.model_dump(mode="json"),
        }

    monkeypatch.setattr(module, "measure_recovered_comparison", measure_comparison)

    def plan(source, index, output, **options):
        assert (source, index, output) == (args.destination, args.parent_index, args.delta_root)
        assert options["allow_incomplete_historical"] is True
        assert options["epoch"] == args.operation_id
        assert options["immutable_link_witness"].path == args.immutable_link_witness
        calls.append(options)
        return manifest

    def restore(root, **options):
        assert root == args.delta_root
        assert options["immutable_link_journal"] == args.immutable_link_journal
        assert options["immutable_link_witness"].path == args.immutable_link_witness
        assert options["journal_byte_budget"] == args.journal_byte_budget
        assert options["destination"] == args.recovery_root
        calls.append(options)
        return SimpleNamespace(model_dump=lambda **_: {"kind": "restore"})

    def measure(source, index, prepared, **options):
        assert (source, index, prepared) == (args.destination, args.parent_index, args.delta_root)
        assert options["link_witness"].path == args.immutable_link_witness
        calls.append(options)
        return {"calibration_only": True, "plan_published": False}

    monkeypatch.setattr(module, "_delta", lambda _: (args.delta_root, manifest))

    monkeypatch.setattr(
        module,
        "WorkspaceV2PhysicalDeltaService",
        lambda service: SimpleNamespace(plan=plan, restore=restore, measure_prepared_plan=measure),
    )
    monkeypatch.setattr(
        module,
        "_iter_delta_files",
        lambda path: iter(
            (
                PhysicalDeltaFile(
                    path="storage/v2/index.duckdb",
                    size_bytes=0,
                    blocks=(),
                    sha256=digest,
                ),
            )
        ),
    )
    if fault and fault not in {"restore", "measure-plan", "measure-comparison"}:
        with pytest.raises(module.MaintenancePhaseError):
            module._execute(args)
        assert bool(calls) == (fault == "database")
    else:
        result = module._execute(args)
        assert result["preparation_only"] is True
        assert result["generation_certificate"] == certificate.model_dump(mode="json")
        assert database in verified and calls


@pytest.mark.parametrize("mismatch", [None, "operation", "controls"])
def test_owned_payload_dispatch_retires_only_bound_regenerable_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    resource_cli_module: ModuleType,
    mismatch: str | None,
) -> None:
    from investment_analyst.workspace.workspace_v2_lifecycle_models import (
        OwnedPayloadRetirementInput,
        OwnedPayloadRole,
    )

    module = resource_cli_module
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    args, _ = _resource_arguments(tmp_path)
    args.phase = "retire-owned-payload"
    args.controls_root = tmp_path / ".local/share/investment-analyst/ops/data-chassis-45/cleanup-E0"
    root = tmp_path / "projects/investment-analyst/.worktrees/data-chassis-45/.pytest_cache"
    root.mkdir(parents=True)
    (root / "CACHEDIR.TAG").write_text("regenerable")
    request = OwnedPayloadRetirementInput(
        operation_id=uuid4() if mismatch == "operation" else args.operation_id,
        role=OwnedPayloadRole.REGENERABLE_LOCAL_CACHE,
        source_root=root,
        retained_controls_root=args.controls_root / "different"
        if mismatch == "controls"
        else args.controls_root,
        consuming_generation=None,
        releasing_stage="E0",
        no_users=True,
        no_unique_data=True,
    )
    args.owned_payload_input = tmp_path / "owned-payload-input.json"
    args.owned_payload_input.write_text(request.model_dump_json())
    if mismatch:
        with pytest.raises(
            module.MaintenancePhaseError, match="owned_payload_operation_binding_differs"
        ):
            module._execute(args)
        assert root.exists()
    else:
        result = module._execute(args)
        assert result["owned_payload_retirement"]["checkpoint"]["phase"] == "retired"
        assert not root.exists()
        assert module._execute(args) == result


def test_unused_release_preclean_dispatch_keeps_release_service_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, resource_cli_module: ModuleType
) -> None:
    module = resource_cli_module
    args, _ = _resource_arguments(tmp_path)
    args.phase = "preclean"
    args.preclean_kind = "unused_release"
    args.retire_release_sha = "a" * 40
    args.retained_controls = args.controls_root
    args.retained_release_path = [tmp_path / "releases" / ("b" * 40)]
    calls = []

    def retire(sha, **kwargs):
        calls.append((sha, kwargs))
        return SimpleNamespace(model_dump=lambda **unused: {"phase": "retired"})

    monkeypatch.setattr(
        module, "LocalReleaseService", lambda: SimpleNamespace(retire_unused_release=retire)
    )
    assert module._execute(args) == {"phase": "retired"}
    assert calls == [
        (
            args.retire_release_sha,
            {
                "operation_id": args.operation_id,
                "retained_controls": args.controls_root,
                "retained_release_paths": tuple(args.retained_release_path),
            },
        )
    ]
    args.retire_release_sha = None
    with pytest.raises(module.MaintenancePhaseError, match="missing_retire_release_sha"):
        module._execute(args)


def test_recovery_rejects_incompatible_reader_budget_before_opening_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, resource_cli_module: ModuleType
) -> None:
    from investment_analyst.workspace.workspace_v2_physical_delta import PhysicalDeltaManifest

    module = resource_cli_module
    args, budget = _resource_arguments(tmp_path)
    args.recovery_root = tmp_path / "restored"
    args.delta_root = tmp_path / "delta"
    args.delta_root.mkdir()
    manifest = PhysicalDeltaManifest(
        epoch=args.operation_id,
        source_root=args.destination,
        source_workspace_id=uuid4(),
        parent_backup_id=uuid4(),
        parent_manifest_sha256="a" * 64,
        parent_index_sha256="b" * 64,
        files_sha256="c" * 64,
        file_count=0,
        deleted_count=0,
        logical_bytes=0,
        new_bytes=0,
        reused_bytes=0,
    )
    data = manifest.model_dump_json().encode()
    (args.delta_root / "delta-manifest.json").write_bytes(data)
    receipt = PhysicalDeltaReceipt(
        kind="restore",
        restored_root=args.recovery_root,
        epoch=args.operation_id,
        manifest_sha256=hashlib.sha256(data).hexdigest(),
        files_sha256=manifest.files_sha256,
        confirmed_files=0,
        completed_at=datetime.now(UTC),
    )
    allocation = budget.assignments[0].model_copy(
        update={
            "database": args.recovery_root / "storage/v2/index.duckdb",
            "policy": budget.assignments[0].policy.model_copy(update={"threads": 2}),
        }
    )
    invalid = MaintenanceResourceBudget.model_validate(
        {**budget.model_dump(), "profile": "compatible_44", "assignments": (allocation,)}
    )
    args.resource_budget.write_text(invalid.model_dump_json())

    def forbidden(*unused, **kwargs):
        raise AssertionError("incompatible reader budget opened a database")

    monkeypatch.setattr(module, "WorkspaceService", forbidden)
    with pytest.raises(
        module.MaintenancePhaseError, match="recovery_requires_single_compatible_44_allocation"
    ):
        module._complete_recovery(args, receipt, manifest)


@pytest.mark.parametrize(
    ("window_limit_seconds", "window_authorization", "expected_runtime"),
    (
        (1800, None, "1700"),
        (5400, "human-dc45-cut-5400-20261019", "5300"),
    ),
)
def test_client_loss_leaves_cut_unit_and_its_recovery_hook_running(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    resource_cli_module: ModuleType,
    window_limit_seconds: int,
    window_authorization: str | None,
    expected_runtime: str,
):
    from investment_analyst.storage.workspace_v2_inventory import reconciliation_file_reference
    from investment_analyst.workspace.workspace_v2_migration_models import (
        CutWindowBound,
        CutWindowGo,
    )

    module = resource_cli_module
    args, budget = _resource_arguments(tmp_path)
    args.phase = "cut"
    proof = tmp_path / "measured.json"
    proof.write_text("{}")
    ref = reconciliation_file_reference(proof, "control")
    go = CutWindowGo(
        bounds=tuple(
            CutWindowBound(
                step=step,
                upper_seconds=100,
                measurement=ref,
                measured_at=datetime.now(UTC),
                universe="fixture",
                workload="small fixture",
                changes_since_measurement="none",
            )
            for step in (
                "freeze",
                "comparison_delta",
                "operational",
                "physical_plan",
                "upload",
                "restore_open",
                "activation_health",
                "cancel_close_restart",
            )
        ),
        margin_seconds=100,
        baseline=ref,
        prerequisites=(ref,),
        guard=ref,
        capacity=ref,
        source_workspace_id=uuid4(),
        destination_workspace_id=uuid4(),
        admitted_at=datetime.now(UTC),
        window_limit_seconds=window_limit_seconds,
        window_authorization=window_authorization,
        attempt_id=args.operation_id if window_limit_seconds == 5400 else None,
    )
    args.cut_go = tmp_path / "go.json"
    args.cut_go.write_text(go.model_dump_json())
    commands = []
    monkeypatch.setattr(
        module,
        "_unit_properties",
        lambda: {"MainPID": "0", "ControlPID": "0", "ActiveState": "inactive"},
    )
    monkeypatch.setattr(module, "_capacity", lambda: 30_000_000_000)
    monkeypatch.setattr(module.subprocess, "run", lambda command, **kw: commands.append(command))
    monkeypatch.setattr(
        module,
        "_stop_owned_worker",
        lambda *_: pytest.fail("lost client stopped the cut controller"),
    )

    def lost_client(*args):
        raise KeyboardInterrupt

    monkeypatch.setattr(module, "_wait_cut_unit", lost_client)
    with pytest.raises(KeyboardInterrupt):
        module._supervise(args, ["cut"])
    assert len(commands) == 1
    command = commands[0]
    assert f"--property=RuntimeMaxSec={expected_runtime}" in command
    assert [arg for arg in command if arg.startswith("--property=TimeoutStopSec=")] == [
        "--property=TimeoutStopSec=100"
    ]
    hook = next(arg for arg in command if arg.startswith("--property=ExecStopPost="))
    assert "--cut-recover" in hook and "--supervised-worker" in hook
    assert "cut-recovery-phase.json" in hook
    assert "--property=Restart=no" in command


def test_cut_client_waits_until_stop_post_finishes_before_reading_terminal_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, resource_cli_module: ModuleType
):
    module = resource_cli_module
    output = tmp_path / "result.json"
    output.write_text('{"terminal":"recovered"}')
    states = iter(
        [
            {"MainPID": "0", "ControlPID": "123", "ActiveState": "deactivating"},
            {"MainPID": "0", "ControlPID": "0", "ActiveState": "inactive"},
        ]
    )
    sleeps = []
    monkeypatch.setattr(module, "_unit_properties", lambda: next(states))
    monkeypatch.setattr(module.time, "sleep", sleeps.append)
    assert module._wait_cut_unit(output, tmp_path) == {"terminal": "recovered"}
    assert sleeps == [1]


def test_5400_cut_rejects_foreign_attempt_before_creating_a_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, resource_cli_module: ModuleType
):
    from contextlib import contextmanager

    from investment_analyst.storage.workspace_v2_inventory import reconciliation_file_reference
    from investment_analyst.workspace.workspace_v2_migration_models import (
        CutWindowBound,
        CutWindowGo,
    )

    module = resource_cli_module
    controls = tmp_path / "controls"
    run = controls / "run"
    run.mkdir(parents=True)
    proof = controls / "proof.json"
    proof.write_text("{}")
    reference = reconciliation_file_reference(proof, "control")
    go = CutWindowGo(
        bounds=tuple(
            CutWindowBound(
                step=step,
                upper_seconds=100,
                measurement=reference,
                measured_at=datetime.now(UTC),
                universe="fixture",
                workload="fixture",
                changes_since_measurement="none",
            )
            for step in (
                "freeze",
                "comparison_delta",
                "operational",
                "physical_plan",
                "upload",
                "restore_open",
                "activation_health",
                "cancel_close_restart",
            )
        ),
        margin_seconds=100,
        baseline=reference,
        prerequisites=(reference,),
        guard=reference,
        capacity=reference,
        source_workspace_id=uuid4(),
        destination_workspace_id=uuid4(),
        admitted_at=datetime.now(UTC),
        window_limit_seconds=5400,
        window_authorization="human-dc45-cut-5400-20261019",
        attempt_id=uuid4(),
    )
    go_path = controls / "go.json"
    go_path.write_text(go.model_dump_json())
    args = SimpleNamespace(
        cut_window=controls / "window.json",
        cut_go=go_path,
        controls_root=controls,
        resource_run_root=run,
        source=tmp_path / "source",
        destination=tmp_path / "destination",
        operation_id=uuid4(),
    )
    monkeypatch.setattr(
        module,
        "_CutMonitor",
        lambda unused: SimpleNamespace(
            start_ready=lambda *args, **kwargs: None,
            require_continue=lambda: None,
            close=lambda: None,
        ),
    )
    monkeypatch.setattr(module, "WorkspaceService", lambda: SimpleNamespace())
    monkeypatch.setattr(module, "LocalReleaseService", lambda: SimpleNamespace())

    @contextmanager
    def resources(unused):
        yield

    monkeypatch.setattr(module, "_worker_resources", resources)
    with pytest.raises(
        module.MaintenancePhaseError, match="cut_window_authorization_attempt_differs"
    ):
        module._run_global_cut(args)
    assert not (controls / "window.json").exists()
