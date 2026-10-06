"""Tests for provider-independent multi-asset scheduling and recovery."""

import json
import threading
import time as monotonic_time
import time as time_module
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from uuid import UUID
from zoneinfo import ZoneInfo

import duckdb
import pytest
from pydantic import ValidationError

from investment_analyst.application.multi_asset_scheduler import (
    MultiAssetScheduler,
    MultiAssetSchedulerStatus,
    MultiAssetScheduleState,
    MultiAssetScheduleStateStore,
    ProviderJobTelemetry,
    RegisteredScheduledJob,
    ScheduledJobAttempt,
    ScheduledJobAttemptStatus,
    ScheduledJobDefinition,
    ScheduledJobDomain,
    ScheduledJobExecution,
    ScheduledJobFailure,
    ScheduledJobFailureCategory,
    ScheduledJobFreshness,
    ScheduledJobHealth,
    ScheduledJobInvocation,
    ScheduledJobRunError,
    scheduled_job_failure,
)
from investment_analyst.application.storage_observability import (
    StorageObservabilityCollector,
    StorageObservabilityError,
    StorageObservabilityState,
)
from investment_analyst.core.operation_control import current_operation_control


def _definition(job_id: str, *, provider: str = "test-provider") -> ScheduledJobDefinition:
    return ScheduledJobDefinition(
        job_id=job_id,
        asset_id=f"equity:test:{job_id}",
        provider=provider,
        domain=ScheduledJobDomain.MARKET_DAILY,
        data_frequency="day_1",
        timezone="America/Lima",
        run_at=time(hour=7),
        max_attempts_per_day=3,
        retry_backoff_seconds=60,
    )


def _execution(invocation: ScheduledJobInvocation, *, created: int = 1) -> ScheduledJobExecution:
    return ScheduledJobExecution(
        job_id=invocation.definition.job_id,
        effective_known_at=invocation.started_at,
        evidence_changed=created > 0,
        source_ids=(f"source:{invocation.definition.job_id}",),
        created_count=created,
        reused_count=0,
    )


class _CountingScheduleStateStore(MultiAssetScheduleStateStore):
    def __init__(self, path: Path) -> None:
        super().__init__(path)
        self.load_calls = 0

    def load(self):  # type: ignore[no-untyped-def]
        self.load_calls += 1
        return super().load()


@pytest.mark.parametrize(
    ("category", "retryable"),
    [
        (ScheduledJobFailureCategory.RATE_LIMIT, True),
        (ScheduledJobFailureCategory.TRANSPORT, True),
        (ScheduledJobFailureCategory.TRANSIENT_HTTP, True),
        (ScheduledJobFailureCategory.INTERRUPTED, True),
        (ScheduledJobFailureCategory.CONFIGURATION, False),
        (ScheduledJobFailureCategory.AUTHENTICATION, False),
        (ScheduledJobFailureCategory.UNSUPPORTED_CAPABILITY, False),
        (ScheduledJobFailureCategory.PROVIDER_CONTRACT, False),
        (ScheduledJobFailureCategory.VALIDATION, False),
        (ScheduledJobFailureCategory.STORAGE_STATE, False),
        (ScheduledJobFailureCategory.HTTP, False),
        (ScheduledJobFailureCategory.UNEXPECTED, False),
    ],
)
def test_new_failures_use_the_canonical_category_policy(
    category: ScheduledJobFailureCategory,
    retryable: bool,
) -> None:
    failure = scheduled_job_failure(category, "safe failure")

    assert failure.retryable is retryable
    with pytest.raises(ValueError, match="retryable must match"):
        ScheduledJobFailure(category=category, message="safe failure", retryable=not retryable)
    with pytest.raises(ValueError, match="canonical"):
        ScheduledJobFailure(
            category="legacy-provider-error",
            message="safe failure",
            retryable=False,
        )


def test_crypto_derivatives_domain_round_trips_without_changing_existing_domains() -> None:
    definition = ScheduledJobDefinition(
        job_id="deribit:crypto:btc-usd:crypto-derivatives",
        asset_id="crypto:btc-usd",
        provider="deribit",
        domain=ScheduledJobDomain.CRYPTO_DERIVATIVES,
        data_frequency="hour_1/day_1/event",
        run_at=time(hour=7, minute=10),
        freshness_threshold_seconds=129_600,
    )

    assert definition.to_json_dict()["domain"] == "crypto_derivatives"
    assert ScheduledJobDomain.MARKET_DAILY.value == "market_daily"


def test_state_store_loads_legacy_failure_categories_without_rewriting_bytes(
    tmp_path: Path,
) -> None:
    definition = _definition("legacy-state")
    legacy = {
        "schema_version": "multi-asset-schedule-state-v1",
        "attempts": [
            {
                "schema_version": "scheduled-job-attempt-v1",
                "attempt_id": "00000000-0000-4000-8000-000000000099",
                "definition": definition.to_json_dict(),
                "local_date": "2026-07-29",
                "scheduled_for": "2026-07-29T12:00:00+00:00",
                "attempt_number": 1,
                "status": "failed",
                "started_at": "2026-07-29T12:01:00+00:00",
                "completed_at": "2026-07-29T12:02:00+00:00",
                "execution": None,
                "failure": {
                    "category": "ListedMarketRefreshError",
                    "message": "historical safe failure",
                    "retryable": True,
                },
                "telemetry": None,
            }
        ],
    }
    path = tmp_path / "legacy.json"
    path.write_text(json.dumps(legacy), encoding="utf-8")
    original = path.read_bytes()

    state = MultiAssetScheduleStateStore(path).load()

    assert state.attempts[0].failure is not None
    assert state.attempts[0].failure.safe_category is ScheduledJobFailureCategory.LEGACY_UNKNOWN
    assert path.read_bytes() == original


def test_scheduler_runs_all_due_jobs_and_preserves_success_after_later_failure(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 7, 29, 12, 5, tzinfo=UTC)
    calls: list[str] = []
    first = _definition("a-market")
    second = _definition("b-market")

    def succeed(invocation: ScheduledJobInvocation) -> ScheduledJobExecution:
        calls.append(invocation.definition.job_id)
        return _execution(invocation)

    def fail(invocation: ScheduledJobInvocation) -> ScheduledJobExecution:
        calls.append(invocation.definition.job_id)
        raise ScheduledJobRunError(
            scheduled_job_failure(
                ScheduledJobFailureCategory.TRANSIENT_HTTP,
                "provider unavailable",
            )
        )

    store = MultiAssetScheduleStateStore(tmp_path / "schedule.json")
    scheduler = MultiAssetScheduler(
        (
            RegisteredScheduledJob(second, fail),
            RegisteredScheduledJob(first, succeed),
        ),
        store,
        clock=lambda: now,
        attempt_id_factory=iter(
            (
                UUID("00000000-0000-4000-8000-000000000001"),
                UUID("00000000-0000-4000-8000-000000000002"),
            )
        ).__next__,
    )

    completed = scheduler.tick()

    assert calls == ["a-market", "b-market"]
    assert tuple(item.status for item in completed) == (
        ScheduledJobAttemptStatus.SUCCEEDED,
        ScheduledJobAttemptStatus.FAILED,
    )
    assert tuple(item.status for item in store.load().attempts) == (
        ScheduledJobAttemptStatus.SUCCEEDED,
        ScheduledJobAttemptStatus.FAILED,
    )
    assert completed[0].telemetry is not None
    assert completed[0].telemetry.provider == "test-provider"
    assert completed[0].telemetry.created_count == 1
    assert completed[0].telemetry.coverage_complete is True
    assert completed[1].telemetry is not None
    assert completed[1].telemetry.failure_category == "transient_http_error"
    assert completed[1].telemetry.provider_call_count is None
    assert completed[1].telemetry.response_bytes is None
    status = scheduler.status()
    assert status.due_count == 0
    assert status.failed_count == 1
    assert status.next_run_at == now + timedelta(seconds=60)


