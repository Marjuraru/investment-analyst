"""Focused tests for cooperative scheduled-job memory budgets."""

import json
import time as time_module
from datetime import UTC, datetime, time
from pathlib import Path
from threading import Event
from uuid import UUID

import pytest

from investment_analyst.application import job_memory_budget
from investment_analyst.application.job_memory_budget import (
    JobMemoryBudget,
    read_process_rss_kb,
)
from investment_analyst.application.multi_asset_scheduler import (
    MultiAssetScheduler,
    MultiAssetScheduleStateStore,
    ProviderJobTelemetry,
    RegisteredScheduledJob,
    ScheduledJobAttemptStatus,
    ScheduledJobDefinition,
    ScheduledJobDomain,
    ScheduledJobExecution,
    ScheduledJobFailure,
    ScheduledJobFailureCategory,
    ScheduledJobInvocation,
    scheduled_job_failure,
)
from investment_analyst.core.operation_control import (
    OperationCancelledError,
    OperationControl,
    current_operation_control,
)


class _CountingOperationControl(OperationControl):
    def __init__(self) -> None:
        super().__init__()
        self.cancel_calls = 0

    def cancel(self) -> None:
        self.cancel_calls += 1
        super().cancel()


def _definition(job_id: str) -> ScheduledJobDefinition:
    return ScheduledJobDefinition(
        job_id=job_id,
        asset_id=f"equity:test:{job_id}",
        provider="test-provider",
        domain=ScheduledJobDomain.MARKET_DAILY,
        data_frequency="day_1",
        timezone="America/Lima",
        run_at=time(hour=7),
        retry_backoff_seconds=60,
    )


def _execution(invocation: ScheduledJobInvocation) -> ScheduledJobExecution:
    return ScheduledJobExecution(
        job_id=invocation.definition.job_id,
        effective_known_at=invocation.started_at,
        evidence_changed=True,
        source_ids=(f"source:{invocation.definition.job_id}",),
        created_count=1,
        reused_count=0,
    )


def _telemetry_payload() -> dict[str, object]:
    return {
        "schema_version": "provider-job-telemetry-v1",
        "job_id": "test-job",
        "provider": "test-provider",
        "domain": "market_daily",
        "started_at": "2026-09-07T12:00:00+00:00",
        "completed_at": "2026-09-07T12:00:01+00:00",
        "duration_ms": 1000,
        "provider_call_count": None,
        "response_bytes": None,
        "created_count": 1,
        "reused_count": 0,
        "coverage_complete": True,
        "failure_category": None,
    }


def test_watchdog_cancels_once_when_ceiling_is_exceeded(monkeypatch: pytest.MonkeyPatch) -> None:
    control = _CountingOperationControl()
    monkeypatch.setattr(job_memory_budget, "read_process_rss_kb", lambda: 2)

    with JobMemoryBudget(ceiling_bytes=1024, sample_interval_seconds=0.001).watch(
        control
    ) as watchdog:
        assert control.cancelled
        time_module.sleep(0.005)

    assert control.cancel_calls == 1
    assert watchdog.breached
    assert watchdog.peak_rss_kb == 2
    assert not watchdog.thread_alive


def test_watchdog_thread_is_joined_on_success_and_on_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(job_memory_budget, "read_process_rss_kb", lambda: 1)
    budget = JobMemoryBudget(ceiling_bytes=1024 * 1024, sample_interval_seconds=0.001)
    success = budget.watch(OperationControl())
    with success:
        assert success.thread_alive
    assert not success.thread_alive

    failure = budget.watch(OperationControl())
    with pytest.raises(RuntimeError, match="synthetic failure"), failure:
        raise RuntimeError("synthetic failure")
    assert not failure.thread_alive


def test_missing_proc_source_yields_none_peak_and_inert_watchdog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    control = _CountingOperationControl()
    monkeypatch.setattr(job_memory_budget, "read_process_rss_kb", lambda: None)

    with JobMemoryBudget(ceiling_bytes=1, sample_interval_seconds=0.001).watch(control) as watchdog:
        time_module.sleep(0.005)

    assert watchdog.peak_rss_kb is None
    assert not watchdog.breached
    assert control.cancel_calls == 0


