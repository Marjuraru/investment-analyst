"""Tests for provider-independent multi-asset scheduling and recovery."""

import threading
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from uuid import UUID

import pytest

from investment_analyst.application.multi_asset_scheduler import (
    MultiAssetScheduler,
    MultiAssetScheduleStateStore,
    RegisteredScheduledJob,
    ScheduledJobAttempt,
    ScheduledJobAttemptStatus,
    ScheduledJobDefinition,
    ScheduledJobDomain,
    ScheduledJobExecution,
    ScheduledJobFailure,
    ScheduledJobFailureCategory,
    ScheduledJobFreshness,
    ScheduledJobInvocation,
    ScheduledJobRunError,
)


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
            ScheduledJobFailure(
                category="provider_unavailable",
                message="provider unavailable",
                retryable=True,
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
    assert completed[1].telemetry.failure_category == "provider_unavailable"
    assert completed[1].telemetry.provider_call_count is None
    assert completed[1].telemetry.response_bytes is None
    status = scheduler.status()
    assert status.due_count == 0
    assert status.failed_count == 1
    assert status.next_run_at == now + timedelta(seconds=60)


def test_scheduler_retries_only_failed_job_after_backoff(tmp_path: Path) -> None:
    now = datetime(2026, 7, 29, 12, 5, tzinfo=UTC)
    definition = _definition("retry-market")
    calls = 0

    def run(invocation: ScheduledJobInvocation) -> ScheduledJobExecution:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ScheduledJobRunError(
                ScheduledJobFailure(
                    category="temporary",
                    message="temporary provider failure",
                    retryable=True,
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

    assert calls == 2
    assert second[0].attempt_number == 2
    assert second[0].status is ScheduledJobAttemptStatus.SUCCEEDED
    assert scheduler.status().failed_count == 0


@pytest.mark.parametrize(
    ("mode", "expected_status", "expected_category"),
    [
        (
            "permanent",
            ScheduledJobAttemptStatus.SKIPPED,
            ScheduledJobFailureCategory.AUTHENTICATION,
        ),
        (
            "validation",
            ScheduledJobAttemptStatus.SKIPPED,
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
                ScheduledJobFailure(
                    category=ScheduledJobFailureCategory.AUTHENTICATION,
                    message="safe authentication failure",
                    retryable=False,
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
    assert any("latest scheduled job failed" in issue for issue in job_status.issues)


def test_scheduler_preserves_last_success_after_later_permanent_failure(tmp_path: Path) -> None:
    now = datetime(2026, 7, 29, 12, 5, tzinfo=UTC)
    should_fail = False

    def run(invocation: ScheduledJobInvocation) -> ScheduledJobExecution:
        if should_fail:
            raise ScheduledJobRunError(
                ScheduledJobFailure(
                    category=ScheduledJobFailureCategory.CONFIGURATION,
                    message="safe configuration failure",
                    retryable=False,
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
    assert second.status is ScheduledJobAttemptStatus.SKIPPED
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
    now += timedelta(days=3)
    stale = scheduler.status()
    assert stale.jobs[0].freshness is ScheduledJobFreshness.STALE
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