def test_scheduler_loads_history_once_per_tick_for_multiple_due_jobs(tmp_path: Path) -> None:
    now = datetime(2026, 7, 29, 12, 5, tzinfo=UTC)
    store = _CountingScheduleStateStore(tmp_path / "schedule.json")
    scheduler = MultiAssetScheduler(
        (
            RegisteredScheduledJob(_definition("a-market"), _execution),
            RegisteredScheduledJob(_definition("b-market"), _execution),
        ),
        store,
        clock=lambda: now,
        attempt_id_factory=iter(
            (
                UUID("00000000-0000-4000-8000-000000000011"),
                UUID("00000000-0000-4000-8000-000000000012"),
            )
        ).__next__,
    )

    completed = scheduler.tick()

    assert len(completed) == 2
    assert store.load_calls == 1


def test_scheduler_retries_only_failed_job_after_backoff(tmp_path: Path) -> None:
    now = datetime(2026, 7, 29, 12, 5, tzinfo=UTC)
    definition = _definition("retry-market")
    calls = 0

    def run(invocation: ScheduledJobInvocation) -> ScheduledJobExecution:
        nonlocal calls
        calls += 1
        if calls < 3:
            raise ScheduledJobRunError(
                scheduled_job_failure(
                    ScheduledJobFailureCategory.TRANSPORT,
                    "temporary provider failure",
                )
            )
        return _execution(invocation, created=0)

    scheduler = MultiAssetScheduler(
        (RegisteredScheduledJob(definition, run),),
        MultiAssetScheduleStateStore(tmp_path / "schedule.json"),
        clock=lambda: now,
    )

    first = scheduler.tick()
    assert first[0].attempt_number == 1
    assert scheduler.tick() == ()
    now += timedelta(seconds=61)
    second = scheduler.tick()
    retry_status = scheduler.status().jobs[0]
    now += timedelta(seconds=121)
    third = scheduler.tick()

    assert calls == 3
    assert second[0].attempt_number == 2
    assert second[0].status is ScheduledJobAttemptStatus.FAILED
    assert retry_status.health is ScheduledJobHealth.RETRY_WAIT
    assert retry_status.next_retry_at == now - timedelta(seconds=1)
    assert retry_status.retry_budget_remaining == 1
    assert third[0].attempt_number == 3
    assert third[0].status is ScheduledJobAttemptStatus.SUCCEEDED
    assert scheduler.status().failed_count == 0


@pytest.mark.parametrize(
    ("mode", "expected_status", "expected_category"),
    [
        (
            "permanent",
            ScheduledJobAttemptStatus.FAILED,
            ScheduledJobFailureCategory.AUTHENTICATION,
        ),
        (
            "validation",
            ScheduledJobAttemptStatus.FAILED,
            ScheduledJobFailureCategory.VALIDATION,
        ),
        (
            "unexpected",
            ScheduledJobAttemptStatus.FAILED,
            ScheduledJobFailureCategory.UNEXPECTED,
        ),
    ],
)
def test_scheduler_never_retries_permanent_validation_or_unexpected_failures(
    tmp_path: Path,
    mode: str,
    expected_status: ScheduledJobAttemptStatus,
    expected_category: ScheduledJobFailureCategory,
) -> None:
    now = datetime(2026, 7, 29, 12, 5, tzinfo=UTC)
    calls = 0

    def run(invocation: ScheduledJobInvocation) -> ScheduledJobExecution:
        nonlocal calls
        del invocation
        calls += 1
        if mode == "permanent":
            raise ScheduledJobRunError(
                scheduled_job_failure(
                    ScheduledJobFailureCategory.AUTHENTICATION,
                    "safe authentication failure",
                )
            )
        if mode == "validation":
            raise ValueError("simulated-secret")
        raise RuntimeError("simulated-secret")

    scheduler = MultiAssetScheduler(
        (RegisteredScheduledJob(_definition(f"{mode}-job"), run),),
        MultiAssetScheduleStateStore(tmp_path / f"{mode}.json"),
        clock=lambda: now,
    )

    first = scheduler.tick()
    now += timedelta(minutes=30)

    assert first[0].status is expected_status
    assert first[0].failure is not None
    assert first[0].failure.category == expected_category
    assert first[0].failure.retryable is False
    assert "simulated-secret" not in first[0].failure.message
    assert scheduler.tick() == ()
    assert calls == 1
    job_status = scheduler.status().jobs[0]
    assert job_status.due is False
    assert job_status.health is ScheduledJobHealth.BLOCKED
    assert job_status.failure_category is expected_category
    assert any("latest scheduled job failed" in issue for issue in job_status.issues)


def test_scheduler_preserves_last_success_after_later_permanent_failure(tmp_path: Path) -> None:
    now = datetime(2026, 7, 29, 12, 5, tzinfo=UTC)
    should_fail = False

    def run(invocation: ScheduledJobInvocation) -> ScheduledJobExecution:
        if should_fail:
            raise ScheduledJobRunError(
                scheduled_job_failure(
                    ScheduledJobFailureCategory.CONFIGURATION,
                    "safe configuration failure",
                )
            )
        return _execution(invocation)

    scheduler = MultiAssetScheduler(
        (RegisteredScheduledJob(_definition("last-success"), run),),
        MultiAssetScheduleStateStore(tmp_path / "last-success.json"),
        clock=lambda: now,
    )

    first = scheduler.tick()[0]
    should_fail = True
    now += timedelta(days=1)
    second = scheduler.tick()[0]
    status = scheduler.status().jobs[0]

    assert first.status is ScheduledJobAttemptStatus.SUCCEEDED
    assert second.status is ScheduledJobAttemptStatus.FAILED
    assert status.latest_attempt == second
    assert status.latest_success == first


def test_scheduler_recovers_interrupted_attempt_before_retry(tmp_path: Path) -> None:
    now = datetime(2026, 7, 29, 12, 5, tzinfo=UTC)
    definition = _definition("interrupted-market")
    store = MultiAssetScheduleStateStore(tmp_path / "schedule.json")
    running = ScheduledJobAttempt(
        attempt_id=UUID("00000000-0000-4000-8000-000000000010"),
        definition=definition,
        local_date=date(2026, 7, 29),
        scheduled_for=datetime(2026, 7, 29, 12, tzinfo=UTC),
        attempt_number=1,
        status=ScheduledJobAttemptStatus.RUNNING,
        started_at=datetime(2026, 7, 29, 12, 1, tzinfo=UTC),
    )
    store.write_attempt(running)
    observed: list[ScheduledJobAttempt] = []
    scheduler = MultiAssetScheduler(
        (RegisteredScheduledJob(definition, _execution),),
        store,
        observer=observed.append,
        clock=lambda: now,
    )

    recovered = scheduler.tick()

    assert len(recovered) == 1
    assert recovered[0].attempt_id == running.attempt_id
    assert recovered[0].status is ScheduledJobAttemptStatus.FAILED
    assert recovered[0].failure is not None
    assert recovered[0].failure.category == "interrupted_job"
    assert observed == [recovered[0]]
    assert scheduler.status().next_run_at == now + timedelta(seconds=60)


