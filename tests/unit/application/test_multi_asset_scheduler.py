"""Tests for provider-independent multi-asset scheduling and recovery."""

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
