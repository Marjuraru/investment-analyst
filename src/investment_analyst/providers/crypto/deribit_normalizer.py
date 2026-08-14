"""Deterministic RawRecord and observation normalization for Deribit v1."""

import hashlib
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import JsonValue

from investment_analyst.core.models import (
    Asset,
    DataFrequency,
    DataQuality,
    NormalizedObservation,
    RawRecord,
    SourceDefinition,
    SourceReference,
    SourceType,
)
from investment_analyst.providers.asset_config import DeribitAssetConfiguration
from investment_analyst.providers.crypto.deribit import (
    OFFICIAL_BASE_URL,
    DeribitDvolCandle,
    DeribitFundingPoint,
    DeribitPerpetualSummary,
)

FUNDING_SCHEMA_VERSION = "deribit-funding-point-v1"
DVOL_SCHEMA_VERSION = "deribit-dvol-candle-v1"
SUMMARY_SCHEMA_VERSION = "deribit-perpetual-summary-v1"
FUNDING_TRANSFORMATION_VERSION = "deribit-funding-normalizer-v1"
DVOL_TRANSFORMATION_VERSION = "deribit-dvol-daily-normalizer-v1"
SUMMARY_TRANSFORMATION_VERSION = "deribit-perpetual-summary-normalizer-v1"


def canonical_json_text(value: JsonValue) -> str:
    """Encode one already-validated JSON value with stable UTF-8 semantics."""
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def canonical_identity(preimage: dict[str, JsonValue]) -> UUID:
    """Return UUID5/NAMESPACE_URL over one explicit canonical JSON preimage."""
    return uuid5(NAMESPACE_URL, canonical_json_text(preimage))


def create_deribit_asset(configuration: DeribitAssetConfiguration) -> Asset:
    """Return the canonical asset without changing its existing provider-symbol map."""
    return Asset(
        asset_id=configuration.asset_id,
        symbol=configuration.symbol,
        name=configuration.name,
        asset_class=configuration.asset_class,
        quote_currency=configuration.quote_currency,
        exchange=configuration.exchange,
        provider_symbols=configuration.provider_symbols,
        is_active=True,
    )


def create_deribit_sources(
    configuration: DeribitAssetConfiguration,
) -> tuple[SourceDefinition, SourceDefinition, SourceDefinition]:
    """Return the three official, independent Deribit dataset definitions."""
    limitation = (
        "Public Deribit data; historical endpoints are not a demonstrated vintage archive, "
        "coverage, retention, access, rate limits, and fields may change."
    )
    return (
        SourceDefinition(
            source_id=configuration.funding_source_id,
            provider_name="Deribit",
            dataset_name=f"{configuration.instrument_name} funding rate history",
            source_type=SourceType.MARKET,
            base_url=OFFICIAL_BASE_URL,
            is_official=True,
            coverage_notes=limitation,
        ),
        SourceDefinition(
            source_id=configuration.dvol_source_id,
            provider_name="Deribit",
            dataset_name=f"{configuration.currency} DVOL daily index candles",
            source_type=SourceType.MARKET,
            base_url=OFFICIAL_BASE_URL,
            is_official=True,
            coverage_notes=limitation,
        ),
        SourceDefinition(
            source_id=configuration.summary_source_id,
            provider_name="Deribit",
            dataset_name=f"{configuration.instrument_name} public book summary",
            source_type=SourceType.MARKET,
            base_url=OFFICIAL_BASE_URL,
            is_official=True,
            coverage_notes=(
                "Prospective local snapshots only; nullable fields are not synthetic zeros."
            ),
        ),
    )


def funding_to_raw_record(
    point: DeribitFundingPoint,
    *,
    configuration: DeribitAssetConfiguration,
    received_at: datetime,
    request_url: str,
) -> RawRecord:
    """Preserve one funding object and assign its revision-aware deterministic ID."""
    _require_configuration(configuration, point.instrument_name)
    return _raw_record(
        payload=point.raw_payload,
        asset_id=configuration.asset_id,
        source_id=configuration.funding_source_id,
        schema_version=FUNDING_SCHEMA_VERSION,
        event_time=point.timestamp,
        received_at=received_at,
        request_url=request_url,
    )


