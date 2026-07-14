"""Tests for strict selected SEC fact models."""

from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from pydantic import ValidationError

from investment_analyst.core.models import DataFrequency, DataQuality
from investment_analyst.providers.fundamentals.sec_fact_models import (
    ASSET_ID,
    SEC_FACT_DEFINITIONS,
    SecFilingMetadata,
    SecFundamentalFact,
)

ACCEPTED_AT = datetime(2026, 1, 30, 21, tzinfo=UTC)


def _fact(**overrides: object) -> SecFundamentalFact:
    values: dict[str, object] = {
        "asset_id": ASSET_ID,
        "companyfacts_record_id": uuid4(),
        "submissions_record_id": uuid4(),
        "field_name": "fundamental.revenue",
        "taxonomy": "us-gaap",
        "tag": "RevenueFromContractWithCustomerExcludingAssessedTax",
        "unit": "USD",
        "value": Decimal("1000.25"),
        "accession_number": "0000320193-26-000001",
        "form": "10-K",
        "fiscal_year": 2025,
        "fiscal_period": "FY",
        "period_start": date(2025, 1, 1),
        "period_end": date(2025, 12, 31),
        "filed_date": date(2026, 1, 30),
        "acceptance_at": ACCEPTED_AT,
        "frequency": DataFrequency.ANNUAL,
        "quality": DataQuality.VALID,
    }
    values.update(overrides)
    return SecFundamentalFact.model_validate(values)


def test_five_exact_fact_definitions() -> None:
    assert [
        (item.field_name, item.taxonomy, item.tag, item.unit) for item in SEC_FACT_DEFINITIONS
    ] == [
        (
            "fundamental.revenue",
            "us-gaap",
            "RevenueFromContractWithCustomerExcludingAssessedTax",
            "USD",
        ),
        ("fundamental.net_income", "us-gaap", "NetIncomeLoss", "USD"),
        ("fundamental.assets", "us-gaap", "Assets", "USD"),
        ("fundamental.liabilities", "us-gaap", "Liabilities", "USD"),
        (
            "fundamental.stockholders_equity",
            "us-gaap",
            "StockholdersEquity",
            "USD",
        ),
    ]


@pytest.mark.parametrize("value", [True, 1.25])
def test_fact_rejects_bool_and_float(value: object) -> None:
    with pytest.raises(ValidationError, match="float or bool"):
        _fact(value=value)


@pytest.mark.parametrize("value", [Decimal("NaN"), Decimal("Infinity")])
def test_fact_rejects_non_finite_decimal(value: Decimal) -> None:
    with pytest.raises(ValidationError, match="finite"):
        _fact(value=value)


def test_duration_requires_start_and_rejects_inverted_period() -> None:
    with pytest.raises(ValidationError, match="require period_start"):
        _fact(period_start=None)
    with pytest.raises(ValidationError, match="period_start"):
        _fact(period_start=date(2026, 1, 1))


def test_instant_fact_rejects_start() -> None:
    with pytest.raises(ValidationError, match="must not define period_start"):
        _fact(
            field_name="fundamental.assets",
            tag="Assets",
            period_start=date(2025, 1, 1),
        )


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("asset_id", "equity:us:other", "Apple"),
        ("unit", "EUR", "USD"),
        ("frequency", DataFrequency.DAY_1, "quarterly or annual"),
    ],
)
def test_fact_rejects_scope_violations(field: str, value: object, message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        _fact(**{field: value})


def test_filing_metadata_amendment_and_timezone_validation() -> None:
    amended = SecFilingMetadata(
        accession_number="0000320193-26-000003",
        form="10-Q/A",
        filing_date=date(2026, 5, 1),
        report_date=date(2026, 3, 31),
        acceptance_at=datetime(2026, 5, 1, 20, tzinfo=UTC),
        primary_document="aapl-20260331x10qa.htm",
        is_amendment=True,
    )
    assert amended.is_amendment is True
    with pytest.raises(ValidationError, match="is_amendment"):
        SecFilingMetadata(**{**amended.model_dump(), "is_amendment": False})
    with pytest.raises(ValidationError, match="timezone"):
        SecFilingMetadata(**{**amended.model_dump(), "acceptance_at": datetime(2026, 5, 1)})


def test_fact_serialization_is_json_compatible() -> None:
    payload = _fact().to_json_dict()
    assert payload["value"] == "1000.25"
    assert payload["acceptance_at"] == "2026-01-30T21:00:00+00:00"
    assert _fact().model_dump_json()
