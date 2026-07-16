"""Tests for deterministic local Apple scheduling and persistent attempt state."""

from datetime import UTC, date, datetime, time
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from typing import cast
from uuid import uuid4

import pytest
from pydantic import ValidationError

from investment_analyst.application.aapl_bootstrap_models import (
    AaplWorkspaceBootstrapRequest,
)
from investment_analyst.application.aapl_daily_runner import AaplDailyRunExecutionError
from investment_analyst.application.aapl_scheduler import (
    AaplDailyScheduleAttempt,
    AaplDailyScheduleConfig,
    AaplDailyScheduler,
    AaplDailyScheduleStateStore,
    AaplLocalServiceAlreadyRunningError,
    AaplLocalServiceLock,
    AaplScheduleAttemptStatus,
)
from investment_analyst.application.operational_models import (
    AaplDailyRunFailure,
    AaplDailyRunState,
)
from investment_analyst.application.operational_state import (
    AaplDailyRunAlreadyRunningError,
)


def _config() -> AaplDailyScheduleConfig:
    return AaplDailyScheduleConfig(
        timezone="America/Lima",
        run_at=time(hour=7),
        market_start=date(2025, 1, 1),
        market_end_lag_days=1,
    )


def _result() -> AaplDailyRunState:
    return cast(AaplDailyRunState, SimpleNamespace(run_id=uuid4()))


def test_schedule_builds_explicit_calendar_request_and_rejects_invalid_config() -> None:
    config = _config()

    assert config.scheduled_for(date(2026, 7, 16)) == datetime(2026, 7, 16, 12, tzinfo=UTC)
    request = config.request_for(date(2026, 7, 16))
    assert request.market_start == date(2025, 1, 1)
    assert request.market_end == date(2026, 7, 15)
    assert request.requested_known_at is None
    assert request.require_complete is True
    assert config.to_json_dict()["run_at"] == "07:00"

    with pytest.raises(ValidationError, match="IANA timezone"):
        AaplDailyScheduleConfig(
            timezone="Mars/Olympus",
            market_start=date(2025, 1, 1),
        )
    with pytest.raises(ValidationError, match="must be an integer"):
        AaplDailyScheduleConfig(
            market_start=date(2025, 1, 1),
            market_end_lag_days=True,
        )


def test_scheduler_runs_once_after_due_and_persists_success(tmp_path: Path) -> None:
    now = datetime(2026, 7, 16, 12, 5, tzinfo=UTC)
    requests: list[AaplWorkspaceBootstrapRequest] = []
    result = _result()

    def run(request: AaplWorkspaceBootstrapRequest) -> AaplDailyRunState:
        requests.append(request)
        return result

    store = AaplDailyScheduleStateStore(tmp_path / "state" / "schedule.json")
    scheduler = AaplDailyScheduler(_config(), store, run, clock=lambda: now)

    completed = scheduler.tick()

    assert completed is not None
    assert completed.status is AaplScheduleAttemptStatus.SUCCEEDED
    assert completed.run_id == result.run_id
    assert requests[0].market_end == date(2026, 7, 15)
    assert store.load() == completed
    assert scheduler.tick() is None
    status = scheduler.status()
    assert status.due is False
    assert status.next_run_at == datetime(2026, 7, 17, 12, tzinfo=UTC)
    assert status.issues == ()


def test_scheduler_waits_before_due_and_records_safe_failure_once(tmp_path: Path) -> None:
    before = datetime(2026, 7, 16, 11, 59, tzinfo=UTC)
    store = AaplDailyScheduleStateStore(tmp_path / "schedule.json")
    calls = 0

    def fail(request: AaplWorkspaceBootstrapRequest) -> AaplDailyRunState:
        nonlocal calls
        del request
        calls += 1
        failure = AaplDailyRunFailure(category="provider_error", message="provider unavailable")
        raise AaplDailyRunExecutionError(failure, ValueError("internal detail"))

    clock_value = before
    scheduler = AaplDailyScheduler(_config(), store, fail, clock=lambda: clock_value)

    assert scheduler.tick() is None
    assert scheduler.status().next_run_at == datetime(2026, 7, 16, 12, tzinfo=UTC)
    clock_value = datetime(2026, 7, 16, 12, 1, tzinfo=UTC)
    failed = scheduler.tick()

    assert failed is not None
    assert failed.status is AaplScheduleAttemptStatus.FAILED
    assert failed.failure is not None
    assert failed.failure.message == "provider unavailable"
    assert scheduler.tick() is None
    assert calls == 1
    assert scheduler.status().issues == ("the latest scheduled attempt failed",)


