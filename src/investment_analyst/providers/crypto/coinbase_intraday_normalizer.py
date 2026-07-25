"""Normalize Coinbase BTC-USD one-minute candles as a separate dataset."""

from datetime import datetime, timedelta
from uuid import NAMESPACE_URL, UUID, uuid5

from investment_analyst.core.models import (
    DataFrequency,
    DataQuality,
    NormalizedObservation,
    RawRecord,
    SourceDefinition,
    SourceReference,
    SourceType,
)
from investment_analyst.providers.crypto.coinbase_exchange import (
    MINUTE_GRANULARITY_SECONDS,
    CoinbaseCandle,
)
from investment_analyst.providers.crypto.coinbase_normalizer import (
    ASSET_ID,
    PRODUCT_ID,
    create_coinbase_asset,
    raw_candle_checksum,
)

SOURCE_ID = "coinbase-exchange:btc-usd:minute-1-candles"
RAW_SCHEMA_VERSION = "coinbase-exchange-minute-candles-v1"
TRANSFORMATION_VERSION = "coinbase-minute-candle-normalizer-v1"


def create_coinbase_intraday_source() -> SourceDefinition:
    """Return the official Coinbase Exchange one-minute source definition."""
    return SourceDefinition(
        source_id=SOURCE_ID,
        provider_name="Coinbase Exchange",
        dataset_name="BTC-USD One-Minute Candles",
        source_type=SourceType.MARKET,
        base_url="https://api.exchange.coinbase.com",
        is_official=True,
        coverage_notes=(
            "Coinbase Exchange market only; one-minute OHLCV candles may omit intervals without "
            "trades and do not represent an aggregate price across all exchanges."
        ),
    )


def candle_to_intraday_raw_record(
    candle: CoinbaseCandle,
    *,
    retrieved_at: datetime,
    request_url: str,
) -> RawRecord:
    """Create a deterministic raw record for one minute of Coinbase evidence."""
    checksum = raw_candle_checksum(candle)
    record_name = "|".join(
        (
            SOURCE_ID,
            candle.product_id,
            candle.start.isoformat(),
            str(MINUTE_GRANULARITY_SECONDS),
            checksum,
        )
    )
    record_id = uuid5(NAMESPACE_URL, record_name)
    reference = SourceReference(
        source_id=SOURCE_ID,
        record_key=(f"{candle.product_id}:{candle.start.isoformat()}:{MINUTE_GRANULARITY_SECONDS}"),
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
            "granularity_seconds": MINUTE_GRANULARITY_SECONDS,
            "raw_candle": list(candle.raw_values),
        },
        schema_version=RAW_SCHEMA_VERSION,
    )


def intraday_observation_id(raw_record_id: UUID, field_name: str) -> UUID:
    """Return the stable identity for one normalized minute-candle field."""
    return uuid5(NAMESPACE_URL, f"{raw_record_id}|{field_name}")


def candle_to_intraday_observations(
    candle: CoinbaseCandle,
    raw_record: RawRecord,
    *,
    normalized_at: datetime,
) -> tuple[NormalizedObservation, ...]:
    """Create exactly five one-minute OHLCV observations."""
    fields = (
        ("open", candle.open, "USD"),
        ("high", candle.high, "USD"),
        ("low", candle.low, "USD"),
        ("close", candle.close, "USD"),
        ("volume", candle.volume, "BTC"),
    )
    period_end = candle.start + timedelta(seconds=MINUTE_GRANULARITY_SECONDS)
    return tuple(
        NormalizedObservation(
            observation_id=intraday_observation_id(raw_record.record_id, field_name),
            raw_record_id=raw_record.record_id,
            asset_id=ASSET_ID,
            field_name=field_name,
            value=value,
            unit=unit,
            frequency=DataFrequency.MINUTE_1,
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


__all__ = [
    "ASSET_ID",
    "PRODUCT_ID",
    "RAW_SCHEMA_VERSION",
    "SOURCE_ID",
    "TRANSFORMATION_VERSION",
    "candle_to_intraday_observations",
    "candle_to_intraday_raw_record",
    "create_coinbase_asset",
    "create_coinbase_intraday_source",
    "intraday_observation_id",
]
