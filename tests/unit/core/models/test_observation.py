"""Tests for raw records, source references, and normalized observations."""

from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

import pytest
from pydantic import ValidationError

from investment_analyst.core.models import (
    DataFrequency,
    DataQuality,
    NormalizedObservation,
    RawRecord,
    SourceReference,
)


def make_source(retrieved_at: datetime | None = None) -> SourceReference:
    return SourceReference(
        source_id="alpaca:bars",
        record_key="AAPL:2026-07-10",
        retrieved_at=retrieved_at or datetime(2026, 7, 11, tzinfo=UTC),
        checksum_sha256="a" * 64,
    )


def test_rejects_naive_datetime() -> None:
    with pytest.raises(ValidationError, match="timezone"):
        make_source(datetime(2026, 7, 11))


def test_converts_offset_timestamp_to_utc() -> None:
    offset = timezone(timedelta(hours=-5))
    source = make_source(datetime(2026, 7, 11, 8, 30, tzinfo=offset))

    assert source.retrieved_at == datetime(2026, 7, 11, 13, 30, tzinfo=UTC)
    assert source.retrieved_at.tzinfo is UTC


def test_source_reference_validates_sha256_checksum() -> None:
    with pytest.raises(ValidationError, match="string_pattern_mismatch"):
        SourceReference(
            source_id="alpaca:bars",
            retrieved_at=datetime(2026, 7, 11, tzinfo=UTC),
            checksum_sha256="not-a-sha256",
        )


def test_raw_record_rejects_availability_after_receipt() -> None:
    with pytest.raises(ValidationError, match="available_at"):
        RawRecord(
            source=make_source(),
            available_at=datetime(2026, 7, 11, 12, tzinfo=UTC),
            received_at=datetime(2026, 7, 11, 11, tzinfo=UTC),
            payload={"close": "210.50"},
            schema_version="1",
        )


def test_normalized_observation_rejects_inverted_period() -> None:
    with pytest.raises(ValidationError, match="period_start"):
        NormalizedObservation(
            raw_record_id=uuid4(),
            asset_id="equity:us:aapl",
            field_name="revenue",
            value=Decimal("1000.00"),
            unit="USD",
            frequency=DataFrequency.QUARTERLY,
            period_start=datetime(2026, 4, 1, tzinfo=UTC),
            period_end=datetime(2026, 3, 31, tzinfo=UTC),
            available_at=datetime(2026, 5, 1, tzinfo=UTC),
            normalized_at=datetime(2026, 5, 1, 1, tzinfo=UTC),
            source=make_source(),
            quality=DataQuality.VALID,
            transformation_version="1.0.0",
        )


def test_observation_serializes_decimal_uuid_and_datetime_to_json() -> None:
    raw_record_id = uuid4()
    observation = NormalizedObservation(
        raw_record_id=raw_record_id,
        asset_id="equity:us:aapl",
        field_name="close",
        value=Decimal("210.50"),
        unit="USD",
        frequency=DataFrequency.DAY_1,
        observed_at=datetime(2026, 7, 10, 16, tzinfo=UTC),
        available_at=datetime(2026, 7, 10, 16, 1, tzinfo=UTC),
        normalized_at=datetime(2026, 7, 10, 16, 2, tzinfo=UTC),
        source=make_source(),
        quality=DataQuality.VALID,
        transformation_version="1.0.0",
    )

    serialized = observation.model_dump_json()

    assert str(observation.observation_id) in serialized
    assert str(raw_record_id) in serialized
    assert '"value":"210.50"' in serialized
    assert "2026-07-10T16:00:00Z" in serialized
