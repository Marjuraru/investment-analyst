from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

from investment_analyst.analytics.cazatiburones.institutional_weight_engine import calculate
from investment_analyst.core.models import NormalizedObservation, SourceReference
from investment_analyst.core.models.enums import DataFrequency, DataQuality


def _observation(field_name: str, value: Decimal) -> NormalizedObservation:
    now = datetime(2025, 1, 1, tzinfo=UTC)
    return NormalizedObservation(
        observation_id=uuid4(),
        raw_record_id=uuid4(),
        asset_id="equity:us:aapl",
        field_name=field_name,
        value=value,
        unit="USD",
        frequency=DataFrequency.QUARTERLY,
        period_end=now,
        available_at=now,
        normalized_at=now,
        source=SourceReference(source_id="test", retrieved_at=now),
        quality=DataQuality.VALID,
        transformation_version="test-v1",
    )


def test_engine_uses_declared_field_and_never_treats_missing_option_as_zero() -> None:
    result = calculate(
        asset_id="equity:us:aapl",
        manager_cik="0001067983",
        report_period="2024-12-31",
        known_at=datetime(2025, 2, 1, tzinfo=UTC),
        artifact_id="artifact",
        accession="accession",
        status="original_complete",
        total=Decimal("40"),
        total_quality=DataQuality.VALID,
        observations=(_observation("institutional_reported_fair_value", Decimal("10")),),
        lineage={"row_id": "row", "cusip": "037833100", "title_of_class": "COM", "put_call": None},
    )
    assert result.candidates[0].value == Decimal("0.25")
    assert result.candidates[0].metric_key.endswith("declared_position_value_weight")
    assert {item.reason for item in result.skipped} == {"missing_field"}


def test_engine_rejects_missing_zero_and_duplicate_positions_without_defaulting() -> None:
    arguments = {
        "asset_id": "equity:us:aapl",
        "manager_cik": "0001067983",
        "report_period": "2024-12-31",
        "known_at": datetime(2025, 2, 1, tzinfo=UTC),
        "artifact_id": "artifact",
        "accession": "accession",
        "status": "original_complete",
        "total_quality": DataQuality.VALID,
        "lineage": {
            "row_id": "row",
            "cusip": "037833100",
            "title_of_class": "COM",
            "put_call": None,
        },
    }
    missing_total = calculate(total=None, observations=(), **arguments)
    assert {item.reason for item in missing_total.skipped} == {"missing_total"}
    zero_total = calculate(total=Decimal("0"), observations=(), **arguments)
    assert {item.reason for item in zero_total.skipped} == {"zero_total"}
    duplicate = calculate(
        total=Decimal("40"),
        observations=(
            _observation("institutional_reported_fair_value", Decimal("10")),
            _observation("institutional_reported_fair_value", Decimal("10")),
        ),
        **arguments,
    )
    assert {item.reason for item in duplicate.skipped} == {"duplicate_position", "missing_field"}


def test_engine_keeps_option_and_common_value_fields_separate() -> None:
    result = calculate(
        asset_id="equity:us:aapl",
        manager_cik="0001067983",
        report_period="2024-12-31",
        known_at=datetime(2025, 2, 1, tzinfo=UTC),
        artifact_id="artifact",
        accession="accession",
        status="amended",
        total=Decimal("40"),
        total_quality=DataQuality.VALID,
        observations=(
            _observation("institutional_reported_fair_value", Decimal("10")),
            _observation("institutional_option_fair_value", Decimal("5")),
        ),
        lineage={
            "row_id": "row",
            "cusip": "037833100",
            "title_of_class": "COM",
            "put_call": "CALL",
        },
    )
    assert {item.metric_key: item.value for item in result.candidates} == {
        "cazatiburones.institutional.declared_position_value_weight": Decimal("0.25"),
        "cazatiburones.institutional.declared_option_position_value_weight": Decimal("0.125"),
    }
