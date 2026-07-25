"""Tests for the separate Coinbase one-minute normalization contract."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from investment_analyst.core.models import DataFrequency, SourceType
from investment_analyst.providers.crypto.coinbase_exchange import CoinbaseCandle
from investment_analyst.providers.crypto.coinbase_intraday_normalizer import (
    SOURCE_ID,
    candle_to_intraday_observations,
    candle_to_intraday_raw_record,
    create_coinbase_intraday_source,
)
from investment_analyst.providers.crypto.coinbase_normalizer import candle_to_raw_record

_START = datetime(2026, 7, 12, 11, 55, tzinfo=UTC)
_RETRIEVED_AT = datetime(2026, 7, 12, 12, tzinfo=UTC)
_REQUEST_URL = (
    "https://api.exchange.coinbase.com/products/BTC-USD/candles?"
    "start=2026-07-12T11%3A55%3A00%2B00%3A00&"
    "end=2026-07-12T11%3A56%3A00%2B00%3A00&granularity=60"
)


def _candle() -> CoinbaseCandle:
    return CoinbaseCandle(
        product_id="BTC-USD",
        start=_START,
        low=Decimal("107900"),
        high=Decimal("108050"),
        open=Decimal("107950"),
        close=Decimal("108000"),
        volume=Decimal("1.25"),
        raw_values=(
            "1783857300",
            "107900",
            "108050",
            "107950",
            "108000",
            "1.25",
        ),
    )


def test_intraday_source_and_identity_are_separate_from_daily() -> None:
    candle = _candle()
    minute = candle_to_intraday_raw_record(
        candle,
        retrieved_at=_RETRIEVED_AT,
        request_url=_REQUEST_URL,
    )
    daily = candle_to_raw_record(
        candle,
        retrieved_at=_RETRIEVED_AT,
        request_url=_REQUEST_URL.replace("granularity=60", "granularity=86400"),
    )
    source = create_coinbase_intraday_source()

    assert source.source_id == SOURCE_ID
    assert source.source_type is SourceType.MARKET
    assert source.is_official
    assert minute.record_id != daily.record_id
    assert minute.source.source_id == SOURCE_ID
    assert minute.payload["granularity_seconds"] == 60


def test_intraday_observations_preserve_one_minute_period_and_ids() -> None:
    candle = _candle()
    record = candle_to_intraday_raw_record(
        candle,
        retrieved_at=_RETRIEVED_AT,
        request_url=_REQUEST_URL,
    )
    observations = candle_to_intraday_observations(
        candle,
        record,
        normalized_at=_RETRIEVED_AT + timedelta(seconds=1),
    )

    assert [item.field_name for item in observations] == [
        "open",
        "high",
        "low",
        "close",
        "volume",
    ]
    assert all(item.frequency is DataFrequency.MINUTE_1 for item in observations)
    assert all(item.period_start == _START for item in observations)
    assert all(item.period_end == _START + timedelta(minutes=1) for item in observations)
    assert len({item.observation_id for item in observations}) == 5
