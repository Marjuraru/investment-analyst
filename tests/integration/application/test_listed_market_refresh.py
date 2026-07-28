"""Offline integration tests for a generic catalog-backed Alpaca refresh."""

import json
from collections.abc import Mapping
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from investment_analyst.analytics.market.diagnostic_pipeline import (
    MarketDiagnosticPipeline,
)
from investment_analyst.analytics.market.diagnostic_rules import MarketDiagnosticEngine
from investment_analyst.analytics.market.diagnostic_selection import (
    MarketDiagnosticMetricSelector,
)
from investment_analyst.analytics.market.history_service import HistoricalMarketDataService
from investment_analyst.analytics.market.statistics_engine import MarketStatisticsEngine
from investment_analyst.analytics.market.statistics_pipeline import MarketStatisticsPipeline
from investment_analyst.application.aapl_bootstrap_models import AaplMarketRefreshMode
from investment_analyst.application.aapl_refresh_planner import AaplMarketRefreshPlanner
from investment_analyst.application.listed_market_refresh import (
    ListedMarketKnownAtTooEarlyError,
    ListedMarketRefreshPipeline,
)
from investment_analyst.application.listed_market_refresh_models import (
    ListedMarketRefreshRequest,
)
from investment_analyst.core.models import AssetClass
from investment_analyst.providers.asset_config import AlpacaAssetConfiguration
from investment_analyst.providers.http import HttpResponse
from investment_analyst.providers.market.alpaca_pipeline import AlpacaHistoricalPipeline
from investment_analyst.providers.market.alpaca_stock import (
    AlpacaCredentials,
    AlpacaStockClient,
)
from investment_analyst.storage import LocalStorage, StoragePaths

_FIXTURE_PATH = Path("tests/fixtures/alpaca/aapl_daily.json")
_FETCHED_AT = datetime(2026, 7, 12, 12, tzinfo=UTC)
_RUN_AT = datetime(2026, 7, 12, 12, 5, tzinfo=UTC)
_CONFIGURATION = AlpacaAssetConfiguration(
    asset_id="equity:us:bvn",
    symbol="BVN",
    feed="iex",
    adjustment="all",
    source_id="alpaca-market-data:iex:bvn:daily-bars:adjustment-all",
    name="Compañía de Minas Buenaventura S.A.A.",
    asset_class=AssetClass.EQUITY,
    quote_currency="USD",
    exchange="NYSE",
)


class _FixtureTransport:
    def __init__(self) -> None:
        fixture = json.loads(_FIXTURE_PATH.read_bytes())
        fixture["symbol"] = _CONFIGURATION.symbol
        self._body = json.dumps(fixture).encode()
        self.calls: list[str] = []

    def get(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        timeout_seconds: float,
    ) -> HttpResponse:
        self.calls.append(url)
        return HttpResponse(status_code=200, body=self._body, headers={}, url=url)


def _pipeline(
    storage: LocalStorage,
    transport: _FixtureTransport,
) -> ListedMarketRefreshPipeline:
    history = HistoricalMarketDataService(storage)
    return ListedMarketRefreshPipeline(
        configuration=_CONFIGURATION,
        refresh_planner=AaplMarketRefreshPlanner(
            storage,
            configuration=_CONFIGURATION,
        ),
        market_pipeline=AlpacaHistoricalPipeline(
            storage,
            AlpacaStockClient(
                transport,
                AlpacaCredentials(api_key="test-key", secret_key="test-secret"),
                clock=lambda: _FETCHED_AT,
            ),
            configuration=_CONFIGURATION,
            clock=lambda: _FETCHED_AT,
        ),
        statistics_pipeline=MarketStatisticsPipeline(
            storage,
            history,
            MarketStatisticsEngine(),
            clock=lambda: _RUN_AT,
        ),
        diagnostic_pipeline=MarketDiagnosticPipeline(
            storage,
            MarketDiagnosticMetricSelector(storage),
            MarketDiagnosticEngine(),
            clock=lambda: _RUN_AT,
        ),
        clock=lambda: _RUN_AT,
    )


def _request(*, known_at: datetime | None = None) -> ListedMarketRefreshRequest:
    return ListedMarketRefreshRequest(
        asset_id=_CONFIGURATION.asset_id,
        market_start=date(2026, 7, 7),
        market_end=date(2026, 7, 9),
        requested_known_at=known_at,
    )


def test_listed_refresh_isolated_identity_and_idempotent_rerun(tmp_path: Path) -> None:
    transport = _FixtureTransport()
    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        pipeline = _pipeline(storage, transport)

        first = pipeline.run(_request())
        second = pipeline.run(_request())

        assert first.asset_id == _CONFIGURATION.asset_id
        assert first.source_id == _CONFIGURATION.source_id
        assert first.refresh_plan.mode is AaplMarketRefreshMode.INITIAL
        assert first.intervals_executed == 1
        assert first.bars_received == 3
        assert first.raw_records_created == 3
        assert first.observations_created == 21
        assert first.metric_results_created > 0
        assert first.diagnostics_created == 1
        assert first.market_as_of == datetime(2026, 7, 9, 4, tzinfo=UTC)
        assert first.traceability_verified is True

        assert second.refresh_plan.mode is AaplMarketRefreshMode.ALREADY_CURRENT
        assert second.effective_known_at == first.effective_known_at == _FETCHED_AT
        assert second.intervals_executed == 0
        assert second.bars_received == 0
        assert second.metric_results_created == 0
        assert second.metric_results_reused == first.metric_results_created
        assert second.diagnostics_created == 0
        assert second.diagnostics_reused == 1
        assert len(transport.calls) == 1
        assert all(
            item.asset_id == _CONFIGURATION.asset_id
            for item in storage.observations.list(asset_id=_CONFIGURATION.asset_id)
        )


def test_listed_refresh_preserves_ingestion_when_explicit_cut_is_too_early(
    tmp_path: Path,
) -> None:
    transport = _FixtureTransport()
    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        pipeline = _pipeline(storage, transport)

        with pytest.raises(ListedMarketKnownAtTooEarlyError, match="predates newly fetched"):
            pipeline.run(_request(known_at=datetime(2026, 7, 12, 11, 59, tzinfo=UTC)))

        assert len(storage.raw_records.list(source_id=_CONFIGURATION.source_id)) == 4
        assert len(storage.observations.list(asset_id=_CONFIGURATION.asset_id)) == 21
        assert storage.metric_results.list() == []
        assert storage.diagnostics.list() == []
