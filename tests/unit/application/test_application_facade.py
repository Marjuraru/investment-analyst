"""Tests for the stable application facade."""

from datetime import UTC, date, datetime
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
from investment_analyst.analytics.market.intraday_models import IntradayInterval
from investment_analyst.analytics.valuation import (
    CorporateValuationRequest,
    ValuationReasonCode,
    ValuationSnapshotStatus,
)
from investment_analyst.application.btc_intraday_models import BtcIntradayChartRequest
from investment_analyst.application.facade import InvestmentAnalystApplication
from investment_analyst.application.runtime import ApplicationRuntime, StorageLocationRequest
from investment_analyst.catalog.models import (
    AssetCatalogDocument,
    CatalogAsset,
    ProviderBinding,
)
from investment_analyst.catalog.provider_context import (
    ProviderAssetContextResolver,
    ProviderAssetNotConfiguredError,
)
from investment_analyst.catalog.service import AssetCatalogService
from investment_analyst.core.models import AssetClass, DataFrequency
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


def _sec_issuer_application(home: Path) -> InvestmentAnalystApplication:
    capabilities = (
        "fundamentals.company_facts",
        "fundamentals.submissions",
    )
    catalog = AssetCatalogService(
        AssetCatalogDocument(
            catalog_version=1,
            assets=(
                CatalogAsset(
                    asset_id="equity:us:amd",
                    symbol="AMD",
                    name="Advanced Micro Devices, Inc.",
                    asset_class=AssetClass.EQUITY,
                    quote_currency="USD",
                    exchange="NASDAQ",
                    provider_symbols={},
                    aliases=("AMD",),
                    provider_bindings=(
                        ProviderBinding(
                            provider="sec",
                            namespace="cik",
                            identifier="0000002488",
                            capabilities=capabilities,
                        ),
                        ProviderBinding(
                            provider="sec",
                            namespace="taxonomy",
                            identifier="us-gaap",
                            capabilities=capabilities,
                        ),
                        ProviderBinding(
                            provider="sec",
                            namespace="ticker",
                            identifier="AMD",
                            capabilities=capabilities,
                        ),
                    ),
                ),
            ),
        )
    )
    return InvestmentAnalystApplication(
        ApplicationRuntime(
            WorkspaceService(environ={}, home=home),
            catalog,
            ProviderAssetContextResolver(catalog),
        ),
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


def test_valuation_query_is_read_only_provider_free_and_capability_driven(
    tmp_path: Path,
) -> None:
    root = tmp_path / "legacy-valuation"
    storage_paths = StoragePaths.from_root(root)
    with LocalStorage(storage_paths):
        pass
    database_before = storage_paths.database_path.read_bytes()
    application = _application(tmp_path)
    location = StorageLocationRequest(legacy_root=root)

    def query(asset_id: str):
        return application.query_corporate_valuation(
            CorporateValuationRequest(
                asset_id=asset_id,
                known_at=datetime(2026, 7, 14, 4, 41, 55, tzinfo=UTC),
                valuation_date=date(2026, 7, 13),
            ),
            location=location,
        )

    apple = query("equity:us:aapl")
    foreign_adr = query("equity:us:bvn")
    etf = query("etf:us:ibit")
    bitcoin = query("crypto:btc-usd")

    assert apple.schema_version == "corporate-valuation-snapshot-v1"
    assert apple.status is ValuationSnapshotStatus.NOT_EVALUABLE
    assert {item.reason_code for item in apple.metrics} == {ValuationReasonCode.PRICE_UNAVAILABLE}
    assert {item.reason_code for item in foreign_adr.metrics} == {
        ValuationReasonCode.SHARE_BASIS_UNAVAILABLE
    }
    assert etf.status is bitcoin.status is ValuationSnapshotStatus.NOT_APPLICABLE
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


def test_btc_intraday_chart_query_is_empty_bounded_and_read_only(tmp_path: Path) -> None:
    root = tmp_path / "legacy-btc-intraday-chart"
    storage_paths = StoragePaths.from_root(root)
    with LocalStorage(storage_paths):
        pass
    database_before = storage_paths.database_path.read_bytes()

    chart = _application(tmp_path).query_btc_intraday_chart(
        BtcIntradayChartRequest(
            known_at=datetime(2026, 7, 14, 4, 41, 55, tzinfo=UTC),
            interval=IntradayInterval.MINUTE_5,
        ),
        location=StorageLocationRequest(legacy_root=root),
    )

    assert chart.schema_version == "btc-intraday-chart-v1"
    assert chart.asset_id == "crypto:btc-usd"
    assert chart.source_id == "coinbase-exchange:btc-usd:minute-1-candles"
    assert chart.interval is IntradayInterval.MINUTE_5
    assert chart.start.isoformat() == "2026-07-13T04:41:00+00:00"
    assert chart.end.isoformat() == "2026-07-14T04:41:00+00:00"
    assert chart.bars == ()
    assert chart.source_bar_count == 0
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


def test_sec_issuer_facade_is_catalog_backed_read_only_and_versioned(
    tmp_path: Path,
) -> None:
    root = tmp_path / "legacy-sec-issuer"
    storage_paths = StoragePaths.from_root(root)
    with LocalStorage(storage_paths):
        pass
    database_before = storage_paths.database_path.read_bytes()
    request = AaplFundamentalResearchRequest(
        known_at=datetime(2026, 7, 14, 4, 41, 55, tzinfo=UTC),
        frequency=DataFrequency.ANNUAL,
        limit=5,
    )
    application = _sec_issuer_application(tmp_path)
    location = StorageLocationRequest(legacy_root=root)

    trend = application.query_sec_fundamental_trend(
        AaplFundamentalTrendRequest(
            known_at=request.known_at,
            frequency=request.frequency,
            period_limit=5,
        ),
        asset_id="equity:us:amd",
        location=location,
    )
    research = application.query_sec_fundamental_research(
        request,
        asset_id="equity:us:amd",
        location=location,
    )
    history = application.query_sec_fundamental_research_history(
        request,
        asset_id="equity:us:amd",
        location=location,
    )
    analysis = application.query_sec_fundamental_analysis(
        request,
        asset_id="equity:us:amd",
        location=location,
    )

    assert trend.schema_version == "sec-fundamental-trend-v2"
    assert research.schema_version == "sec-fundamental-research-v3"
    assert history.schema_version == "sec-fundamental-research-history-v3"
    assert analysis.schema_version == "sec-fundamental-analysis-v2"
    assert (
        trend.asset_id
        == research.asset_id
        == history.asset_id
        == analysis.asset_id
        == ("equity:us:amd")
    )
    assert (
        trend.source_id
        == research.source_id
        == history.source_id
        == analysis.source_id
        == "sec-edgar:amd:companyfacts"
    )
    assert trend.periods == ()
    assert research.periods == ()
    assert history.series == ()
    assert analysis.coverage.latest_period_metrics == 0
    assert storage_paths.database_path.read_bytes() == database_before


def test_sec_issuer_facade_rejects_missing_catalog_binding_before_storage(
    tmp_path: Path,
) -> None:
    missing = tmp_path / "missing-sec-storage"
    request = AaplFundamentalResearchRequest(
        known_at=datetime(2026, 7, 14, 4, 41, 55, tzinfo=UTC),
        frequency=DataFrequency.ANNUAL,
    )

    with pytest.raises(ProviderAssetNotConfiguredError):
        _application(tmp_path).query_sec_fundamental_analysis(
            request,
            asset_id="etf:us:gbtc",
            location=StorageLocationRequest(legacy_root=missing),
        )

    assert not missing.exists()


def test_query_missing_workspace_fails_without_creating_it(tmp_path: Path) -> None:
    missing = tmp_path / "missing"

    with pytest.raises(WorkspaceNotInitializedError):
        _application(tmp_path).query_aapl_diagnostics(
            _request(),
            location=StorageLocationRequest(workspace=missing),
        )

    assert not missing.exists()
