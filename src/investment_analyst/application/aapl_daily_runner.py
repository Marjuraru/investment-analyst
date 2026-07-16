"""Single-run operational orchestration over the stable application facade."""

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol
from uuid import UUID, uuid4

from investment_analyst.analytics.consolidated_diagnostic_service import (
    ConsolidatedDiagnosticQueryError,
)
from investment_analyst.application.aapl_bootstrap import AaplWorkspaceBootstrapError
from investment_analyst.application.aapl_bootstrap_models import (
    AaplWorkspaceBootstrapRequest,
)
from investment_analyst.application.facade import (
    AaplApplicationBootstrapResult,
    InvestmentAnalystApplication,
)
from investment_analyst.application.operational_models import (
    AaplDailyRunCounts,
    AaplDailyRunFailure,
    AaplDailyRunState,
    AaplDailyRunStatus,
    AaplOperationalHealth,
    AaplOperationalHealthStatus,
)
from investment_analyst.application.operational_state import (
    AaplDailyRunLock,
    AaplDailyRunStateStore,
)
from investment_analyst.application.runtime import ApplicationRuntime, ApplicationRuntimeError
from investment_analyst.providers.fundamentals.sec_edgar import SecEdgarIdentity
from investment_analyst.providers.http import HttpRequestError
from investment_analyst.providers.market.alpaca_stock import AlpacaCredentials
from investment_analyst.storage import StorageError
from investment_analyst.workspace.service import WorkspaceError, WorkspaceService

_STATE_FILE_NAME = "aapl_daily_run_state.json"
_LOCK_FILE_NAME = "aapl_daily_run.lock"


class AaplDailyRunExecutionError(RuntimeError):
    """Safe operational failure that retains its original cause for adapters."""

    def __init__(self, failure: AaplDailyRunFailure, cause: Exception) -> None:
        self.failure = failure
        self.cause = cause
        super().__init__(failure.message)


class _ApplicationOperations(Protocol):
    def bootstrap_aapl_workspace(
        self,
        request: AaplWorkspaceBootstrapRequest,
        *,
        workspace: Path | None,
        alpaca_credentials: AlpacaCredentials,
        sec_identity: SecEdgarIdentity,
    ) -> AaplApplicationBootstrapResult:
        """Run the complete Apple bootstrap."""
        ...


