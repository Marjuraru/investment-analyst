"""Pre-connect DuckDB budgets, shared by every application connection factory."""

from __future__ import annotations

import hashlib
import importlib
import os
import re
import sys
import threading
import time
import weakref
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager, nullcontext, suppress
from contextvars import ContextVar
from decimal import Decimal
from pathlib import Path
from typing import Literal
from uuid import UUID

import duckdb
from pydantic import ConfigDict, Field, field_validator, model_validator

from investment_analyst.core.models.base import ContractModel, UTCDateTime
from investment_analyst.storage.errors import StorageError


class ResourcePolicyError(StorageError):
    """A connection or resource admission cannot prove its finite budget."""

    def __init__(self, message: str) -> None:
        self.code = (
            message if re.fullmatch(r"[a-z][a-z0-9_]{0,80}", message) else "resource_policy_failure"
        )
        super().__init__(message)


def _safe_absolute(path: Path) -> Path:
    if (
        not path.is_absolute()
        or ".." in path.parts
        or any(entry.is_symlink() for entry in (path, *path.parents))
    ):
        raise ValueError("resource path must be absolute without symlinks")
    return path


class DuckDBResourcePolicy(ContractModel):
    """Exact native settings supplied before the engine can replay a WAL."""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")
    memory_limit_bytes: int = Field(default=2_000_000_000, gt=0)
    threads: int = Field(default=2, ge=1)
    spill_limit_bytes: int = Field(default=1_000_000_000, ge=0)
    temp_directory: Path | None = None

    @field_validator("temp_directory")
    @classmethod
    def validate_directory(cls, value: Path | None) -> Path | None:
        return None if value is None else _safe_absolute(value)

    def connect_config(self) -> dict[str, str]:
        config = {
            "memory_limit": f"{self.memory_limit_bytes}B",
            "threads": str(self.threads),
            "max_temp_directory_size": f"{self.spill_limit_bytes}B",
        }
        if self.temp_directory is not None:
            _safe_absolute(self.temp_directory)
            self.temp_directory.mkdir(parents=True, exist_ok=True)
            config["temp_directory"] = str(self.temp_directory)
        return config