def test_scheduler_recovers_interrupted_removed_job_without_reactivating_it(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 7, 29, 12, 5, tzinfo=UTC)
    removed = _definition("removed-market")
    replacement = _definition("replacement-disabled").model_copy(update={"enabled": False})
    store = MultiAssetScheduleStateStore(tmp_path / "removed.json")
    running = ScheduledJobAttempt(
        attempt_id=UUID("00000000-0000-4000-8000-000000000020"),
        definition=removed,
        local_date=date(2026, 7, 29),
        scheduled_for=datetime(2026, 7, 29, 12, tzinfo=UTC),
        attempt_number=1,
        status=ScheduledJobAttemptStatus.RUNNING,
        started_at=datetime(2026, 7, 29, 12, 1, tzinfo=UTC),
    )
    store.write_attempt(running)
    provider_calls = 0

    def replacement_run(invocation: ScheduledJobInvocation) -> ScheduledJobExecution:
        nonlocal provider_calls
        provider_calls += 1
        return _execution(invocation)

    scheduler = MultiAssetScheduler(
        (RegisteredScheduledJob(replacement, replacement_run),),
        store,
        clock=lambda: now,
    )

    completed = scheduler.tick()

    assert len(completed) == 1
    assert completed[0].attempt_id == running.attempt_id
    assert completed[0].failure is not None
    assert completed[0].failure.category == ScheduledJobFailureCategory.INTERRUPTED
    assert provider_calls == 0
    assert scheduler.registered_job_definitions() == (replacement,)


def test_scheduler_rejects_duplicate_jobs_and_naive_clock(tmp_path: Path) -> None:
    definition = _definition("duplicate")
    job = RegisteredScheduledJob(definition, _execution)
    with pytest.raises(ValueError, match="unique"):
        MultiAssetScheduler(
            (job, job),
            MultiAssetScheduleStateStore(tmp_path / "schedule.json"),
        )

    scheduler = MultiAssetScheduler(
        (job,),
        MultiAssetScheduleStateStore(tmp_path / "other.json"),
        clock=lambda: datetime(2026, 7, 29, 12),
    )
    with pytest.raises(ValueError, match="timezone-aware"):
        scheduler.status()


def test_scheduler_retries_observer_without_rerunning_provider_job(tmp_path: Path) -> None:
    now = datetime(2026, 7, 29, 12, 5, tzinfo=UTC)
    definition = _definition("observer-retry")
    provider_calls = 0
    observer_calls = 0

    def run(invocation: ScheduledJobInvocation) -> ScheduledJobExecution:
        nonlocal provider_calls
        provider_calls += 1
        return _execution(invocation)

    def observe(attempt: ScheduledJobAttempt) -> None:
        nonlocal observer_calls
        del attempt
        observer_calls += 1
        if observer_calls == 1:
            raise OSError("transient alert store failure")

    scheduler = MultiAssetScheduler(
        (RegisteredScheduledJob(definition, run),),
        MultiAssetScheduleStateStore(tmp_path / "schedule.json"),
        observer=observe,
        clock=lambda: now,
    )

    scheduler.tick()
    assert scheduler.status().issues == ("scheduled job observer could not persist its result",)
    assert scheduler.tick() == ()

    assert provider_calls == 1
    assert observer_calls == 2
    assert scheduler.status().issues == ()


def test_scheduler_exposes_current_stale_and_incomplete_coverage(tmp_path: Path) -> None:
    now = datetime(2026, 7, 29, 12, 5, tzinfo=UTC)
    definition = _definition("freshness")
    coverage_complete = True

    def run(invocation: ScheduledJobInvocation) -> ScheduledJobExecution:
        execution = _execution(invocation)
        return execution.model_copy(update={"coverage_complete": coverage_complete})

    scheduler = MultiAssetScheduler(
        (RegisteredScheduledJob(definition, run),),
        MultiAssetScheduleStateStore(tmp_path / "schedule.json"),
        clock=lambda: now,
    )

    assert scheduler.status().jobs[0].freshness is ScheduledJobFreshness.NEVER_RUN
    scheduler.tick()
    assert scheduler.status().jobs[0].freshness is ScheduledJobFreshness.CURRENT
    assert scheduler.status().jobs[0].health is ScheduledJobHealth.CURRENT
    now += timedelta(days=3)
    stale = scheduler.status()
    assert stale.jobs[0].freshness is ScheduledJobFreshness.STALE
    assert stale.jobs[0].health is ScheduledJobHealth.STALE
    assert stale.stale_count == 1

    second = _definition("incomplete")
    coverage_complete = False
    incomplete_scheduler = MultiAssetScheduler(
        (RegisteredScheduledJob(second, run),),
        MultiAssetScheduleStateStore(tmp_path / "incomplete.json"),
        clock=lambda: now,
    )
    incomplete_scheduler.tick()
    incomplete = incomplete_scheduler.status()
    assert incomplete.jobs[0].freshness is ScheduledJobFreshness.INCOMPLETE
    assert incomplete.jobs[0].health is ScheduledJobHealth.INCOMPLETE
    assert incomplete.incomplete_count == 1


def test_registry_reconciliation_during_active_provider_preserves_identity_and_history(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 7, 29, 12, 5, tzinfo=UTC)
    active = _definition("a-active-market")
    queued = _definition("b-queued-market")
    replacement = _definition("replacement-market")
    provider_started = threading.Event()
    provider_release = threading.Event()
    provider_calls = 0
    queued_calls = 0

    def blocked(invocation: ScheduledJobInvocation) -> ScheduledJobExecution:
        nonlocal provider_calls
        provider_calls += 1
        provider_started.set()
        assert provider_release.wait(timeout=5)
        return _execution(invocation)

    def must_be_retired(invocation: ScheduledJobInvocation) -> ScheduledJobExecution:
        nonlocal queued_calls
        queued_calls += 1
        return _execution(invocation)

    store = MultiAssetScheduleStateStore(tmp_path / "reconciled.json")
    active_job = RegisteredScheduledJob(active, blocked)
    queued_job = RegisteredScheduledJob(queued, must_be_retired)
    replacement_job = RegisteredScheduledJob(replacement, _execution)
    scheduler = MultiAssetScheduler((active_job, queued_job), store, clock=lambda: now)
    completed: list[tuple[ScheduledJobAttempt, ...]] = []
    thread = threading.Thread(target=lambda: completed.append(scheduler.tick()))
    thread.start()
    assert provider_started.wait(timeout=5)

    scheduler.reconcile_jobs((replacement_job,))
    assert scheduler.registered_job_definitions() == (replacement,)
    provider_release.set()
    thread.join(timeout=5)

    assert not thread.is_alive()
    assert provider_calls == 1
    assert queued_calls == 0
    assert len(completed[0]) == 1
    assert completed[0][0].definition.job_id == active.job_id
    assert completed[0][0].status is ScheduledJobAttemptStatus.SUCCEEDED
    assert tuple(item.definition.job_id for item in store.load().attempts) == (active.job_id,)

    scheduler.reconcile_jobs((active_job,))
    reactivated = scheduler.status().jobs[0]
    assert reactivated.definition.job_id == active.job_id
    assert reactivated.latest_success == completed[0][0]
    assert scheduler.tick() == ()
    assert provider_calls == 1


