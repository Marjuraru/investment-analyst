"""End-to-end scratch flow for scheduler durability and operational measurements."""

from __future__ import annotations

import importlib.util
import json
import pathlib
import sys
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from uuid import UUID

from investment_analyst.application.multi_asset_scheduler import (
    MultiAssetScheduler,
    MultiAssetScheduleStateStore,
    RegisteredScheduledJob,
    ScheduledJobAttempt,
    ScheduledJobDefinition,
    ScheduledJobDomain,
    ScheduledJobExecution,
    ScheduledJobFailureCategory,
    ScheduledJobInvocation,
    ScheduledJobRunError,
    scheduled_job_failure,
)
from investment_analyst.application.scheduled_observers import ScheduledJobObserverChain
from investment_analyst.application.storage_observability import (
    ScheduledJobObservation,
    StorageObservabilityCollector,
)
from investment_analyst.storage import LocalStorage, StoragePaths

SCRIPTS = pathlib.Path(__file__).resolve().parents[3] / "scripts"
_cycle_spec = importlib.util.spec_from_file_location("cycle_probe_flow", SCRIPTS / "cycle_probe.py")
assert _cycle_spec is not None and _cycle_spec.loader is not None
cycle_probe = importlib.util.module_from_spec(_cycle_spec)
sys.modules["cycle_probe_flow"] = cycle_probe
_cycle_spec.loader.exec_module(cycle_probe)


class _FailFirstCollector(StorageObservabilityCollector):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.completions = 0

    def complete_attempt(
        self,
        handle,
        observation: ScheduledJobObservation,
        *,
        execution_completed_at: datetime | None = None,
        result_persisted_at: datetime | None = None,
    ):
        self.completions += 1
        if self.completions == 1:
            raise OSError("scratch collector failure with private details")
        return super().complete_attempt(
            handle,
            observation,
            execution_completed_at=execution_completed_at,
            result_persisted_at=result_persisted_at,
        )


def _definition(job_id: str, asset_id: str) -> ScheduledJobDefinition:
    return ScheduledJobDefinition(
        job_id=job_id,
        asset_id=asset_id,
        provider="scratch-only",
        domain=ScheduledJobDomain.MARKET_DAILY,
        data_frequency="day_1",
        timezone="America/Lima",
        run_at=time(hour=19),
        max_attempts_per_day=2,
        retry_backoff_seconds=60,
    )


def _execution(invocation: ScheduledJobInvocation) -> ScheduledJobExecution:
    return ScheduledJobExecution(
        job_id=invocation.definition.job_id,
        effective_known_at=invocation.started_at,
        evidence_changed=True,
        source_ids=(f"scratch:{invocation.definition.asset_id}",),
        created_count=1,
        reused_count=0,
    )


