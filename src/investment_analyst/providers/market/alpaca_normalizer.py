"""Convert validated Alpaca IEX bars into auditable core models."""

import json
from datetime import datetime
from uuid import NAMESPACE_URL, UUID, uuid5

from investment_analyst.core.models import (
    Asset,
    AssetClass,
    DataFrequency,
    DataQuality,
    NormalizedObservation,
    RawRecord,
    SourceDefinition,
    SourceReference,
    SourceType,
)
from investment_analyst.providers.market.alpaca_stock import (
    ADJUSTMENT,
    FEED,
    TIMEFRAME,
    AlpacaStockBar,
)
from investment_analyst.storage.serialization import sha256_hex

ASSET_ID = "equity:us:aapl"
SYMBOL = "AAPL"
SOURCE_ID = "alpaca-market-data:iex:aapl:daily-bars:adjustment-all"
RAW_SCHEMA_VERSION = "alpaca-stock-bars-v1"
TRANSFORMATION_VERSION = "alpaca-iex-bar-normalizer-v1"


def create_alpaca_asset() -> Asset:
    """Return the stable AAPL asset definition used by the importer."""
    return Asset(
        asset_id=ASSET_ID,
        symbol=SYMBOL,
        name="Apple Inc.",
        asset_class=AssetClass.EQUITY,
        quote_currency="USD",
        exchange="NASDAQ",
        provider_symbols={"alpaca_iex": SYMBOL},
        is_active=True,
    )


def create_alpaca_source() -> SourceDefinition:
    """Return the official Alpaca IEX daily-bar source definition."""
    return SourceDefinition(
        source_id=SOURCE_ID,
        provider_name="Alpaca Market Data",
        dataset_name="AAPL IEX Daily Bars, Adjustment All",
        source_type=SourceType.MARKET,
        base_url="https://data.alpaca.markets",
        is_official=True,
        coverage_notes=(
            "IEX feed only; it does not represent all United States exchanges, so volume and "
            "trade count are partial. Prices use adjustment=all. These data must not yet be "
            "used to infer institutional activity or power Cazatiburones."
        ),
    )


def raw_bar_checksum(bar: AlpacaStockBar) -> str:
    """Hash the canonical provider-string representation stored as raw_bar."""
    encoded = json.dumps(
        dict(bar.raw_values),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return sha256_hex(encoded)


def bar_to_raw_record(
    bar: AlpacaStockBar,
    *,
    retrieved_at: datetime,
    request_url: str,
) -> RawRecord:
    """Create a version-aware raw record from one Alpaca IEX bar."""
    checksum = raw_bar_checksum(bar)
    record_name = "|".join(
        (
            SOURCE_ID,
            bar.symbol,
            bar.timestamp.isoformat(),
            FEED,
            ADJUSTMENT,
            checksum,
        )
    )
    record_id = uuid5(NAMESPACE_URL, record_name)
    source = SourceReference(
        source_id=SOURCE_ID,
        record_key=f"{bar.symbol}:{bar.timestamp.isoformat()}:{FEED}:{ADJUSTMENT}",
        retrieved_at=retrieved_at,
        raw_uri=request_url,
        checksum_sha256=checksum,
    )
    return RawRecord(
        record_id=record_id,
        asset_id=ASSET_ID,
        source=source,
        event_time=bar.timestamp,
        available_at=retrieved_at,
        received_at=retrieved_at,
        payload={
            "symbol": bar.symbol,
            "timeframe": TIMEFRAME,
            "feed": FEED,
            "adjustment": ADJUSTMENT,
            "raw_bar": dict(bar.raw_values),
        },
        schema_version=RAW_SCHEMA_VERSION,
    )


def observation_id(raw_record_id: UUID, field_name: str) -> UUID:
    """Return the stable UUID5 assigned to one normalized Alpaca bar field."""
    return uuid5(NAMESPACE_URL, f"{raw_record_id}|{field_name}")


def bar_to_observations(
    bar: AlpacaStockBar,
    raw_record: RawRecord,
    *,
    normalized_at: datetime,
) -> tuple[NormalizedObservation, ...]:
    """Create exactly seven IEX bar observations without inventing reporting periods."""
    fields = (
        ("open", bar.open, "USD"),
        ("high", bar.high, "USD"),
        ("low", bar.low, "USD"),
        ("close", bar.close, "USD"),
        ("volume", bar.volume, "shares"),
        ("trade_count", bar.trade_count, "count"),
        ("vwap", bar.vwap, "USD"),
    )
    return tuple(
        NormalizedObservation(
            observation_id=observation_id(raw_record.record_id, field_name),
            raw_record_id=raw_record.record_id,
            asset_id=ASSET_ID,
            field_name=field_name,
            value=value,
            unit=unit,
            frequency=DataFrequency.DAY_1,
            observed_at=bar.timestamp,
            period_start=None,
            period_end=None,
            available_at=raw_record.available_at,
            normalized_at=normalized_at,
            source=raw_record.source,
            quality=DataQuality.PARTIAL,
            transformation_version=TRANSFORMATION_VERSION,
        )
        for field_name, value, unit in fields
    )
