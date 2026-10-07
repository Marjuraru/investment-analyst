"""Runtime adoption of workspace-owned incremental market metrics."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID

from investment_analyst.analytics.analysis_snapshot import build_analysis_snapshot
from investment_analyst.analytics.analytical_access_models import (
    FeatureSetSpec,
    MetricSeriesQuery,
)
from investment_analyst.analytics.market.bar_models import (
    HistoricalBarQuery,
    MarketBarCoverage,
    MarketBarSeries,
)
from investment_analyst.analytics.market.bar_schemas import (
    ALPACA_SOURCE_ID,
    COINBASE_SOURCE_ID,
)
from investment_analyst.analytics.market.history_service import HistoricalMarketDataService
from investment_analyst.analytics.market.history_v2 import HistoricalMarketDataV2Service
from investment_analyst.analytics.market.statistics_engine import MarketStatisticsEngine
from investment_analyst.analytics.market.statistics_models import MarketStatisticsRequest
from investment_analyst.analytics.market.statistics_pipeline import MarketStatisticsPipeline
from investment_analyst.application.runtime import ApplicationRuntime, StorageLocationRequest
from investment_analyst.core.models import AssetClass
from investment_analyst.providers.asset_config import AlpacaAssetConfiguration
from investment_analyst.providers.crypto.coinbase_exchange import CoinbaseCandle
from investment_analyst.providers.crypto.coinbase_normalizer import (
    ASSET_ID as COINBASE_ASSET_ID,
)
from investment_analyst.providers.crypto.coinbase_normalizer import (
    candle_to_observations,
    candle_to_raw_record,
)
from investment_analyst.providers.market.alpaca_normalizer import (
    ASSET_ID,
    alpaca_source_id,
    bar_to_observations,
    bar_to_raw_record,
)
from investment_analyst.providers.market.alpaca_stock import AlpacaStockBar
from investment_analyst.storage.analysis_snapshot_v2 import canonical_snapshot_artifact_digest
from investment_analyst.storage.analytical_v2_validation import (
    AnalyticalV2ValidationContext,
    market_artifact_digests_for_metrics,
)
from investment_analyst.storage.workspace_incremental_v2 import (
    verify_workspace_incremental_v2,
)
from investment_analyst.workspace.models import WorkspaceAccessMode
from investment_analyst.workspace.service import WorkspaceService
from investment_analyst.workspace.workspace_v2_backup import WorkspaceV2BackupService

_BASE = datetime(2024, 1, 2, 16, tzinfo=UTC)
_KNOWN_AT = datetime(2025, 1, 1, tzinfo=UTC)


def _assert_full_engine_values_match(storage, request: MarketStatisticsRequest) -> None:
    history = HistoricalMarketDataV2Service(storage.store.raw_staging)
    projection = history.query(request.query)
    materialized = history.materialize_many(projection.bars, request.query)
    bars = tuple(sorted(materialized.values(), key=lambda item: item.timestamp))
    series = MarketBarSeries(
        query=request.query,
        bars=bars,
        coverage=MarketBarCoverage(
            candidate_versions=projection.candidate_versions,
            selected_versions=len(bars),
            discarded_revisions=projection.discarded_revisions,
            bar_count=len(bars),
            earliest_timestamp=bars[0].timestamp,
            latest_timestamp=bars[-1].timestamp,
        ),
        traceability_verified=True,
    )
    oracle = MarketStatisticsEngine().compute(series, request)

    def result_key(item):
        return (
            item.metric_key,
            item.as_of,
            item.parameters.get("window"),
            item.parameters.get("fast_window"),
            item.parameters.get("slow_window"),
            item.parameters.get("signal_window"),
        )

    expected = {result_key(item): item.value for item in oracle.calculations}
    actual = {
        result_key(item): item.value
        for item in storage.metric_results.list(asset_id=request.query.asset_id)
        if item.parameters.get("source_id") == request.query.source_id
    }
    assert actual == expected


def _bar(index: int) -> AlpacaStockBar:
    timestamp = _BASE + timedelta(days=index)
    close = Decimal("150") + Decimal(index) / Decimal("8")
    open_value = close - Decimal("0.25")
    return AlpacaStockBar(
        symbol="AAPL",
        timestamp=timestamp,
        open=open_value,
        high=close + Decimal("1"),
        low=open_value - Decimal("1"),
        close=close,
        volume=Decimal("10000") + Decimal(index * 11),
        trade_count=Decimal("100") + Decimal(index),
        vwap=close,
        raw_values={
            "t": timestamp.isoformat().replace("+00:00", "Z"),
            "o": str(open_value),
            "h": str(close + Decimal("1")),
            "l": str(open_value - Decimal("1")),
            "c": str(close),
            "v": str(Decimal("10000") + Decimal(index * 11)),
            "n": str(Decimal("100") + Decimal(index)),
            "vw": str(close),
        },
    )


def _configuration() -> AlpacaAssetConfiguration:
    return AlpacaAssetConfiguration(
        asset_id=ASSET_ID,
        symbol="AAPL",
        feed="iex",
        adjustment="all",
        source_id=alpaca_source_id("AAPL"),
        name="Apple Inc.",
        asset_class=AssetClass.EQUITY,
        quote_currency="USD",
        exchange="NASDAQ",
    )


def test_daily_runtime_uses_workspace_metric_rows_for_incremental_checkpoints(
    tmp_path: Path,
) -> None:
    workspaces = WorkspaceService(environ={}, home=tmp_path / "home")
    workspace_root = tmp_path / "workspace"
    workspaces.initialize(workspace_root, format_version=2)
    runtime = ApplicationRuntime.create_default(workspace_service=workspaces)
    location = StorageLocationRequest(workspace=workspace_root)
    count = 40
    end = _BASE + timedelta(days=count)
    query = HistoricalBarQuery(
        asset_id=ASSET_ID,
        source_id=ALPACA_SOURCE_ID,
        start=_BASE,
        end=end,
        known_at=_KNOWN_AT,
    )
    request = MarketStatisticsRequest(query=query)

    with runtime.open_storage(location, access_mode=WorkspaceAccessMode.READ_WRITE) as storage:
        configuration = _configuration()
        for index in range(count):
            bar = _bar(index)
            available_at = bar.timestamp + timedelta(hours=2)
            raw = bar_to_raw_record(
                bar,
                retrieved_at=available_at,
                request_url="https://data.alpaca.markets/runtime-smoke",
                configuration=configuration,
            )
            storage.raw_records.save(raw)
            storage.observations.save_many(
                bar_to_observations(
                    bar,
                    raw,
                    normalized_at=available_at + timedelta(minutes=1),
                    configuration=configuration,
                )
            )

        first_pipeline = MarketStatisticsPipeline(
            storage,
            HistoricalMarketDataService(storage),
            MarketStatisticsEngine(),
            clock=lambda: datetime(2025, 1, 2, tzinfo=UTC),
        )
        first = first_pipeline.run(request)
        assert first.bar_count == count
        assert first.results_created > 0
        assert first.results_generated == first.results_created + first.results_reused
        assert sum(first.result_counts.values()) == first.results_generated
        _assert_full_engine_values_match(storage, request)

        connection = storage.store.connection
        table_names = {
            str(row[0])
            for row in connection.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema = 'main'"
            ).fetchall()
        }
        assert "workspace_metric_results_v2" in table_names
        assert "metric_results_v2" not in table_names
        assert storage.metric_results.count(asset_id=ASSET_ID) == first.results_generated

        checkpoint_ids = tuple(
            UUID(str(row[0]))
            for row in connection.execute(
                "SELECT checkpoint_id FROM market_recursive_checkpoints_v2 "
                "ORDER BY checkpoint_id LIMIT 256"
            ).fetchall()
        )
        checkpoints = storage.store.raw_staging.get_market_recursive_checkpoints(
            checkpoint_ids,
            metric_results=storage.metric_results,
        )
        assert checkpoints

        metric_ids = tuple(storage.metric_results.list_ids(asset_id=ASSET_ID))
        market_digests = market_artifact_digests_for_metrics(
            connection,
            metric_ids,
            validation_context=AnalyticalV2ValidationContext(),
        )
        snapshot = build_analysis_snapshot(
            asset_id=ASSET_ID,
            domain="market",
            known_at=_KNOWN_AT,
            policy_version="incremental-runtime-v2-integration-v1",
            metric_ids=metric_ids,
            evidence_set_digest=canonical_snapshot_artifact_digest((), market_digests),
            created_at=datetime(2025, 1, 2, 1, tzinfo=UTC),
        )
        storage.store.raw_staging.save_analysis_snapshots((snapshot,))
        assert storage.store.raw_staging.get_analysis_snapshot(snapshot.snapshot_id) == snapshot
        incremental_inventory = verify_workspace_incremental_v2(storage)
        assert incremental_inventory.daily_evidence_prefixes > 0
        assert incremental_inventory.market_recursive_checkpoints >= count * 5
        assert incremental_inventory.analysis_snapshots == 1

        access = runtime.analytical_access(storage)
        features = access.features(
            snapshot,
            FeatureSetSpec(
                feature_set_id="market-daily-v1",
                version="1",
                domain="market",
                metric_keys=("market.technical.ema",),
            ),
        )
        assert features.values[0].status == "available"
        assert features.values[0].values
        series = access.metric_series(
            MetricSeriesQuery(
                asset_id=ASSET_ID,
                domain="market",
                known_at=_KNOWN_AT,
                metric_keys=("market.technical.ema",),
                source_id=ALPACA_SOURCE_ID,
                limit=17,
            )
        )
        assert len(series.items) == 17
        assert series.truncated
        assert series.next_cursor is not None
        explanation = access.explain(snapshot, (series.items[0].result_id,))
        assert explanation.items[0].formula
        assert explanation.items[0].input_observation_ids

        second_pipeline = MarketStatisticsPipeline(
            storage,
            HistoricalMarketDataService(storage),
            MarketStatisticsEngine(),
            clock=lambda: datetime(2025, 1, 3, tzinfo=UTC),
        )
        second = second_pipeline.run(request)
        assert second.results_created == 0
        assert second.results_reused == first.results_generated
        assert storage.metric_results.count(asset_id=ASSET_ID) == first.results_generated

    backup_service = WorkspaceV2BackupService(workspaces)
    backup_path = tmp_path / "v2-backup"
    manifest = backup_service.create(workspace_root, backup_path)
    restored_roots = (
        tmp_path / "restored-a",
        tmp_path / "restored-b",
    )
    for restored_root in restored_roots:
        restored = backup_service.restore(backup_path, restored_root)
        assert restored.format_version == 2
        with runtime.open_storage(
            StorageLocationRequest(workspace=restored_root),
            access_mode=WorkspaceAccessMode.READ_ONLY,
        ) as storage:
            assert storage.store.raw_staging.get_analysis_snapshot(snapshot.snapshot_id) == snapshot
            assert set(storage.metric_results.list_ids(asset_id=ASSET_ID)) == set(metric_ids)
            assert verify_workspace_incremental_v2(storage) == incremental_inventory

    resumed_results = []
    resumed_inventory = []
    resumed_request = request.model_copy(
        update={"query": request.query.model_copy(update={"end": end + timedelta(days=1)})}
    )
    for index, restored_root in enumerate(restored_roots):
        with runtime.open_storage(
            StorageLocationRequest(workspace=restored_root),
            access_mode=WorkspaceAccessMode.READ_WRITE,
        ) as storage:
            configuration = _configuration()
            bar = _bar(count)
            available_at = bar.timestamp + timedelta(hours=2)
            raw = bar_to_raw_record(
                bar,
                retrieved_at=available_at,
                request_url="https://data.alpaca.markets/runtime-resume",
                configuration=configuration,
            )
            storage.raw_records.save(raw)
            storage.observations.save_many(
                bar_to_observations(
                    bar,
                    raw,
                    normalized_at=available_at + timedelta(minutes=1),
                    configuration=configuration,
                )
            )
            resumed = MarketStatisticsPipeline(
                storage,
                HistoricalMarketDataService(storage),
                MarketStatisticsEngine(),
                clock=lambda index=index: datetime(2025, 1, 4 + index, tzinfo=UTC),
            ).run(resumed_request)
            assert resumed.results_created > 0
            assert resumed.results_reused > 0
            count_after_resume = storage.metric_results.count(asset_id=ASSET_ID)
            rerun = MarketStatisticsPipeline(
                storage,
                HistoricalMarketDataService(storage),
                MarketStatisticsEngine(),
                clock=lambda: datetime(2025, 1, 6, tzinfo=UTC),
            ).run(resumed_request)
            assert rerun.results_created == 0
            assert rerun.results_reused == resumed.results_generated
            assert storage.metric_results.count(asset_id=ASSET_ID) == count_after_resume
            current_ids = tuple(storage.metric_results.list_ids(asset_id=ASSET_ID))
            current = storage.metric_results.get_many(current_ids)
            resumed_results.append(
                {
                    identifier: result.model_dump(exclude={"computed_at"})
                    for identifier, result in current.items()
                }
            )
            resumed_inventory.append(verify_workspace_incremental_v2(storage))

    assert resumed_results[0] == resumed_results[1]
    assert resumed_inventory[0] == resumed_inventory[1]
    assert manifest.source_workspace_id == restored.workspace_id


def test_coinbase_daily_runtime_uses_workspace_v2_and_matches_full_engine(tmp_path: Path) -> None:
    workspaces = WorkspaceService(environ={}, home=tmp_path / "home")
    workspace_root = tmp_path / "workspace"
    workspaces.initialize(workspace_root, format_version=2)
    runtime = ApplicationRuntime.create_default(workspace_service=workspaces)
    location = StorageLocationRequest(workspace=workspace_root)
    count = 40
    end = _BASE + timedelta(days=count)
    request = MarketStatisticsRequest(
        query=HistoricalBarQuery(
            asset_id=COINBASE_ASSET_ID,
            source_id=COINBASE_SOURCE_ID,
            start=_BASE,
            end=end,
            known_at=_KNOWN_AT,
        )
    )

    with runtime.open_storage(location, access_mode=WorkspaceAccessMode.READ_WRITE) as storage:
        for index in range(count):
            timestamp = _BASE + timedelta(days=index)
            close = Decimal("42000") + Decimal(index * 17)
            candle = CoinbaseCandle(
                product_id="BTC-USD",
                start=timestamp,
                low=close - Decimal("2"),
                high=close + Decimal("2"),
                open=close - Decimal("1"),
                close=close,
                volume=Decimal("10") + Decimal(index),
                raw_values=(
                    str(int(timestamp.timestamp())),
                    str(close - Decimal("2")),
                    str(close + Decimal("2")),
                    str(close - Decimal("1")),
                    str(close),
                    str(Decimal("10") + Decimal(index)),
                ),
            )
            available_at = timestamp + timedelta(hours=2)
            raw = candle_to_raw_record(
                candle,
                retrieved_at=available_at,
                request_url="https://api.exchange.coinbase.com/runtime-smoke",
            )
            storage.raw_records.save(raw)
            storage.observations.save_many(
                candle_to_observations(
                    candle,
                    raw,
                    normalized_at=available_at + timedelta(minutes=1),
                )
            )

        pipeline = MarketStatisticsPipeline(
            storage,
            HistoricalMarketDataService(storage),
            MarketStatisticsEngine(),
            clock=lambda: datetime(2025, 1, 2, tzinfo=UTC),
        )
        first = pipeline.run(request)
        assert first.results_created > 0
        assert first.results_reused == 0
        _assert_full_engine_values_match(storage, request)

        repeated = MarketStatisticsPipeline(
            storage,
            HistoricalMarketDataService(storage),
            MarketStatisticsEngine(),
            clock=lambda: datetime(2025, 1, 3, tzinfo=UTC),
        ).run(request)
        assert repeated.results_created == 0
        assert repeated.results_reused == first.results_generated
