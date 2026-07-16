"""Tests for locked Apple operational orchestration."""

from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from uuid import uuid4

import pytest

from investment_analyst.analytics.consolidated_diagnostic_models import (
    ConsolidatedDiagnosticStatus,
)
from investment_analyst.application.aapl_bootstrap_models import (
    AaplMarketRefreshMode,
    AaplWorkspaceBootstrapRequest,
)
from investment_analyst.application.aapl_daily_runner import (
    AaplDailyRunExecutionError,
    AaplDailyRunner,
)
from investment_analyst.application.facade import AaplApplicationBootstrapResult
from investment_analyst.application.operational_models import (
    AaplDailyRunState,
    AaplDailyRunStatus,
    AaplOperationalHealthStatus,
)
from investment_analyst.application.operational_state import (
    AaplDailyRunLock,
    AaplDailyRunStateStore,
)
from investment_analyst.core.models import DataFrequency
from investment_analyst.providers.fundamentals.sec_edgar import SecEdgarIdentity
from investment_analyst.providers.market.alpaca_stock import AlpacaCredentials
from investment_analyst.workspace.service import WorkspaceService

_KNOWN_AT = datetime(2026, 7, 16, 15, 46, 9, tzinfo=UTC)


class _FakeApplication:
    def __init__(self, service: WorkspaceService, failure: Exception | None = None) -> None:
        self._service = service
        self._failure = failure
        self.calls = 0

    def bootstrap_aapl_workspace(
        self,
        request: AaplWorkspaceBootstrapRequest,
        *,
        workspace: Path | None,
        alpaca_credentials: AlpacaCredentials,
        sec_identity: SecEdgarIdentity,
    ) -> AaplApplicationBootstrapResult:
        del request, alpaca_credentials, sec_identity
        self.calls += 1
        if self._failure is not None:
            raise self._failure
        initialization = self._service.initialize(workspace)
        created = 1 if self.calls == 1 else 0
        summary = SimpleNamespace(
            effective_known_at=_KNOWN_AT,
            refresh_plan=SimpleNamespace(mode=AaplMarketRefreshMode.ALREADY_CURRENT),
            overall_status=ConsolidatedDiagnosticStatus.COMPLETE,
            raw_records_created=created,
            raw_records_reused=2,
            observations_created=created,
            observations_reused=206,
            metric_results_created=created,
            metric_results_reused=1990,
            diagnostics_created=created,
            diagnostics_reused=2,
            traceability_verified=True,
        )
        return cast(
            AaplApplicationBootstrapResult,
            SimpleNamespace(initialization=initialization, summary=summary),
        )


def _request() -> AaplWorkspaceBootstrapRequest:
    return AaplWorkspaceBootstrapRequest(
        market_start=date(2025, 1, 1),
        market_end=date(2026, 7, 15),
        fundamental_frequency=DataFrequency.QUARTERLY,
        require_complete=True,
    )


def _credentials() -> tuple[AlpacaCredentials, SecEdgarIdentity]:
    return (
        AlpacaCredentials(api_key="test-key", secret_key="test-secret"),
        SecEdgarIdentity("Investment Analyst tests@example.com"),
    )


def _clock(start: datetime) -> Callable[[], datetime]:
    values = iter(start + timedelta(minutes=offset) for offset in range(4))
    return lambda: next(values)


def test_runner_persists_success_and_replaces_latest_state_idempotently(tmp_path: Path) -> None:
    service = WorkspaceService(environ={}, home=tmp_path)
    application = _FakeApplication(service)
    run_ids = iter((uuid4(), uuid4()))
    runner = AaplDailyRunner(
        application,
        service,
        clock=_clock(datetime(2026, 7, 16, 15, tzinfo=UTC)),
        run_id_factory=lambda: next(run_ids),
    )
    workspace = tmp_path / "workspace"
    credentials, identity = _credentials()

    first = runner.run(
        _request(),
        workspace=workspace,
        alpaca_credentials=credentials,
        sec_identity=identity,
    )
    second = runner.run(
        _request(),
        workspace=workspace,
        alpaca_credentials=credentials,
        sec_identity=identity,
    )

    assert first.status is AaplDailyRunStatus.SUCCEEDED
    assert first.counts is not None and first.counts.raw_records_created == 1
    assert second.counts is not None and second.counts.raw_records_created == 0
    assert second.run_id != first.run_id
    state_path = service.resolve(workspace).state_root / "aapl_daily_run_state.json"
    assert AaplDailyRunStateStore(state_path).load() == second
    health = runner.inspect(workspace=workspace)
    assert health.status is AaplOperationalHealthStatus.READY
    assert health.latest_run == second
    assert health.issues == ()