def test_scratch_scheduler_restart_observers_collector_and_memory_jsonl(
    tmp_path: Path,
) -> None:
    day = date(2026, 9, 24)
    now = [datetime(2026, 9, 25, 0, 0, tzinfo=UTC)]  # 19:00 on the requested Lima day.
    definitions = (
        _definition("asset-a", "equity:test:a"),
        _definition("asset-b", "equity:test:b"),
    )
    provider_calls = {definition.job_id: 0 for definition in definitions}

    def run_a(invocation: ScheduledJobInvocation) -> ScheduledJobExecution:
        provider_calls[invocation.definition.job_id] += 1
        now[0] += timedelta(seconds=10)
        return _execution(invocation)

    def run_b(invocation: ScheduledJobInvocation) -> ScheduledJobExecution:
        provider_calls[invocation.definition.job_id] += 1
        if invocation.attempt_number == 1:
            now[0] += timedelta(seconds=10)
            raise ScheduledJobRunError(
                scheduled_job_failure(
                    ScheduledJobFailureCategory.TRANSIENT_HTTP,
                    "safe simulated provider failure",
                    reason_code="scratch_transient",
                )
            )
        now[0] += timedelta(seconds=10)
        return _execution(invocation)

    observer_invocations: dict[str, dict[str, int]] = {
        "first": {},
        "middle": {},
        "last": {},
    }
    observer_deliveries: dict[str, set[UUID]] = {name: set() for name in observer_invocations}

    def make_observer(name: str, *, fail_once: bool):
        def observe(attempt: ScheduledJobAttempt) -> None:
            key = str(attempt.attempt_id)
            observer_invocations[name][key] = observer_invocations[name].get(key, 0) + 1
            if fail_once and observer_invocations[name][key] == 1:
                raise OSError("private observer transport detail")
            observer_deliveries[name].add(attempt.attempt_id)

        return observe

    chain = ScheduledJobObserverChain(
        (
            make_observer("first", fail_once=True),
            make_observer("middle", fail_once=True),
            make_observer("last", fail_once=False),
        )
    )
    paths = StoragePaths.from_root(tmp_path / "scratch-workspace")
    state_root = tmp_path / "scratch-state"
    schedule_store = MultiAssetScheduleStateStore(state_root / "schedule.json")
    attempt_ids = iter(UUID(f"00000000-0000-4000-8000-{value:012d}") for value in (1, 2, 3))
    jobs = (
        RegisteredScheduledJob(definitions[0], run_a),
        RegisteredScheduledJob(definitions[1], run_b),
    )

    with LocalStorage(paths) as scratch_storage:
        collector = _FailFirstCollector(
            state_root=state_root,
            database_path=scratch_storage.paths.database_path,
            clock=lambda: now[0],
            measurement_timeout_seconds=2,
        )
        # The collector opens its own bounded read-only engine for the scratch DB.
        scratch_storage.close()
        scheduler = MultiAssetScheduler(
            jobs,
            schedule_store,
            observer=chain,
            clock=lambda: now[0],
            attempt_id_factory=attempt_ids.__next__,
            storage_observability=collector,
        )

        first_tick = scheduler.tick()
        assert [attempt.status.value for attempt in first_tick] == ["succeeded", "failed"]
        partial = schedule_store.load()
        assert [attempt.status.value for attempt in partial.attempts] == ["succeeded", "failed"]
        assert collector.completions == 2
        assert observer_deliveries["last"] == {attempt.attempt_id for attempt in first_tick}
        assert observer_deliveries["first"] == set()
        assert observer_deliveries["middle"] == set()

        # The first and middle failures are retried before the provider retry is due.
        now[0] += timedelta(seconds=60)
        second_tick = scheduler.tick()
        assert len(second_tick) == 1
        assert second_tick[0].definition.asset_id == "equity:test:b"
        assert second_tick[0].attempt_number == 2
        assert observer_deliveries["first"] == {attempt.attempt_id for attempt in first_tick}
        assert observer_deliveries["middle"] == {attempt.attempt_id for attempt in first_tick}
        assert observer_deliveries["last"] == {
            attempt.attempt_id for attempt in (*first_tick, *second_tick)
        }

        # All three observers receive the new attempt; first and middle fail once again.
        now[0] += timedelta(seconds=1)
        assert scheduler.tick() == ()
        assert second_tick[0].attempt_id in observer_deliveries["first"]
        assert second_tick[0].attempt_id in observer_deliveries["middle"]
        assert all(len(deliveries) == 3 for deliveries in observer_deliveries.values())
        assert observer_invocations["last"] == {
            str(attempt.attempt_id): 1 for attempt in (*first_tick, *second_tick)
        }

        state_before_restart = schedule_store.load()
        assert len(state_before_restart.attempts) == 3
        restarted_store = MultiAssetScheduleStateStore(state_root / "schedule.json")
        restarted = MultiAssetScheduler(
            jobs,
            restarted_store,
            clock=lambda: now[0],
            attempt_id_factory=attempt_ids.__next__,
        )
        assert restarted.status().to_json_dict()["failed_count"] == 0
        assert restarted.tick() == ()
        assert restarted_store.load() == state_before_restart
        assert provider_calls == {"asset-a": 1, "asset-b": 2}

        stored_cycle = cycle_probe.cycle(day.isoformat(), schedule_store.journal_dir)
        assert stored_cycle["attempts"] == 3
        assert all(job["local_date"] == day.isoformat() for job in stored_cycle["all_jobs"])
        assert all(job["started_at"].startswith("2026-09-25T") for job in stored_cycle["all_jobs"])
        assert (
            next(job for job in stored_cycle["all_jobs"] if job["attempt_number"] == 2)["failure"]
            is None
        )

        sha = "a" * 40
        identity = {
            "pid": 777,
            "process_starttime_ticks": 3000,
            "boot_id": "boot-scratch",
            "cgroup_generation": "generation-scratch",
            "release_sha": sha,
            "release_sha_state": "known",
            "sample_interval_seconds": 5,
        }
        attempts_by_id = {attempt.attempt_id: attempt for attempt in state_before_restart.attempts}
        a_attempt = next(
            attempt
            for attempt in state_before_restart.attempts
            if attempt.definition.job_id == "asset-a"
        )
        b_first = next(
            attempt
            for attempt in state_before_restart.attempts
            if attempt.definition.job_id == "asset-b" and attempt.attempt_number == 1
        )
        b_retry = attempts_by_id[second_tick[0].attempt_id]
        assert a_attempt.completed_at is not None
        assert b_first.completed_at is not None
        assert b_retry.completed_at is not None
        v2 = [
            {
                **identity,
                "at": (a_attempt.started_at + timedelta(seconds=2)).isoformat(),
                "VmRSS": 100_000_000,
                "memory_current_bytes": 80_000_000,
                "memory_peak_bytes": 90_000_000,
                "memory_events": {"high": 5, "max": 1, "oom": 0, "oom_kill": 0},
            },
            {
                **identity,
                "at": (a_attempt.started_at + timedelta(seconds=6)).isoformat(),
                "VmRSS": 110_000_000,
                "memory_current_bytes": 85_000_000,
                "memory_peak_bytes": 95_000_000,
                "memory_events": {"high": 7, "max": 1, "oom": 0, "oom_kill": 0},
            },
            {
                **identity,
                "at": (b_first.completed_at + timedelta(seconds=20)).isoformat(),
                "VmRSS": 120_000_000,
                "memory_current_bytes": 100_000_000,
                "memory_peak_bytes": 110_000_000,
                "memory_events": {"high": 8, "max": 1, "oom": 0, "oom_kill": 0},
            },
            {
                **identity,
                "at": (b_retry.started_at + timedelta(seconds=2)).isoformat(),
                "VmRSS": 130_000_000,
                "memory_current_bytes": 105_000_000,
                "memory_peak_bytes": 120_000_000,
                "memory_events": {"high": 10, "max": 1, "oom": 0, "oom_kill": 0},
            },
            {
                **identity,
                "at": (b_retry.started_at + timedelta(seconds=6)).isoformat(),
                "VmRSS": 140_000_000,
                "memory_current_bytes": 110_000_000,
                "memory_peak_bytes": 125_000_000,
                "memory_events": {"high": 2, "max": 0, "oom": 0, "oom_kill": 0},
            },
        ]
        v1 = [
            {
                "schema_version": "operational-memory-sample-v1",
                "timestamp": (b_first.started_at + timedelta(seconds=2)).isoformat(),
                "rss_bytes": 115_000_000,
                "pid": 777,
                "high_events": 8,
            },
            {
                "schema_version": "operational-memory-sample-v1",
                "timestamp": (b_first.started_at + timedelta(seconds=7)).isoformat(),
                "rss_bytes": 117_000_000,
                "pid": 777,
                "high_events": 9,
            },
        ]
        samples_dir = tmp_path / "ops" / "samples"
        samples_dir.mkdir(parents=True)
        sample_file = samples_dir / f"mem-{day.isoformat()}.jsonl"
        sample_file.write_text(
            "".join(json.dumps(item, sort_keys=True) + "\n" for item in [*v2, *v1]),
            encoding="utf-8",
        )
        sample_file_mtime = sample_file.stat().st_mtime_ns

        observation_report = cycle_probe.observability(
            day.isoformat(), artifact_path=collector.artifact_path
        )
        measured = cycle_probe.memory_by_job(
            day.isoformat(),
            stored_cycle["all_jobs"],
            samples_dir=samples_dir,
            observability_rows=observation_report["today"],
        )

    by_attempt = {item["attempt_id"]: item for item in measured["by_job"]}
    assert measured["sample_count"] == 7
    assert measured["capture_kind"] == "series"
    assert measured["comparable"] is False  # The legacy point lacks full runtime identity.
    assert measured["release_identity_state"] == "unknown"
    assert by_attempt[str(a_attempt.attempt_id)]["rss_net"] == 10_000_000
    assert by_attempt[str(b_first.attempt_id)]["rss_net_reason"] == "identity_incomplete"
    assert by_attempt[str(b_retry.attempt_id)]["high_events_reason"] == "counter_reset"
    assert by_attempt[str(b_retry.attempt_id)]["collector_durations_ms"] is not None
    assert measured["unattributed_sample_count"] >= 1
    assert sample_file.stat().st_mtime_ns == sample_file_mtime
