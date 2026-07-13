"""Integration tests for Coinbase and Alpaca bars in one local database."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from investment_analyst.analytics.market.bar_models import HistoricalBarQuery, MarketBar
from investment_analyst.analytics.market.bar_schemas import ALPACA_SOURCE_ID, COINBASE_SOURCE_ID
from investment_analyst.analytics.market.history_service import HistoricalMarketDataService
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


def _coinbase_candle(timestamp: datetime) -> CoinbaseCandle:
    unix_time = str(int(timestamp.timestamp()))
    return CoinbaseCandle(
        product_id="BTC-USD",
        start=timestamp,
        low=Decimal("100000"),
        high=Decimal("110000"),
        open=Decimal("102000"),
        close=Decimal("108000"),
        volume=Decimal("12000"),
        raw_values=(unix_time, "100000", "110000", "102000", "108000", "12000"),
    )


def _alpaca_bar(timestamp: datetime) -> AlpacaStockBar:
    raw_values = {
        "t": timestamp.isoformat().replace("+00:00", "Z"),
        "o": "205",
        "h": "211",
        "l": "203",
        "c": "209",
        "v": "50000000",
        "n": "650000",
        "vw": "207",
    }
    return AlpacaStockBar(
        symbol="AAPL",
        timestamp=timestamp,
        open=Decimal("205"),
        high=Decimal("211"),
        low=Decimal("203"),
        close=Decimal("209"),
        volume=Decimal("50000000"),
        trade_count=Decimal("650000"),
        vwap=Decimal("207"),
        raw_values=raw_values,
    )


def _store_both(storage: LocalStorage) -> tuple[datetime, datetime]:
    timestamp = datetime(2026, 7, 2, tzinfo=UTC)
    retrieved = datetime(2026, 7, 3, tzinfo=UTC)
    normalized = retrieved + timedelta(minutes=1)

    candle = _coinbase_candle(timestamp)
    coinbase_raw = candle_to_raw_record(
        candle,
        retrieved_at=retrieved,
        request_url="https://api.exchange.coinbase.com/test",
    )
    storage.raw_records.save(coinbase_raw)
    for observation in candle_to_observations(candle, coinbase_raw, normalized_at=normalized):
        storage.observations.save(observation)

    stock_bar = _alpaca_bar(timestamp)
    alpaca_raw = bar_to_raw_record(
        stock_bar,
        retrieved_at=retrieved,
        request_url="https://data.alpaca.markets/test",
    )
    storage.raw_records.save(alpaca_raw)
    for observation in bar_to_observations(stock_bar, alpaca_raw, normalized_at=normalized):
        storage.observations.save(observation)
    return timestamp, retrieved


def test_unified_history_keeps_assets_sources_and_quality_separate(tmp_path) -> None:
    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        timestamp, retrieved = _store_both(storage)
        service = HistoricalMarketDataService(storage)
        btc = service.query(
            HistoricalBarQuery(
                asset_id="crypto:btc-usd",
                source_id=COINBASE_SOURCE_ID,
                start=timestamp,
                end=timestamp + timedelta(days=1),
                known_at=retrieved,
            )
        )
        aapl = service.query(
            HistoricalBarQuery(
                asset_id="equity:us:aapl",
                source_id=ALPACA_SOURCE_ID,
                start=timestamp,
                end=timestamp + timedelta(days=1),
                known_at=retrieved,
            )
        )
        metric_count = len(storage.metric_results.list())
        diagnostic_count = len(storage.diagnostics.list())

    assert len(btc.bars) == len(aapl.bars) == 1
    assert isinstance(btc.bars[0], MarketBar)
    assert isinstance(aapl.bars[0], MarketBar)
    assert set(btc.bars[0].observation_ids) == {"open", "high", "low", "close", "volume"}
    assert set(aapl.bars[0].observation_ids) == {
        "open",
        "high",
        "low",
        "close",
        "volume",
        "trade_count",
        "vwap",
    }
    assert btc.bars[0].quality is DataQuality.VALID
    assert aapl.bars[0].quality is DataQuality.PARTIAL
    assert btc.bars[0].timestamp.tzinfo is UTC
    assert aapl.bars[0].timestamp.tzinfo is UTC
    assert btc.traceability_verified and aapl.traceability_verified
    assert metric_count == diagnostic_count == 0


def test_point_in_time_query_is_reproducible(tmp_path) -> None:
    with LocalStorage(StoragePaths.from_root(tmp_path)) as storage:
        timestamp, retrieved = _store_both(storage)
        query = HistoricalBarQuery(
            asset_id="crypto:btc-usd",
            source_id=COINBASE_SOURCE_ID,
            start=timestamp,
            end=timestamp + timedelta(days=1),
            known_at=retrieved,
        )
        service = HistoricalMarketDataService(storage)
        first = service.query(query)
        second = service.query(query)

    assert first == second
    assert first.to_json_dict() == second.to_json_dict()