@pytest.mark.parametrize(
    ("failure", "expected_category", "expected_message"),
    [
        (ValueError("rejected test-secret"), "ValueError", "rejected <redacted>"),
        (RuntimeError("unexpected test-secret"), "unexpected_error", "failed unexpectedly"),
    ],
)
def test_runner_persists_sanitized_failure_and_raises_safe_error(
    tmp_path: Path,
    failure: Exception,
    expected_category: str,
    expected_message: str,
) -> None:
    service = WorkspaceService(environ={}, home=tmp_path)
    runner = AaplDailyRunner(
        _FakeApplication(service, failure),
        service,
        clock=_clock(datetime(2026, 7, 16, 15, tzinfo=UTC)),
    )
    workspace = tmp_path / "workspace"
    credentials, identity = _credentials()

    with pytest.raises(AaplDailyRunExecutionError) as captured:
        runner.run(
            _request(),
            workspace=workspace,
            alpaca_credentials=credentials,
            sec_identity=identity,
        )

    assert captured.value.cause is failure
    assert "test-secret" not in str(captured.value)
    state_path = service.resolve(workspace).state_root / "aapl_daily_run_state.json"
    state = AaplDailyRunStateStore(state_path).load()
    assert isinstance(state, AaplDailyRunState)
    assert state.status is AaplDailyRunStatus.FAILED
    assert state.failure is not None
    assert state.failure.category == expected_category
    assert expected_message in state.failure.message
    assert "test-secret" not in state_path.read_text(encoding="utf-8")


def test_health_marks_unlocked_running_state_as_interrupted(tmp_path: Path) -> None:
    service = WorkspaceService(environ={}, home=tmp_path)
    workspace = tmp_path / "workspace"
    initialization = service.initialize(workspace)
    running = AaplDailyRunState(
        run_id=uuid4(),
        status=AaplDailyRunStatus.RUNNING,
        workspace_root=workspace,
        request=_request(),
        started_at=datetime(2026, 7, 16, 15, tzinfo=UTC),
    )
    AaplDailyRunStateStore(initialization.paths.state_root / "aapl_daily_run_state.json").write(
        running
    )

    health = AaplDailyRunner(_FakeApplication(service), service).inspect(workspace=workspace)

    assert health.status is AaplOperationalHealthStatus.DEGRADED
    assert "interrupted" in " ".join(health.issues)


def test_health_reports_running_while_process_lock_is_held(tmp_path: Path) -> None:
    service = WorkspaceService(environ={}, home=tmp_path)
    workspace = tmp_path / "workspace"
    initialization = service.initialize(workspace)
    running = AaplDailyRunState(
        run_id=uuid4(),
        status=AaplDailyRunStatus.RUNNING,
        workspace_root=workspace,
        request=_request(),
        started_at=datetime(2026, 7, 16, 15, tzinfo=UTC),
    )
    AaplDailyRunStateStore(initialization.paths.state_root / "aapl_daily_run_state.json").write(
        running
    )
    lock = AaplDailyRunLock(
        initialization.paths.state_root / "aapl_daily_run.lock",
        run_id=running.run_id,
        started_at=running.started_at.isoformat(),
    )

    with lock:
        health = AaplDailyRunner(_FakeApplication(service), service).inspect(workspace=workspace)

    assert health.status is AaplOperationalHealthStatus.RUNNING
    assert health.latest_run == running
    assert health.issues == ()