def dvol_to_raw_record(
    candle: DeribitDvolCandle,
    *,
    configuration: DeribitAssetConfiguration,
    received_at: datetime,
    request_url: str,
) -> RawRecord:
    """Preserve one DVOL array and assign its revision-aware deterministic ID."""
    if candle.currency != configuration.currency:
        raise ValueError("DVOL candle does not match the Deribit configuration")
    return _raw_record(
        payload=list(candle.raw_payload),
        asset_id=configuration.asset_id,
        source_id=configuration.dvol_source_id,
        schema_version=DVOL_SCHEMA_VERSION,
        event_time=candle.start,
        received_at=received_at,
        request_url=request_url,
    )


def summary_to_raw_record(
    summary: DeribitPerpetualSummary,
    *,
    configuration: DeribitAssetConfiguration,
    received_at: datetime,
    request_url: str,
) -> RawRecord:
    """Preserve one prospective summary, including provider field ``last``."""
    _require_configuration(configuration, summary.instrument_name)
    return _raw_record(
        payload=summary.raw_payload,
        asset_id=configuration.asset_id,
        source_id=configuration.summary_source_id,
        schema_version=SUMMARY_SCHEMA_VERSION,
        event_time=summary.creation_timestamp,
        received_at=received_at,
        request_url=request_url,
    )


def funding_to_observations(
    point: DeribitFundingPoint,
    raw_record: RawRecord,
    *,
    configuration: DeribitAssetConfiguration,
    normalized_at: datetime,
) -> tuple[NormalizedObservation, ...]:
    """Promote historical interest and prices without conflating snapshot funding."""
    _require_raw(raw_record, configuration, FUNDING_SCHEMA_VERSION)
    fields = (
        (
            "funding_interest_1h",
            point.interest_1h,
            "ratio",
            point.timestamp - timedelta(hours=1),
            point.timestamp,
        ),
        (
            "funding_interest_8h",
            point.interest_8h,
            "ratio",
            point.timestamp - timedelta(hours=8),
            point.timestamp,
        ),
        ("index_price", point.index_price, "USD", None, None),
        ("prev_index_price", point.prev_index_price, "USD", None, None),
    )
    return tuple(
        _observation(
            raw_record,
            field_name=field_name,
            value=value,
            unit=unit,
            frequency=DataFrequency.HOUR_1,
            observed_at=point.timestamp,
            period_start=period_start,
            period_end=period_end,
            normalized_at=normalized_at,
            transformation_version=FUNDING_TRANSFORMATION_VERSION,
        )
        for field_name, value, unit, period_start, period_end in fields
    )


def dvol_to_observations(
    candle: DeribitDvolCandle,
    raw_record: RawRecord,
    *,
    configuration: DeribitAssetConfiguration,
    normalized_at: datetime,
) -> tuple[NormalizedObservation, ...]:
    """Promote exact daily DVOL OHLC values with a half-open candle period."""
    _require_raw(raw_record, configuration, DVOL_SCHEMA_VERSION)
    period_end = candle.start + timedelta(days=1)
    fields = (
        ("dvol_open", candle.open),
        ("dvol_high", candle.high),
        ("dvol_low", candle.low),
        ("dvol_close", candle.close),
    )
    return tuple(
        _observation(
            raw_record,
            field_name=field_name,
            value=value,
            unit="dvol_index_points",
            frequency=DataFrequency.DAY_1,
            observed_at=candle.start,
            period_start=candle.start,
            period_end=period_end,
            normalized_at=normalized_at,
            transformation_version=DVOL_TRANSFORMATION_VERSION,
        )
        for field_name, value in fields
    )


def summary_to_observations(
    summary: DeribitPerpetualSummary,
    raw_record: RawRecord,
    *,
    configuration: DeribitAssetConfiguration,
    normalized_at: datetime,
) -> tuple[NormalizedObservation, ...]:
    """Promote only present summary fields with explicit ``last`` mapping."""
    _require_raw(raw_record, configuration, SUMMARY_SCHEMA_VERSION)
    fields = (
        ("open_interest", summary.open_interest, "USD_contract_units"),
        ("mark_price", summary.mark_price, "USD"),
        ("bid_price", summary.bid_price, "USD"),
        ("ask_price", summary.ask_price, "USD"),
        ("mid_price", summary.mid_price, "USD"),
        ("last_price", summary.last_price, "USD"),
        ("current_funding", summary.current_funding, "ratio"),
        ("funding_8h", summary.funding_8h, "ratio"),
        ("volume_24h", summary.volume_24h, configuration.currency),
        ("volume_usd_24h", summary.volume_usd_24h, "USD"),
        ("price_change_24h", summary.price_change_24h, "percent"),
    )
    return tuple(
        _observation(
            raw_record,
            field_name=field_name,
            value=value,
            unit=unit,
            frequency=DataFrequency.EVENT,
            observed_at=summary.creation_timestamp,
            period_start=None,
            period_end=None,
            normalized_at=normalized_at,
            transformation_version=SUMMARY_TRANSFORMATION_VERSION,
        )
        for field_name, value, unit in fields
        if value is not None
    )