def test_scheduler_cancellation_marks_active_job_interrupted_and_skips_remaining_due_jobs(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 7, 29, 12, 5, tzinfo=UTC)
    active = _definition("a-active-cancellable")
    queued = _definition("b-must-not-start")
    started = threading.Event()
    queued_calls = 0

    def cancellable(invocation: ScheduledJobInvocation) -> ScheduledJobExecution:
        del invocation
        control = current_operation_control()
        assert control is not None
        started.set()
        while not control.cancelled:
            time_module.sleep(0.005)
        control.raise_if_cancelled()
        raise AssertionError("cancellation should have raised")

    def must_not_start(invocation: ScheduledJobInvocation) -> ScheduledJobExecution:
        nonlocal queued_calls
        queued_calls += 1
        return _execution(invocation)

    store = MultiAssetScheduleStateStore(tmp_path / "cancelled.json")
    scheduler = MultiAssetScheduler(
        (
            RegisteredScheduledJob(active, cancellable),
            RegisteredScheduledJob(queued, must_not_start),
        ),
        store,
        clock=lambda: now,
    )
    stop_event = threading.Event()
    thread = threading.Thread(
        target=scheduler.run_forever,
        args=(stop_event,),
        kwargs={"poll_seconds": 0.01},
    )
    thread.start()
    assert started.wait(timeout=2)
    stop_event.set()
    thread.join(timeout=2)

    assert not thread.is_alive()
    assert queued_calls == 0
    attempts = store.load().attempts
    assert len(attempts) == 1
    assert attempts[0].status is ScheduledJobAttemptStatus.FAILED
    assert attempts[0].failure is not None
    assert attempts[0].failure.category is ScheduledJobFailureCategory.INTERRUPTED
    assert current_operation_control() is None


def test_scheduler_emits_storage_observability_without_changing_persisted_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "investment_analyst.application.job_memory_budget.read_process_rss_kb",
        lambda: 4242,
    )
    now = datetime(2026, 7, 29, 12, 5, tzinfo=UTC)
    definition = _definition("observability-market")
    attempt_id = UUID("00000000-0000-4000-8000-0000000000a1")

    def run(invocation: ScheduledJobInvocation) -> ScheduledJobExecution:
        return _execution(invocation, created=2)

    def schedule(store_path: Path) -> tuple[tuple[str, bytes], ...]:
        store = MultiAssetScheduleStateStore(store_path)
        scheduler = MultiAssetScheduler(
            (RegisteredScheduledJob(definition, run),),
            store,
            clock=lambda: now,
            attempt_id_factory=lambda: attempt_id,
        )
        completed = scheduler.tick()
        assert completed[0].status is ScheduledJobAttemptStatus.SUCCEEDED
        return store.persisted_signatures()

    baseline = schedule(tmp_path / "baseline.json")

    database = tmp_path / "storage" / "data" / "processed" / "investment_analyst.duckdb"
    database.parent.mkdir(parents=True)
    connection = duckdb.connect(str(database))
    try:
        connection.execute("CREATE TABLE metric_results (result_id VARCHAR, document_json VARCHAR)")
        connection.execute("INSERT INTO metric_results VALUES ('a', '{\"x\": 1}')")
    finally:
        connection.close()
    collector = StorageObservabilityCollector(
        state_root=tmp_path / "state",
        database_path=database,
        clock=lambda: now,
    )
    store_path = tmp_path / "observed.json"
    store = MultiAssetScheduleStateStore(store_path)
    scheduler = MultiAssetScheduler(
        (RegisteredScheduledJob(definition, run),),
        store,
        storage_observability=collector,
        clock=lambda: now,
        attempt_id_factory=lambda: attempt_id,
    )

    completed = scheduler.tick()

    assert completed[0].attempt_id == attempt_id
    assert store.load().attempts[0].status is ScheduledJobAttemptStatus.SUCCEEDED
    assert store.persisted_signatures() == baseline
    assert scheduler.status().issues == ()

    state = collector.state()
    assert isinstance(state, StorageObservabilityState)
    assert len(state.records) == 1
    record = state.records[0]
    assert record.attempt_id == attempt_id
    assert record.job_id == definition.job_id
    assert record.attempt_status == "succeeded"
    assert record.rows_created == 2
    assert record.rows_reused == 0
    assert record.evidence_changed is True
    assert record.database_delta_bytes == 0
    assert record.table_bytes == ()
    assert record.growth is not None
    assert record.durations is not None
    assert record.durations.total_ms >= record.durations.job_execution_ms
    assert record.durations.query_open_ms + record.durations.query_select_ms <= (
        record.durations.query_ms
    )


def test_scheduler_measures_job_duration_with_a_monotonic_clock(tmp_path: Path) -> None:
    now = datetime(2026, 7, 29, 12, 5, tzinfo=UTC)
    definition = _definition("monotonic-observation")

    def run(invocation: ScheduledJobInvocation) -> ScheduledJobExecution:
        monotonic_time.sleep(0.05)
        return _execution(invocation, created=1)

    collector = StorageObservabilityCollector(
        state_root=tmp_path / "state",
        database_path=tmp_path / "missing.duckdb",
        clock=lambda: now,
    )
    scheduler = MultiAssetScheduler(
        (RegisteredScheduledJob(definition, run),),
        MultiAssetScheduleStateStore(tmp_path / "schedule.json"),
        clock=lambda: now,
        storage_observability=collector,
    )

    attempt = scheduler.tick()[0]

    record = collector.state().records[0]
    assert attempt.status is ScheduledJobAttemptStatus.SUCCEEDED
    assert record.durations is not None
    assert record.durations.job_execution_ms >= 40
    assert record.durations.total_ms >= record.durations.job_execution_ms


def test_collector_retry_writes_the_last_job_envelope_without_rerunning_provider(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 7, 29, 12, 5, tzinfo=UTC)
    definition = _definition("sec:institutional:13f-history")
    provider_calls = 0

    def run(invocation: ScheduledJobInvocation) -> ScheduledJobExecution:
        nonlocal provider_calls
        provider_calls += 1
        return _execution(invocation, created=1)

    class FailOnceAppendCollector(StorageObservabilityCollector):
        append_calls = 0

        def _append_line(self, record) -> None:  # type: ignore[no-untyped-def]
            self.append_calls += 1
            if self.append_calls == 1:
                raise StorageObservabilityError(
                    "simulated transient append fault", reason_code="artifact_write_failed"
                )
            super()._append_line(record)

    collector = FailOnceAppendCollector(
        state_root=tmp_path / "state",
        database_path=tmp_path / "missing.duckdb",
        clock=lambda: now,
    )
    store = MultiAssetScheduleStateStore(tmp_path / "schedule.json")
    scheduler = MultiAssetScheduler(
        (RegisteredScheduledJob(definition, run),),
        store,
        clock=lambda: now,
        storage_observability=collector,
        attempt_id_factory=lambda: UUID("00000000-0000-4000-8000-0000000000a2"),
    )

    first = scheduler.tick()
    assert first[0].status is ScheduledJobAttemptStatus.SUCCEEDED
    assert provider_calls == 1
    assert collector.state().records == ()
    assert scheduler.status().issues == (
        "storage observability could not record its result",
        "storage observability failure reason: artifact_write_failed",
    )

    second = scheduler.tick()

    assert second == ()
    assert provider_calls == 1
    assert collector.append_calls == 2
    assert len(collector.state().records) == 1
    assert collector.state().records[0].job_id == "sec:institutional:13f-history"
    assert scheduler.status().issues == ()
    assert len(store.load().attempts) == 1


