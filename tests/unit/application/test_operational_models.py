"""Tests for versioned Apple operational state contracts."""

from datetime import UTC, date, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from investment_analyst.analytics.consolidated_diagnostic_models import (
    ConsolidatedDiagnosticStatus,
)
from investment_analyst.application.aapl_bootstrap_models import (
    AaplMarketRefreshMode,
    AaplWorkspaceBootstrapRequest,
)
from investment_analyst.application.operational_models import (
    AaplDailyRunCounts,
    AaplDailyRunFailure,
    AaplDailyRunState,
    AaplDailyRunStatus,
)
from investment_analyst.core.models import DataFrequency


def _request() -> AaplWorkspaceBootstrapRequest:
    return AaplWorkspaceBootstrapRequest(
        market_start=date(2025, 1, 1),
        market_end=date(2026, 7, 15),
        fundamental_frequency=DataFrequency.QUARTERLY,
        require_complete=True,
    )


def _counts() -> AaplDailyRunCounts:
    return AaplDailyRunCounts(
        raw_records_created=0,
        raw_records_reused=2,
        observations_created=0,
        observations_reused=206,
        metric_results_created=0,
        metric_results_reused=1990,
        diagnostics_created=0,
        diagnostics_reused=2,
    )


def test_state_contract_serializes_running_success_and_failure(tmp_path: Path) -> None:
    started = datetime(2026, 7, 16, 15, tzinfo=UTC)
    running = AaplDailyRunState(
        run_id=uuid4(),
        status=AaplDailyRunStatus.RUNNING,
        workspace_root=tmp_path,
        request=_request(),
        started_at=started,
    )
    assert running.to_json_dict()["status"] == "running"
    assert running.to_json_dict()["failure"] is None

    succeeded = AaplDailyRunState(
        run_id=uuid4(),
        status=AaplDailyRunStatus.SUCCEEDED,
        workspace_root=tmp_path,
        workspace_id=uuid4(),
        request=_request(),
        started_at=started,
        completed_at=datetime(2026, 7, 16, 15, 5, tzinfo=UTC),
        effective_known_at=datetime(2026, 7, 16, 15, 1, tzinfo=UTC),
        refresh_mode=AaplMarketRefreshMode.ALREADY_CURRENT,
        overall_status=ConsolidatedDiagnosticStatus.COMPLETE,
        counts=_counts(),
        traceability_verified=True,
    )
    assert succeeded.to_json_dict()["schema_version"] == "aapl-daily-run-state-v1"
    assert succeeded.to_json_dict()["counts"]["metric_results_reused"] == 1990

    failed = AaplDailyRunState(
        run_id=uuid4(),
        status=AaplDailyRunStatus.FAILED,
        workspace_root=tmp_path,
        request=_request(),
        started_at=started,
        completed_at=datetime(2026, 7, 16, 15, 2, tzinfo=UTC),
        failure=AaplDailyRunFailure(category="provider_error", message="request failed"),
    )
    assert failed.to_json_dict()["failure"] == {
        "category": "provider_error",
        "message": "request failed",
    }


def test_state_contract_rejects_incoherent_lifecycle_and_boolean_counts(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="complete outcome"):
        AaplDailyRunState(
            run_id=uuid4(),
            status=AaplDailyRunStatus.SUCCEEDED,
            workspace_root=tmp_path,
            request=_request(),
            started_at=datetime(2026, 7, 16, 15, tzinfo=UTC),
        )

    values = _counts().model_dump()
    values["raw_records_created"] = True
    with pytest.raises(ValidationError, match="must be integers"):
        AaplDailyRunCounts(**values)