class DatabaseResourceAssignment(ContractModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")
    database: Path
    policy: DuckDBResourcePolicy

    @field_validator("database")
    @classmethod
    def validate_database(cls, value: Path) -> Path:
        return _safe_absolute(value)

    @model_validator(mode="after")
    def validate_temp_boundary(self) -> DatabaseResourceAssignment:
        directory = self.policy.temp_directory
        if directory is None or directory.is_relative_to(self.database.parent):
            raise ValueError("maintenance temporaries must be outside the database directory")
        return self


class MaintenanceResourceBudget(ContractModel):
    """Aggregate allocation; one assignment represents one native DB instance."""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")
    operation_id: UUID
    profile: Literal["verification", "wal_recovery", "compatible_44"]
    assignments: tuple[DatabaseResourceAssignment, ...] = Field(min_length=1)
    reader_worker: DatabaseResourceAssignment | None = None
    memory_high_bytes: Literal[2_684_354_560] = 2_684_354_560
    memory_max_bytes: Literal[3_221_225_472] = 3_221_225_472
    memory_swap_max_bytes: Literal[0] = 0
    host_floor_bytes: int = Field(default=25_000_000_000, ge=25_000_000_000)
    next_unit_growth_bytes: int = Field(default=0, ge=0)
    cache_and_control_bytes: int = Field(default=0, ge=0)
    open_timeout_seconds: int = Field(default=180, gt=0, le=180)
    sql_timeout_seconds: int = Field(default=120, gt=0, le=120)
    stop_grace_seconds: int = Field(default=30, gt=0, le=30)
    sample_interval_seconds: int = Field(default=5, gt=0, le=5)
    log_limit_bytes: int = Field(default=50_000_000, gt=0, le=50_000_000)

    @property
    def required_host_bytes(self) -> int:
        allocations = self.assignments + (
            () if self.reader_worker is None else (self.reader_worker,)
        )
        return max(
            self.host_floor_bytes,
            5_000_000_000
            + self.next_unit_growth_bytes
            + self.cache_and_control_bytes
            + sum(item.policy.spill_limit_bytes for item in allocations),
        )

    @model_validator(mode="after")
    def validate_aggregate(self) -> MaintenanceResourceBudget:
        databases = [entry.database for entry in self.assignments]
        allocations = self.assignments + (
            () if self.reader_worker is None else (self.reader_worker,)
        )
        directories = [entry.policy.temp_directory for entry in allocations]
        if len(set(databases)) != len(databases) or len(set(directories)) != len(directories):
            raise ValueError("resource allocations must identify disjoint instances/directories")
        if sum(entry.policy.spill_limit_bytes for entry in allocations) > 1_000_000_000:
            raise ValueError("aggregate spill exceeds maintenance budget")
        if sum(entry.policy.memory_limit_bytes for entry in allocations) > 2_000_000_000:
            raise ValueError("aggregate memory exceeds maintenance budget")
        if self.profile in {"wal_recovery", "compatible_44"} and len(databases) != 1:
            raise ValueError("recovery/readback requires exactly one instance")
        if self.reader_worker is not None and self.profile != "verification":
            raise ValueError("recovery/readback cannot allocate a reader worker")
        memory_cap = {
            "verification": 1_000_000_000,
            "wal_recovery": 1_500_000_000,
            "compatible_44": 2_000_000_000,
        }[self.profile]
        threads = 2 if self.profile == "compatible_44" else 1
        for assignment in allocations:
            if (
                assignment.policy.memory_limit_bytes > memory_cap
                or assignment.policy.threads != threads
            ):
                raise ValueError("native allocation differs from the admitted profile")
        return self


class MaintenanceResourceReceipt(ContractModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")
    schema_version: Literal["workspace-v2-maintenance-resource-receipt-v1"] = (
        "workspace-v2-maintenance-resource-receipt-v1"
    )
    budget: MaintenanceResourceBudget
    pid: int = Field(gt=0)
    process_start_ticks: int = Field(ge=0)
    boot_id: UUID
    cgroup_path: Path
    resource_policy_sha256: str | None = None
    coordinator_sha256: str | None = None
    python_version: str | None = None
    duckdb_version: str | None = None
    stage: Literal["admitted", "stopped"] = "admitted"
    observed_memory_high_bytes: int | None
    observed_memory_max_bytes: int | None
    observed_swap_max_bytes: int | None
    host_free_bytes: int | None
    effective_settings: tuple[tuple[str, str], ...]
    stop_reason: str | None = None
    recorded_at: UTCDateTime

    @field_validator("resource_policy_sha256", "coordinator_sha256")
    @classmethod
    def validate_code_digest(cls, value: str | None) -> str | None:
        if value is not None and re.fullmatch(r"[0-9a-f]{64}", value) is None:
            raise ValueError("resource code digest must be SHA-256")
        return value

    @model_validator(mode="after")
    def validate_admission(self) -> MaintenanceResourceReceipt:
        if self.stage == "admitted" and (
            self.observed_memory_high_bytes != self.budget.memory_high_bytes
            or self.observed_memory_max_bytes != self.budget.memory_max_bytes
            or self.observed_swap_max_bytes != self.budget.memory_swap_max_bytes
            or self.host_free_bytes is None
            or self.host_free_bytes < self.budget.required_host_bytes
            or self.stop_reason is not None
        ):
            raise ValueError("resource admission is not established")
        if self.stage == "stopped" and not self.stop_reason:
            raise ValueError("stopped receipt requires an explicit reason")
        return self


class MaintenanceResourceSample(ContractModel):
    """Non-SQL observation of the whole worker cgroup, including descendants."""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")
    host_free_bytes: int | None
    memory_high_bytes: int | None
    memory_max_bytes: int | None
    swap_max_bytes: int | None
    memory_current_bytes: int = Field(ge=0)
    available_memory_bytes: int = Field(ge=0)
    parent_headroom_bytes: tuple[int | None, ...]
    temp_allocated_bytes: int = Field(ge=0)
    temp_logical_bytes: int = Field(ge=0)
    log_bytes: int = Field(ge=0)
    events: tuple[int, int, int]
    heartbeat_age_seconds: int = Field(ge=0)


def resource_stop_reason(
    budget: MaintenanceResourceBudget,
    sample: MaintenanceResourceSample,
    *,
    initial_events: tuple[int, int, int] | None = None,
) -> str | None:
    if (
        sample.memory_high_bytes != budget.memory_high_bytes
        or sample.memory_max_bytes != budget.memory_max_bytes
        or sample.swap_max_bytes != budget.memory_swap_max_bytes
    ):
        return "cgroup_limits_missing_or_incompatible"
    if sample.host_free_bytes is None or sample.host_free_bytes < budget.required_host_bytes:
        return "host_capacity_unavailable_or_below_floor"
    if max(sample.temp_allocated_bytes, sample.temp_logical_bytes) > 1_000_000_000:
        return "aggregate_spill_exceeded"
    if sample.log_bytes > budget.log_limit_bytes:
        return "log_budget_exceeded"
    if sample.heartbeat_age_seconds > budget.sample_interval_seconds:
        return "heartbeat_unavailable"
    if sample.memory_current_bytes >= budget.memory_high_bytes:
        return "memory_high_reached"
    remaining = max(0, budget.memory_max_bytes - sample.memory_current_bytes)
    reserve = remaining + 1_073_741_824
    if sample.available_memory_bytes < reserve or any(
        headroom is not None and headroom < reserve for headroom in sample.parent_headroom_bytes
    ):
        return "external_memory_reserve_unavailable"
    if initial_events is not None and sample.events != initial_events:
        return "cgroup_memory_event_changed"
    return None


def sample_cgroup_resources(
    budget: MaintenanceResourceBudget,
    cgroup: Path,
    *,
    host_free_bytes: int | None,
    log_bytes: int,
    heartbeat_age_seconds: int,
) -> MaintenanceResourceSample:
    """Read only known cgroup/proc paths and dedicated flat spill directories."""
    boundary = Path("/sys/fs/cgroup")
    if not cgroup.is_absolute() or not cgroup.is_relative_to(boundary):
        raise ResourcePolicyError("cgroup path is outside the kernel boundary")

    def limit(path: Path) -> int | None:
        text = path.read_text().strip()
        return None if text == "max" else int(text)

    parents: list[int | None] = []
    for parent in cgroup.parents:
        if parent == boundary.parent:
            break
        if not parent.is_relative_to(boundary):
            raise ResourcePolicyError("cgroup parent is outside the kernel boundary")
        if parent == boundary and not (parent / "memory.max").exists():
            parents.append(None)  # The kernel root does not impose memory.max.
            break
        maximum = limit(parent / "memory.max")
        parents.append(
            None if maximum is None else maximum - int((parent / "memory.current").read_text())
        )
    allocated = logical = 0
    entries = budget.assignments + (() if budget.reader_worker is None else (budget.reader_worker,))
    for entry in entries:
        directory = entry.policy.temp_directory
        if directory is None:
            raise ResourcePolicyError("maintenance temporary directory is missing")
        _safe_absolute(directory)
        if not directory.exists():
            continue
        for ordinal, path in enumerate(directory.iterdir()):
            if ordinal >= 4096 or path.is_symlink() or not path.is_file():
                raise ResourcePolicyError("temporary allocation cannot be measured safely")
            try:
                stat = path.stat()
            except FileNotFoundError:
                continue
            allocated += stat.st_blocks * 512
            logical += stat.st_size
    memory = dict(line.split(":", 1) for line in Path("/proc/meminfo").read_text().splitlines())
    events = dict(line.split() for line in (cgroup / "memory.events").read_text().splitlines())
    return MaintenanceResourceSample(
        host_free_bytes=host_free_bytes,
        memory_high_bytes=limit(cgroup / "memory.high"),
        memory_max_bytes=limit(cgroup / "memory.max"),
        swap_max_bytes=limit(cgroup / "memory.swap.max"),
        memory_current_bytes=int((cgroup / "memory.current").read_text()),
        available_memory_bytes=int(memory["MemAvailable"].split()[0]) * 1024,
        parent_headroom_bytes=tuple(parents),
        temp_allocated_bytes=allocated,
        temp_logical_bytes=logical,
        log_bytes=log_bytes,
        heartbeat_age_seconds=heartbeat_age_seconds,
        events=tuple(int(events[name]) for name in ("oom", "oom_kill", "max")),
    )


class MaintenanceExecution:
    """Per-worker deadlines and non-SQL heartbeat; no unrelated process control."""

    def __init__(
        self, budget: MaintenanceResourceBudget, observe: Callable[[str, float, int], None]
    ) -> None:
        self.budget = budget
        self.observe = observe
        self.phase = "ready"
        self.started = time.monotonic()
        self.deadline = 0
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._heartbeat, daemon=True)
        self._connections: weakref.WeakSet[duckdb.DuckDBPyConnection] = weakref.WeakSet()
        self._policies: weakref.WeakKeyDictionary[
            duckdb.DuckDBPyConnection, DuckDBResourcePolicy
        ] = weakref.WeakKeyDictionary()
        self._databases: weakref.WeakKeyDictionary[duckdb.DuckDBPyConnection, tuple[str, bool]] = (
            weakref.WeakKeyDictionary()
        )
        self.effective_settings: dict[str, tuple[tuple[str, str], ...]] = {}
        self.cancel_reason: str | None = None
        self.active_query_sha256: str | None = None
        self._query_profile: dict[str, tuple[int, float, float]] = {}
        self._measurements: dict[str, int] = {}
        self._query_plans: dict[str, str] = {}

    def record_query_plan(self, name: str, plan: str) -> None:
        with self._lock:
            if len(plan.encode("utf-8")) > 65_536 or len(self._query_plans) >= 16:
                raise ResourcePolicyError("maintenance_query_plan_budget_exceeded")
            self._query_plans[name] = plan

    def query_plans(self) -> dict[str, str]:
        with self._lock:
            return dict(self._query_plans)

    def record_measurement(self, name: str, value: int) -> None:
        with self._lock:
            if len(self._measurements) >= 32 and name not in self._measurements:
                raise ResourcePolicyError("maintenance_measurement_budget_exceeded")
            self._measurements[name] = value

    def measurements(self) -> dict[str, int]:
        with self._lock:
            return dict(self._measurements)

    def record_query(self, query_sha256: str, elapsed_seconds: float) -> None:
        with self._lock:
            key = (
                query_sha256
                if len(self._query_profile) < 128 or query_sha256 in self._query_profile
                else "other"
            )
            count, total, maximum = self._query_profile.get(key, (0, 0.0, 0.0))
            self._query_profile[key] = (
                count + 1,
                total + elapsed_seconds,
                max(maximum, elapsed_seconds),
            )
            self.active_query_sha256 = None

    def query_profile(self) -> dict[str, tuple[int, float, float]]:
        with self._lock:
            return dict(self._query_profile)

    def _heartbeat(self) -> None:
        while not self._stop.is_set():
            with self._lock:
                state = self.phase, self.started, self.deadline
            self.observe(*state)
            self._stop.wait(1)

    def register(
        self,
        connection: duckdb.DuckDBPyConnection,
        policy: DuckDBResourcePolicy,
        database: str,
        settings: tuple[tuple[str, str], ...],
        read_only: bool,
    ) -> None:
        with self._lock:
            if self.cancel_reason is not None:
                connection.close()
                raise ResourcePolicyError(self.cancel_reason)
            self._connections.add(connection)
            self._policies[connection] = policy
            self._databases[connection] = (database, read_only)
            self.effective_settings[database] = settings

    def duplicate_database(
        self, database: str, read_only: bool
    ) -> duckdb.DuckDBPyConnection | None:
        """Reuse the same admitted native instance for nested service handles."""
        with self._lock:
            candidates = tuple(self._databases.items())
        for connection, (path, physical_read_only) in candidates:
            if path != database:
                continue
            try:
                cursor = connection.cursor()
            except duckdb.ConnectionException:
                continue
            if physical_read_only != read_only:
                cursor.close()
                raise ResourcePolicyError("database_physical_mode_conflicts_with_live_instance")
            return cursor
        return None

    def verify(self, connection: duckdb.DuckDBPyConnection) -> None:
        with self._lock:
            policy = self._policies.get(connection)
        if policy is None:
            raise ResourcePolicyError("query_connection_is_not_registered")
        verify_connection_policy(connection, policy)

    def settings_snapshot(self) -> dict[str, tuple[tuple[str, str], ...]]:
        with self._lock:
            return dict(self.effective_settings)

    def cancel(self, reason: str) -> None:
        with self._lock:
            self.cancel_reason = self.cancel_reason or reason
            connections = tuple(self._connections)
        for connection in connections:
            with suppress(duckdb.Error):
                connection.interrupt()

    @contextmanager
    def step(self, phase: str, seconds: int) -> Iterator[None]:
        with self._lock:
            if self.cancel_reason is not None:
                raise ResourcePolicyError(self.cancel_reason)
            previous = self.phase, self.started, self.deadline
            self.phase, self.started, self.deadline = phase, time.monotonic(), seconds
        try:
            yield
        finally:
            with self._lock:
                self.phase, self.started, self.deadline = previous

    def __enter__(self) -> MaintenanceExecution:
        self._thread.start()
        return self

    def __exit__(self, *_: object) -> None:
        self._stop.set()
        self._thread.join(timeout=2)


_EXECUTION: ContextVar[MaintenanceExecution | None] = ContextVar(
    "maintenance_execution", default=None
)


@contextmanager
def maintenance_execution(execution: MaintenanceExecution) -> Iterator[None]:
    token = _EXECUTION.set(execution)
    try:
        with execution:
            yield
    finally:
        _EXECUTION.reset(token)


def execute_bounded(
    connection: duckdb.DuckDBPyConnection,
    sql: str,
    parameters: Sequence[object] | None = None,
) -> duckdb.DuckDBPyConnection:
    execution = _EXECUTION.get()
    if execution is None:
        raise ResourcePolicyError("bounded maintenance execution is missing")
    expired = threading.Event()

    def cancel() -> None:
        expired.set()
        connection.interrupt()

    timer = threading.Timer(execution.budget.sql_timeout_seconds, cancel)
    execution.verify(connection)
    with execution.step("sql", execution.budget.sql_timeout_seconds):
        query_sha256 = hashlib.sha256(sql.encode("utf-8")).hexdigest()
        execution.active_query_sha256 = query_sha256
        started = time.monotonic()
        timer.start()
        try:
            result = connection.execute(sql, parameters)
            if expired.is_set():
                raise ResourcePolicyError("sql_timeout")
            execution.verify(connection)
            return result
        except duckdb.Error:
            if expired.is_set():
                raise ResourcePolicyError("sql_timeout") from None
            raise
        finally:
            timer.cancel()
            timer.join()
            execution.record_query(query_sha256, time.monotonic() - started)


def execute_with_resource_budget(
    connection: duckdb.DuckDBPyConnection,
    sql: str,
    parameters: Sequence[object] | None = None,
) -> duckdb.DuckDBPyConnection:
    """Preserve ordinary callers; enforce SQL deadlines inside maintenance."""
    if _BUDGET.get() is not None:
        return execute_bounded(connection, sql, parameters)
    return connection.execute(sql, parameters)


_BUDGET: ContextVar[MaintenanceResourceBudget | None] = ContextVar("duckdb_budget", default=None)


def require_maintenance_admission(receipt: MaintenanceResourceReceipt) -> MaintenanceExecution:
    """Require the admission of this supervised process, before recovery writes."""
    execution = _EXECUTION.get()
    if (
        execution is None
        or _BUDGET.get() != receipt.budget
        or execution.budget != receipt.budget
        or receipt.stage != "admitted"
        or receipt.pid != os.getpid()
        or execution.cancel_reason is not None
    ):
        raise ResourcePolicyError("maintenance_admission_is_missing_or_foreign")
    return execution


@contextmanager
def maintenance_resource_budget(budget: MaintenanceResourceBudget) -> Iterator[None]:
    if _BUDGET.get() is not None:
        raise ResourcePolicyError("maintenance budget is already bound")
    token = _BUDGET.set(budget)
    try:
        yield
    finally:
        _BUDGET.reset(token)


def connection_policy(
    database: str | Path,
    *,
    policy: DuckDBResourcePolicy | None = None,
    default_memory_bytes: int = 2_000_000_000,
    default_threads: int = 2,
) -> DuckDBResourcePolicy:
    budget = _BUDGET.get()
    if budget is not None:
        path = Path(database).absolute()
        matches = [entry.policy for entry in budget.assignments if entry.database == path]
        if len(matches) != 1 or (policy is not None and policy != matches[0]):
            raise ResourcePolicyError("database has no compatible maintenance allocation")
        return matches[0]
    return policy or DuckDBResourcePolicy(
        memory_limit_bytes=default_memory_bytes, threads=default_threads
    )


def reader_worker_policy(database: Path) -> DuckDBResourcePolicy:
    """Reserve a separate native instance explicitly before a child is spawned."""
    budget = _BUDGET.get()
    if budget is None:
        return DuckDBResourcePolicy(memory_limit_bytes=256_000_000, threads=1)
    assignment = budget.reader_worker
    if assignment is None or assignment.database != database.absolute():
        raise ResourcePolicyError("reader worker has no aggregate resource allocation")
    return assignment.policy


def _display_contains(display: str, requested: int) -> bool:
    """DuckDB truncates byte limits for display; compare its reporting interval.

    The exact integer is supplied in config; the human-readable effective value
    must describe that integer. This is not a resource reservation by itself.
    """
    match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)\s*(bytes|B|KiB|MiB|GiB|TiB|kB|MB|GB|TB)", display)
    if match is None:
        return False
    factors = {
        "bytes": 1,
        "B": 1,
        "kB": 1000,
        "MB": 1000**2,
        "GB": 1000**3,
        "TB": 1000**4,
        "KiB": 1024,
        "MiB": 1024**2,
        "GiB": 1024**3,
        "TiB": 1024**4,
    }
    number = Decimal(match[1])
    factor = factors[match[2]]
    quantum = Decimal(10) ** number.as_tuple().exponent
    return number * factor <= requested < (number + quantum) * factor


