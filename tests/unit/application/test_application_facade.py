"""Tests for the stable application facade."""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from investment_analyst.analytics.consolidated_diagnostic_models import (
    ConsolidatedDiagnosticRequest,
)
from investment_analyst.application.facade import InvestmentAnalystApplication
from investment_analyst.application.runtime import ApplicationRuntime, StorageLocationRequest
from investment_analyst.core.models import DataFrequency
from investment_analyst.providers.http import HttpTransport
from investment_analyst.storage import LocalStorage, StoragePaths
from investment_analyst.workspace.service import (
    WorkspaceNotInitializedError,
    WorkspaceService,
)


def _request() -> ConsolidatedDiagnosticRequest:
    return ConsolidatedDiagnosticRequest(
        known_at=datetime(2026, 7, 14, 4, 41, 55, tzinfo=UTC),
        fundamental_frequency=DataFrequency.QUARTERLY,
    )


def _unexpected_transport() -> HttpTransport:
    raise AssertionError("read-only queries must not create a provider transport")


def _application(home: Path) -> InvestmentAnalystApplication:
    runtime = ApplicationRuntime.create_default(
        workspace_service=WorkspaceService(environ={}, home=home)
    )
    return InvestmentAnalystApplication(
        runtime,
        transport_factory=_unexpected_transport,
    )


def test_query_returns_versioned_report_without_writes_or_providers(tmp_path: Path) -> None:
    root = tmp_path / "legacy"
    storage_paths = StoragePaths.from_root(root)
    with LocalStorage(storage_paths):
        pass
    database_before = storage_paths.database_path.read_bytes()

    report = _application(tmp_path).query_aapl_diagnostics(
        _request(),
        location=StorageLocationRequest(legacy_root=root),
    )

    assert report.to_json_dict()["schema_version"] == "aapl-daily-diagnostic-report-v1"
    assert report.to_json_dict()["status"] == "unavailable"
    assert report.view.request == _request()
    assert storage_paths.database_path.read_bytes() == database_before


def test_query_missing_workspace_fails_without_creating_it(tmp_path: Path) -> None:
    missing = tmp_path / "missing"

    with pytest.raises(WorkspaceNotInitializedError):
        _application(tmp_path).query_aapl_diagnostics(
            _request(),
            location=StorageLocationRequest(workspace=missing),
        )

    assert not missing.exists()
