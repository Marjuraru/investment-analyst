"""Explicit stored-bar schemas for each supported market-data source."""

import re
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from decimal import Decimal
from types import MappingProxyType

from investment_analyst.core.models.enums import DataFrequency, DataQuality

COINBASE_SOURCE_ID = "coinbase-exchange:btc-usd:daily-candles"
COINBASE_INTRADAY_SOURCE_ID = "coinbase-exchange:btc-usd:minute-1-candles"
ALPACA_SOURCE_ID = "alpaca-market-data:iex:aapl:daily-bars:adjustment-all"
_ALPACA_SOURCE_PATTERN = re.compile(
    r"^alpaca-market-data:iex:[a-z][a-z0-9.-]{0,15}:daily-bars:adjustment-all$"
)
_COINBASE_DAILY_SOURCE_PATTERN = re.compile(r"^coinbase-exchange:([a-z0-9]+)-usd:daily-candles$")
SIMULATED_SOURCE_ID = "simulated:daily-bars"


@dataclass(frozen=True, slots=True)
class MarketBarSchema:
    """Required fields, units, and quality for one explicit stored source."""

    source_id: str
    frequency: DataFrequency
    required_fields: tuple[str, ...]
    optional_fields: tuple[str, ...]
    units: Mapping[str, str]
    expected_quality: DataQuality


_COINBASE_SCHEMA = MarketBarSchema(
    source_id=COINBASE_SOURCE_ID,
    frequency=DataFrequency.DAY_1,
    required_fields=("open", "high", "low", "close", "volume"),
    optional_fields=(),
    units=MappingProxyType(
        {"open": "USD", "high": "USD", "low": "USD", "close": "USD", "volume": "BTC"}
    ),
    expected_quality=DataQuality.VALID,
)

_COINBASE_INTRADAY_SCHEMA = MarketBarSchema(
    source_id=COINBASE_INTRADAY_SOURCE_ID,
    frequency=DataFrequency.MINUTE_1,
    required_fields=("open", "high", "low", "close", "volume"),
    optional_fields=(),
    units=MappingProxyType(
        {"open": "USD", "high": "USD", "low": "USD", "close": "USD", "volume": "BTC"}
    ),
    expected_quality=DataQuality.VALID,
)

_ALPACA_SCHEMA = MarketBarSchema(
    source_id=ALPACA_SOURCE_ID,
    frequency=DataFrequency.DAY_1,
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
    frequency=DataFrequency.DAY_1,
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
    schema.source_id: schema
    for schema in (
        _COINBASE_SCHEMA,
        _COINBASE_INTRADAY_SCHEMA,
        _ALPACA_SCHEMA,
        _SIMULATED_SCHEMA,
    )
}


def get_market_bar_schema(source_id: str) -> MarketBarSchema:
    """Return the exact schema for a supported source or fail explicitly."""
    coinbase_daily = _COINBASE_DAILY_SOURCE_PATTERN.fullmatch(source_id)
    if coinbase_daily:
        base_unit = coinbase_daily.group(1).upper()
        return MarketBarSchema(
            source_id=source_id,
            frequency=_COINBASE_SCHEMA.frequency,
            required_fields=_COINBASE_SCHEMA.required_fields,
            optional_fields=_COINBASE_SCHEMA.optional_fields,
            units=MappingProxyType(
                {
                    "open": "USD",
                    "high": "USD",
                    "low": "USD",
                    "close": "USD",
                    "volume": base_unit,
                }
            ),
            expected_quality=_COINBASE_SCHEMA.expected_quality,
        )
    if _ALPACA_SOURCE_PATTERN.fullmatch(source_id):
        return MarketBarSchema(
            source_id=source_id,
            frequency=_ALPACA_SCHEMA.frequency,
            required_fields=_ALPACA_SCHEMA.required_fields,
            optional_fields=_ALPACA_SCHEMA.optional_fields,
            units=_ALPACA_SCHEMA.units,
            expected_quality=_ALPACA_SCHEMA.expected_quality,
        )
    try:
        return _SCHEMAS[source_id]
    except KeyError as error:
        raise ValueError(f"unsupported market source: {source_id}") from error


def missing_market_bar_fields(
    schema: MarketBarSchema,
    *,
    present_fields: Collection[str],
    values: Mapping[str, Decimal],
) -> tuple[str, ...]:
    """Return required fields absent from one already-scoped market bar."""
    missing = set(schema.required_fields) - set(present_fields)
    if "vwap" in missing and _alpaca_vwap_may_be_absent(schema, values):
        missing.discard("vwap")
    return tuple(sorted(missing))


def _alpaca_vwap_may_be_absent(
    schema: MarketBarSchema,
    values: Mapping[str, Decimal],
) -> bool:
    if _ALPACA_SOURCE_PATTERN.fullmatch(schema.source_id) is None:
        return False
    if schema.frequency is not DataFrequency.DAY_1:
        return False
    volume = values.get("volume")
    trade_count = values.get("trade_count")
    return (
        isinstance(volume, Decimal)
        and isinstance(trade_count, Decimal)
        and volume == 0
        and trade_count == 0
    )