def verify_connection_policy(
    connection: duckdb.DuckDBPyConnection, policy: DuckDBResourcePolicy
) -> tuple[tuple[str, str], ...]:
    cursor = connection.cursor()
    try:
        settings = tuple(
            (name, str(cursor.execute("SELECT current_setting(?)", [name]).fetchone()[0]))
            for name in ("memory_limit", "threads", "max_temp_directory_size", "temp_directory")
        )
    finally:
        cursor.close()
    values = dict(settings)
    if (
        not _display_contains(values["memory_limit"], policy.memory_limit_bytes)
        or not _display_contains(values["max_temp_directory_size"], policy.spill_limit_bytes)
        or values["threads"] != str(policy.threads)
        or (
            policy.temp_directory is not None
            and Path(values["temp_directory"]) != policy.temp_directory
        )
    ):
        raise ResourcePolicyError("effective DuckDB settings differ from admitted policy")
    return settings


def cache_absent_optional_pandas() -> None:
    """Cache one negative optional-dependency lookup for DuckDB parameter binding.

    DuckDB probes pandas for every bound parameter. When pandas is not installed,
    each probe otherwise repeats the full import search and dominates read-path
    latency. A cached negative entry raises ImportError immediately and does not
    install or emulate pandas.
    """
    if "pandas" in sys.modules:
        return
    try:
        importlib.import_module("pandas")
    except ImportError:
        sys.modules["pandas"] = None