def test_watchdog_without_ceiling_measures_peak_and_never_cancels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    control = _CountingOperationControl()
    values = iter((3, 8, 5))
    monkeypatch.setattr(job_memory_budget, "read_process_rss_kb", lambda: next(values, 5))

    with JobMemoryBudget(sample_interval_seconds=0.001).watch(control) as watchdog:
        time_module.sleep(0.005)

    assert watchdog.peak_rss_kb == 8
    assert not watchdog.breached
    assert control.cancel_calls == 0


def test_watchdog_reads_only_its_own_process_memory() -> None:
    source = Path(job_memory_budget.__file__).read_text(encoding="utf-8")

    assert Path("/proc/self/status") == job_memory_budget._PROC_STATUS
    assert 'Path("/proc/self/status")' in source
    assert "/proc/meminfo" not in source


def test_memory_budget_category_is_canonical_and_not_retryable() -> None:
    failure = scheduled_job_failure(
        ScheduledJobFailureCategory.MEMORY_BUDGET,
        "safe memory budget failure",
    )

    assert ScheduledJobFailureCategory.MEMORY_BUDGET.value == "memory_budget_exceeded"
    assert failure.retryable is False
    with pytest.raises(ValueError, match="retryable must match"):
        ScheduledJobFailure(
            category=ScheduledJobFailureCategory.MEMORY_BUDGET,
            message="safe memory budget failure",
            retryable=True,
        )


def test_exceeding_job_fails_as_memory_budget_exceeded(tmp_path: Path) -> None:
    now = datetime(2026, 9, 7, 12, 5, tzinfo=UTC)
    definition = _definition("memory-exceeded")

    def over_budget(invocation: ScheduledJobInvocation) -> ScheduledJobExecution:
        payload = bytearray(2 * 1024 * 1024)
        for index in range(0, len(payload), 4096):
            payload[index] = 1
        control = current_operation_control()
        assert control is not None
        while not control.cancelled:
            time_module.sleep(0.001)
        control.raise_if_cancelled()
        return _execution(invocation)

    scheduler = MultiAssetScheduler(
        (RegisteredScheduledJob(definition, over_budget),),
        MultiAssetScheduleStateStore(tmp_path / "memory-exceeded.json"),
        clock=lambda: now,
        memory_ceiling_bytes=1,
        attempt_id_factory=lambda: UUID("00000000-0000-4000-8000-000000000101"),
    )

    completed = scheduler.tick()

    assert len(completed) == 1
    attempt = completed[0]
    assert attempt.status is ScheduledJobAttemptStatus.FAILED
    assert attempt.failure is not None
    assert attempt.failure.category is ScheduledJobFailureCategory.MEMORY_BUDGET
    assert attempt.failure.retryable is False
    assert attempt.telemetry is not None
    assert attempt.telemetry.peak_rss_kb is not None
    assert attempt.telemetry.peak_rss_kb > 0


def test_stop_event_cancellation_still_fails_as_interrupted_job(tmp_path: Path) -> None:
    now = datetime(2026, 9, 7, 12, 5, tzinfo=UTC)
    definition = _definition("stop-event")
    started = Event()

    def cancellable(invocation: ScheduledJobInvocation) -> ScheduledJobExecution:
        del invocation
        control = current_operation_control()
        assert control is not None
        started.set()
        while not control.cancelled:
            time_module.sleep(0.001)
        control.raise_if_cancelled()
        raise AssertionError("cancellation should have raised")

    scheduler = MultiAssetScheduler(
        (RegisteredScheduledJob(definition, cancellable),),
        MultiAssetScheduleStateStore(tmp_path / "stop-event.json"),
        clock=lambda: now,
    )
    stop_event = Event()
    import threading

    thread = threading.Thread(
        target=scheduler.run_forever,
        args=(stop_event,),
        kwargs={"poll_seconds": 0.001},
    )
    thread.start()
    assert started.wait(timeout=2)
    stop_event.set()
    thread.join(timeout=2)

    assert not thread.is_alive()
    attempt = scheduler._store.load().attempts[0]
    assert attempt.failure is not None
    assert attempt.failure.category is ScheduledJobFailureCategory.INTERRUPTED


