"""Convert validated Coinbase candles into auditable core models."""

import json
from datetime import datetime, timedelta
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
from investment_analyst.providers.crypto.coinbase_exchange import (
    DAILY_GRANULARITY_SECONDS,
    CoinbaseCandle,
)
from investment_analyst.storage.serialization import sha256_hex

ASSET_ID = "crypto:btc-usd"
PRODUCT_ID = "BTC-USD"
SOURCE_ID = "coinbase-exchange:btc-usd:daily-candles"
RAW_SCHEMA_VERSION = "coinbase-exchange-candles-v1"
TRANSFORMATION_VERSION = "coinbase-candle-normalizer-v1"


def create_coinbase_asset() -> Asset:
    """Return the stable BTC-USD asset definition used by the importer."""
    return Asset(
        asset_id=ASSET_ID,
        symbol="BTC",
        name="Bitcoin",
        asset_class=AssetClass.CRYPTO,
        quote_currency="USD",
        exchange="COINBASE",
        provider_symbols={"coinbase_exchange": PRODUCT_ID},
        is_active=True,
    )


def create_coinbase_source() -> SourceDefinition:
    """Return the official Coinbase Exchange daily-candle source definition."""
    return SourceDefinition(
        source_id=SOURCE_ID,
        provider_name="Coinbase Exchange",
        dataset_name="BTC-USD Daily Candles",
        source_type=SourceType.MARKET,
        base_url="https://api.exchange.coinbase.com",
        is_official=True,
        coverage_notes=(
            "Coinbase Exchange market only; daily OHLCV candles may omit intervals without "
            "trades and do not represent an aggregate price across all exchanges."
        ),
    )


def raw_candle_checksum(candle: CoinbaseCandle) -> str:
    """Hash the compact ordered string representation stored in raw_candle."""
    encoded = json.dumps(
        list(candle.raw_values),
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return sha256_hex(encoded)


def candle_to_raw_record(
    candle: CoinbaseCandle,
    *,
    retrieved_at: datetime,
    request_url: str,
) -> RawRecord:
    """Create a version-aware raw record from one Coinbase candle."""
    checksum = raw_candle_checksum(candle)
    record_name = "|".join(
        (
            SOURCE_ID,
            candle.product_id,
            candle.start.isoformat(),
            str(DAILY_GRANULARITY_SECONDS),
            checksum,
        )
    )
    record_id = uuid5(NAMESPACE_URL, record_name)
    reference = SourceReference(
        source_id=SOURCE_ID,
        record_key=(f"{candle.product_id}:{candle.start.isoformat()}:{DAILY_GRANULARITY_SECONDS}"),
        retrieved_at=retrieved_at,
        raw_uri=request_url,
        checksum_sha256=checksum,
    )
    return RawRecord(
        record_id=record_id,
        asset_id=ASSET_ID,
        source=reference,
        event_time=candle.start,
        available_at=retrieved_at,
        received_at=retrieved_at,
        payload={
            "product_id": candle.product_id,
            "granularity_seconds": DAILY_GRANULARITY_SECONDS,
            "raw_candle": list(candle.raw_values),
        },
        schema_version=RAW_SCHEMA_VERSION,
    )


def observation_id(raw_record_id: UUID, field_name: str) -> UUID:
    """Return the stable UUID5 assigned to one normalized candle field."""
    return uuid5(NAMESPACE_URL, f"{raw_record_id}|{field_name}")


def candle_to_observations(
    candle: CoinbaseCandle,
    raw_record: RawRecord,
    *,
    normalized_at: datetime,
) -> tuple[NormalizedObservation, ...]:
    """Create exactly five OHLCV observations for a stored raw candle."""
    fields = (
        ("open", candle.open, "USD"),
        ("high", candle.high, "USD"),
        ("low", candle.low, "USD"),
        ("close", candle.close, "USD"),
        ("volume", candle.volume, "BTC"),
    )
    period_end = candle.start + timedelta(days=1)
    return tuple(
        NormalizedObservation(
            observation_id=observation_id(raw_record.record_id, field_name),
            raw_record_id=raw_record.record_id,
            asset_id=ASSET_ID,
            field_name=field_name,
            value=value,
            unit=unit,
            frequency=DataFrequency.DAY_1,
            observed_at=candle.start,
            period_start=candle.start,
            period_end=period_end,
            available_at=raw_record.available_at,
            normalized_at=normalized_at,
            source=raw_record.source,
            quality=DataQuality.VALID,
            transformation_version=TRANSFORMATION_VERSION,
        )
        for field_name, value, unit in fields
    )
