"""Tests for the stable application facade."""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from investment_analyst.analytics.consolidated_diagnostic_models import (
    ConsolidatedDiagnosticRequest,
)
from investment_analyst.analytics.fundamental_trend_models import AaplFundamentalTrendRequest
from investment_analyst.analytics.fundamentals.research_models import (
    AaplFundamentalResearchRequest,
)
from investment_analyst.analytics.market.chart_models import (
    AaplMarketChartRequest,
    BtcMarketChartRequest,
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


def test_chart_query_is_empty_bounded_and_read_only(tmp_path: Path) -> None:
    root = tmp_path / "legacy-chart"
    storage_paths = StoragePaths.from_root(root)
    with LocalStorage(storage_paths):
        pass
    database_before = storage_paths.database_path.read_bytes()

    chart = _application(tmp_path).query_aapl_market_chart(
        AaplMarketChartRequest(known_at=datetime(2026, 7, 14, 4, 41, 55, tzinfo=UTC)),
        location=StorageLocationRequest(legacy_root=root),
    )

    assert chart.schema_version == "aapl-market-chart-v5"
    assert chart.points == ()
    assert chart.session_limit == 132
    assert chart.traceability_verified
    assert storage_paths.database_path.read_bytes() == database_before


def test_btc_chart_query_is_empty_bounded_and_read_only(tmp_path: Path) -> None:
    root = tmp_path / "legacy-btc-chart"
    storage_paths = StoragePaths.from_root(root)
    with LocalStorage(storage_paths):
        pass
    database_before = storage_paths.database_path.read_bytes()

    chart = _application(tmp_path).query_btc_market_chart(
        BtcMarketChartRequest(known_at=datetime(2026, 7, 14, 4, 41, 55, tzinfo=UTC)),
        location=StorageLocationRequest(legacy_root=root),
    )

    assert chart.schema_version == "btc-market-chart-v1"
    assert chart.asset_id == "crypto:btc-usd"
    assert chart.source_id == "coinbase-exchange:btc-usd:daily-candles"
    assert chart.volume_unit == "BTC"
    assert chart.points == ()
    assert chart.traceability_verified
    assert storage_paths.database_path.read_bytes() == database_before


def test_fundamental_trend_query_is_empty_bounded_and_read_only(tmp_path: Path) -> None:
    root = tmp_path / "legacy-fundamental-trend"
    storage_paths = StoragePaths.from_root(root)
    with LocalStorage(storage_paths):
        pass
    database_before = storage_paths.database_path.read_bytes()

    trend = _application(tmp_path).query_aapl_fundamental_trend(
        AaplFundamentalTrendRequest(
            known_at=datetime(2026, 7, 14, 4, 41, 55, tzinfo=UTC),
            frequency=DataFrequency.QUARTERLY,
            period_limit=8,
        ),
        location=StorageLocationRequest(legacy_root=root),
    )

    assert trend.schema_version == "aapl-fundamental-trend-v1"
    assert trend.periods == ()
    assert trend.coverage.periods_returned == 0
    assert trend.traceability_verified
    assert storage_paths.database_path.read_bytes() == database_before


def test_fundamental_research_query_is_empty_bounded_and_read_only(tmp_path: Path) -> None:
    root = tmp_path / "legacy-fundamental-research"
    storage_paths = StoragePaths.from_root(root)
    with LocalStorage(storage_paths):
        pass
    database_before = storage_paths.database_path.read_bytes()

    research = _application(tmp_path).query_aapl_fundamental_research(
        AaplFundamentalResearchRequest(
            known_at=datetime(2026, 7, 14, 4, 41, 55, tzinfo=UTC),
            frequency=DataFrequency.ANNUAL,
            limit=5,
        ),
        location=StorageLocationRequest(legacy_root=root),
    )

    assert research.schema_version == "aapl-fundamental-research-v2"
    assert research.periods == ()
    assert research.coverage.output_periods == 0
    assert research.traceability_verified
    assert storage_paths.database_path.read_bytes() == database_before


def test_fundamental_research_history_is_empty_bounded_and_read_only(tmp_path: Path) -> None:
    root = tmp_path / "legacy-fundamental-research-history"
    storage_paths = StoragePaths.from_root(root)
    with LocalStorage(storage_paths):
        pass
    database_before = storage_paths.database_path.read_bytes()
    request = AaplFundamentalResearchRequest(
        known_at=datetime(2026, 7, 14, 4, 41, 55, tzinfo=UTC),
        frequency=DataFrequency.ANNUAL,
        limit=5,
    )

    history = _application(tmp_path).query_aapl_fundamental_research_history(
        request,
        location=StorageLocationRequest(legacy_root=root),
    )

    assert history.schema_version == "aapl-fundamental-research-history-v2"
    assert history.research.schema_version == "aapl-fundamental-research-v2"
    assert history.series == ()
    assert history.coverage.series_returned == 0
    assert history.traceability_verified
    assert storage_paths.database_path.read_bytes() == database_before


def test_fundamental_analysis_is_empty_bounded_and_read_only(tmp_path: Path) -> None:
    root = tmp_path / "legacy-fundamental-analysis"
    storage_paths = StoragePaths.from_root(root)
    with LocalStorage(storage_paths):
        pass
    database_before = storage_paths.database_path.read_bytes()
    request = AaplFundamentalResearchRequest(
        known_at=datetime(2026, 7, 14, 4, 41, 55, tzinfo=UTC),
        frequency=DataFrequency.ANNUAL,
        limit=5,
    )

    analysis = _application(tmp_path).query_aapl_fundamental_analysis(
        request,
        location=StorageLocationRequest(legacy_root=root),
    )

    assert analysis.schema_version == "aapl-fundamental-analysis-v1"
    assert analysis.history.research.periods == ()
    assert tuple(item.definition.section_key for item in analysis.sections) == (
        "growth_and_per_share",
        "profitability",
        "returns_and_efficiency",
        "earnings_quality",
        "liquidity_and_balance",
        "debt_and_solvency",
        "cash_and_reinvestment",
        "capital_allocation",
    )
    assert all(item.coverage.latest_period_metrics == 0 for item in analysis.sections)
    assert analysis.coverage.expected_metrics == 40
    assert analysis.classification.status == "insufficient_evidence"
    assert analysis.traceability_verified
    assert storage_paths.database_path.read_bytes() == database_before


def test_query_missing_workspace_fails_without_creating_it(tmp_path: Path) -> None:
    missing = tmp_path / "missing"

    with pytest.raises(WorkspaceNotInitializedError):
        _application(tmp_path).query_aapl_diagnostics(
            _request(),
            location=StorageLocationRequest(workspace=missing),
        )

    assert not missing.exists()
