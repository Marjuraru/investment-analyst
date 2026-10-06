#!/usr/bin/env python3
"""Finite offline smoke for per-attempt collection and cycle-linked observability."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import inspect
import io
import json
import os
import platform
import resource
import statistics
import subprocess
import sys
import tarfile
import tempfile
import time as monotonic_time
from collections import Counter
from collections.abc import Callable, Iterator
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from unittest.mock import patch
from uuid import UUID

import duckdb

import investment_analyst.application.storage_observability as storage_observability_module
from investment_analyst.application.multi_asset_scheduler import (
    MultiAssetScheduler,
    MultiAssetScheduleStateStore,
    RegisteredScheduledJob,
    ScheduledJobAttempt,
    ScheduledJobDefinition,
    ScheduledJobDomain,
    ScheduledJobExecution,
    ScheduledJobInvocation,
)
from investment_analyst.application.storage_observability import (
    ScheduledJobObservation,
    StorageObservabilityCollector,
    StorageObservabilityError,
)
from investment_analyst.application.storage_observability_report import (
    StorageObservabilityReportService,
)

_FIXTURE_JOB_COUNT = 102
_FIXTURE_ROW_COUNTS = (257, 1537)
_FIXTURE_RELEASE = "a" * 40
_BASE_TIME = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)
_WORK_BLOCK_BASE_SHA = "2c82cc89b72c366b521387c92c00b63fed2facae"
_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def _load_cycle_probe():
    script_path = Path(__file__).with_name("cycle_probe.py")
    spec = importlib.util.spec_from_file_location("smoke_cycle_probe", script_path)
    if spec is None or spec.loader is None:
        raise RuntimeError("cycle probe is unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


cycle_probe = _load_cycle_probe()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _create_fixture_database(path: Path, row_count: int) -> tuple[str, ...]:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = duckdb.connect(str(path))
    documents = [
        (
            "crypto.derivatives.fixture" if index % 2 else "fixture.metric",
            json.dumps(
                {
                    "metric_key": "crypto.derivatives.fixture" if index % 2 else "fixture.metric",
                    "parameters": {"window": "30" if index % 4 < 2 else "720"},
                    "sequence": index,
                    "label": "muestra-á",
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
        )
        for index in range(row_count)
    ]
    try:
        connection.execute(
            "CREATE TABLE metric_results "
            "(metric_key VARCHAR, document_json VARCHAR, inserted_at TIMESTAMP)"
        )
        connection.execute(
            "CREATE TABLE raw_record_index (record_id VARCHAR, document_json VARCHAR)"
        )
        for table_name in (
            "normalized_observations",
            "diagnostic_results",
            "fundamental_results",
            "sec_document_revisions",
            "sec_filing_metadata",
        ):
            connection.execute(
                f'CREATE TABLE "{table_name}" (record_id VARCHAR, document_json VARCHAR)'
            )
        connection.executemany(
            "INSERT INTO metric_results VALUES (?, ?, TIMESTAMP '2026-10-05 12:00:00')",
            [(metric_key, document) for metric_key, document in documents],
        )
    finally:
        connection.close()
    return tuple(document for _, document in documents)


def _definitions(count: int) -> tuple[ScheduledJobDefinition, ...]:
    definitions: list[ScheduledJobDefinition] = []
    primary_document_tickers = ("aapl", "mstr", "amzn", "cvx", "pltr")
    for index in range(count):
        if index < count - len(primary_document_tickers) - 1:
            job_id = f"fixture:asset-{index % 6}:job-{index:03d}"
            asset_id = f"fixture:asset-{index % 6}"
            provider = "offline-fixture"
            domain = ScheduledJobDomain.MARKET_DAILY
        elif index < count - 1:
            ticker = primary_document_tickers[index - (count - len(primary_document_tickers) - 1)]
            job_id = f"sec:company-document:{ticker}"
            asset_id = f"equity:us:{ticker}"
            provider = "sec-edgar"
            domain = ScheduledJobDomain.FUNDAMENTALS
        else:
            job_id = "sec:institutional:13f-history"
            asset_id = "equity:us:aapl"
            provider = "sec-edgar"
            domain = ScheduledJobDomain.FUNDAMENTALS
        definitions.append(
            ScheduledJobDefinition(
                job_id=job_id,
                asset_id=asset_id,
                provider=provider,
                domain=domain,
                data_frequency="day_1",
                timezone="America/Lima",
                run_at=time(hour=7),
            )
        )
    return tuple(definitions)


def _attempt_signature(attempt: ScheduledJobAttempt) -> str:
    payload = attempt.to_json_dict()
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _run_scheduler(
    root: Path,
    *,
    collector: StorageObservabilityCollector | None,
) -> dict[str, object]:
    definitions = _definitions(_FIXTURE_JOB_COUNT)
    schedule_path = root / "state" / "multi_asset_schedule_state_v1.json"
    store = MultiAssetScheduleStateStore(schedule_path)
    identifiers = iter(UUID(int=index + 1) for index in range(_FIXTURE_JOB_COUNT))
    provider_calls = 0

    def run(invocation) -> ScheduledJobExecution:
        nonlocal provider_calls
        provider_calls += 1
        return ScheduledJobExecution(
            job_id=invocation.definition.job_id,
            effective_known_at=invocation.started_at,
            evidence_changed=False,
            source_ids=(f"fixture-source:{invocation.definition.asset_id}",),
            created_count=0,
            reused_count=1,
        )

    scheduler = MultiAssetScheduler(
        tuple(RegisteredScheduledJob(definition, run) for definition in definitions),
        store,
        storage_observability=collector,
        clock=lambda: _BASE_TIME,
        attempt_id_factory=lambda: next(identifiers),
    )
    with patch(
        "investment_analyst.application.job_memory_budget.read_process_rss_kb",
        return_value=4096,
    ):
        completed = scheduler.tick()
    persisted = store.load().attempts
    expected_attempt_ids = tuple(UUID(int=index + 1) for index in range(_FIXTURE_JOB_COUNT))
    actual_attempt_ids = tuple(item.attempt_id for item in completed)
    job_ids = tuple(item.definition.job_id for item in completed)
    required_primary_jobs = tuple(
        f"sec:company-document:{ticker}" for ticker in ("aapl", "mstr", "amzn", "cvx", "pltr")
    )
    if (
        len(set(actual_attempt_ids)) != _FIXTURE_JOB_COUNT
        or set(actual_attempt_ids) != set(expected_attempt_ids)
        or not set(required_primary_jobs).issubset(job_ids)
        or not job_ids
        or job_ids[-1] != "sec:institutional:13f-history"
    ):
        raise AssertionError("scratch cycle did not preserve the expected 102-job inventory")
    return {
        "terminal_count": sum(item.status.value != "running" for item in persisted),
        "success_count": sum(item.status.value == "succeeded" for item in persisted),
        "execution_digest": hashlib.sha256(
            "\n".join(_attempt_signature(item) for item in completed).encode("utf-8")
        ).hexdigest(),
        "attempts": tuple(completed),
        "provider_calls": provider_calls,
        "expected_attempt_ids": tuple(str(item) for item in expected_attempt_ids),
        "actual_attempt_ids": tuple(str(item) for item in actual_attempt_ids),
        "job_ids": job_ids,
        "required_primary_jobs": required_primary_jobs,
    }


class _TrackedConnection:
    def __init__(self, connection, scans: list[str]) -> None:
        self._connection = connection
        self._scans = scans

    def execute(self, query: str, parameters: object = None) -> _TrackedConnection:
        normalized = query.casefold()
        if "from metric_results" in normalized and "document_json" in normalized:
            self._scans.append(query)
        if parameters is None:
            self._connection.execute(query)
        else:
            self._connection.execute(query, parameters)
        return self

    def fetchall(self) -> list[tuple[object, ...]]:
        return self._connection.fetchall()

    def fetchone(self) -> tuple[object, ...] | None:
        return self._connection.fetchone()

    def close(self) -> None:
        self._connection.close()


def _full_measurement_once(database_path: Path) -> tuple[dict[str, object], int]:
    scans: list[str] = []
    connect = duckdb.connect

    def tracked_connect(*args: object, **kwargs: object) -> _TrackedConnection:
        return _TrackedConnection(connect(*args, **kwargs), scans)

    with patch.object(duckdb, "connect", tracked_connect):
        result = cycle_probe.database(database_path)
    return result, len(scans)


def _cost_case(root: Path, row_count: int) -> dict[str, object]:
    database_path = root / "storage" / "data" / "processed" / "fixture.duckdb"
    documents = _create_fixture_database(database_path, row_count)
    before_hash = _sha256(database_path)
    wal_path = Path(f"{database_path}.wal")
    before_wal_exists = wal_path.exists()

    baseline = _run_scheduler(root / "baseline", collector=None)
    collector = StorageObservabilityCollector(
        state_root=root / "observed" / "state",
        database_path=database_path,
        clock=lambda: _BASE_TIME,
    )
    original_begin = StorageObservabilityCollector.begin_attempt
    original_complete = StorageObservabilityCollector.complete_attempt
    original_row_count = storage_observability_module._table_row_count
    phase = ["idle"]
    phase_timings: dict[str, list[float]] = {"begin": [], "complete": []}
    select_counts: Counter[str] = Counter()
    connection_opens: Counter[str] = Counter()
    row_queries: Counter[str] = Counter()
    row_values: Counter[tuple[str, int]] = Counter()
    document_scans: list[str] = []
    operation_timings_ms: dict[str, list[float]] = {
        "open": [],
        "select": [],
        "append": [],
        "verify": [],
    }

    real_connect = duckdb.connect
    original_table_names = storage_observability_module._document_table_names
    original_append = StorageObservabilityCollector._append_line
    original_verify = StorageObservabilityCollector._verify_append

    def tracked_connect(*args: object, **kwargs: object):
        is_measurement_open = (
            args
            and Path(str(args[0])).resolve() == database_path.resolve()
            and kwargs.get("read_only")
        )
        started = monotonic_time.perf_counter()
        result = real_connect(*args, **kwargs)
        if is_measurement_open:
            connection_opens[phase[0]] += 1
            operation_timings_ms["open"].append((monotonic_time.perf_counter() - started) * 1000)
        return result

    def timed_begin(current: StorageObservabilityCollector, *, job_id: str, attempt_id: UUID):
        started = monotonic_time.perf_counter()
        phase[0] = "begin"
        before_opens = getattr(current, "_measurement_open_count", 0)
        before_selects = getattr(current, "_measurement_select_count", 0)
        try:
            return original_begin(current, job_id=job_id, attempt_id=attempt_id)
        finally:
            phase_timings["begin"].append((monotonic_time.perf_counter() - started) * 1000)
            if hasattr(current, "_measurement_open_count"):
                connection_opens["begin"] += current._measurement_open_count - before_opens
                select_counts["begin"] += current._measurement_select_count - before_selects
            phase[0] = "idle"

    def timed_complete(
        current: StorageObservabilityCollector,
        handle,
        observation: ScheduledJobObservation,
        *,
        execution_completed_at: datetime | None = None,
        result_persisted_at: datetime | None = None,
        job_execution_ms: int | None = None,
    ) -> object:
        started = monotonic_time.perf_counter()
        phase[0] = "complete"
        before_opens = getattr(current, "_measurement_open_count", 0)
        before_selects = getattr(current, "_measurement_select_count", 0)
        try:
            parameters = inspect.signature(original_complete).parameters
            arguments: dict[str, object] = {
                "execution_completed_at": execution_completed_at,
                "result_persisted_at": result_persisted_at,
            }
            if "job_execution_ms" in parameters:
                arguments["job_execution_ms"] = job_execution_ms
            return original_complete(
                current,
                handle,
                observation,
                **arguments,
            )
        finally:
            phase_timings["complete"].append((monotonic_time.perf_counter() - started) * 1000)
            if hasattr(current, "_measurement_open_count"):
                connection_opens["complete"] += current._measurement_open_count - before_opens
                select_counts["complete"] += current._measurement_select_count - before_selects
            phase[0] = "idle"

    def counted_row_count(connection, table_name: str) -> int:
        started = monotonic_time.perf_counter()
        result = original_row_count(connection, table_name)
        operation_timings_ms["select"].append((monotonic_time.perf_counter() - started) * 1000)
        row_queries[phase[0]] += 1
        select_counts[phase[0]] += 1
        row_values[(table_name, result)] += 1
        return result

    def counted_inventory(connection) -> tuple[str, ...]:
        started = monotonic_time.perf_counter()
        names = original_table_names(connection)
        operation_timings_ms["select"].append((monotonic_time.perf_counter() - started) * 1000)
        row_queries[phase[0]] += 1
        select_counts[phase[0]] += 1
        return names

    def timed_append(current, record) -> None:  # type: ignore[no-untyped-def]
        started = monotonic_time.perf_counter()
        try:
            original_append(current, record)
        finally:
            operation_timings_ms["append"].append((monotonic_time.perf_counter() - started) * 1000)

    def timed_verify(current, record) -> None:  # type: ignore[no-untyped-def]
        started = monotonic_time.perf_counter()
        try:
            original_verify(current, record)
        finally:
            operation_timings_ms["verify"].append((monotonic_time.perf_counter() - started) * 1000)

    def forbidden_document_scan(current: StorageObservabilityCollector):
        document_scans.append(str(current.artifact_path))
        raise AssertionError("per-attempt collection scanned document_json")

    with (
        patch.object(StorageObservabilityCollector, "begin_attempt", timed_begin),
        patch.object(StorageObservabilityCollector, "complete_attempt", timed_complete),
        patch.object(duckdb, "connect", tracked_connect),
        patch.object(
            StorageObservabilityCollector,
            "_measure_table_bytes",
            forbidden_document_scan,
        ),
        patch.object(StorageObservabilityCollector, "_append_line", timed_append),
        patch.object(StorageObservabilityCollector, "_verify_append", timed_verify),
        patch.object(storage_observability_module, "_table_row_count", counted_row_count),
        patch.object(storage_observability_module, "_document_table_names", counted_inventory),
    ):
        observed = _run_scheduler(root / "observed", collector=collector)

    state = collector.state()
    is_v3 = bool(state.records) and getattr(state.records[0], "schema_version", None) == (
        "storage-observability-v3"
    )
    if is_v3:
        assert connection_opens == Counter(
            {"begin": _FIXTURE_JOB_COUNT, "complete": _FIXTURE_JOB_COUNT}
        )
        assert select_counts == Counter(
            {"begin": _FIXTURE_JOB_COUNT + 1, "complete": _FIXTURE_JOB_COUNT}
        )
    else:
        assert connection_opens == Counter(
            {"begin": _FIXTURE_JOB_COUNT, "complete": _FIXTURE_JOB_COUNT}
        )
        assert select_counts == Counter(
            {"begin": 8 * _FIXTURE_JOB_COUNT, "complete": 8 * _FIXTURE_JOB_COUNT}
        )
    assert baseline["execution_digest"] == observed["execution_digest"]
    assert baseline["terminal_count"] == observed["terminal_count"] == _FIXTURE_JOB_COUNT
    assert observed["success_count"] == _FIXTURE_JOB_COUNT
    assert len(state.records) == _FIXTURE_JOB_COUNT
    assert all(not record.table_bytes for record in state.records)
    assert document_scans == []
    if not is_v3:
        assert row_queries == Counter(
            {"begin": 8 * _FIXTURE_JOB_COUNT, "complete": 8 * _FIXTURE_JOB_COUNT}
        )
    else:
        for record in state.records:
            assert record.table_rows_before is not None
            assert record.table_rows_after is not None
            for item in (*record.table_rows_before, *record.table_rows_after):
                row_values[(item.table_name, item.row_count)] += 1
    assert {value for _, value in row_values} == {0, row_count}
    assert all(record.growth is not None for record in state.records)
    per_attempt_latency = [
        round(begin_ms + complete_ms, 3)
        for begin_ms, complete_ms in zip(
            phase_timings["begin"], phase_timings["complete"], strict=True
        )
    ]
    duration_rows = [
        {key: value for key, value in record.durations.to_json_dict().items()}
        if hasattr(record.durations, "to_json_dict")
        else {}
        for record in state.records
    ]
    if is_v3:
        operation_timings_ms["open"] = [
            float(item["query_open_ms"])
            for item in duration_rows
            if isinstance(item.get("query_open_ms"), int)
        ]
        operation_timings_ms["select"] = [
            float(item["query_select_ms"])
            for item in duration_rows
            if isinstance(item.get("query_select_ms"), int)
        ]
    observed_table_counts = [
        {"table_name": table_name, "row_count": value, "observations": count}
        for (table_name, value), count in sorted(row_values.items())
    ]
    execution_digest = observed["execution_digest"]

    full_report, logical_scan_count = _full_measurement_once(database_path)
    expected_logical_bytes = sum(len(document.encode("utf-8")) for document in documents)
    assert logical_scan_count == 1
    assert full_report["metric_rows"] == row_count
    assert full_report["metric_document_bytes"] == expected_logical_bytes
    assert {item["metric_key"] for item in full_report["by_metric_key"]} == {
        "crypto.derivatives.fixture",
        "fixture.metric",
    }
    assert sum(item["rows"] for item in full_report["crypto_by_window"]) == row_count // 2
    assert sum(item["rows"] for item in full_report["recent_growth"]) == row_count
    after_hash = _sha256(database_path)
    after_wal_exists = wal_path.exists()
    assert before_hash == after_hash
    assert before_wal_exists == after_wal_exists is False

    return {
        "fixture_rows": row_count,
        "terminal_attempts": observed["terminal_count"],
        "observability_records": len(state.records),
        "unique_attempt_ids": len(set(observed["actual_attempt_ids"])),
        "expected_attempt_ids_sha256": hashlib.sha256(
            "\n".join(sorted(observed["expected_attempt_ids"])).encode("ascii")
        ).hexdigest(),
        "actual_attempt_ids_sha256": hashlib.sha256(
            "\n".join(sorted(observed["actual_attempt_ids"])).encode("ascii")
        ).hexdigest(),
        "provider_calls": observed["provider_calls"],
        "expected_attempt_ids": observed["expected_attempt_ids"],
        "actual_attempt_ids": observed["actual_attempt_ids"],
        "terminal_status_counts": {
            status: sum(item.status.value == status for item in observed["attempts"])
            for status in ("succeeded", "failed", "skipped")
        },
        "primary_document_jobs": observed["required_primary_jobs"],
        "last_job_id": observed["job_ids"][-1],
        "implementation_schema_versions": dict(
            Counter(record.schema_version for record in state.records)
        ),
        "measurement_coverage": {
            state_name: sum(
                getattr(record, "measurement_state", None) == state_name for record in state.records
            )
            for state_name in ("complete", "partial", "unavailable")
        },
        "execution_digest": execution_digest,
        "execution_digest_matches_without_collector": True,
        "payload_document_scans_per_attempt": 0,
        "full_logical_measurement_scans_after_cycle": logical_scan_count,
        "connection_opens_by_phase": dict(sorted(connection_opens.items())),
        "selects_by_phase": dict(sorted(select_counts.items())),
        "collector_workers_reaped": list(getattr(collector, "_measurement_worker_exit_codes", [])),
        "worker_memory_peak_kb": resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss,
        "rows_per_table_observed": {
            name: count for (name, count), _calls in sorted(row_values.items())
        },
        "table_count_observations": observed_table_counts,
        "per_attempt_latency_ms": per_attempt_latency,
        "record_durations_by_attempt": duration_rows,
        "operation_timings_ms": operation_timings_ms,
        "first_attempt_process_cold_ms": per_attempt_latency[0],
        "steady_attempt_hot_ms": per_attempt_latency[1:],
        "filesystem_page_cache": (
            "not forced or inferred; cold/hot labels refer to reader process lifecycle"
        ),
        "wall_seconds": round(sum(per_attempt_latency) / 1000, 6),
        "parent_memory_peak_kb": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        "phase_latency_ms": {
            name: {
                "count": len(values),
                "median": round(statistics.median(values), 3) if values else None,
                "max": round(max(values), 3) if values else None,
            }
            for name, values in phase_timings.items()
        },
        "logical_document_bytes": expected_logical_bytes,
        "database_sha256_unchanged": True,
        "wal_unchanged": True,
        "latency_interpretation": "descriptive; exact count(*) cost may vary with row count",
    }


def _job(start: datetime, end: datetime, index: int) -> dict[str, object]:
    attempt_id = str(UUID(int=10_000 + index))
    return {
        "job_id": f"fixture:asset-{index % 6}:job-{index:03d}",
        "attempt_id": attempt_id,
        "attempt_number": 1,
        "local_date": "2026-10-05",
        "status": "succeeded",
        "seconds": (end - start).total_seconds(),
        "started_at": start.isoformat(),
        "completed_at": end.isoformat(),
        "failure": None,
    }


def _memory_sample(
    at: datetime,
    *,
    pid: int = 123,
    release_sha: str = _FIXTURE_RELEASE,
) -> dict[str, object]:
    return {
        "at": at.isoformat(),
        "VmRSS": 1_000_000 + pid,
        "memory_current_bytes": 2_000_000 + pid,
        "memory_peak_bytes": 3_000_000 + pid,
        "memory_events": {"high": 4, "max": 0, "oom": 0, "oom_kill": 0},
        "pid": pid,
        "process_starttime_ticks": 45_678,
        "boot_id": "fixture-boot",
        "cgroup_generation": "fixture-cgroup",
        "release_sha": release_sha,
        "release_sha_state": "known",
        "sample_interval_seconds": 5,
    }


def _series_profile() -> dict[str, object]:
    start = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)
    cycle_seconds = 1_765
    jobs: list[dict[str, object]] = []
    for index in range(_FIXTURE_JOB_COUNT):
        offset = round(index * (cycle_seconds - 8) / (_FIXTURE_JOB_COUNT - 1))
        job_start = start + timedelta(seconds=offset)
        jobs.append(_job(job_start, job_start + timedelta(seconds=8), index))
    cycle_samples = [_memory_sample(start + timedelta(seconds=index * 5)) for index in range(354)]
    previous = [
        _memory_sample(
            datetime(2026, 10, 4, tzinfo=UTC) + timedelta(seconds=index * 10),
            pid=800 + index % 3,
            release_sha="b" * 40,
        )
        for index in range(4_000)
    ]
    following = [
        _memory_sample(
            datetime(2026, 10, 6, tzinfo=UTC) + timedelta(seconds=index * 10),
            pid=900 + index % 3,
            release_sha="c" * 40,
        )
        for index in range(4_065)
    ]
    samples = [*previous, *cycle_samples, *following]
    measured = cycle_probe.memory_by_job("2026-10-05", jobs, samples_data=samples)
    assert measured["loaded_sample_count"] == 8419
    assert measured["cycle_sample_count"] == measured["sample_count"] == 354
    assert measured["external_sample_count"] == 8065
    assert measured["comparable"] is True
    assert measured["release_sha"] == _FIXTURE_RELEASE

    changed = list(samples)
    changed[4_000 + 100] = {**changed[4_000 + 100], "pid": 124}
    changed_identity = cycle_probe.memory_by_job("2026-10-05", jobs, samples_data=changed)
    assert changed_identity["comparable"] is False
    assert changed_identity["comparison_reason"] == "process_or_cgroup_identity_changed"

    observations = [
        {
            "attempt_id": str(UUID(int=10_000 + index)),
            "measurement_state": "complete",
            "durations": {"total_ms": 7, "job_execution_ms": 5},
            "collector_overhead_ms": 2,
        }
        for index in range(_FIXTURE_JOB_COUNT)
    ]
    complete_coverage = cycle_probe._observability_coverage(
        {"all_jobs": jobs},
        {"today": observations},
        {"available": True, "reason_code": None},
    )
    missing_coverage = cycle_probe._observability_coverage(
        {"all_jobs": jobs},
        {"today": []},
        {"available": True, "reason_code": "measurement_timeout"},
    )
    partial_observations = list(observations)
    partial_observations[-1] = {
        **partial_observations[-1],
        "measurement_state": "unavailable",
        "collector_overhead_ms": None,
    }
    partial_coverage = cycle_probe._observability_coverage(
        {"all_jobs": jobs},
        {"today": partial_observations},
        {"available": True, "reason_code": "measurement_timeout"},
    )
    assert complete_coverage["expected_terminal_attempts"] == 102
    assert complete_coverage["observed_attempts"] == 102
    assert complete_coverage["missing_attempts"] == 0
    assert complete_coverage["collector_overhead_ms_known"] == 204
    assert complete_coverage["collector_overhead_ms_unknown_attempts"] == 0
    assert complete_coverage["measurement_coverage"] == {
        "state": "complete",
        "complete_attempts": 102,
        "partial_attempts": 0,
        "unavailable_attempts": 0,
        "unknown_attempts": 0,
    }
    assert partial_coverage["state"] == "complete"
    assert partial_coverage["observed_attempts"] == 102
    assert partial_coverage["measurement_coverage"]["state"] == "partial"
    assert partial_coverage["measurement_coverage"]["complete_attempts"] == 101
    assert partial_coverage["measurement_coverage"]["unavailable_attempts"] == 1
    assert missing_coverage["missing_attempts"] == 102
    assert missing_coverage["collector_overhead_ms_known"] is None
    assert missing_coverage["collector_overhead_ms_unknown_attempts"] == 102
    assert missing_coverage["latest_scheduler_reason_code"] == "measurement_timeout"

    return {
        "loaded_samples_from_three_days": measured["loaded_sample_count"],
        "cycle_samples": measured["cycle_sample_count"],
        "external_samples_excluded": measured["external_sample_count"],
        "cycle_release_sha": measured["release_sha"],
        "cycle_comparable": measured["comparable"],
        "internal_identity_change_comparable": changed_identity["comparable"],
        "complete_coverage": {
            "expected": complete_coverage["expected_terminal_attempts"],
            "observed": complete_coverage["observed_attempts"],
            "missing": complete_coverage["missing_attempts"],
            "collector_overhead_ms_known": complete_coverage["collector_overhead_ms_known"],
            "measurement": complete_coverage["measurement_coverage"],
        },
        "partial_measurement_coverage": partial_coverage["measurement_coverage"],
        "missing_coverage": {
            "expected": missing_coverage["expected_terminal_attempts"],
            "observed": missing_coverage["observed_attempts"],
            "missing": missing_coverage["missing_attempts"],
            "collector_overhead_ms_known": missing_coverage["collector_overhead_ms_known"],
            "collector_overhead_ms_unknown_attempts": missing_coverage[
                "collector_overhead_ms_unknown_attempts"
            ],
            "reason_code": missing_coverage["latest_scheduler_reason_code"],
        },
        "inter_job_gap_seconds": complete_coverage["inter_job_gap_seconds"],
        "inter_job_gap_attribution": complete_coverage["inter_job_gap_attribution"],
    }


def _rollover_profile(root: Path) -> dict[str, object]:
    database_path = root / "storage" / "data" / "processed" / "rollover.duckdb"
    _create_fixture_database(database_path, 1)
    first_day = date(2026, 9, 5)
    for offset in range(31):
        local_day = first_day + timedelta(days=offset)
        moment = datetime.combine(local_day, time(hour=17), tzinfo=UTC)
        attempt_id = UUID(int=20_000 + offset)
        collector = StorageObservabilityCollector(
            state_root=root / "state",
            database_path=database_path,
            clock=lambda value=moment: value,
        )
        handle = collector.begin_attempt(job_id="fixture:rollover", attempt_id=attempt_id)
        record = collector.complete_attempt(
            handle,
            ScheduledJobObservation(
                attempt_id=attempt_id,
                job_id="fixture:rollover",
                attempt_number=1,
                local_date=local_day,
                attempt_status="succeeded",
                evidence_changed=False,
                rows_created=0,
                rows_reused=1,
            ),
        )
        assert record.table_bytes == ()

    final_state = StorageObservabilityCollector(
        state_root=root / "state",
        database_path=database_path,
        clock=lambda: _BASE_TIME,
    ).state()
    report = StorageObservabilityReportService(state_root=root / "state").report()
    window_30 = next(window for window in report.windows if window.window_days == 30)
    assert len(final_state.daily_snapshots) == 30
    assert len(final_state.records) == 1
    assert window_30.attempts == 30
    assert not window_30.missing_days
    assert report.budget is not None
    return {
        "collector_instances": 31,
        "retained_daily_snapshots": len(final_state.daily_snapshots),
        "open_records": len(final_state.records),
        "budget_report_schema": report.schema_version,
        "closed_30_day_attempts": window_30.attempts,
        "closed_30_day_missing_days": len(window_30.missing_days),
        "budget_reader_available": report.budget is not None,
    }


def _failure_profile(root: Path) -> dict[str, object]:
    database_path = root / "storage" / "data" / "processed" / "failure.duckdb"
    _create_fixture_database(database_path, 1)
    definition = _definitions(1)[0]
    invocations: Counter[str] = Counter()

    def run(invocation) -> ScheduledJobExecution:
        invocations[invocation.definition.job_id] += 1
        return ScheduledJobExecution(
            job_id=invocation.definition.job_id,
            effective_known_at=invocation.started_at,
            evidence_changed=False,
            source_ids=("fixture-source:failure",),
            created_count=0,
            reused_count=1,
        )

    class FailingBeginCollector(StorageObservabilityCollector):
        def begin_attempt(self, *, job_id: str, attempt_id: UUID):
            raise StorageObservabilityError(
                "sensitive fixture path must not escape", reason_code="engine_unavailable"
            )

    class FailingCompleteCollector(StorageObservabilityCollector):
        def complete_attempt(
            self,
            handle,
            observation: ScheduledJobObservation,
            *,
            execution_completed_at: datetime | None = None,
            result_persisted_at: datetime | None = None,
            job_execution_ms: int | None = None,
        ) -> object:
            raise StorageObservabilityError(
                "sensitive fixture path must not escape", reason_code="measurement_timeout"
            )

    results: dict[str, object] = {}
    for index, collector_type in enumerate((FailingBeginCollector, FailingCompleteCollector)):
        schedule_root = root / f"failure-{index}"
        calls_before = invocations[definition.job_id]
        store = MultiAssetScheduleStateStore(schedule_root / "schedule.json")
        collector = collector_type(
            state_root=schedule_root / "state",
            database_path=database_path,
            clock=lambda: _BASE_TIME,
        )
        scheduler = MultiAssetScheduler(
            (RegisteredScheduledJob(definition, run),),
            store,
            storage_observability=collector,
            clock=lambda: _BASE_TIME,
            attempt_id_factory=lambda index=index: UUID(int=30_000 + index),
        )
        first = scheduler.tick()
        second = scheduler.tick()
        issues = scheduler.status().issues
        assert len(first) == 1
        assert not second
        assert first[0].status.value == "succeeded"
        assert len(store.load().attempts) == 1
        assert invocations[definition.job_id] - calls_before == 1
        assert "sensitive fixture path" not in " ".join(issues)
        expected_reason = "engine_unavailable" if index == 0 else "measurement_timeout"
        assert f"storage observability failure reason: {expected_reason}" in issues
        results["begin" if index == 0 else "complete"] = {
            "terminal_status": first[0].status.value,
            "provider_call_count": invocations[definition.job_id] - calls_before,
            "issue_reason_codes": [item.rsplit(": ", 1)[-1] for item in issues if ": " in item],
            "exception_text_exposed": False,
        }
    return results


def _scheduled_fault_injections(root: Path) -> dict[str, object]:
    database_path = root / "storage" / "data" / "processed" / "fault-injection.duckdb"
    _create_fixture_database(database_path, 1)
    first = ScheduledJobDefinition(
        job_id="fixture:observer:before-13f",
        asset_id="fixture:asset:observer",
        provider="offline-fixture",
        domain=ScheduledJobDomain.MARKET_DAILY,
        data_frequency="day_1",
        timezone="America/Lima",
        run_at=time(hour=7),
        max_attempts_per_day=1,
    )
    last = ScheduledJobDefinition(
        job_id="sec:institutional:13f-history",
        asset_id="equity:us:aapl",
        provider="sec-edgar",
        domain=ScheduledJobDomain.FUNDAMENTALS,
        data_frequency="day_1",
        timezone="America/Lima",
        run_at=time(hour=7),
        max_attempts_per_day=1,
    )
    scenarios = {
        "open_timeout": ("measurement", "begin", "measurement_timeout"),
        "query_timeout": ("measurement", "end", "measurement_timeout"),
        "engine_unavailable": ("measurement", "begin", "engine_unavailable"),
        "append_failure": ("append", "append", "artifact_write_failed"),
        "verify_after_append": ("verify", "verify", "artifact_invalid"),
    }
    results: dict[str, object] = {}

    def runner_for(
        calls: Counter[str],
    ) -> Callable[[ScheduledJobInvocation], ScheduledJobExecution]:
        def run(invocation: ScheduledJobInvocation) -> ScheduledJobExecution:
            calls[invocation.definition.job_id] += 1
            return ScheduledJobExecution(
                job_id=invocation.definition.job_id,
                effective_known_at=invocation.started_at,
                evidence_changed=False,
                source_ids=(f"fixture-source:{invocation.definition.asset_id}",),
                created_count=0,
                reused_count=1,
            )

        return run

    def id_factory(start: int) -> Callable[[], UUID]:
        identifiers: Iterator[UUID] = iter(UUID(int=value) for value in range(start, start + 2))
        return lambda: next(identifiers)

    def collector_for(
        *,
        fault_kind: str,
        phase_name: str,
        fault_name: str,
        failure_reason: str,
        last_job_id: str,
    ) -> type[StorageObservabilityCollector]:
        class InjectedCollector(StorageObservabilityCollector):
            def __init__(self, **arguments: object) -> None:
                super().__init__(**arguments)  # type: ignore[arg-type]
                self.active_job_id = ""
                self.read_requests: Counter[str] = Counter()
                self.append_faulted = False
                self.verify_faulted = False

            def begin_attempt(self, *, job_id: str, attempt_id: UUID):
                self.active_job_id = job_id
                return super().begin_attempt(job_id=job_id, attempt_id=attempt_id)

            def _request_read_measurement(self):
                self.read_requests[self.active_job_id] += 1
                request_number = self.read_requests[self.active_job_id]
                if (
                    fault_kind == "measurement"
                    and self.active_job_id == last_job_id
                    and (
                        (phase_name == "begin" and request_number == 1)
                        or (phase_name == "end" and request_number == 2)
                    )
                ):
                    raise StorageObservabilityError(
                        f"injected {fault_name}", reason_code=failure_reason
                    )
                return super()._request_read_measurement()

            def _append_line(self, record) -> None:  # type: ignore[no-untyped-def]
                if (
                    fault_kind == "append"
                    and record.job_id == last_job_id
                    and not self.append_faulted
                ):
                    self.append_faulted = True
                    raise StorageObservabilityError(
                        "injected append failure", reason_code="artifact_write_failed"
                    )
                super()._append_line(record)

            def _verify_append(self, record) -> None:  # type: ignore[no-untyped-def]
                super()._verify_append(record)
                if (
                    fault_kind == "verify"
                    and record.job_id == last_job_id
                    and not self.verify_faulted
                ):
                    self.verify_faulted = True
                    raise StorageObservabilityError(
                        "injected verification failure", reason_code="artifact_invalid"
                    )

        return InjectedCollector

    for scenario_index, (name, (fault_type, target_phase, reason)) in enumerate(scenarios.items()):
        schedule_root = root / name
        provider_calls: Counter[str] = Counter()
        runner = runner_for(provider_calls)
        collector_type = collector_for(
            fault_kind=fault_type,
            phase_name=target_phase,
            fault_name=name,
            failure_reason=reason,
            last_job_id=last.job_id,
        )
        collector = collector_type(
            state_root=schedule_root / "state",
            database_path=database_path,
            clock=lambda: _BASE_TIME,
        )
        store = MultiAssetScheduleStateStore(schedule_root / "schedule.json")
        scheduler = MultiAssetScheduler(
            (RegisteredScheduledJob(first, runner), RegisteredScheduledJob(last, runner)),
            store,
            storage_observability=collector,
            clock=lambda: _BASE_TIME,
            attempt_id_factory=id_factory(70_000 + scenario_index * 10 + 1),
        )
        terminal = scheduler.tick()
        request_count_before_retry = collector.read_requests[last.job_id]
        retry = scheduler.tick()
        records = collector.state().records
        terminal_attempts = store.load().attempts
        last_attempt = next(
            item for item in terminal_attempts if item.definition.job_id == last.job_id
        )
        last_records = tuple(item for item in records if item.job_id == last.job_id)
        if (
            len(terminal) != 2
            or len(retry) != 0
            or len(terminal_attempts) != 2
            or len({item.attempt_id for item in records}) != 2
            or len(last_records) != 1
            or provider_calls != Counter({first.job_id: 1, last.job_id: 1})
            or last_attempt.status.value != "succeeded"
            or collector.read_requests[last.job_id] != request_count_before_retry
        ):
            raise AssertionError(f"fault injection {name} changed job execution or duplicated data")
        record = last_records[0]
        if fault_type == "measurement":
            if record.measurement_state != "partial" or record.failure_reason != reason:
                raise AssertionError(f"fault injection {name} lost its measurement failure")
        elif record.measurement_state != "complete":
            raise AssertionError(f"fault injection {name} changed a complete measurement")
        results[name] = {
            "injected_phase": target_phase,
            "injected_reason": reason,
            "provider_calls": dict(provider_calls),
            "terminal_attempts": len(terminal_attempts),
            "unique_envelopes": len({item.attempt_id for item in records}),
            "other_observer_ran": provider_calls[first.job_id] == 1,
            "last_job_id": last_attempt.definition.job_id,
            "last_job_status": last_attempt.status.value,
            "last_measurement_state": record.measurement_state,
            "last_failure_phase": record.failure_phase,
            "last_failure_reason": record.failure_reason,
            "records_for_last_attempt": len(last_records),
            "retry_tick_provider_reruns": 0,
            "retry_tick_new_measurements": 0,
            "artifact_lines": len(records),
            "injected_once": True,
        }
    return results


def _distribution(values: list[float]) -> dict[str, object]:
    ordered = sorted(values)
    if not ordered:
        return {"count": 0, "median": None, "pstdev": None, "p95": None, "min": None, "max": None}
    return {
        "count": len(ordered),
        "median": round(statistics.median(ordered), 3),
        "pstdev": round(statistics.pstdev(ordered), 3),
        "p95": round(ordered[max(0, (95 * len(ordered) + 99) // 100 - 1)], 3),
        "min": round(ordered[0], 3),
        "max": round(ordered[-1], 3),
    }


def _extract_verified_base_source(destination: Path) -> Path:
    resolved = subprocess.run(
        ["git", "rev-parse", f"{_WORK_BLOCK_BASE_SHA}^{{commit}}"],
        cwd=_REPOSITORY_ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if resolved != _WORK_BLOCK_BASE_SHA:
        raise RuntimeError("ABBA base commit does not match the declared Work Block base")
    archive = subprocess.run(
        ["git", "archive", _WORK_BLOCK_BASE_SHA, "src"],
        cwd=_REPOSITORY_ROOT,
        check=True,
        capture_output=True,
    ).stdout
    destination.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as bundle:
        members = bundle.getmembers()
        if any(
            not (
                member.name == "src"
                and member.isdir()
                or member.name.startswith("src/")
                and (member.isdir() or member.isfile())
            )
            for member in members
        ):
            raise RuntimeError("ABBA base archive contains an unexpected path or file type")
        bundle.extractall(destination, members=members, filter="data")
    source = destination / "src"
    if not (source / "investment_analyst").is_dir():
        raise RuntimeError("ABBA base archive does not contain the application source")
    return source


def _run_cost_subprocess(
    *,
    release: str,
    source_root: Path,
    scratch_root: Path,
    row_count: int,
) -> dict[str, object]:
    environment = os.environ.copy()
    environment.pop("SEC_USER_AGENT", None)
    environment["PYTHONPATH"] = str(source_root)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--single-profile",
        "--row-count",
        str(row_count),
        "--scratch-root",
        str(scratch_root),
    ]
    started = monotonic_time.perf_counter()
    completed = subprocess.run(
        command,
        cwd=_REPOSITORY_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    elapsed_ms = (monotonic_time.perf_counter() - started) * 1000
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout)[-4000:]
        raise RuntimeError(
            f"ABBA {release} profile N={row_count} exited {completed.returncode}: {detail}"
        )
    try:
        profile = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise RuntimeError(f"ABBA {release} profile output was not valid JSON") from error
    if not isinstance(profile, dict):
        raise RuntimeError(f"ABBA {release} profile output must be an object")
    return {
        "release": release,
        "exit_code": completed.returncode,
        "process_wall_ms": round(elapsed_ms, 3),
        "worker_exit_codes": profile.get("collector_workers_reaped", []),
        "profile": profile,
    }


def _paired_cost_profile(root: Path) -> dict[str, object]:
    base_source = _extract_verified_base_source(root / "verified-base")
    candidate_source = _REPOSITORY_ROOT / "src"
    profiles: dict[str, object] = {}
    for row_count in _FIXTURE_ROW_COUNTS:
        samples: list[dict[str, object]] = []
        for position, release in enumerate(("base", "candidate", "candidate", "base")):
            source = base_source if release == "base" else candidate_source
            sample = _run_cost_subprocess(
                release=release,
                source_root=source,
                scratch_root=root / f"n-{row_count}" / f"position-{position + 1}",
                row_count=row_count,
            )
            sample["abba_position"] = position + 1
            samples.append(sample)
        signatures = {str(sample["profile"].get("execution_digest")) for sample in samples}
        if len(signatures) != 1:
            raise AssertionError(f"ABBA N={row_count} changed terminal execution semantics")
        table_counts = {
            json.dumps(
                sample["profile"].get("table_count_observations"),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            for sample in samples
        }
        if len(table_counts) != 1:
            raise AssertionError(f"ABBA N={row_count} did not measure the same table inventory")
        base = [sample for sample in samples if sample["release"] == "base"]
        candidate = [sample for sample in samples if sample["release"] == "candidate"]
        base_select_totals = {
            sum(profile["selects_by_phase"].values())
            for profile in (sample["profile"] for sample in base)
        }
        candidate_select_totals = {
            sum(profile["selects_by_phase"].values())
            for profile in (sample["profile"] for sample in candidate)
        }
        if len(base_select_totals) != 1 or len(candidate_select_totals) != 1:
            raise AssertionError(f"ABBA N={row_count} SELECT counts varied between positions")
        base_select_total = next(iter(base_select_totals))
        candidate_select_total = next(iter(candidate_select_totals))
        base_selects_per_attempt = base_select_total / _FIXTURE_JOB_COUNT
        candidate_selects_per_attempt = candidate_select_total / _FIXTURE_JOB_COUNT
        if base_select_total <= 0 or candidate_select_total > base_select_total:
            raise AssertionError(f"ABBA N={row_count} did not reduce bounded SELECT work")
        select_reduction = (base_select_total - candidate_select_total) / base_select_total
        if select_reduction < 0.5:
            raise AssertionError(f"ABBA N={row_count} reduced SELECTs by less than 50%")
        base_steady = [
            float(value) for sample in base for value in sample["profile"]["steady_attempt_hot_ms"]
        ]
        candidate_steady = [
            float(value)
            for sample in candidate
            for value in sample["profile"]["steady_attempt_hot_ms"]
        ]
        base_median = statistics.median(base_steady)
        candidate_median = statistics.median(candidate_steady)
        if candidate_median >= base_median:
            raise AssertionError(
                f"ABBA N={row_count} candidate steady median {candidate_median:.3f} ms "
                f"is not below baseline {base_median:.3f} ms; base spread="
                f"{_distribution(base_steady)}; candidate spread={_distribution(candidate_steady)}"
            )
        profiles[str(row_count)] = {
            "fixture_rows": row_count,
            "table_count_observations_sha256": hashlib.sha256(
                next(iter(table_counts)).encode("utf-8")
            ).hexdigest(),
            "execution_digest": next(iter(signatures)),
            "abba_order": [sample["release"] for sample in samples],
            "baseline_select_count_total": base_select_total,
            "candidate_select_count_total": candidate_select_total,
            "baseline_selects_per_attempt": round(base_selects_per_attempt, 4),
            "candidate_selects_per_attempt": round(candidate_selects_per_attempt, 4),
            "select_reduction_fraction": round(select_reduction, 4),
            "candidate_median_below_baseline": candidate_median < base_median,
            "baseline_steady_attempts_ms": _distribution(base_steady),
            "candidate_steady_attempts_ms": _distribution(candidate_steady),
            "cold_first_attempt_ms": {
                release: [
                    sample["profile"]["first_attempt_process_cold_ms"]
                    for sample in samples
                    if sample["release"] == release
                ]
                for release in ("base", "candidate")
            },
            "samples": samples,
            "paired_cost_interpretation": (
                "First reader-process attempt is reported as cold-start; subsequent attempts are "
                "reported as steady/hot. Filesystem page cache is not forcibly cleared or inferred."
            ),
        }
    return {
        "schema_version": "collector-paired-cost-abba-v1",
        "base_sha": _WORK_BLOCK_BASE_SHA,
        "row_counts": list(_FIXTURE_ROW_COUNTS),
        "table_count": 7,
        "attempts_per_position": _FIXTURE_JOB_COUNT,
        "abba_sequence_per_profile": ["base", "candidate", "candidate", "base"],
        "environment": {
            "python": platform.python_version(),
            "duckdb": duckdb.__version__,
            "platform": platform.platform(),
            "fresh_process_per_position": True,
            "scratch_only": True,
        },
        "profiles": profiles,
    }


def run_smoke(
    *,
    scratch_parent: Path = Path("/tmp"),
    include_abba: bool = True,
) -> dict[str, object]:
    scratch_parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix="operational-observability-", dir=scratch_parent
    ) as name:
        root = Path(name)
        paired_cost = _paired_cost_profile(root / "abba") if include_abba else None
        cost = (
            {
                str(row_count): _cost_case(root / f"cost-{row_count}", row_count)
                for row_count in _FIXTURE_ROW_COUNTS
            }
            if paired_cost is None
            else {
                str(row_count): next(
                    sample["profile"]
                    for sample in paired_cost["profiles"][str(row_count)]["samples"]
                    if sample["release"] == "candidate"
                )
                for row_count in _FIXTURE_ROW_COUNTS
            }
        )
        series = _series_profile()
        series["restart_and_rollover"] = _rollover_profile(root / "rollover")
        series["collector_failure_isolation"] = _failure_profile(root / "failures")
        series["scheduled_fault_injections"] = _scheduled_fault_injections(root / "faults")
        if include_abba:
            failure_profile = series["collector_failure_isolation"]
            cycle_profile = cost[str(_FIXTURE_ROW_COUNTS[0])]
            terminal_attempts = {
                "expected_terminal_attempts": _FIXTURE_JOB_COUNT,
                "observed_terminal_attempts": cycle_profile["terminal_attempts"],
                "expected_unique_attempt_ids": _FIXTURE_JOB_COUNT,
                "observed_unique_attempt_ids": cycle_profile["unique_attempt_ids"],
                "expected_attempt_ids_sha256": cycle_profile["expected_attempt_ids_sha256"],
                "actual_attempt_ids_sha256": cycle_profile["actual_attempt_ids_sha256"],
                "attempt_id_sha256_matches": (
                    cycle_profile["expected_attempt_ids_sha256"]
                    == cycle_profile["actual_attempt_ids_sha256"]
                ),
                "provider_calls": cycle_profile["provider_calls"],
                "complete_measurement_attempts": cycle_profile["measurement_coverage"]["complete"],
                "primary_document_jobs": cycle_profile["primary_document_jobs"],
                "last_job_id": cycle_profile["last_job_id"],
                "fault_injections": series["scheduled_fault_injections"],
                "begin_failure_smoke": failure_profile["begin"],
                "complete_failure_smoke": failure_profile["complete"],
            }
    code_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=_REPOSITORY_ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    working_tree = subprocess.run(
        ["git", "status", "--short"],
        cwd=_REPOSITORY_ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    return {
        "schema_version": "operational-observability-smoke-v1",
        "status": "pass",
        "captured_at": datetime.now(UTC).isoformat(),
        "code_sha": code_sha,
        "working_tree_clean": not working_tree,
        "command": "scripts/smoke_operational_observability.py",
        "environment": {
            "python": platform.python_version(),
            "duckdb": duckdb.__version__,
            "platform": platform.system(),
            "scratch_only": True,
            "provider_calls": 0,
        },
        "profiles": {
            "operational_observability_cost": cost,
            "operational_observability_series": series,
        },
        "deliverables": (
            {
                "collector_paired_cost": paired_cost,
                "collector_102_terminal_attempts": terminal_attempts,
            }
            if include_abba
            else {}
        ),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="optional durable local JSON output path")
    parser.add_argument("--single-profile", action="store_true")
    parser.add_argument("--row-count", type=int, choices=_FIXTURE_ROW_COUNTS)
    parser.add_argument("--scratch-root", type=Path)
    arguments = parser.parse_args(argv)
    if arguments.single_profile:
        if arguments.row_count is None or arguments.scratch_root is None:
            parser.error("--single-profile requires --row-count and --scratch-root")
        result = _cost_case(arguments.scratch_root, arguments.row_count)
    else:
        if arguments.row_count is not None or arguments.scratch_root is not None:
            parser.error("--row-count and --scratch-root require --single-profile")
        result = run_smoke()
    rendered = json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    if arguments.output is not None:
        arguments.output.parent.mkdir(parents=True, exist_ok=True)
        arguments.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
