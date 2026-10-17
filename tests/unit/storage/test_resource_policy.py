"""Native pre-connect limits and fail-closed aggregate resource allocation."""

from pathlib import Path
from uuid import uuid4

import duckdb
import pytest
from pydantic import ValidationError

import investment_analyst.storage.resource_policy as policies
from investment_analyst.storage.resource_policy import (
    DatabaseResourceAssignment,
    DuckDBResourcePolicy,
    MaintenanceExecution,
    MaintenanceResourceBudget,
    MaintenanceResourceSample,
    ResourcePolicyError,
    compatible_reader_instance,
    connect_duckdb,
    execute_bounded,
    maintenance_execution,
    maintenance_resource_budget,
    reader_worker_policy,
    resource_stop_reason,
    verify_connection_policy,
)


def _assignment(
    tmp_path: Path, name: str, *, memory: int, spill: int
) -> DatabaseResourceAssignment:
    return DatabaseResourceAssignment(
        database=tmp_path / name / "db.duckdb",
        policy=DuckDBResourcePolicy(
            memory_limit_bytes=memory,
            threads=1,
            spill_limit_bytes=spill,
            temp_directory=tmp_path / "temporary" / name,
        ),
    )


def test_limits_reach_native_connect_before_first_query(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    native = duckdb.connect
    captured: list[dict[str, str]] = []

    def spy(database: str, *, read_only: bool, config: dict[str, str]) -> duckdb.DuckDBPyConnection:
        assert database == ":memory:" and not (tmp_path / "db.duckdb").exists()
        captured.append(dict(config))
        assert config["memory_limit"] == "256000000B"
        assert config["threads"] == "1"
        assert config["max_temp_directory_size"] == "0B"
        return native(database, read_only=read_only, config=config)

    monkeypatch.setattr(policies.duckdb, "connect", spy)
    policy = DuckDBResourcePolicy(memory_limit_bytes=256_000_000, threads=1, spill_limit_bytes=0)
    with connect_duckdb(tmp_path / "db.duckdb", read_only=False, policy=policy) as connection:
        assert connection.execute("SELECT 42").fetchone() == (42,)
        verify_connection_policy(connection, policy)
    assert len(captured) == 1


@pytest.mark.parametrize(
    "spill_bytes,read_only", [(0, False), (1_000_000, False), (1_000_000, True)]
)
def test_native_spill_limit_actually_stops_materialization_before_quota(
    tmp_path: Path,
    spill_bytes: int,
    read_only: bool,
) -> None:
    database = tmp_path / "db.duckdb"
    with duckdb.connect(str(database)) as initial:
        initial.execute("CREATE TABLE committed_progress AS SELECT 42 v")
    policy = DuckDBResourcePolicy(
        memory_limit_bytes=32_000_000,
        threads=1,
        spill_limit_bytes=spill_bytes,
        temp_directory=tmp_path / "temp",
    )
    with connect_duckdb(database, read_only=read_only, policy=policy) as connection:
        assert len(connection.execute("PRAGMA database_list").fetchall()) == 1
        with connection.cursor() as auxiliary:
            assert auxiliary.execute("SELECT v FROM committed_progress").fetchone() == (42,)
        if read_only:
            with pytest.raises(duckdb.Error, match="read-only"):
                connection.execute("INSERT INTO committed_progress VALUES (43)")
        with pytest.raises(duckdb.OutOfMemoryException):
            connection.execute(
                "CREATE TEMP TABLE cap_probe AS SELECT i,sha256(i::VARCHAR)||"
                "sha256((i+1)::VARCHAR)||sha256((i+2)::VARCHAR)||"
                "sha256((i+3)::VARCHAR) payload FROM range(1000000) t(i)"
            )
        assert (
            sum(path.stat().st_blocks * 512 for path in (tmp_path / "temp").iterdir())
            <= spill_bytes
        )
        assert connection.execute("SELECT v FROM committed_progress").fetchone() == (42,)


def test_nested_maintenance_handles_share_one_native_instance(tmp_path: Path) -> None:
    assignment = _assignment(tmp_path, "db", memory=64_000_000, spill=0)
    assignment.database.parent.mkdir()
    budget = MaintenanceResourceBudget(
        operation_id=uuid4(), profile="verification", assignments=(assignment,)
    )
    execution = MaintenanceExecution(budget, lambda *_: None)
    with maintenance_resource_budget(budget), maintenance_execution(execution):
        first = connect_duckdb(assignment.database, read_only=False)
        first.execute("CREATE TABLE progress AS SELECT 42 v")
        second = connect_duckdb(assignment.database, read_only=False)
        assert second.execute("SELECT v FROM progress").fetchone() == (42,)
        second.close()
        assert first.execute("SELECT v FROM progress").fetchone() == (42,)
        first.close()


@pytest.mark.parametrize("fault", ["memory", "spill", "duplicate", "wal_pair", "threads"])
def test_aggregate_policy_rejects_hidden_multiplication(tmp_path: Path, fault: str) -> None:
    first = _assignment(tmp_path, "source", memory=1_000_000_000, spill=0)
    second = _assignment(tmp_path, "destination", memory=1_000_000_000, spill=1_000_000_000)
    if fault == "memory":
        second = second.model_copy(
            update={
                "policy": second.policy.model_copy(update={"memory_limit_bytes": 1_500_000_000})
            }
        )
    elif fault == "spill":
        first = first.model_copy(
            update={"policy": first.policy.model_copy(update={"spill_limit_bytes": 1})}
        )
    elif fault == "duplicate":
        second = first
    elif fault == "threads":
        first = first.model_copy(update={"policy": first.policy.model_copy(update={"threads": 2})})
    with pytest.raises(ValidationError):
        MaintenanceResourceBudget(
            operation_id=uuid4(),
            profile="wal_recovery" if fault == "wal_pair" else "verification",
            assignments=(first, second),
        )


def test_unknown_connection_cannot_escape_maintenance_allocation(tmp_path: Path) -> None:
    assignment = _assignment(tmp_path, "destination", memory=1_000_000_000, spill=1_000_000_000)
    budget = MaintenanceResourceBudget(
        operation_id=uuid4(), profile="verification", assignments=(assignment,)
    )
    outside = tmp_path / "unallocated.duckdb"
    with maintenance_resource_budget(budget):
        with pytest.raises(ResourcePolicyError, match="no compatible"):
            connect_duckdb(outside, read_only=False)
        with pytest.raises(ResourcePolicyError, match="no compatible"):
            connect_duckdb(assignment.database, read_only=False, policy=DuckDBResourcePolicy())
    assert not outside.exists()


@pytest.mark.parametrize(
    "setting,value", [("threads", "2"), ("memory_limit", "2GB"), ("max_temp_directory_size", "2GB")]
)
def test_helpers_cannot_relax_admitted_settings(tmp_path: Path, setting: str, value: str) -> None:
    policy = DuckDBResourcePolicy(memory_limit_bytes=1_000_000_000, threads=1)
    with connect_duckdb(tmp_path / "db.duckdb", read_only=False, policy=policy) as connection:
        connection.execute(f"SET {setting} = '{value}'")
        with pytest.raises(ResourcePolicyError, match="effective"):
            verify_connection_policy(connection, policy)


def test_temp_symlink_is_rechecked_at_connect(tmp_path: Path) -> None:
    directory = tmp_path / "temporary"
    policy = DuckDBResourcePolicy(temp_directory=directory)
    other = tmp_path / "other"
    other.mkdir()
    directory.symlink_to(other, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        connect_duckdb(tmp_path / "db.duckdb", read_only=False, policy=policy)
    assert not (tmp_path / "db.duckdb").exists()


def test_native_legacy_reader_reuses_preconfigured_instance(tmp_path: Path) -> None:
    policy = DuckDBResourcePolicy(
        memory_limit_bytes=2_000_000_000,
        threads=2,
        spill_limit_bytes=1_000_000_000,
        temp_directory=tmp_path / "temporary",
    )
    database = tmp_path / "db.duckdb"
    native = duckdb.connect
    with (
        compatible_reader_instance(database, read_only=False, policy=policy) as anchor,
        duckdb.connect(str(database), read_only=False) as legacy,
    ):
        # This is the legacy 44 pattern, including its post-connect SETs.
        legacy.execute("SET memory_limit='2GB'")
        legacy.execute("SET threads=2")
        verify_connection_policy(legacy, policy)
        verify_connection_policy(anchor, policy)
        with pytest.raises(ResourcePolicyError, match="unallocated"):
            duckdb.connect(str(tmp_path / "other.duckdb"))
    assert duckdb.connect is native


@pytest.mark.parametrize(
    "fault",
    [
        "high",
        "max",
        "swap",
        "host",
        "spill",
        "log",
        "heartbeat",
        "memory",
        "parent",
        "available",
        "event",
    ],
)
def test_missing_or_exceeded_resources_fail_closed(tmp_path: Path, fault: str) -> None:
    assignment = _assignment(tmp_path, "db", memory=1_000_000_000, spill=1_000_000_000)
    budget = MaintenanceResourceBudget(
        operation_id=uuid4(), profile="verification", assignments=(assignment,)
    )
    sample = MaintenanceResourceSample(
        host_free_bytes=30_000_000_000,
        memory_high_bytes=budget.memory_high_bytes,
        memory_max_bytes=budget.memory_max_bytes,
        swap_max_bytes=0,
        memory_current_bytes=10_000_000,
        available_memory_bytes=6_000_000_000,
        parent_headroom_bytes=(None, 8_000_000_000),
        temp_allocated_bytes=0,
        temp_logical_bytes=0,
        log_bytes=0,
        events=(0, 0, 0),
        heartbeat_age_seconds=0,
    )
    assert resource_stop_reason(budget, sample, initial_events=(0, 0, 0)) is None
    changes: dict[str, object] = {
        "high": {"memory_high_bytes": None},
        "max": {"memory_max_bytes": None},
        "swap": {"swap_max_bytes": None},
        "host": {"host_free_bytes": None},
        "spill": {"temp_allocated_bytes": 1_000_000_001},
        "log": {"log_bytes": 50_000_001},
        "heartbeat": {"heartbeat_age_seconds": 6},
        "memory": {"memory_current_bytes": budget.memory_high_bytes},
        "parent": {"parent_headroom_bytes": (None, 1)},
        "available": {"available_memory_bytes": 1},
        "event": {"events": (0, 1, 0)},
    }
    assert (
        resource_stop_reason(
            budget, sample.model_copy(update=changes[fault]), initial_events=(0, 0, 0)
        )
        is not None
    )


def test_unallocated_child_cannot_multiply_parent_budget(tmp_path: Path) -> None:
    assignment = _assignment(tmp_path, "db", memory=1_000_000_000, spill=1_000_000_000)
    budget = MaintenanceResourceBudget(
        operation_id=uuid4(), profile="verification", assignments=(assignment,)
    )
    with (
        maintenance_resource_budget(budget),
        pytest.raises(ResourcePolicyError, match="worker"),
    ):
        reader_worker_policy(assignment.database)


def test_native_sql_timeout_preserves_committed_progress(tmp_path: Path) -> None:
    assignment = _assignment(tmp_path, "db", memory=256_000_000, spill=0)
    assignment.database.parent.mkdir()
    budget = MaintenanceResourceBudget(
        operation_id=uuid4(),
        profile="verification",
        assignments=(assignment,),
        sql_timeout_seconds=1,
    )
    execution = MaintenanceExecution(budget, lambda phase, started, seconds: None)
    with (
        maintenance_resource_budget(budget),
        maintenance_execution(execution),
        connect_duckdb(assignment.database, read_only=False) as connection,
    ):
        connection.execute("CREATE TABLE progress AS SELECT 7 AS committed")
        assert execute_bounded(connection, "SELECT 42").fetchone() == (42,)
        with pytest.raises(ResourcePolicyError, match="sql_timeout"):
            execute_bounded(
                connection,
                "SELECT sum(a.range*b.range) FROM range(1000000) a, range(1000000) b",
            )
        assert connection.execute("SELECT committed FROM progress").fetchone() == (7,)