def test_restart_recovery_persists_unavailable_without_inventing_boundaries(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 7, 29, 12, 5, tzinfo=UTC)
    definition = _definition("restart-recovery").model_copy(update={"max_attempts_per_day": 1})
    local_date = now.astimezone(ZoneInfo(definition.timezone)).date()
    running = ScheduledJobAttempt(
        attempt_id=UUID("00000000-0000-4000-8000-0000000000a3"),
        definition=definition,
        local_date=local_date,
        scheduled_for=definition.scheduled_for(local_date),
        attempt_number=1,
        status=ScheduledJobAttemptStatus.RUNNING,
        started_at=now - timedelta(minutes=1),
    )
    store = MultiAssetScheduleStateStore(tmp_path / "schedule.json")
    store.write_attempt_from_state(MultiAssetScheduleState(attempts=()), running)
    collector = StorageObservabilityCollector(
        state_root=tmp_path / "state",
        database_path=tmp_path / "not-created.duckdb",
        clock=lambda: now,
    )
    provider_calls = 0

    def run(invocation: ScheduledJobInvocation) -> ScheduledJobExecution:
        nonlocal provider_calls
        provider_calls += 1
        return _execution(invocation)

    scheduler = MultiAssetScheduler(
        (RegisteredScheduledJob(definition, run),),
        store,
        storage_observability=collector,
        clock=lambda: now,
    )

    recovered = scheduler.tick()

    assert len(recovered) == 1
    assert recovered[0].status is ScheduledJobAttemptStatus.FAILED
    assert recovered[0].failure is not None
    assert recovered[0].failure.category is ScheduledJobFailureCategory.INTERRUPTED
    assert provider_calls == 0
    record = collector.state().records[0]
    assert record.attempt_id == running.attempt_id
    assert record.attempt_status == "failed"
    assert record.measurement_state == "unavailable"
    assert record.database_bytes_before is None
    assert record.database_bytes_after is None
    assert record.table_rows_before is None
    assert record.table_rows_after is None
    assert record.durations is None
    assert record.failure_phase is None
    assert record.failure_reason is None


