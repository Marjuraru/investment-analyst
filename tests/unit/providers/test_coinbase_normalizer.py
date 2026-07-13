"""Tests for Coinbase asset, source, raw-record, and observation normalization."""

from datetime import UTC, datetime
from decimal import Decimal

from investment_analyst.core.models import AssetClass, DataFrequency, SourceType
from investment_analyst.providers.crypto.coinbase_exchange import CoinbaseCandle
from investment_analyst.providers.crypto.coinbase_normalizer import (
    ASSET_ID,
    SOURCE_ID,
    candle_to_observations,
    candle_to_raw_record,
    create_coinbase_asset,
    create_coinbase_source,
    raw_candle_checksum,
)

RETRIEVED_AT = datetime(2026, 7, 12, 12, tzinfo=UTC)
REQUEST_URL = (
    "https://api.exchange.coinbase.com/products/BTC-USD/candles?"
    "start=2026-07-11T00%3A00%3A00%2B00%3A00&"
    "end=2026-07-12T00%3A00%3A00%2B00%3A00&granularity=86400"
)


def _candle(*, close: str = "109500.75") -> CoinbaseCandle:
    return CoinbaseCandle(
        product_id="BTC-USD",
        start=datetime(2026, 7, 11, tzinfo=UTC),
        low=Decimal("108100.10"),
        high=Decimal("110000.00"),
        open=Decimal("108800.25"),
        close=Decimal(close),
        volume=Decimal("15234.125"),
        raw_values=(
            "1783728000",
            "108100.10",
            "110000.00",
            "108800.25",
            close,
            "15234.125",
        ),
    )


def test_coinbase_asset_and_source_definitions() -> None:
    asset = create_coinbase_asset()
    source = create_coinbase_source()

    assert asset.asset_id == ASSET_ID
    assert asset.symbol == "BTC"
    assert asset.asset_class is AssetClass.CRYPTO
    assert asset.exchange == "COINBASE"
    assert asset.provider_symbols == {"coinbase_exchange": "BTC-USD"}
    assert source.source_id == SOURCE_ID
    assert source.source_type is SourceType.MARKET
    assert source.is_official is True
    assert "not represent an aggregate" in source.coverage_notes


def test_raw_record_uuid_and_checksum_are_stable() -> None:
    first = candle_to_raw_record(
        _candle(),
        retrieved_at=RETRIEVED_AT,
        request_url=REQUEST_URL,
    )
    second = candle_to_raw_record(
        _candle(),
        retrieved_at=RETRIEVED_AT,
        request_url=REQUEST_URL,
    )

    assert first.record_id == second.record_id
    assert first.source.checksum_sha256 == raw_candle_checksum(_candle())
    assert first.payload["raw_candle"] == list(_candle().raw_values)
    assert first.model_dump_json()


def test_revised_candle_gets_new_raw_record_identifier() -> None:
    original = candle_to_raw_record(
        _candle(),
        retrieved_at=RETRIEVED_AT,
        request_url=REQUEST_URL,
    )
    revised = candle_to_raw_record(
        _candle(close="109600.75"),
        retrieved_at=RETRIEVED_AT,
        request_url=REQUEST_URL,
    )

    assert original.record_id != revised.record_id
    assert original.source.checksum_sha256 != revised.source.checksum_sha256


def test_creates_exactly_five_daily_observations() -> None:
    candle = _candle()
    raw_record = candle_to_raw_record(
        candle,
        retrieved_at=RETRIEVED_AT,
        request_url=REQUEST_URL,
    )
    observations = candle_to_observations(
        candle,
        raw_record,
        normalized_at=datetime(2026, 7, 12, 12, 1, tzinfo=UTC),
    )

    assert [item.field_name for item in observations] == [
        "open",
        "high",
        "low",
        "close",
        "volume",
    ]
    assert all(item.field_name != "trade_count" for item in observations)
    assert {item.unit for item in observations[:-1]} == {"USD"}
    assert observations[-1].unit == "BTC"
    assert all(item.frequency is DataFrequency.DAY_1 for item in observations)
    assert all(item.period_start == candle.start for item in observations)
    assert all(item.period_end == datetime(2026, 7, 12, tzinfo=UTC) for item in observations)
    assert len({item.observation_id for item in observations}) == 5
    assert all(item.raw_record_id == raw_record.record_id for item in observations)
    assert all(item.model_dump_json() for item in observations)