def _raw_record(
    *,
    payload: JsonValue,
    asset_id: str,
    source_id: str,
    schema_version: str,
    event_time: datetime,
    received_at: datetime,
    request_url: str,
) -> RawRecord:
    normalized_received = _utc(received_at, field_name="received_at")
    payload_text = canonical_json_text(payload)
    checksum = hashlib.sha256(payload_text.encode("utf-8")).hexdigest()
    preimage: dict[str, JsonValue] = {
        "asset_id": asset_id,
        "event_time": _utc(event_time, field_name="event_time").isoformat(),
        "payload_sha256": checksum,
        "schema_version": schema_version,
        "source_id": source_id,
    }
    record_id = canonical_identity(preimage)
    return RawRecord(
        record_id=record_id,
        asset_id=asset_id,
        source=SourceReference(
            source_id=source_id,
            record_key=canonical_json_text(preimage),
            retrieved_at=normalized_received,
            raw_uri=request_url,
            checksum_sha256=checksum,
        ),
        event_time=_utc(event_time, field_name="event_time"),
        available_at=normalized_received,
        received_at=normalized_received,
        payload=payload,
        schema_version=schema_version,
    )


def _observation(
    raw_record: RawRecord,
    *,
    field_name: str,
    value: Decimal,
    unit: str,
    frequency: DataFrequency,
    observed_at: datetime,
    period_start: datetime | None,
    period_end: datetime | None,
    normalized_at: datetime,
    transformation_version: str,
) -> NormalizedObservation:
    normalized = _utc(normalized_at, field_name="normalized_at")
    if normalized < raw_record.available_at:
        raise ValueError("normalized_at must not predate first local availability")
    preimage: dict[str, JsonValue] = {
        "field_name": field_name,
        "raw_record_id": str(raw_record.record_id),
        "transformation_version": transformation_version,
    }
    return NormalizedObservation(
        observation_id=canonical_identity(preimage),
        raw_record_id=raw_record.record_id,
        asset_id=raw_record.asset_id or "",
        field_name=field_name,
        value=value,
        unit=unit,
        frequency=frequency,
        observed_at=_utc(observed_at, field_name="observed_at"),
        period_start=(
            _utc(period_start, field_name="period_start") if period_start is not None else None
        ),
        period_end=_utc(period_end, field_name="period_end") if period_end is not None else None,
        available_at=raw_record.received_at,
        normalized_at=normalized,
        source=raw_record.source,
        quality=DataQuality.VALID,
        transformation_version=transformation_version,
    )


def _require_configuration(
    configuration: DeribitAssetConfiguration,
    instrument_name: str,
) -> None:
    if instrument_name != configuration.instrument_name:
        raise ValueError("Deribit provider object does not match the configured instrument")


def _require_raw(
    raw_record: RawRecord,
    configuration: DeribitAssetConfiguration,
    schema_version: str,
) -> None:
    expected_source = {
        FUNDING_SCHEMA_VERSION: configuration.funding_source_id,
        DVOL_SCHEMA_VERSION: configuration.dvol_source_id,
        SUMMARY_SCHEMA_VERSION: configuration.summary_source_id,
    }[schema_version]
    if (
        raw_record.asset_id != configuration.asset_id
        or raw_record.source.source_id != expected_source
        or raw_record.schema_version != schema_version
        or raw_record.available_at != raw_record.received_at
    ):
        raise ValueError("Deribit raw record does not match the normalization contract")


def _utc(value: datetime, *, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must include timezone information")
    return value.astimezone(UTC)


__all__ = [
    "DVOL_SCHEMA_VERSION",
    "DVOL_TRANSFORMATION_VERSION",
    "FUNDING_SCHEMA_VERSION",
    "FUNDING_TRANSFORMATION_VERSION",
    "SUMMARY_SCHEMA_VERSION",
    "SUMMARY_TRANSFORMATION_VERSION",
    "canonical_identity",
    "canonical_json_text",
    "create_deribit_asset",
    "create_deribit_sources",
    "dvol_to_observations",
    "dvol_to_raw_record",
    "funding_to_observations",
    "funding_to_raw_record",
    "summary_to_observations",
    "summary_to_raw_record",
]