def test_scheduler_reports_interrupted_persisted_attempt(tmp_path: Path) -> None:
    now = datetime(2026, 7, 16, 13, tzinfo=UTC)
    running = AaplDailyScheduleAttempt(
        attempt_id=uuid4(),
        local_date=date(2026, 7, 16),
        scheduled_for=datetime(2026, 7, 16, 12, tzinfo=UTC),
        status=AaplScheduleAttemptStatus.RUNNING,
        started_at=datetime(2026, 7, 16, 12, tzinfo=UTC),
    )
    store = AaplDailyScheduleStateStore(tmp_path / "schedule.json")
    store.write(running)
    scheduler = AaplDailyScheduler(_config(), store, lambda request: _result(), clock=lambda: now)

    status = scheduler.status()

    assert status.due is False
    assert status.latest_attempt == running
    assert status.issues == ("the latest scheduled attempt was interrupted",)


def test_local_service_lock_rejects_second_instance_and_releases(tmp_path: Path) -> None:
    path = tmp_path / "service.lock"
    first = AaplLocalServiceLock(
        path,
        service_id=uuid4(),
        started_at="2026-07-16T12:00:00+00:00",
    )

    with (
        first,
        pytest.raises(AaplLocalServiceAlreadyRunningError, match="already owns"),
        AaplLocalServiceLock(
            path,
            service_id=uuid4(),
            started_at="2026-07-16T12:01:00+00:00",
        ),
    ):
        raise AssertionError("a second service must never enter")

    with AaplLocalServiceLock(
        path,
        service_id=uuid4(),
        started_at="2026-07-16T12:02:00+00:00",
    ):
        assert path.is_file()


@pytest.mark.parametrize(
    ("error", "expected_status", "expected_category"),
    [
        (
            AaplDailyRunAlreadyRunningError("active"),
            AaplScheduleAttemptStatus.SKIPPED,
            "AaplDailyRunAlreadyRunningError",
        ),
        (
            RuntimeError("SECRET internal detail"),
            AaplScheduleAttemptStatus.FAILED,
            "unexpected_error",
        ),
    ],
)
def test_scheduler_bounds_concurrent_and_unexpected_failures(
    tmp_path: Path,
    error: Exception,
    expected_status: AaplScheduleAttemptStatus,
    expected_category: str,
) -> None:
    def fail(request: AaplWorkspaceBootstrapRequest) -> AaplDailyRunState:
        del request
        raise error

    scheduler = AaplDailyScheduler(
        _config(),
        AaplDailyScheduleStateStore(tmp_path / "schedule.json"),
        fail,
        clock=lambda: datetime(2026, 7, 16, 12, 5, tzinfo=UTC),
    )

    completed = scheduler.tick()

    assert completed is not None
    assert completed.status is expected_status
    assert completed.failure is not None
    assert completed.failure.category == expected_category
    assert "SECRET" not in completed.failure.message


def test_scheduler_loop_survives_malformed_state_and_validates_poll_interval(
    tmp_path: Path,
) -> None:
    path = tmp_path / "schedule.json"
    path.write_text("{invalid", encoding="utf-8")
    scheduler = AaplDailyScheduler(
        _config(),
        AaplDailyScheduleStateStore(path),
        lambda request: _result(),
        clock=lambda: datetime(2026, 7, 16, 12, 5, tzinfo=UTC),
    )
    stop_event = Event()
    errors: list[str] = []

    def handle_error(message: str) -> None:
        errors.append(message)
        stop_event.set()

    scheduler.run_forever(stop_event, poll_seconds=0.01, error_handler=handle_error)

    assert errors == ["daily scheduler could not evaluate its persisted state"]
    with pytest.raises(ValueError, match="must be positive"):
        scheduler.run_forever(stop_event, poll_seconds=0)
