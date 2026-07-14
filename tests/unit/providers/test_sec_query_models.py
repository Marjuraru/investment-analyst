"""Tests for strict SEC point-in-time query models."""

from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

import pytest
from pydantic import ValidationError

from investment_analyst.core.models import DataFrequency
from investment_analyst.providers.fundamentals.sec_query_models import (
    SecFundamentalPeriodView,
    SecFundamentalPointInTimeResult,
    SecFundamentalQuery,
    SecSelectedFundamentalFact,
    allowed_sec_fundamental_fields,
)


def _fact(field_name: str = "fundamental.revenue") -> SecSelectedFundamentalFact:
    period_end = datetime(2025, 9, 27, tzinfo=UTC)
    return SecSelectedFundamentalFact(
        observation_id=uuid4(),
        raw_record_id=uuid4(),
        field_name=field_name,
        value=Decimal("100"),
        unit="USD",
        frequency=DataFrequency.ANNUAL,
        period_start=datetime(2024, 9, 29, tzinfo=UTC),
        period_end=period_end,
        available_at=datetime(2025, 10, 31, 20, tzinfo=UTC),
        normalized_at=datetime(2025, 11, 1, tzinfo=UTC),
        accession_number="0000320193-25-000001",
        taxonomy="us-gaap",
        tag="RevenueFromContractWithCustomerExcludingAssessedTax",
        source_id="sec-edgar:aapl:companyfacts",
        record_key="{}",
        superseded_count=0,
    )


def test_query_normalizes_offset_and_validates_range() -> None:
    query = SecFundamentalQuery(
        known_at=datetime(2026, 1, 1, 3, tzinfo=timezone(timedelta(hours=-5))),
        frequency="annual",
        start_period_end=date(2024, 1, 1),
        end_period_end=date(2025, 12, 31),
        limit=10,
    )

    assert query.known_at == datetime(2026, 1, 1, 8, tzinfo=UTC)
    assert query.frequency is DataFrequency.ANNUAL


@pytest.mark.parametrize(
    "values",
    [
        {"known_at": datetime(2026, 1, 1), "frequency": "annual"},
        {"known_at": datetime(2026, 1, 1, tzinfo=UTC), "frequency": "day_1"},
        {
            "known_at": datetime(2026, 1, 1, tzinfo=UTC),
            "frequency": "annual",
            "start_period_end": date(2026, 1, 2),
            "end_period_end": date(2026, 1, 1),
        },
        {"known_at": datetime(2026, 1, 1, tzinfo=UTC), "frequency": "annual", "limit": 0},
        {
            "known_at": datetime(2026, 1, 1, tzinfo=UTC),
            "frequency": "annual",
            "limit": True,
        },
        {
            "known_at": datetime(2026, 1, 1, tzinfo=UTC),
            "frequency": "annual",
            "limit": 501,
        },
    ],
)
def test_query_rejects_invalid_values(values: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        SecFundamentalQuery.model_validate(values)


def test_selected_fact_preserves_decimal_and_rejects_binary_values() -> None:
    fact = _fact()

    assert fact.value == Decimal("100")
    with pytest.raises(ValidationError):
        _fact().model_copy(update={"value": 1.5}).model_validate(
            {**_fact().model_dump(), "value": 1.5}
        )
    with pytest.raises(ValidationError):
        SecSelectedFundamentalFact.model_validate({**fact.model_dump(), "value": True})


def test_period_and_result_are_deterministic_and_json_serializable() -> None:
    fact = _fact()
    available = (fact.field_name,)
    missing = tuple(sorted(set(allowed_sec_fundamental_fields()) - set(available)))
    period = SecFundamentalPeriodView(
        period_end=fact.period_end,
        frequency=fact.frequency,
        facts=(fact,),
        missing_fields=missing,
        available_fields=available,
        is_complete=False,
        latest_available_at=fact.available_at,
    )
    query = SecFundamentalQuery(
        known_at=datetime(2026, 1, 1, tzinfo=UTC),
        frequency=DataFrequency.ANNUAL,
    )
    result = SecFundamentalPointInTimeResult(
        query=query,
        periods=(period,),
        observations_examined=1,
        observations_eligible=1,
        observations_selected=1,
        observations_superseded=0,
        periods_returned=1,
        earliest_period_end=fact.period_end,
        latest_period_end=fact.period_end,
        latest_period_complete=False,
        traceability_verified=True,
    )

    payload = result.to_json_dict()
    assert payload["periods"][0]["facts"][0]["value"] == "100"
    assert payload["periods"][0]["available_fields"] == ["fundamental.revenue"]


def test_models_reject_extra_fields_and_unsorted_facts() -> None:
    with pytest.raises(ValidationError):
        SecFundamentalQuery(
            known_at=datetime(2026, 1, 1, tzinfo=UTC),
            frequency=DataFrequency.ANNUAL,
            unknown="value",
        )

    revenue = _fact("fundamental.revenue")
    assets = _fact("fundamental.assets").model_copy(
        update={"period_start": None, "observation_id": uuid4()}
    )
    missing = tuple(
        sorted(set(allowed_sec_fundamental_fields()) - {revenue.field_name, assets.field_name})
    )
    with pytest.raises(ValidationError, match="ordered"):
        SecFundamentalPeriodView(
            period_end=revenue.period_end,
            frequency=DataFrequency.ANNUAL,
            facts=(revenue, assets),
            missing_fields=missing,
            available_fields=(revenue.field_name, assets.field_name),
            is_complete=False,
            latest_available_at=revenue.available_at,
        )