class AaplDailyRunner:
    """Execute at most one Apple refresh per workspace and persist its safe status."""

    def __init__(
        self,
        application: _ApplicationOperations,
        workspace_service: WorkspaceService,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        run_id_factory: Callable[[], UUID] = uuid4,
    ) -> None:
        self._application = application
        self._workspace_service = workspace_service
        self._clock = clock
        self._run_id_factory = run_id_factory

    @classmethod
    def create_default(cls) -> "AaplDailyRunner":
        """Build one runner and facade around the same application runtime."""
        runtime = ApplicationRuntime.create_default()
        return cls(InvestmentAnalystApplication(runtime), runtime.workspace_service)

    def run(
        self,
        request: AaplWorkspaceBootstrapRequest,
        *,
        workspace: Path | None,
        alpaca_credentials: AlpacaCredentials,
        sec_identity: SecEdgarIdentity,
    ) -> AaplDailyRunState:
        """Run once, atomically recording running, succeeded, or failed state."""
        paths = self._workspace_service.resolve(workspace)
        run_id = self._run_id_factory()
        started_at = self._now()
        store = AaplDailyRunStateStore(paths.state_root / _STATE_FILE_NAME)
        lock = AaplDailyRunLock(
            paths.state_root / _LOCK_FILE_NAME,
            run_id=run_id,
            started_at=started_at.isoformat(),
        )
        with lock:
            store.write(
                AaplDailyRunState(
                    run_id=run_id,
                    status=AaplDailyRunStatus.RUNNING,
                    workspace_root=paths.root,
                    request=request,
                    started_at=started_at,
                )
            )
            try:
                result = self._application.bootstrap_aapl_workspace(
                    request,
                    workspace=paths.root,
                    alpaca_credentials=alpaca_credentials,
                    sec_identity=sec_identity,
                )
            except Exception as error:
                failure = self._safe_failure(
                    error,
                    secrets=(
                        alpaca_credentials.api_key,
                        alpaca_credentials.secret_key,
                        sec_identity.user_agent,
                    ),
                )
                failed = AaplDailyRunState(
                    run_id=run_id,
                    status=AaplDailyRunStatus.FAILED,
                    workspace_root=paths.root,
                    request=request,
                    started_at=started_at,
                    completed_at=self._now(),
                    failure=failure,
                )
                store.write(failed)
                raise AaplDailyRunExecutionError(failure, error) from error

            summary = result.summary
            succeeded = AaplDailyRunState(
                run_id=run_id,
                status=AaplDailyRunStatus.SUCCEEDED,
                workspace_root=result.initialization.paths.root,
                workspace_id=result.initialization.manifest.workspace_id,
                request=request,
                started_at=started_at,
                completed_at=self._now(),
                effective_known_at=summary.effective_known_at,
                refresh_mode=summary.refresh_plan.mode,
                overall_status=summary.overall_status,
                counts=AaplDailyRunCounts(
                    raw_records_created=summary.raw_records_created,
                    raw_records_reused=summary.raw_records_reused,
                    observations_created=summary.observations_created,
                    observations_reused=summary.observations_reused,
                    metric_results_created=summary.metric_results_created,
                    metric_results_reused=summary.metric_results_reused,
                    diagnostics_created=summary.diagnostics_created,
                    diagnostics_reused=summary.diagnostics_reused,
                ),
                traceability_verified=summary.traceability_verified,
            )
            store.write(succeeded)
            return succeeded

    def inspect(self, *, workspace: Path | None) -> AaplOperationalHealth:
        """Inspect the initialized workspace and latest run without writing state."""
        paths = self._workspace_service.resolve(workspace)
        inspection = self._workspace_service.inspect(paths.root)
        latest = AaplDailyRunStateStore(paths.state_root / _STATE_FILE_NAME).load()
        issues: list[str] = []
        if inspection.status != "ready":
            issues.extend((*inspection.errors, *inspection.warnings))

        status = AaplOperationalHealthStatus.READY
        if latest is None:
            issues.append("no operational run has been recorded")
        elif latest.status is AaplDailyRunStatus.FAILED:
            status = AaplOperationalHealthStatus.DEGRADED
            issues.append("the latest operational run failed")
        elif latest.status is AaplDailyRunStatus.RUNNING:
            if AaplDailyRunLock.is_held(paths.state_root / _LOCK_FILE_NAME):
                status = AaplOperationalHealthStatus.RUNNING
            else:
                status = AaplOperationalHealthStatus.DEGRADED
                issues.append("the latest run was interrupted before completion")
        if inspection.status != "ready":
            status = AaplOperationalHealthStatus.DEGRADED

        return AaplOperationalHealth(
            status=status,
            workspace=inspection,
            latest_run=latest,
            issues=tuple(issues),
        )

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("operational clock must return a timezone-aware datetime")
        return value.astimezone(UTC)

    @staticmethod
    def _safe_failure(error: Exception, *, secrets: tuple[str, ...]) -> AaplDailyRunFailure:
        expected = isinstance(
            error,
            (
                AaplWorkspaceBootstrapError,
                ApplicationRuntimeError,
                ConsolidatedDiagnosticQueryError,
                HttpRequestError,
                StorageError,
                ValueError,
                WorkspaceError,
            ),
        )
        if expected:
            message = str(error).strip() or "The operational run failed validation."
            for secret in secrets:
                if secret:
                    message = message.replace(secret, "<redacted>")
            message = message[:500]
            category = type(error).__name__
        else:
            category = "unexpected_error"
            message = "The operational run failed unexpectedly."
        return AaplDailyRunFailure(category=category, message=message)