def test_category_is_not_derived_from_exception_message(tmp_path: Path) -> None:
    now = datetime(2026, 9, 7, 12, 5, tzinfo=UTC)
    definition = _definition("message-independent")

    def raises_cancellation(invocation: ScheduledJobInvocation) -> ScheduledJobExecution:
        del invocation
        raise OperationCancelledError("memory_budget_exceeded")

    scheduler = MultiAssetScheduler(
        (RegisteredScheduledJob(definition, raises_cancellation),),
        MultiAssetScheduleStateStore(tmp_path / "message-independent.json"),
        clock=lambda: now,
    )

    attempt = scheduler.tick()[0]

    assert attempt.failure is not None
    assert attempt.failure.category is ScheduledJobFailureCategory.INTERRUPTED


def test_job_below_ceiling_behaves_exactly_as_base(tmp_path: Path) -> None:
    now = datetime(2026, 9, 7, 12, 5, tzinfo=UTC)
    definition = _definition("below-ceiling")

    def below_budget(invocation: ScheduledJobInvocation) -> ScheduledJobExecution:
        payload = bytearray(1024 * 1024)
        for index in range(0, len(payload), 4096):
            payload[index] = 1
        return _execution(invocation)

    scheduler = MultiAssetScheduler(
        (RegisteredScheduledJob(definition, below_budget),),
        MultiAssetScheduleStateStore(tmp_path / "below-ceiling.json"),
        clock=lambda: now,
        memory_ceiling_bytes=1024 * 1024 * 1024,
    )

    attempt = scheduler.tick()[0]

    assert attempt.status is ScheduledJobAttemptStatus.SUCCEEDED
    assert attempt.execution is not None
    assert attempt.failure is None
    assert attempt.execution.job_id == definition.job_id
    assert attempt.telemetry is not None
    assert attempt.telemetry.peak_rss_kb is not None


def test_non_positive_memory_ceiling_fails_closed(tmp_path: Path) -> None:
    definition = _definition("invalid-ceiling")
    for value in (0, -1):
        with pytest.raises(ValueError, match="ceiling_bytes"):
            MultiAssetScheduler(
                (RegisteredScheduledJob(definition, _execution),),
                MultiAssetScheduleStateStore(tmp_path / f"invalid-{value}.json"),
                memory_ceiling_bytes=value,
            )


def test_telemetry_accepts_optional_peak_rss_kb() -> None:
    telemetry = ProviderJobTelemetry.model_validate({**_telemetry_payload(), "peak_rss_kb": 1234})

    assert telemetry.peak_rss_kb == 1234
    assert telemetry.schema_version == "provider-job-telemetry-v1"


def test_base_telemetry_document_validates_and_round_trips_without_the_field() -> None:
    payload = _telemetry_payload()

    telemetry = ProviderJobTelemetry.model_validate(payload)
    serialized = json.loads(telemetry.model_dump_json())

    assert "peak_rss_kb" not in payload
    assert telemetry.peak_rss_kb is None
    assert serialized.get("peak_rss_kb") != 0
    assert telemetry.schema_version == "provider-job-telemetry-v1"


def test_absent_peak_is_none_and_never_zero() -> None:
    telemetry = ProviderJobTelemetry.model_validate(_telemetry_payload())

    assert telemetry.peak_rss_kb is None
    assert telemetry.peak_rss_kb != 0


def test_missing_proc_status_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(job_memory_budget, "_PROC_STATUS", Path("/tmp/no-such-status"))

    assert read_process_rss_kb() is None
