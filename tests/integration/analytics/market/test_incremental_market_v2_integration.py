"""Raw v2 daily indicator checkpoints reconcile full, repeat and revision runs."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import duckdb

from investment_analyst.analytics.market.bar_models import (
    HistoricalBarQuery,
    MarketBarCoverage,
    MarketBarSeries,
)
from investment_analyst.analytics.market.bar_schemas import ALPACA_SOURCE_ID
from investment_analyst.analytics.market.history_v2 import HistoricalMarketDataV2Service
from investment_analyst.analytics.market.incremental_service import (
    IncrementalMarketRequest,
    IncrementalMarketService,
)
from investment_analyst.analytics.market.statistics_definitions import (
    ATR_KEY,
    BOLLINGER_BANDWIDTH_KEY,
    BOLLINGER_LOWER_KEY,
    BOLLINGER_PERCENT_B_KEY,
    BOLLINGER_UPPER_KEY,
    EMA_KEY,
    MACD_HISTOGRAM_KEY,
    MACD_LINE_KEY,
    MACD_SIGNAL_KEY,
    RELATIVE_VOLUME_KEY,
    RSI_AVERAGE_GAIN_KEY,
    RSI_AVERAGE_LOSS_KEY,
    RSI_KEY,
    SIMPLE_RETURN_KEY,
    SMA_KEY,
    TRUE_RANGE_KEY,
    VOLATILITY_KEY,
)
from investment_analyst.analytics.market.statistics_engine import MarketStatisticsEngine
from investment_analyst.analytics.market.statistics_models import MarketStatisticsRequest
from investment_analyst.catalog.provider_configuration import coinbase_source_id
from investment_analyst.core.models import AssetClass
from investment_analyst.providers.asset_config import (
    AlpacaAssetConfiguration,
    CoinbaseAssetConfiguration,
)
from investment_analyst.providers.crypto.coinbase_exchange import (
    DAILY_GRANULARITY_SECONDS,
    CoinbaseCandle,
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
from investment_analyst.storage.raw_v2 import RawV2Staging
from investment_analyst.workspace.raw_v2_backup import (
    RAW_V2_BACKUP_MANIFEST_SCHEMA_V5,
    RawV2StagingBackupService,
)

_BASE = datetime(2026, 1, 5, 16, tzinfo=UTC)
_KNOWN_AT = datetime(2026, 2, 1, tzinfo=UTC)
_COMPUTED_AT = datetime(2026, 2, 2, tzinfo=UTC)


def _bar(
    index: int,
    *,
    close_delta: Decimal = Decimal("0"),
    symbol: str = "AAPL",
) -> AlpacaStockBar:
    timestamp = _BASE + timedelta(days=index)
    close = Decimal("200") + Decimal(index * 2) + close_delta
    raw_values = {
        "t": timestamp.isoformat().replace("+00:00", "Z"),
        "o": str(close - 1),
        "h": str(close + 2),
        "l": str(close - 2),
        "c": str(close),
        "v": str(Decimal("1000") + Decimal(index * 100)),
        "n": str(Decimal("100") + Decimal(index)),
        "vw": str(close),
    }
    return AlpacaStockBar(
        symbol=symbol,
        timestamp=timestamp,
        open=close - 1,
        high=close + 2,
        low=close - 2,
        close=close,
        volume=Decimal("1000") + Decimal(index * 100),
        trade_count=Decimal("100") + Decimal(index),
        vwap=close,
        raw_values=raw_values,
    )


def _stage_bar(staging: RawV2Staging, bar: AlpacaStockBar, *, retrieved_at: datetime) -> None:
    raw = bar_to_raw_record(
        bar,
        retrieved_at=retrieved_at,
        request_url="https://data.alpaca.markets/test",
    )
    staging.save(raw)
    staging.save_observations(
        bar_to_observations(bar, raw, normalized_at=retrieved_at + timedelta(minutes=1))
    )


def _request(
    *,
    start: datetime,
    end: datetime,
    history_end: datetime | None = None,
    known_at: datetime = _KNOWN_AT,
    computed_at: datetime = _COMPUTED_AT,
) -> IncrementalMarketRequest:
    return IncrementalMarketRequest(
        statistics=MarketStatisticsRequest(
            query=HistoricalBarQuery(
                asset_id=ASSET_ID,
                source_id=ALPACA_SOURCE_ID,
                start=start,
                end=end,
                known_at=known_at,
            ),
            sma_windows=(3,),
            volatility_window=3,
            relative_volume_window=3,
            bollinger_window=3,
            ema_windows=(3,),
            rsi_window=3,
            atr_window=3,
            macd_fast_window=2,
            macd_slow_window=3,
            macd_signal_window=2,
        ),
        history_start=_BASE,
        history_end=history_end or end,
        computed_at=computed_at,
    )


def _stage_range(
    staging: RawV2Staging,
    start_index: int,
    end_index: int,
) -> None:
    for index in range(start_index, end_index):
        _stage_bar(
            staging,
            _bar(index),
            retrieved_at=_BASE + timedelta(days=index, hours=2),
        )


def _open_staging(destination: Path) -> RawV2Staging:
    destination.mkdir()
    connection = duckdb.connect(str(destination / "raw-v2-index.duckdb"))
    return RawV2Staging(destination, connection)


def test_incremental_daily_metrics_match_full_engine_and_reuse_then_revise(
    tmp_path: Path,
) -> None:
    destination = (tmp_path / "raw-v2").absolute()
    destination.mkdir()
    connection = duckdb.connect(str(destination / "raw-v2-index.duckdb"))
    staging = RawV2Staging(destination, connection)
    with staging:
        count = 8
        for index in range(count):
            _stage_bar(
                staging,
                _bar(index),
                retrieved_at=_BASE + timedelta(days=index, hours=2),
            )
        request = _request(start=_BASE, end=_BASE + timedelta(days=count))
        service = IncrementalMarketService(staging)
        first = service.run(request)
        assert first.selected_bars == count
        assert first.close_prefixes_created == count
        assert first.hlc_prefixes_created == count
        assert first.checkpoints_created == count * 5
        assert first.bars_recalculated == count
        assert first.recurrence_steps == count * 5
        assert first.bar_models_hydrated == count
        assert first.finite_bar_models_hydrated == count
        assert first.finite_window_calculations > 0
        assert first.metrics_created > 0

        history_service = HistoricalMarketDataV2Service(staging)
        history_query = HistoricalBarQuery(
            asset_id=ASSET_ID,
            source_id=ALPACA_SOURCE_ID,
            start=_BASE,
            end=_BASE + timedelta(days=count),
            known_at=_KNOWN_AT,
        )
        projection = history_service.query(history_query)
        materialized = history_service.materialize_many(projection.bars, history_query)
        bars = tuple(sorted(materialized.values(), key=lambda item: item.timestamp))
        series = MarketBarSeries(
            query=history_query,
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
        expected = MarketStatisticsEngine().compute(
            series,
            request.statistics.model_copy(update={"query": history_query}),
        )
        compared_keys = {
            SIMPLE_RETURN_KEY,
            SMA_KEY,
            VOLATILITY_KEY,
            RELATIVE_VOLUME_KEY,
            BOLLINGER_UPPER_KEY,
            BOLLINGER_LOWER_KEY,
            BOLLINGER_BANDWIDTH_KEY,
            BOLLINGER_PERCENT_B_KEY,
            EMA_KEY,
            RSI_AVERAGE_GAIN_KEY,
            RSI_AVERAGE_LOSS_KEY,
            RSI_KEY,
            TRUE_RANGE_KEY,
            ATR_KEY,
            MACD_LINE_KEY,
            MACD_SIGNAL_KEY,
            MACD_HISTOGRAM_KEY,
        }

        def result_key(item):
            return (
                item.metric_key,
                item.as_of,
                item.parameters.get("window"),
                item.parameters.get("fast_window"),
                item.parameters.get("slow_window"),
                item.parameters.get("signal_window"),
            )

        expected_by_key_time = {
            result_key(item): item.value
            for item in expected.calculations
            if item.metric_key in compared_keys
        }
        actual_metrics = staging.list_metrics(asset_id=ASSET_ID)
        actual_by_key_time = {
            result_key(item): item.value
            for item in actual_metrics
            if item.metric_key in compared_keys
        }
        assert actual_by_key_time == expected_by_key_time

        actual_by_id = {item.result_id: item for item in actual_metrics}
        actual_by_bar = {
            (item.metric_key, item.as_of, item.parameters.get("window")): item
            for item in actual_metrics
        }
        for timestamp in {item.as_of for item in actual_metrics}:
            rsi = actual_by_bar.get((RSI_KEY, timestamp, 3))
            if rsi is not None:
                assert set(rsi.input_metric_result_ids) == {
                    actual_by_bar[(RSI_AVERAGE_GAIN_KEY, timestamp, 3)].result_id,
                    actual_by_bar[(RSI_AVERAGE_LOSS_KEY, timestamp, 3)].result_id,
                }
            atr = actual_by_bar.get((ATR_KEY, timestamp, 3))
            true_range = actual_by_bar.get((TRUE_RANGE_KEY, timestamp, 3))
            if atr is not None and true_range is not None:
                assert atr.input_metric_result_ids == [true_range.result_id]
            line = actual_by_bar.get((MACD_LINE_KEY, timestamp, None))
            if line is not None:
                assert set(line.input_metric_result_ids) == {
                    actual_by_bar[(EMA_KEY, timestamp, 2)].result_id,
                    actual_by_bar[(EMA_KEY, timestamp, 3)].result_id,
                }
                signal = actual_by_bar.get((MACD_SIGNAL_KEY, timestamp, None))
                histogram = actual_by_bar.get((MACD_HISTOGRAM_KEY, timestamp, None))
                if signal is not None and histogram is not None:
                    assert signal.input_metric_result_ids == [line.result_id]
                    assert set(histogram.input_metric_result_ids) == {
                        line.result_id,
                        signal.result_id,
                    }
                    assert all(
                        result_id in actual_by_id for result_id in line.input_metric_result_ids
                    )

        second = service.run(request)
        assert second.close_prefixes_created == 0
        assert second.hlc_prefixes_created == 0
        assert second.checkpoints_created == 0
        assert second.checkpoints_reused == count * 5
        assert second.bars_recalculated == 0
        assert second.bar_models_hydrated == 0
        assert second.finite_bar_models_hydrated == 0
        assert second.finite_window_calculations == 0
        assert second.metrics_created == 0
        assert second.metrics_reused > 0

        corrected_index = 4
        corrected_at = _BASE + timedelta(days=count + 1)
        _stage_bar(
            staging,
            _bar(corrected_index, close_delta=Decimal("3")),
            retrieved_at=corrected_at,
        )
        correction = service.run(
            _request(
                start=_BASE + timedelta(days=corrected_index),
                end=_BASE + timedelta(days=corrected_index + 1),
                history_end=_BASE + timedelta(days=count),
                known_at=corrected_at + timedelta(hours=1),
                computed_at=corrected_at + timedelta(hours=2),
            )
        )
        assert correction.close_divergence_index == corrected_index
        assert correction.hlc_divergence_index == corrected_index
        assert correction.bars_recalculated == count - corrected_index
        assert correction.recurrence_steps == (count - corrected_index) * 5
        assert correction.selected_bars == count
        assert correction.metrics_created > 0
        assert staging.list_metrics(asset_id=ASSET_ID, available_to=_KNOWN_AT)

        backup_service = RawV2StagingBackupService()
        manifest = backup_service.create(
            staging,
            staging._connection,
            tmp_path / "incremental-backup",
        )
        assert manifest.schema_version == RAW_V2_BACKUP_MANIFEST_SCHEMA_V5
        assert manifest.incremental_counts is not None
        assert manifest.incremental_counts.daily_prefixes >= count * 2
        assert manifest.incremental_counts.recursive_checkpoints >= count * 5
        restored_path = (tmp_path / "incremental-restored").absolute()
        restored_manifest = backup_service.restore(tmp_path / "incremental-backup", restored_path)
        assert restored_manifest.backup_id == manifest.backup_id
        restored_connection = duckdb.connect(str(restored_path / "raw-v2-index.duckdb"))
        restored = RawV2Staging(restored_path, restored_connection, read_only=True)
        with restored:
            prefix_ids = [
                UUID(str(row[0]))
                for row in restored_connection.execute(
                    "SELECT prefix_id FROM market_daily_prefixes_v2 ORDER BY prefix_id"
                ).fetchall()
            ]
            checkpoint_ids = [
                UUID(str(row[0]))
                for row in restored_connection.execute(
                    "SELECT checkpoint_id FROM market_recursive_checkpoints_v2 "
                    "ORDER BY checkpoint_id"
                ).fetchall()
            ]
            assert len(restored.get_daily_evidence_prefixes(prefix_ids)) == len(prefix_ids)
            assert len(restored.get_market_recursive_checkpoints(checkpoint_ids)) == len(
                checkpoint_ids
            )
        restored.close()
    staging.close()


def test_full_and_resumed_daily_metrics_have_identical_ids_and_values(tmp_path: Path) -> None:
    count = 10
    split = 5
    full_path = (tmp_path / "full").absolute()
    resumed_path = (tmp_path / "resumed").absolute()
    full = _open_staging(full_path)
    with full:
        _stage_range(full, 0, count)
        IncrementalMarketService(full).run(_request(start=_BASE, end=_BASE + timedelta(days=count)))
        full_metrics = {item.result_id: item for item in full.list_metrics(asset_id=ASSET_ID)}
        full_prefix_ids = {
            UUID(str(row[0]))
            for row in full._connection.execute(
                "SELECT prefix_id FROM market_daily_prefixes_v2"
            ).fetchall()
        }
        full_checkpoint_ids = {
            UUID(str(row[0]))
            for row in full._connection.execute(
                "SELECT checkpoint_id FROM market_recursive_checkpoints_v2"
            ).fetchall()
        }
    full.close()

    resumed = _open_staging(resumed_path)
    with resumed:
        _stage_range(resumed, 0, split)
        service = IncrementalMarketService(resumed)
        service.run(_request(start=_BASE, end=_BASE + timedelta(days=split)))
        _stage_range(resumed, split, count)
        resumed_receipt = service.run(
            _request(
                start=_BASE + timedelta(days=split),
                end=_BASE + timedelta(days=count),
                history_end=_BASE + timedelta(days=count),
                computed_at=_COMPUTED_AT + timedelta(days=1),
            )
        )
        resumed_metrics = {item.result_id: item for item in resumed.list_metrics(asset_id=ASSET_ID)}
        resumed_prefix_ids = {
            UUID(str(row[0]))
            for row in resumed._connection.execute(
                "SELECT prefix_id FROM market_daily_prefixes_v2"
            ).fetchall()
        }
        resumed_checkpoint_ids = {
            UUID(str(row[0]))
            for row in resumed._connection.execute(
                "SELECT checkpoint_id FROM market_recursive_checkpoints_v2"
            ).fetchall()
        }
        assert resumed_receipt.bars_recalculated == count - split
        assert resumed_receipt.recurrence_steps == (count - split) * 5
    resumed.close()

    assert resumed_metrics.keys() == full_metrics.keys()
    assert resumed_prefix_ids == full_prefix_ids
    assert resumed_checkpoint_ids == full_checkpoint_ids
    for result_id, full_metric in full_metrics.items():
        resumed_metric = resumed_metrics[result_id]
        assert resumed_metric.value == full_metric.value
        assert resumed_metric.parameters == full_metric.parameters
        assert resumed_metric.input_observation_ids == full_metric.input_observation_ids
        assert resumed_metric.input_metric_result_ids == full_metric.input_metric_result_ids


def test_incremental_daily_service_supports_etf_and_coinbase_sources(tmp_path: Path) -> None:
    asset_cases = (
        (
            "etf",
            "equity:us:spy",
            alpaca_source_id("SPY"),
        ),
        (
            "coinbase",
            "crypto:btc-usd",
            coinbase_source_id("BTC-USD", DAILY_GRANULARITY_SECONDS),
        ),
    )
    count = 7
    for name, asset_id, source_id in asset_cases:
        staging = _open_staging((tmp_path / name).absolute())
        with staging:
            for index in range(count):
                timestamp = _BASE + timedelta(days=index + index // 2)
                retrieved_at = timestamp + timedelta(hours=2)
                if name == "etf":
                    config = AlpacaAssetConfiguration(
                        asset_id=asset_id,
                        symbol="SPY",
                        feed="iex",
                        adjustment="all",
                        source_id=source_id,
                        name="SPDR S&P 500 ETF Trust",
                        asset_class=AssetClass.ETF,
                        quote_currency="USD",
                        exchange="NYSE Arca",
                    )
                    bar = _bar(index, symbol="SPY")
                    raw = bar_to_raw_record(
                        bar,
                        retrieved_at=retrieved_at,
                        request_url="https://data.alpaca.markets/test",
                        configuration=config,
                    )
                    observations = bar_to_observations(
                        bar,
                        raw,
                        normalized_at=retrieved_at + timedelta(minutes=1),
                        configuration=config,
                    )
                else:
                    config = CoinbaseAssetConfiguration(
                        asset_id=asset_id,
                        product_id="BTC-USD",
                        source_id=source_id,
                        granularity_seconds=DAILY_GRANULARITY_SECONDS,
                        base_unit="BTC",
                        quote_unit="USD",
                        symbol="BTC",
                        name="Bitcoin",
                        asset_class=AssetClass.CRYPTO,
                        quote_currency="USD",
                        exchange="COINBASE",
                    )
                    candle_start = timestamp.replace(hour=0)
                    close = Decimal("40000") + Decimal(index * 250)
                    candle = CoinbaseCandle(
                        product_id="BTC-USD",
                        start=candle_start,
                        low=close - 300,
                        high=close + 400,
                        open=close - 100,
                        close=close,
                        volume=Decimal("10") + Decimal(index),
                        raw_values=(
                            str(int(candle_start.timestamp())),
                            str(close - 300),
                            str(close + 400),
                            str(close - 100),
                            str(close),
                            str(Decimal("10") + Decimal(index)),
                        ),
                    )
                    raw = candle_to_raw_record(
                        candle,
                        retrieved_at=retrieved_at,
                        request_url="https://api.exchange.coinbase.com/test",
                        configuration=config,
                    )
                    observations = candle_to_observations(
                        candle,
                        raw,
                        normalized_at=retrieved_at + timedelta(minutes=1),
                        configuration=config,
                    )
                staging.save(raw)
                staging.save_observations(observations)

            request = IncrementalMarketRequest(
                statistics=MarketStatisticsRequest(
                    query=HistoricalBarQuery(
                        asset_id=asset_id,
                        source_id=source_id,
                        start=_BASE - timedelta(days=1),
                        end=_BASE + timedelta(days=count + count // 2 + 1),
                        known_at=_KNOWN_AT,
                    ),
                    sma_windows=(3,),
                    volatility_window=3,
                    relative_volume_window=3,
                    bollinger_window=3,
                    ema_windows=(3,),
                    rsi_window=3,
                    atr_window=3,
                    macd_fast_window=2,
                    macd_slow_window=3,
                    macd_signal_window=2,
                ),
                history_start=_BASE - timedelta(days=1),
                history_end=_BASE + timedelta(days=count + count // 2 + 1),
                computed_at=_COMPUTED_AT,
            )
            receipt = IncrementalMarketService(staging).run(request)
            assert receipt.selected_bars == count
            assert receipt.metrics_created > 0

            projection = HistoricalMarketDataV2Service(staging).query(request.statistics.query)
            materialized = HistoricalMarketDataV2Service(staging).materialize_many(
                projection.bars,
                request.statistics.query,
            )
            bars = tuple(sorted(materialized.values(), key=lambda item: item.timestamp))
            series = MarketBarSeries(
                query=request.statistics.query,
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
            expected_calculations = (
                MarketStatisticsEngine()
                .compute(
                    series,
                    request.statistics,
                )
                .calculations
            )
            finite_keys = {
                SIMPLE_RETURN_KEY,
                SMA_KEY,
                VOLATILITY_KEY,
                RELATIVE_VOLUME_KEY,
                BOLLINGER_UPPER_KEY,
                BOLLINGER_LOWER_KEY,
                BOLLINGER_BANDWIDTH_KEY,
                BOLLINGER_PERCENT_B_KEY,
            }

            def value_key(item):
                return (
                    item.metric_key,
                    item.as_of,
                    item.parameters.get("window"),
                    item.parameters.get("fast_window"),
                    item.parameters.get("slow_window"),
                    item.parameters.get("signal_window"),
                )

            expected_values = {
                value_key(item): item.value
                for item in expected_calculations
                if item.metric_key in finite_keys
            }
            result_ids = [
                UUID(str(row[0]))
                for row in staging._connection.execute(
                    "SELECT result_id FROM metric_results_v2 WHERE asset_id = ?",
                    [asset_id],
                ).fetchall()
            ]
            stored_metrics = staging.get_metrics(result_ids)
            actual_values = {
                value_key(item): item.value
                for item in stored_metrics.values()
                if item.metric_key in finite_keys
            }
            assert actual_values == expected_values
        staging.close()