def connect_duckdb(
    database: str | Path,
    *,
    read_only: bool,
    policy: DuckDBResourcePolicy | None = None,
    default_memory_bytes: int = 2_000_000_000,
    default_threads: int = 2,
) -> duckdb.DuckDBPyConnection:
    cache_absent_optional_pandas()
    selected = connection_policy(
        database,
        policy=policy,
        default_memory_bytes=default_memory_bytes,
        default_threads=default_threads,
    )
    execution = _EXECUTION.get()
    if _BUDGET.get() is not None and execution is None:
        raise ResourcePolicyError("maintenance execution/deadline is missing")
    step = (
        nullcontext()
        if execution is None
        else execution.step("open", execution.budget.open_timeout_seconds)
    )
    with step:
        connection = (
            None if execution is None else execution.duplicate_database(str(database), read_only)
        )
        if connection is None:
            if execution is not None or policy is not None:
                connection = _connect_with_initial_spill_manager(
                    database, read_only=read_only, policy=selected
                )
            else:
                connection = duckdb.connect(
                    str(database), read_only=read_only, config=selected.connect_config()
                )
                # Activate the same finite cap for ordinary callers before any
                # application query. Maintenance uses the pre-attach path above.
                connection.execute(f"SET max_temp_directory_size='{selected.spill_limit_bytes}B'")
    try:
        settings = verify_connection_policy(connection, selected)
        if execution is not None:
            execution.register(connection, selected, str(database), settings, read_only)
    except BaseException:
        connection.close()
        raise
    return connection


