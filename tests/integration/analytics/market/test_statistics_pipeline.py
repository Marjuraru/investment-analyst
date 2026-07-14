"""Integration tests for statistics persistence over unified local market history."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from investment_analyst.analytics.market.bar_models import HistoricalBarQuery
from investment_analyst.analytics.market.bar_schemas import ALPACA_SOURCE_ID, COINBASE_SOURCE_ID
from investment_analyst.analytics.market.history_service import HistoricalMarketDataService
from investment_analyst.analytics.market.statistics_engine import MarketStatisticsEngine
from investment_analyst.analytics.market.statistics_models import MarketStatisticsRequest
from investment_analyst.analytics.market.statistics_pipeline import MarketStatisticsPipeline
from investment_analyst.core.models import DataQuality
from investment_analyst.providers.crypto.coinbase_exchange import CoinbaseCandle
from investment_analyst.providers.crypto.coinbase_normalizer import (
    candle_to_observations,
    candle_to_raw_record,
)
from investment_analyst.providers.market.alpaca_normalizer import (
    bar_to_observations,
    bar_to_raw_record,
)
from investment_analyst.providers.market.alpaca_stock import AlpacaStockBar
from investment_analyst.storage import LocalStorage, StoragePaths


def _store_coinbase(storage: LocalStorage, count: int = 4) -> tuple[datetime, datetime]:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    for index in range(count):
        timestamp = start + timedelta(days=index)
        retrieved = timestamp + timedelta(hours=1)
        close = Decimal("100") + Decimal(index * 2)
        candle = CoinbaseCandle(
            product_id="BTC-USD",
            start=timestamp,
            low=close - 2,
            high=close + 2,
            open=close - 1,
            close=close,
            volume=Decimal("100") + Decimal(index * 10),
            raw_values=(
                str(int(timestamp.timestamp())),
                str(close - 2),
                str(close + 2),
                str(close - 1),
                str(close),
                str(Decimal("100") + Decimal(index * 10)),
            ),
        )
        raw = candle_to_raw_record(
            candle,
            retrieved_at=retrieved,
            request_url="https://api.exchange.coinbase.com/test",
        )
        storage.raw_records.save(raw)
        for observation in candle_to_observations(
            candle,
            raw,
            normalized_at=retrieved + timedelta(minutes=1),
        ):
            storage.observations.save(observation)
    return start, start + timedelta(days=count)


def _store_alpaca(storage: LocalStorage, count: int = 4) -> tuple[datetime, datetime]:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    for index in range(count):
        timestamp = start + timedelta(days=index)
        retrieved = timestamp + timedelta(hours=1)
        close = Decimal("200") + Decimal(index * 2)
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
        bar = AlpacaStockBar(
            symbol="AAPL",
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
        raw = bar_to_raw_record(
            bar,
            retrieved_at=retrieved,
            request_url="https://data.alpaca.markets/test",
        )
        storage.raw_records.save(raw)
        for observation in bar_to_observations(
            bar,
            raw,
            normalized_at=retrieved + timedelta(minutes=1),
        ):
            storage.observations.save(observation)
    return start, start + timedelta(days=count)


def _request(asset_id: str, source_id: str, start: datetime, end: datetime, known_at: datetime):
    return MarketStatisticsRequest(
        query=HistoricalBarQuery(
            asset_id=asset_id,
            source_id=source_id,
            start=start,
            end=end,
            known_at=known_at,
        ),
        sma_windows=(2,),
        volatility_window=2,
        relative_volume_window=2,
    )


def test_btc_and_aapl_statistics_are_persisted_with_quality_and_idempotency(tmp_path) -> None:
    fixed_clock = datetime(2026, 3, 1, tzinfo=UTC)
    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        btc_start, btc_end = _store_coinbase(storage)
        aapl_start, aapl_end = _store_alpaca(storage)
        history = HistoricalMarketDataService(storage)
        pipeline = MarketStatisticsPipeline(
            storage,
            history,
            MarketStatisticsEngine(),
            clock=lambda: fixed_clock,
        )
        btc_request = _request(
            "crypto:btc-usd", COINBASE_SOURCE_ID, btc_start, btc_end, fixed_clock
        )
        aapl_request = _request(
            "equity:us:aapl", ALPACA_SOURCE_ID, aapl_start, aapl_end, fixed_clock
        )
        raw_count = len(storage.raw_records.list())
        observation_count = len(storage.observations.list())

        first_btc = pipeline.run(btc_request)
        btc_ids = {
            item.result_id for item in storage.metric_results.list(asset_id="crypto:btc-usd")
        }
        second_btc = pipeline.run(btc_request)
        aapl_summary = pipeline.run(aapl_request)
        aapl_results = storage.metric_results.list(asset_id="equity:us:aapl")

        assert first_btc.results_created == first_btc.results_generated
        assert second_btc.results_created == 0
        assert second_btc.results_reused == second_btc.results_generated
        assert btc_ids == {
            item.result_id for item in storage.metric_results.list(asset_id="crypto:btc-usd")
        }
        assert all(
            item.quality is DataQuality.VALID
            for item in storage.metric_results.list(asset_id="crypto:btc-usd")
        )
        assert all(item.quality is DataQuality.PARTIAL for item in aapl_results)
        assert aapl_summary.definitions_upserted == 4
        assert len(storage.metric_definitions.list_all()) == 4
        assert len(storage.raw_records.list()) == raw_count
        assert len(storage.observations.list()) == observation_count
        assert storage.diagnostics.list() == []
        assert first_btc.to_json_dict()["traceability_verified"] is True


def test_known_at_is_part_of_result_identity_and_computed_at_is_preserved(tmp_path) -> None:
    first_clock = datetime(2026, 3, 1, tzinfo=UTC)
    second_clock = datetime(2026, 3, 2, tzinfo=UTC)
    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        start, end = _store_coinbase(storage)
        history = HistoricalMarketDataService(storage)
        request = _request("crypto:btc-usd", COINBASE_SOURCE_ID, start, end, first_clock)
        first_pipeline = MarketStatisticsPipeline(
            storage, history, MarketStatisticsEngine(), clock=lambda: first_clock
        )
        first_pipeline.run(request)
        first_results = storage.metric_results.list(asset_id="crypto:btc-usd")
        original_computed = {item.result_id: item.computed_at for item in first_results}

        second_pipeline = MarketStatisticsPipeline(
            storage, history, MarketStatisticsEngine(), clock=lambda: second_clock
        )
        second_pipeline.run(request)
        reused = storage.metric_results.list(asset_id="crypto:btc-usd")
        assert {item.result_id: item.computed_at for item in reused} == original_computed

        later_request = _request(
            "crypto:btc-usd",
            COINBASE_SOURCE_ID,
            start,
            end,
            first_clock + timedelta(hours=1),
        )
        second_pipeline.run(later_request)
        later_results = storage.metric_results.list(asset_id="crypto:btc-usd")
        assert len(later_results) == len(first_results) * 2
