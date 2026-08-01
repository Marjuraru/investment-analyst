"""Offline integration tests for the complete incremental BTC market refresh."""

from collections.abc import Callable, Mapping
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from investment_analyst.analytics.market.diagnostic_pipeline import MarketDiagnosticPipeline
from investment_analyst.analytics.market.diagnostic_rules import MarketDiagnosticEngine
from investment_analyst.analytics.market.diagnostic_selection import (
    MarketDiagnosticMetricSelector,
)
from investment_analyst.analytics.market.history_service import HistoricalMarketDataService
from investment_analyst.analytics.market.statistics_engine import MarketStatisticsEngine
from investment_analyst.analytics.market.statistics_pipeline import MarketStatisticsPipeline
from investment_analyst.application.btc_refresh import (
    BtcMarketExecutionClock,
    BtcMarketKnownAtTooEarlyError,
    BtcMarketRefreshPipeline,
)
from investment_analyst.application.btc_refresh_models import (
    BtcMarketRefreshMode,
    BtcMarketRefreshRequest,
)
from investment_analyst.application.btc_refresh_planner import BtcMarketRefreshPlanner
from investment_analyst.providers.crypto.coinbase_exchange import CoinbaseExchangeClient
from investment_analyst.providers.crypto.coinbase_normalizer import ASSET_ID, SOURCE_ID
from investment_analyst.providers.crypto.coinbase_pipeline import CoinbaseHistoricalPipeline
from investment_analyst.providers.http import HttpResponse
from investment_analyst.storage import LocalStorage, StoragePaths

FIXTURE_PATH = Path("tests/fixtures/coinbase/btc_usd_daily.json")
FETCHED_AT = datetime(2026, 7, 12, 12, tzinfo=UTC)
RUN_AT = datetime(2026, 7, 12, 12, 5, tzinfo=UTC)


class FixtureTransport:
    """Return one deterministic Coinbase fixture and record URLs."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def get(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        timeout_seconds: float,
    ) -> HttpResponse:
        self.calls.append(url)
        return HttpResponse(
            status_code=200,
            body=FIXTURE_PATH.read_bytes(),
            headers={},
            url=url,
        )


def _pipeline(
    storage: LocalStorage,
    transport: FixtureTransport,
    *,
    execution_clock: Callable[[], datetime] | None = None,
) -> BtcMarketRefreshPipeline:
    history = HistoricalMarketDataService(storage)
    fetch_clock = execution_clock or (lambda: FETCHED_AT)
    analytics_clock = execution_clock or (lambda: RUN_AT)
    return BtcMarketRefreshPipeline(
        refresh_planner=BtcMarketRefreshPlanner(storage),
        market_pipeline=CoinbaseHistoricalPipeline(
            storage,
            CoinbaseExchangeClient(
                transport,
                sleep=lambda _: None,
                clock=fetch_clock,
            ),
            clock=fetch_clock,
        ),
        statistics_pipeline=MarketStatisticsPipeline(
            storage,
            history,
            MarketStatisticsEngine(),
            clock=analytics_clock,
        ),
        diagnostic_pipeline=MarketDiagnosticPipeline(
            storage,
            MarketDiagnosticMetricSelector(storage),
            MarketDiagnosticEngine(),
            clock=analytics_clock,
        ),
        clock=analytics_clock,
    )


def _request(*, known_at: datetime | None = None) -> BtcMarketRefreshRequest:
    return BtcMarketRefreshRequest(
        market_start=date(2026, 7, 9),
        market_end=date(2026, 7, 11),
        requested_known_at=known_at,
    )


def test_refresh_ingests_calculates_and_reruns_without_provider_call(tmp_path: Path) -> None:
    transport = FixtureTransport()
    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        pipeline = _pipeline(storage, transport)
        first = pipeline.run(_request())
        second = pipeline.run(_request())

        assert first.asset_id == ASSET_ID
        assert first.source_id == SOURCE_ID
        assert first.refresh_plan.mode is BtcMarketRefreshMode.INITIAL
        assert first.intervals_executed == 1
        assert first.candles_received == 3
        assert first.raw_records_created == 3
        assert first.observations_created == 15
        assert first.metric_results_created > 0
        assert first.diagnostics_created == 1
        assert first.market_as_of == datetime(2026, 7, 11, tzinfo=UTC)
        assert first.analytics_start == datetime(2026, 7, 9, tzinfo=UTC)
        assert first.analytics_end == datetime(2026, 7, 12, tzinfo=UTC)
        assert first.analytics_lookback_days == 90
        assert first.traceability_verified is True

        assert second.refresh_plan.mode is BtcMarketRefreshMode.ALREADY_CURRENT
        assert second.effective_known_at == first.effective_known_at == FETCHED_AT
        assert second.intervals_executed == 0
        assert second.candles_received == 0
        assert second.metric_results_created == 0
        assert second.metric_results_reused == first.metric_results_created
        assert second.diagnostics_created == 0
        assert second.diagnostics_reused == 1
        assert len(transport.calls) == 1
        assert len(storage.raw_records.list(source_id=SOURCE_ID)) == 3
        assert len(storage.observations.list(asset_id=ASSET_ID)) == 15


def test_explicit_cut_before_new_fetch_preserves_ingested_progress(tmp_path: Path) -> None:
    transport = FixtureTransport()
    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        pipeline = _pipeline(storage, transport)

        with pytest.raises(BtcMarketKnownAtTooEarlyError, match="predates newly fetched"):
            pipeline.run(_request(known_at=datetime(2026, 7, 12, 11, 59, tzinfo=UTC)))

        assert len(storage.raw_records.list(source_id=SOURCE_ID)) == 3
        assert len(storage.observations.list(asset_id=ASSET_ID)) == 15
        assert storage.metric_results.list() == []
        assert storage.diagnostics.list() == []


def test_refresh_preserves_point_in_time_when_wall_clock_regresses(tmp_path: Path) -> None:
    high_watermark = RUN_AT + timedelta(microseconds=2)
    regressed = RUN_AT - timedelta(hours=1)
    wall_times = iter((RUN_AT, RUN_AT, high_watermark, regressed, regressed, regressed))
    execution_clock = BtcMarketExecutionClock(lambda: next(wall_times, regressed))
    transport = FixtureTransport()

    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        result = _pipeline(
            storage,
            transport,
            execution_clock=execution_clock,
        ).run(_request())

        assert result.effective_known_at == high_watermark
        assert result.traceability_verified is True
        assert result.metric_results_created > 0
        assert result.diagnostics_created == 1
        assert all(
            observation.available_at <= result.effective_known_at
            for observation in storage.observations.list(asset_id=ASSET_ID)
        )
        assert all(
            metric.available_at <= metric.computed_at
            for metric in storage.metric_results.list(asset_id=ASSET_ID)
        )