def _connect_with_initial_spill_manager(
    database: str | Path, *, read_only: bool, policy: DuckDBResourcePolicy
) -> duckdb.DuckDBPyConnection:
    """Prime the native cap before ATTACH can replay the requested database WAL.

    In installed DuckDB 1.5.4, config reports the spill cap but a native pressure
    fixture exceeds it unless its setting callback initializes the manager. SET
    here repeats the admitted value while no physical database is open. Replacing
    the empty initial catalog preserves the default catalog for auxiliary cursors.
    Bootstrap catalogs have no tables and leave no persisted schema/artifact.
    """
    connection = duckdb.connect(":memory:", read_only=False, config=policy.connect_config())
    try:
        connection.execute(f"SET max_temp_directory_size='{policy.spill_limit_bytes}B'")
        connection.execute("ATTACH ':memory:' AS maintenance_bootstrap")
        connection.execute("USE maintenance_bootstrap")
        connection.execute("DETACH memory")
        escaped = str(_safe_absolute(Path(database))).replace("'", "''")
        mode = "READ_ONLY" if read_only else "READ_WRITE"
        connection.execute(f"ATTACH '{escaped}' AS memory ({mode})")
        connection.execute("USE memory")
        connection.execute("DETACH maintenance_bootstrap")
        return connection
    except BaseException:
        connection.close()
        raise


