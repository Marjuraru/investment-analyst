"""Tests for Alpaca AAPL asset, source, raw records, and observations."""

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from investment_analyst.core.models import DataQuality
from investment_analyst.providers.market.alpaca_normalizer import (
    ASSET_ID,
    SOURCE_ID,
    bar_to_observations,
    bar_to_raw_record,
    create_alpaca_asset,
    create_alpaca_source,
    raw_bar_checksum,
)
from investment_analyst.providers.market.alpaca_stock import AlpacaStockBar

FIXTURE_PATH = Path("tests/fixtures/alpaca/aapl_daily.json")
RETRIEVED_AT = datetime(2026, 7, 12, 12, tzinfo=UTC)
NORMALIZED_AT = datetime(2026, 7, 12, 12, 1, tzinfo=UTC)
REQUEST_URL = "https://data.alpaca.markets/v2/stocks/AAPL/bars?feed=iex"


def _bar(*, close: str = "205.70") -> AlpacaStockBar:
    raw_values = {
        "t": "2026-07-07T04:00:00Z",
        "o": "203.95",
        "h": "206.40",
        "l": "202.85",
        "c": close,
        "v": "34210600",
        "n": "153890",
        "vw": "204.88",
    }
    return AlpacaStockBar(
        symbol="AAPL",
        timestamp=datetime(2026, 7, 7, 4, tzinfo=UTC),
        open=Decimal(raw_values["o"]),
        high=Decimal(raw_values["h"]),
        low=Decimal(raw_values["l"]),
        close=Decimal(raw_values["c"]),
        volume=Decimal(raw_values["v"]),
        trade_count=Decimal(raw_values["n"]),
        vwap=Decimal(raw_values["vw"]),
        raw_values=raw_values,
    )


def test_asset_and_source_identify_partial_iex_coverage() -> None:
    asset = create_alpaca_asset()
    source = create_alpaca_source()

    assert asset.asset_id == ASSET_ID
    assert asset.symbol == "AAPL"
    assert asset.provider_symbols == {"alpaca_iex": "AAPL"}
    assert source.source_id == SOURCE_ID
    assert source.is_official is True
    assert source.coverage_notes is not None
    assert "IEX" in source.coverage_notes
    assert "partial" in source.coverage_notes
    assert "Cazatiburones" in source.coverage_notes


def test_raw_record_uuid_and_checksum_are_stable_and_revisions_are_versioned() -> None:
    bar = _bar()
    first = bar_to_raw_record(bar, retrieved_at=RETRIEVED_AT, request_url=REQUEST_URL)
    second = bar_to_raw_record(bar, retrieved_at=RETRIEVED_AT, request_url=REQUEST_URL)
    revised = bar_to_raw_record(
        _bar(close="205.71"),
        retrieved_at=RETRIEVED_AT,
        request_url=REQUEST_URL,
    )

    assert first.record_id == second.record_id
    assert first.source.checksum_sha256 == raw_bar_checksum(bar)
    assert first.record_id != revised.record_id
    assert first.payload["raw_bar"]["c"] == "205.70"
    assert first.model_dump_json()


def test_normalization_creates_seven_partial_observations_without_periods() -> None:
    bar = _bar()
    raw_record = bar_to_raw_record(
        bar,
        retrieved_at=RETRIEVED_AT,
        request_url=REQUEST_URL,
    )
    observations = bar_to_observations(
        bar,
        raw_record,
        normalized_at=NORMALIZED_AT,
    )

    assert len(observations) == 7
    assert {item.field_name for item in observations} == {
        "open",
        "high",
        "low",
        "close",
        "volume",
        "trade_count",
        "vwap",
    }
    units = {item.field_name: item.unit for item in observations}
    assert units == {
        "open": "USD",
        "high": "USD",
        "low": "USD",
        "close": "USD",
        "volume": "shares",
        "trade_count": "count",
        "vwap": "USD",
    }
    assert all(item.quality is DataQuality.PARTIAL for item in observations)
    assert all(item.period_start is None and item.period_end is None for item in observations)
    assert all(item.source == raw_record.source for item in observations)
    assert len({item.observation_id for item in observations}) == 7
    assert all(item.model_dump_json() for item in observations)


def test_fixture_is_explicitly_local_test_data() -> None:
    document = json.loads(FIXTURE_PATH.read_text())

    assert document["symbol"] == "AAPL"
    assert len(document["bars"]) == 4
