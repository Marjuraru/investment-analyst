"""Tests for provider-independent multi-asset scheduling and recovery."""

import json
import threading
import time as time_module
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from uuid import UUID

import duckdb
import pytest
from pydantic import ValidationError

from investment_analyst.application.multi_asset_scheduler import (
    MultiAssetScheduler,
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
    assert tuple(item.table_name for item in record.table_bytes) == ("metric_results",)
    assert record.table_bytes[0].row_count == 1
    assert record.durations.total_ms == 0


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
