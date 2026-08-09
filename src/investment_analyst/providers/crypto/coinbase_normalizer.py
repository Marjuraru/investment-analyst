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
from investment_analyst.providers.asset_config import CoinbaseAssetConfiguration
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


def _default_configuration() -> CoinbaseAssetConfiguration:
    return CoinbaseAssetConfiguration(
        asset_id=ASSET_ID,
        product_id=PRODUCT_ID,
        source_id=SOURCE_ID,
        granularity_seconds=DAILY_GRANULARITY_SECONDS,
        base_unit="BTC",
        quote_unit="USD",
        symbol="BTC",
        name="Bitcoin",
        asset_class=AssetClass.CRYPTO,
        quote_currency="USD",
        exchange="COINBASE",
    )


def _configuration_or_default(
    configuration: CoinbaseAssetConfiguration | None,
) -> CoinbaseAssetConfiguration:
    return configuration or _default_configuration()


def _validate_candle_configuration(
    candle: CoinbaseCandle,
    configuration: CoinbaseAssetConfiguration,
) -> None:
    if candle.product_id != configuration.product_id:
        raise ValueError("Coinbase candle product does not match the resolved configuration")
    if configuration.granularity_seconds != DAILY_GRANULARITY_SECONDS:
        raise ValueError("Coinbase daily normalizer requires daily candle configuration")


def create_coinbase_asset(configuration: CoinbaseAssetConfiguration | None = None) -> Asset:
    """Build one daily Coinbase asset from its resolved catalog configuration."""
    resolved = _configuration_or_default(configuration)
    return Asset(
        asset_id=resolved.asset_id,
        symbol=resolved.symbol,
        name=resolved.name,
        asset_class=resolved.asset_class,
        quote_currency=resolved.quote_currency,
        exchange=resolved.exchange,
        provider_symbols={"coinbase_exchange": resolved.product_id},
        is_active=True,
    )


def create_coinbase_source(
    configuration: CoinbaseAssetConfiguration | None = None,
) -> SourceDefinition:
    """Build one official Coinbase daily-candle source from configuration."""
    resolved = _configuration_or_default(configuration)
    return SourceDefinition(
        source_id=resolved.source_id,
        provider_name="Coinbase Exchange",
        dataset_name=f"{resolved.product_id} Daily Candles",
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
    configuration: CoinbaseAssetConfiguration | None = None,
) -> RawRecord:
    """Create a version-aware raw record from one Coinbase candle."""
    resolved = _configuration_or_default(configuration)
    _validate_candle_configuration(candle, resolved)
    checksum = raw_candle_checksum(candle)
    record_name = "|".join(
        (
            resolved.source_id,
            candle.product_id,
            candle.start.isoformat(),
            str(resolved.granularity_seconds),
            checksum,
        )
    )
    record_id = uuid5(NAMESPACE_URL, record_name)
    reference = SourceReference(
        source_id=resolved.source_id,
        record_key=(
            f"{candle.product_id}:{candle.start.isoformat()}:{resolved.granularity_seconds}"
        ),
        retrieved_at=retrieved_at,
        raw_uri=request_url,
        checksum_sha256=checksum,
    )
    return RawRecord(
        record_id=record_id,
        asset_id=resolved.asset_id,
        source=reference,
        event_time=candle.start,
        available_at=retrieved_at,
        received_at=retrieved_at,
        payload={
            "product_id": candle.product_id,
            "granularity_seconds": resolved.granularity_seconds,
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
    configuration: CoinbaseAssetConfiguration | None = None,
) -> tuple[NormalizedObservation, ...]:
    """Create exactly five OHLCV observations for a stored raw candle."""
    resolved = _configuration_or_default(configuration)
    _validate_candle_configuration(candle, resolved)
    if (
        raw_record.asset_id != resolved.asset_id
        or raw_record.source.source_id != resolved.source_id
    ):
        raise ValueError("Coinbase raw record does not match the resolved configuration")
    fields = (
        ("open", candle.open, resolved.quote_unit),
        ("high", candle.high, resolved.quote_unit),
        ("low", candle.low, resolved.quote_unit),
        ("close", candle.close, resolved.quote_unit),
        ("volume", candle.volume, resolved.base_unit),
    )
    period_end = candle.start + timedelta(days=1)
    return tuple(
        NormalizedObservation(
            observation_id=observation_id(raw_record.record_id, field_name),
            raw_record_id=raw_record.record_id,
            asset_id=resolved.asset_id,
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
