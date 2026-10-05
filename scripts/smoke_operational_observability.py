#!/usr/bin/env python3
"""Finite offline smoke for per-attempt collection and cycle-linked observability."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import platform
import statistics
import tempfile
import time as monotonic_time
from collections import Counter
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
)
from investment_analyst.application.storage_observability import (
    ScheduledJobObservation,
    StorageObservabilityCollector,
    StorageObservabilityError,
    StorageObservabilityRecord,
)
from investment_analyst.application.storage_observability_report import (
    StorageObservabilityReportService,
)

_FIXTURE_JOB_COUNT = 102
_FIXTURE_ROW_COUNTS = (257, 1537)
_FIXTURE_RELEASE = "a" * 40
_BASE_TIME = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)


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
        connection.executemany(
            "INSERT INTO metric_results VALUES (?, ?, current_timestamp)",
            [(metric_key, document) for metric_key, document in documents],
        )
    finally:
        connection.close()
    return tuple(document for _, document in documents)


def _definitions(count: int) -> tuple[ScheduledJobDefinition, ...]:
    return tuple(
        ScheduledJobDefinition(
            job_id=f"fixture:asset-{index % 6}:job-{index:03d}",
            asset_id=f"fixture:asset-{index % 6}",
            provider="offline-fixture",
            domain=ScheduledJobDomain.MARKET_DAILY,
            data_frequency="day_1",
            timezone="America/Lima",
            run_at=time(hour=7),
        )
        for index in range(count)
    )


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

    def run(invocation) -> ScheduledJobExecution:
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
    return {
        "terminal_count": sum(item.status.value != "running" for item in persisted),
        "success_count": sum(item.status.value == "succeeded" for item in persisted),
        "execution_digest": hashlib.sha256(
            "\n".join(_attempt_signature(item) for item in completed).encode("utf-8")
        ).hexdigest(),
        "attempts": tuple(completed),
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
    row_queries: Counter[str] = Counter()
    row_values: Counter[tuple[str, int]] = Counter()
    document_scans: list[str] = []

    def timed_begin(current: StorageObservabilityCollector, *, job_id: str, attempt_id: UUID):
        started = monotonic_time.perf_counter()
        phase[0] = "begin"
        try:
            return original_begin(current, job_id=job_id, attempt_id=attempt_id)
        finally:
            phase_timings["begin"].append((monotonic_time.perf_counter() - started) * 1000)
            phase[0] = "idle"

    def timed_complete(
        current: StorageObservabilityCollector,
        handle,
        observation: ScheduledJobObservation,
        *,
        execution_completed_at: datetime | None = None,
        result_persisted_at: datetime | None = None,
    ) -> StorageObservabilityRecord:
        started = monotonic_time.perf_counter()
        phase[0] = "complete"
        try:
            return original_complete(
                current,
                handle,
                observation,
                execution_completed_at=execution_completed_at,
                result_persisted_at=result_persisted_at,
            )
        finally:
            phase_timings["complete"].append((monotonic_time.perf_counter() - started) * 1000)
            phase[0] = "idle"

    def counted_row_count(connection, table_name: str) -> int:
        result = original_row_count(connection, table_name)
        row_queries[phase[0]] += 1
        row_values[(table_name, result)] += 1
        return result

    def forbidden_document_scan(current: StorageObservabilityCollector):
        document_scans.append(str(current.artifact_path))
        raise AssertionError("per-attempt collection scanned document_json")

    with (
        patch.object(StorageObservabilityCollector, "begin_attempt", timed_begin),
        patch.object(StorageObservabilityCollector, "complete_attempt", timed_complete),
        patch.object(
            StorageObservabilityCollector,
            "_measure_table_bytes",
            forbidden_document_scan,
        ),
        patch.object(storage_observability_module, "_table_row_count", counted_row_count),
    ):
        observed = _run_scheduler(root / "observed", collector=collector)

    state = collector.state()
    assert baseline["execution_digest"] == observed["execution_digest"]
    assert baseline["terminal_count"] == observed["terminal_count"] == _FIXTURE_JOB_COUNT
    assert observed["success_count"] == _FIXTURE_JOB_COUNT
    assert len(state.records) == _FIXTURE_JOB_COUNT
    assert all(not record.table_bytes for record in state.records)
    assert document_scans == []
    assert row_queries == Counter(
        {"begin": _FIXTURE_JOB_COUNT * 2, "complete": _FIXTURE_JOB_COUNT * 2}
    )
    assert {value for _, value in row_values} == {0, row_count}
    assert all(record.growth is not None for record in state.records)

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
        "execution_digest_matches_without_collector": True,
        "payload_document_scans_per_attempt": 0,
        "full_logical_measurement_scans_after_cycle": logical_scan_count,
        "row_count_queries_by_phase": dict(sorted(row_queries.items())),
        "rows_per_table_observed": {
            name: count for (name, count), _calls in sorted(row_values.items())
        },
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
    assert complete_coverage["expected_terminal_attempts"] == 102
    assert complete_coverage["observed_attempts"] == 102
    assert complete_coverage["missing_attempts"] == 0
    assert complete_coverage["collector_overhead_ms_known"] == 204
    assert complete_coverage["collector_overhead_ms_unknown_attempts"] == 0
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
        },
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
        ) -> StorageObservabilityRecord:
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


def run_smoke(*, scratch_parent: Path = Path("/tmp")) -> dict[str, object]:
    scratch_parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix="operational-observability-", dir=scratch_parent
    ) as name:
        root = Path(name)
        cost = {
            str(row_count): _cost_case(root / f"cost-{row_count}", row_count)
            for row_count in _FIXTURE_ROW_COUNTS
        }
        series = _series_profile()
        series["restart_and_rollover"] = _rollover_profile(root / "rollover")
        series["collector_failure_isolation"] = _failure_profile(root / "failures")
    return {
        "schema_version": "operational-observability-smoke-v1",
        "status": "pass",
        "captured_at": datetime.now(UTC).isoformat(),
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
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="optional durable local JSON output path")
    arguments = parser.parse_args(argv)
    result = run_smoke()
    rendered = json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    if arguments.output is not None:
        arguments.output.parent.mkdir(parents=True, exist_ok=True)
        arguments.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
