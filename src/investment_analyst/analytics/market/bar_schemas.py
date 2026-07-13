"""Explicit stored-bar schemas for each supported market-data source."""

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from investment_analyst.core.models.enums import DataQuality

COINBASE_SOURCE_ID = "coinbase-exchange:btc-usd:daily-candles"
ALPACA_SOURCE_ID = "alpaca-market-data:iex:aapl:daily-bars:adjustment-all"
SIMULATED_SOURCE_ID = "simulated:daily-bars"


@dataclass(frozen=True, slots=True)
class MarketBarSchema:
    """Required fields, units, and quality for one explicit stored source."""

    source_id: str
    required_fields: tuple[str, ...]
    optional_fields: tuple[str, ...]
    units: Mapping[str, str]
    expected_quality: DataQuality


_COINBASE_SCHEMA = MarketBarSchema(
    source_id=COINBASE_SOURCE_ID,
    required_fields=("open", "high", "low", "close", "volume"),
    optional_fields=(),
    units=MappingProxyType(
        {"open": "USD", "high": "USD", "low": "USD", "close": "USD", "volume": "BTC"}
    ),
    expected_quality=DataQuality.VALID,
)

_ALPACA_SCHEMA = MarketBarSchema(
    source_id=ALPACA_SOURCE_ID,
    required_fields=("open", "high", "low", "close", "volume", "trade_count", "vwap"),
    optional_fields=(),
    units=MappingProxyType(
        {
            "open": "USD",
            "high": "USD",
            "low": "USD",
            "close": "USD",
            "volume": "shares",
            "trade_count": "count",
            "vwap": "USD",
        }
    ),
    expected_quality=DataQuality.PARTIAL,
)

_SIMULATED_SCHEMA = MarketBarSchema(
    source_id=SIMULATED_SOURCE_ID,
    required_fields=("open", "high", "low", "close", "volume", "trade_count"),
    optional_fields=(),
    units=MappingProxyType(
        {
            "open": "USD",
            "high": "USD",
            "low": "USD",
            "close": "USD",
            "volume": "units",
            "trade_count": "count",
        }
    ),
    expected_quality=DataQuality.VALID,
)

_SCHEMAS = {
    schema.source_id: schema for schema in (_COINBASE_SCHEMA, _ALPACA_SCHEMA, _SIMULATED_SCHEMA)
}


def get_market_bar_schema(source_id: str) -> MarketBarSchema:
    """Return the exact schema for a supported source or fail explicitly."""
    try:
        return _SCHEMAS[source_id]
    except KeyError as error:
        raise ValueError(f"unsupported market source: {source_id}") from error