def test_scheduler_persists_terminal_result_before_notification_and_observation(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 7, 29, 12, 5, tzinfo=UTC)
    events: list[str] = []

    class RecordingStore(MultiAssetScheduleStateStore):
        def write_attempt_from_state(self, state, attempt):  # type: ignore[no-untyped-def]
            if attempt.status is not ScheduledJobAttemptStatus.RUNNING:
                events.append("terminal")
            return super().write_attempt_from_state(state, attempt)

    class RecordingCollector(StorageObservabilityCollector):
        def complete_attempt(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            events.append("collector")
            return super().complete_attempt(*args, **kwargs)

    collector = RecordingCollector(
        state_root=tmp_path / "state",
        database_path=tmp_path / "missing.duckdb",
        clock=lambda: now,
    )

    def observe(_: ScheduledJobAttempt) -> None:
        events.append("observer")

    scheduler = MultiAssetScheduler(
        (RegisteredScheduledJob(_definition("terminal-order"), _execution),),
        RecordingStore(tmp_path / "schedule.json"),
        observer=observe,
        clock=lambda: now,
        storage_observability=collector,
    )

    result = scheduler.tick()

    assert result[0].status is ScheduledJobAttemptStatus.SUCCEEDED
    assert events == ["terminal", "observer", "collector"]


def test_scheduler_does_not_notify_when_terminal_persistence_fails(tmp_path: Path) -> None:
    now = datetime(2026, 7, 29, 12, 5, tzinfo=UTC)
    notifications: list[ScheduledJobAttempt] = []
    collector_completions: list[bool] = []

    class FailingTerminalStore(MultiAssetScheduleStateStore):
        def write_attempt_from_state(self, state, attempt):  # type: ignore[no-untyped-def]
            if attempt.status is not ScheduledJobAttemptStatus.RUNNING:
                raise OSError("simulated-secret")
            return super().write_attempt_from_state(state, attempt)

    class RecordingCollector(StorageObservabilityCollector):
        def complete_attempt(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            collector_completions.append(True)
            return super().complete_attempt(*args, **kwargs)

    store = FailingTerminalStore(tmp_path / "schedule.json")
    collector = RecordingCollector(
        state_root=tmp_path / "state",
        database_path=tmp_path / "missing.duckdb",
        clock=lambda: now,
    )
    scheduler = MultiAssetScheduler(
        (RegisteredScheduledJob(_definition("terminal-failure"), _execution),),
        store,
        observer=notifications.append,
        clock=lambda: now,
        storage_observability=collector,
    )

    with pytest.raises(OSError, match="simulated-secret"):
        scheduler.tick()

    assert notifications == []
    assert collector_completions == []
    assert store.load().attempts[0].status is ScheduledJobAttemptStatus.RUNNING


def test_persisted_scheduler_contracts_remain_unchanged(tmp_path: Path) -> None:
    now = datetime(2026, 7, 29, 12, 5, tzinfo=UTC)
    store = MultiAssetScheduleStateStore(tmp_path / "persisted.json")
    scheduler = MultiAssetScheduler(
        (RegisteredScheduledJob(_definition("persisted-contracts"), _execution),),
        store,
        clock=lambda: now,
        attempt_id_factory=iter((UUID("00000000-0000-4000-8000-0000000000b1"),)).__next__,
    )

    completed = scheduler.tick()[0]

    assert tuple(ProviderJobTelemetry.model_fields) == (
        "schema_version",
        "job_id",
        "provider",
        "domain",
        "started_at",
        "completed_at",
        "duration_ms",
        "provider_call_count",
        "response_bytes",
        "created_count",
        "reused_count",
        "coverage_complete",
        "failure_category",
        "peak_rss_kb",
    )
    assert tuple(ScheduledJobAttempt.model_fields) == (
        "schema_version",
        "attempt_id",
        "definition",
        "local_date",
        "scheduled_for",
        "attempt_number",
        "status",
        "started_at",
        "completed_at",
        "execution",
        "failure",
        "telemetry",
    )
    assert tuple(MultiAssetScheduleState.model_fields) == (
        "schema_version",
        "attempts",
    )
    assert completed.schema_version == "scheduled-job-attempt-v1"
    assert completed.telemetry is not None
    assert completed.telemetry.schema_version == "provider-job-telemetry-v1"
    assert tuple(completed.telemetry.model_dump(mode="json")) == tuple(
        ProviderJobTelemetry.model_fields
    )
    assert tuple(completed.to_json_dict()) == tuple(ScheduledJobAttempt.model_fields)
    assert tuple(store.load().to_json_dict()) == ("schema_version", "attempts")


def test_journal_reconstructs_the_same_state_as_the_full_rewrite_path(tmp_path: Path) -> None:
    now = datetime(2026, 7, 29, 12, 0, tzinfo=UTC)
    scheduled_for = datetime(2026, 7, 29, 12, tzinfo=UTC)
    def1 = _definition("job-1")
    def2 = _definition("job-2")

    att1_id = UUID("00000000-0000-4000-8000-000000000001")
    att2_id = UUID("00000000-0000-4000-8000-000000000002")

    att1_running = ScheduledJobAttempt(
        attempt_id=att1_id,
        definition=def1,
        local_date=date(2026, 7, 29),
        scheduled_for=scheduled_for,
        attempt_number=1,
        status=ScheduledJobAttemptStatus.RUNNING,
        started_at=now,
    )
    att2_running = ScheduledJobAttempt(
        attempt_id=att2_id,
        definition=def2,
        local_date=date(2026, 7, 29),
        scheduled_for=scheduled_for,
        attempt_number=1,
        status=ScheduledJobAttemptStatus.RUNNING,
        started_at=now + timedelta(minutes=1),
    )

    att1_succeeded = att1_running.model_copy(
        update={
            "status": ScheduledJobAttemptStatus.SUCCEEDED,
            "completed_at": now + timedelta(minutes=2),
            "execution": ScheduledJobExecution(
                job_id=def1.job_id,
                effective_known_at=now,
                evidence_changed=True,
                source_ids=(f"source:{def1.job_id}",),
                created_count=1,
                reused_count=0,
            ),
        }
    )
    att2_failed = att2_running.model_copy(
        update={
            "status": ScheduledJobAttemptStatus.FAILED,
            "completed_at": now + timedelta(minutes=3),
            "failure": scheduled_job_failure(
                ScheduledJobFailureCategory.TRANSPORT, "transport error"
            ),
        }
    )

    transitions = [att1_running, att2_running, att1_succeeded, att2_failed]

    # Reference full rewrite path:
    reference_attempts: list[ScheduledJobAttempt] = []
    for transition in transitions:
        matching = [
            i for i, a in enumerate(reference_attempts) if a.attempt_id == transition.attempt_id
        ]
        if matching:
            reference_attempts[matching[0]] = transition
        else:
            reference_attempts.append(transition)
        reference_attempts.sort(
            key=lambda item: (
                item.started_at,
                item.definition.job_id,
                item.attempt_number,
                str(item.attempt_id),
            )
        )
    reference_state = MultiAssetScheduleState(attempts=tuple(reference_attempts))

    # Journal path:
    store = MultiAssetScheduleStateStore(tmp_path / "journal_schedule.json")
    for transition in transitions:
        store.write_attempt(transition)

    reconstructed_state = store.load()

    assert reconstructed_state.to_json_dict() == reference_state.to_json_dict()
    assert reconstructed_state.attempts == reference_state.attempts
    assert len(reconstructed_state.attempts) == 2
    assert reconstructed_state.attempts[0].status is ScheduledJobAttemptStatus.SUCCEEDED
    assert reconstructed_state.attempts[1].status is ScheduledJobAttemptStatus.FAILED


def test_legacy_v1_state_is_folded_once_and_preserved_byte_for_byte(tmp_path: Path) -> None:
    now = datetime(2026, 7, 29, 12, 0, tzinfo=UTC)
    scheduled_for = datetime(2026, 7, 29, 12, tzinfo=UTC)
    def1 = _definition("legacy-job")
    legacy_att = ScheduledJobAttempt(
        attempt_id=UUID("00000000-0000-4000-8000-000000000055"),
        definition=def1,
        local_date=date(2026, 7, 29),
        scheduled_for=scheduled_for,
        attempt_number=1,
        status=ScheduledJobAttemptStatus.SUCCEEDED,
        started_at=now,
        completed_at=now + timedelta(minutes=1),
        execution=ScheduledJobExecution(
            job_id=def1.job_id,
            effective_known_at=now,
            evidence_changed=True,
            source_ids=(f"source:{def1.job_id}",),
            created_count=2,
            reused_count=0,
        ),
    )
    legacy_state = MultiAssetScheduleState(attempts=(legacy_att,))
    legacy_path = tmp_path / "multi_asset_schedule_state_v1.json"
    legacy_payload = (
        json.dumps(
            legacy_state.to_json_dict(),
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        + b"\n"
    )
    legacy_path.write_bytes(legacy_payload)
    original_bytes = legacy_path.read_bytes()

    store = MultiAssetScheduleStateStore(legacy_path)

    # 1. Loading reads the legacy state without mutating disk
    loaded_initial = store.load()
    assert len(loaded_initial.attempts) == 1
    assert loaded_initial.attempts[0].attempt_id == legacy_att.attempt_id
    assert legacy_path.read_bytes() == original_bytes

    # 2. Writing a new attempt folds legacy state once into snapshot
    def2 = _definition("new-job")
    new_att = ScheduledJobAttempt(
        attempt_id=UUID("00000000-0000-4000-8000-000000000056"),
        definition=def2,
        local_date=date(2026, 7, 29),
        scheduled_for=scheduled_for,
        attempt_number=1,
        status=ScheduledJobAttemptStatus.SUCCEEDED,
        started_at=now + timedelta(minutes=5),
        completed_at=now + timedelta(minutes=6),
        execution=ScheduledJobExecution(
            job_id=def2.job_id,
            effective_known_at=now + timedelta(minutes=5),
            evidence_changed=True,
            source_ids=(f"source:{def2.job_id}",),
            created_count=1,
            reused_count=0,
        ),
    )
    store.write_attempt(new_att)

    # Legacy file is preserved byte-for-byte
    assert legacy_path.read_bytes() == original_bytes

    # Store load now returns both attempts
    reloaded = store.load()
    assert len(reloaded.attempts) == 2
    assert reloaded.attempts[0].attempt_id == legacy_att.attempt_id
    assert reloaded.attempts[1].attempt_id == new_att.attempt_id

    # Verify journal snapshot has folded legacy state
    assert store._journal.has_legacy_v1_folded()
    assert store._journal.has_snapshot()

    # Even if legacy file is deleted, store continues to have all attempts
    legacy_path.unlink()
    reloaded_after_delete = store.load()
    assert len(reloaded_after_delete.attempts) == 2
    assert reloaded_after_delete.attempts[0].attempt_id == legacy_att.attempt_id


def test_reason_code_round_trips_in_scheduled_attempt_and_legacy_attempt_loads(
    tmp_path: Path,
) -> None:
    job_def = _definition("failed-job")
    local_date = date(2026, 8, 1)
    scheduled_for = job_def.scheduled_for(local_date)
    now = scheduled_for + timedelta(minutes=1)

    # 1. Round-trip an attempt with a typed reason_code through the store
    store = MultiAssetScheduleStateStore(tmp_path / "schedule_state.json")
    attempt_with_code = ScheduledJobAttempt(
        attempt_id=UUID("00000000-0000-4000-8000-000000000001"),
        definition=job_def,
        local_date=local_date,
        scheduled_for=scheduled_for,
        attempt_number=1,
        status=ScheduledJobAttemptStatus.FAILED,
        started_at=now,
        completed_at=now + timedelta(seconds=1),
        failure=scheduled_job_failure(
            ScheduledJobFailureCategory.PROVIDER_CONTRACT,
            "scheduled provider payload or refresh contract is invalid",
            reason_code="smv_no_configured_evidence",
        ),
    )
    store.write_attempt(attempt_with_code)

    loaded_state = store.load()
    assert len(loaded_state.attempts) == 1
    loaded_attempt = loaded_state.attempts[0]
    assert loaded_attempt.status is ScheduledJobAttemptStatus.FAILED
    assert loaded_attempt.failure is not None
    assert loaded_attempt.failure.category is ScheduledJobFailureCategory.PROVIDER_CONTRACT
    assert (
        loaded_attempt.failure.message
        == "scheduled provider payload or refresh contract is invalid"
    )
    assert loaded_attempt.failure.retryable is False
    assert loaded_attempt.failure.reason_code == "smv_no_configured_evidence"

    # 2. Legacy attempt payload without reason_code is still valid and readable
    legacy_payload = {
        "schema_version": "scheduled-job-attempt-v1",
        "attempt_id": "00000000-0000-4000-8000-000000000099",
        "definition": job_def.model_dump(mode="json"),
        "local_date": local_date.isoformat(),
        "scheduled_for": scheduled_for.isoformat(),
        "attempt_number": 1,
        "status": "failed",
        "started_at": now.isoformat(),
        "completed_at": (now + timedelta(seconds=1)).isoformat(),
        "failure": {
            "category": "provider_contract_error",
            "message": "scheduled provider payload or refresh contract is invalid",
            "retryable": False,
        },
    }
    legacy_attempt = ScheduledJobAttempt.model_validate(legacy_payload)
    assert legacy_attempt.status is ScheduledJobAttemptStatus.FAILED
    assert legacy_attempt.failure is not None
    assert legacy_attempt.failure.category is ScheduledJobFailureCategory.PROVIDER_CONTRACT
    assert (
        legacy_attempt.failure.message
        == "scheduled provider payload or refresh contract is invalid"
    )
    assert legacy_attempt.failure.retryable is False
    assert legacy_attempt.failure.reason_code is None


def test_retry_policy_per_category_is_unchanged() -> None:
    expected_retryable = {
        ScheduledJobFailureCategory.CONFIGURATION: False,
        ScheduledJobFailureCategory.AUTHENTICATION: False,
        ScheduledJobFailureCategory.UNSUPPORTED_CAPABILITY: False,
        ScheduledJobFailureCategory.PROVIDER_CONTRACT: False,
        ScheduledJobFailureCategory.VALIDATION: False,
        ScheduledJobFailureCategory.STORAGE_STATE: False,
        ScheduledJobFailureCategory.RATE_LIMIT: True,
        ScheduledJobFailureCategory.TRANSPORT: True,
        ScheduledJobFailureCategory.TRANSIENT_HTTP: True,
        ScheduledJobFailureCategory.HTTP: False,
        ScheduledJobFailureCategory.UNEXPECTED: False,
        ScheduledJobFailureCategory.INTERRUPTED: True,
        ScheduledJobFailureCategory.MEMORY_BUDGET: False,
        ScheduledJobFailureCategory.LEGACY_UNKNOWN: False,
    }
    for category, retryable in expected_retryable.items():
        failure = scheduled_job_failure(category, "test message")
        assert failure.category is category
        assert failure.retryable is retryable


def test_malformed_reason_code_is_rejected() -> None:
    # Valid slugs pass
    valid = scheduled_job_failure(
        ScheduledJobFailureCategory.PROVIDER_CONTRACT,
        "test message",
        reason_code="valid_reason_slug",
    )
    assert valid.reason_code == "valid_reason_slug"

    # Malformed slugs fail validation
    malformed_cases = (
        "",
        "ab",  # too short (< 3)
        "a" * 41,  # too long (> 40)
        "UPPERCASE",  # uppercase not allowed
        "CamelCase",
        "has-hyphen",  # hyphens not allowed
        "has space",
        "1starts_with_digit",
        "_starts_with_underscore",
        "has.dot",
    )
    for bad_code in malformed_cases:
        with pytest.raises(ValidationError):
            scheduled_job_failure(
                ScheduledJobFailureCategory.PROVIDER_CONTRACT,
                "test message",
                reason_code=bad_code,
            )


def _attempt(
    definition: ScheduledJobDefinition,
    *,
    local_date: date,
    started_at: datetime,
    attempt_number: int,
    status: ScheduledJobAttemptStatus,
    attempt_id: UUID,
    completed_at: datetime | None = None,
    succeeded: bool = False,
    failed: bool = False,
) -> ScheduledJobAttempt:
    execution = None
    failure = None
    done_at = completed_at
    if succeeded:
        done_at = done_at or started_at + timedelta(minutes=1)
        execution = ScheduledJobExecution(
            job_id=definition.job_id,
            effective_known_at=started_at,
            evidence_changed=True,
            source_ids=(f"source:{definition.job_id}",),
            created_count=1,
            reused_count=0,
        )
    if failed:
        done_at = done_at or started_at + timedelta(minutes=1)
        failure = scheduled_job_failure(
            ScheduledJobFailureCategory.TRANSPORT,
            "transport error",
        )
    return ScheduledJobAttempt(
        attempt_id=attempt_id,
        definition=definition,
        local_date=local_date,
        scheduled_for=definition.scheduled_for(local_date),
        attempt_number=attempt_number,
        status=status,
        started_at=started_at,
        completed_at=done_at,
        execution=execution,
        failure=failure,
    )


def test_store_reuses_only_identical_validated_bytes(tmp_path: Path) -> None:
    path = tmp_path / "schedule.json"
    store = MultiAssetScheduleStateStore(path)
    definition = _definition("cached-job")
    now = datetime(2026, 7, 29, 12, 5, tzinfo=UTC)
    store.write_attempt(
        _attempt(
            definition,
            local_date=date(2026, 7, 29),
            started_at=now,
            attempt_number=1,
            status=ScheduledJobAttemptStatus.SUCCEEDED,
            attempt_id=UUID("00000000-0000-4000-8000-000000000101"),
            succeeded=True,
        )
    )

    first = store.load()
    fingerprint_before = (store._cached_fingerprint, store._cached_state is not None)
    second = store.load()

    assert second is first
    assert fingerprint_before[1] is True
    assert first.to_json_dict() == second.to_json_dict()

    other = MultiAssetScheduleStateStore(path, journal_dir=store.journal_dir)
    assert other.load().to_json_dict() == first.to_json_dict()
    assert other.load() is not first


def test_status_uses_one_history_pass_with_exact_multi_job_result(tmp_path: Path) -> None:
    lima_now = datetime(2026, 7, 29, 12, 5, tzinfo=UTC)
    first = _definition("a-multi")
    second = _definition("b-multi").model_copy(update={"timezone": "Europe/Madrid"})
    running = _attempt(
        first,
        local_date=date(2026, 7, 29),
        started_at=datetime(2026, 7, 29, 12, 1, tzinfo=UTC),
        attempt_number=1,
        status=ScheduledJobAttemptStatus.RUNNING,
        attempt_id=UUID("00000000-0000-4000-8000-000000000201"),
    )
    failed = _attempt(
        first,
        local_date=date(2026, 7, 29),
        started_at=datetime(2026, 7, 29, 12, 1, tzinfo=UTC),
        attempt_number=1,
        status=ScheduledJobAttemptStatus.FAILED,
        attempt_id=UUID("00000000-0000-4000-8000-000000000201"),
        failed=True,
        completed_at=datetime(2026, 7, 29, 12, 2, tzinfo=UTC),
    )
    succeeded = _attempt(
        second,
        local_date=date(2026, 7, 29),
        started_at=datetime(2026, 7, 29, 12, 3, tzinfo=UTC),
        attempt_number=1,
        status=ScheduledJobAttemptStatus.SUCCEEDED,
        attempt_id=UUID("00000000-0000-4000-8000-000000000202"),
        succeeded=True,
    )
    store = MultiAssetScheduleStateStore(tmp_path / "multi.json")
    store.write_attempt(running)
    store.write_attempt(failed)
    store.write_attempt(succeeded)
    state = store.load()

    attempts = [
        item.to_json_dict()
        for item in (MultiAssetScheduleStateStore(tmp_path / "multi.json").load().attempts)
    ]
    assert len(attempts) == len(state.attempts)

    scheduler = MultiAssetScheduler(
        (
            RegisteredScheduledJob(first, _execution),
            RegisteredScheduledJob(second, _execution),
        ),
        store,
        clock=lambda: lima_now,
    )
    later = lima_now + timedelta(hours=2)
    advanced = MultiAssetScheduler(
        (
            RegisteredScheduledJob(first, _execution),
            RegisteredScheduledJob(second, _execution),
        ),
        store,
        clock=lambda: later,
    )

    current = scheduler.status()
    per_job = [
        scheduler._job_status(definition, lima_now, state).to_json_dict()
        for definition in (first, second)
    ]
    reference_jobs = sorted(per_job, key=lambda item: item["definition"]["job_id"])
    reference = MultiAssetSchedulerStatus.model_validate(
        {
            "schema_version": "multi-asset-scheduler-status-v1",
            "enabled": True,
            "jobs": reference_jobs,
            "due_count": sum(item["due"] for item in reference_jobs),
            "running_count": sum(
                item["latest_attempt"] is not None and item["latest_attempt"]["status"] == "running"
                for item in reference_jobs
            ),
            "failed_count": sum(
                item["latest_attempt"] is not None
                and item["latest_attempt"]["status"] in {"failed", "skipped"}
                for item in reference_jobs
            ),
            "blocked_count": sum(item["health"] == "blocked" for item in reference_jobs),
            "retry_wait_count": sum(item["health"] == "retry_wait" for item in reference_jobs),
            "current_count": sum(item["health"] == "current" for item in reference_jobs),
            "stale_count": sum(item["freshness"] == "stale" for item in reference_jobs),
            "incomplete_count": sum(item["freshness"] == "incomplete" for item in reference_jobs),
            "next_run_at": min(item["next_run_at"] for item in reference_jobs),
            "issues": [issue for item in reference_jobs for issue in item["issues"]],
        }
    )
    assert current.to_json_dict() == reference.to_json_dict()

    recomputed = MultiAssetScheduler(
        (
            RegisteredScheduledJob(first, _execution),
            RegisteredScheduledJob(second, _execution),
        ),
        store,
        clock=lambda: lima_now,
    ).status()
    assert current.to_json_dict() == recomputed.to_json_dict()
    assert {item.definition.job_id for item in current.jobs} == {"a-multi", "b-multi"}
    assert advanced.status().to_json_dict() != current.to_json_dict()


def test_store_invalidates_on_append_and_detects_external_corruption(
    tmp_path: Path,
) -> None:
    path = tmp_path / "schedule.json"
    store = MultiAssetScheduleStateStore(path)
    definition = _definition("invalidate-job")
    now = datetime(2026, 7, 29, 12, 5, tzinfo=UTC)
    first_id = UUID("00000000-0000-4000-8000-000000000301")
    second_id = UUID("00000000-0000-4000-8000-000000000302")
    store.write_attempt(
        _attempt(
            definition,
            local_date=date(2026, 7, 29),
            started_at=now,
            attempt_number=1,
            status=ScheduledJobAttemptStatus.SUCCEEDED,
            attempt_id=first_id,
            succeeded=True,
        )
    )
    before = store.load()
    assert len(before.attempts) == 1

    store.write_attempt(
        _attempt(
            definition,
            local_date=date(2026, 7, 30),
            started_at=now + timedelta(days=1),
            attempt_number=1,
            status=ScheduledJobAttemptStatus.SUCCEEDED,
            attempt_id=second_id,
            succeeded=True,
        )
    )
    after = store.load()
    assert after is not before
    assert len(after.attempts) == 2

    journal_files = [item for item in store.journal_dir.rglob("*") if item.is_file()]
    assert journal_files
    target = sorted(journal_files)[0]
    original = target.read_bytes()
    target.write_bytes(original + b'{"corrupted":true}\n')
    try:
        with pytest.raises(Exception, match="malformed|corrupt|digest|invalid"):
            store.load()
    finally:
        target.write_bytes(original)
    assert store.load().to_json_dict() == after.to_json_dict()


def test_store_preserves_legacy_and_journal_bytes_on_cached_reads(
    tmp_path: Path,
) -> None:
    path = tmp_path / "schedule.json"
    legacy = MultiAssetScheduleState(attempts=()).to_json_dict()
    path.write_bytes((json.dumps(legacy, sort_keys=True) + "\n").encode("utf-8"))
    store = MultiAssetScheduleStateStore(path)
    before_legacy = path.read_bytes()
    before_journal = store.persisted_signatures()

    assert store.load().attempts == ()
    assert store.load().attempts == ()
    assert path.read_bytes() == before_legacy
    assert store.persisted_signatures() == before_journal


def test_status_recomputes_clock_and_registry_with_unchanged_state(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 7, 29, 12, 5, tzinfo=UTC)
    first = _definition("clock-a")
    second = _definition("clock-b")
    store = MultiAssetScheduleStateStore(tmp_path / "clock.json")
    scheduler = MultiAssetScheduler(
        (
            RegisteredScheduledJob(first, _execution),
            RegisteredScheduledJob(second, _execution),
        ),
        store,
        clock=lambda: now,
    )

    before = scheduler.status()
    later = now + timedelta(days=1, minutes=2)
    scheduler_later = MultiAssetScheduler(
        (
            RegisteredScheduledJob(first, _execution),
            RegisteredScheduledJob(second, _execution),
        ),
        MultiAssetScheduleStateStore(tmp_path / "clock.json"),
        clock=lambda: later,
    )
    changed = scheduler_later.status()

    assert before.to_json_dict() != changed.to_json_dict()

    scheduler.reconcile_jobs((RegisteredScheduledJob(_definition("replacement"), _execution),))
    replaced = scheduler.status()
    assert {item.definition.job_id for item in replaced.jobs} == {"replacement"}


def test_legacy_execution_without_analytical_inputs_flag_round_trips() -> None:
    definition = _definition("legacy-flag")
    legacy = ScheduledJobExecution(
        job_id=definition.job_id,
        effective_known_at=datetime(2026, 7, 29, 12, tzinfo=UTC),
        evidence_changed=True,
        source_ids=("source:legacy-flag",),
        created_count=1,
        reused_count=0,
    )
    assert legacy.analytical_inputs_changed is None
    assert legacy.to_json_dict()["analytical_inputs_changed"] is None
    restored = ScheduledJobExecution.model_validate(legacy.to_json_dict())
    assert restored == legacy
    flagged = legacy.model_copy(update={"analytical_inputs_changed": False})
    assert flagged.analytical_inputs_changed is False
    assert ScheduledJobExecution.model_validate(flagged.to_json_dict()) == flagged
    with pytest.raises(ValueError, match="analytical_inputs_changed"):
        ScheduledJobExecution.model_validate(
            {**legacy.to_json_dict(), "analytical_inputs_changed": "yes"}
        )