@contextmanager
def compatible_reader_instance(
    database: Path, *, read_only: bool, policy: DuckDBResourcePolicy
) -> Iterator[duckdb.DuckDBPyConnection]:
    """Adapt a legacy reader in an isolated, single-database worker process.

    DuckDB 1.5.4 rejects an unconfigured legacy open beside a configured instance.
    Supply the *same* admitted config to that native open, before WAL replay,
    without editing installed reader files. Other databases/configs are rejected.
    The adapter is process-local and restored on every exit; it must not host
    unrelated workloads. Native connections still execute the real legacy reader.
    """
    if policy.memory_limit_bytes != 2_000_000_000 or policy.threads != 2:
        raise ResourcePolicyError("legacy reader requires the compatible_44 allocation")
    native = duckdb.connect
    config = policy.connect_config()

    def admitted_connect(
        database: str | Path = ":default:",
        *,
        read_only: bool = False,
        config: dict[str, str] | None = None,
    ) -> duckdb.DuckDBPyConnection:
        if Path(database).absolute() != expected or read_only != physical_read_only:
            raise ResourcePolicyError("legacy reader attempted an unallocated connection")
        if config is not None and config != admitted_config:
            raise ResourcePolicyError("legacy reader requested an incompatible configuration")
        execution = _EXECUTION.get()
        step = (
            nullcontext()
            if execution is None
            else execution.step("open", execution.budget.open_timeout_seconds)
        )
        with step:
            connection = anchor.cursor()
        try:
            verify_connection_policy(connection, policy)
        except BaseException:
            connection.close()
            raise
        return connection

    expected = _safe_absolute(database)
    physical_read_only = read_only
    admitted_config = config
    anchor = connect_duckdb(database, read_only=read_only, policy=policy)
    try:
        verify_connection_policy(anchor, policy)
        duckdb.connect = admitted_connect
        yield anchor
        verify_connection_policy(anchor, policy)
    finally:
        duckdb.connect = native
        anchor.close()
