"""Tests for strict Apple SEC fundamental metric models."""

from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

import pytest
from pydantic import ValidationError

from investment_analyst.core.models import DataFrequency, DataQuality
from investment_analyst.providers.fundamentals.sec_metric_models import (
    SEC_FUNDAMENTAL_METRIC_DEFINITIONS,
    SecFundamentalMetricCandidate,
    SecFundamentalMetricInput,
    SecFundamentalMetricRequest,
    SecMetricComparison,
)


def _candidate(**overrides: object) -> SecFundamentalMetricCandidate:
    values: dict[str, object] = {
        "asset_id": "equity:us:aapl",
        "metric_name": "fundamental.net_margin",
        "value": Decimal("0.25"),
        "unit": "ratio",
        "frequency": DataFrequency.ANNUAL,
        "period_end": datetime(2025, 9, 27, tzinfo=UTC),
        "available_at": datetime(2025, 10, 31, tzinfo=UTC),
        "input_roles": (
            SecFundamentalMetricInput(
                role="current_net_income",
                observation_id=uuid4(),
            ),
            SecFundamentalMetricInput(
                role="current_revenue",
                observation_id=uuid4(),
            ),
        ),
        "formula": "net_income / revenue",
        "algorithm_version": "sec-fundamental-net-margin-v1-decimal34",
        "comparison": SecMetricComparison.SAME_PERIOD,
        "fiscal_year": "2025",
        "fiscal_period": "FY",
        "quality": DataQuality.VALID,
    }
    values.update(overrides)
    return SecFundamentalMetricCandidate.model_validate(values)


def test_request_normalizes_offset_and_accepts_range() -> None:
    request = SecFundamentalMetricRequest(
        known_at=datetime(2026, 1, 1, 3, tzinfo=timezone(timedelta(hours=-5))),
        frequency=DataFrequency.QUARTERLY,
        start_period_end=date(2025, 1, 1),
        end_period_end=date(2025, 12, 31),
        limit=8,
    )

    assert request.known_at == datetime(2026, 1, 1, 8, tzinfo=UTC)
    assert request.limit == 8


@pytest.mark.parametrize(
    "values",
    [
        {"known_at": datetime(2026, 1, 1), "frequency": DataFrequency.ANNUAL},
        {
            "known_at": datetime(2026, 1, 1, tzinfo=UTC),
            "frequency": DataFrequency.DAY_1,
        },
        {
            "asset_id": "equity:us:msft",
            "known_at": datetime(2026, 1, 1, tzinfo=UTC),
            "frequency": DataFrequency.ANNUAL,
        },
        {
            "known_at": datetime(2026, 1, 1, tzinfo=UTC),
            "frequency": DataFrequency.ANNUAL,
            "start_period_end": date(2025, 12, 31),
            "end_period_end": date(2025, 1, 1),
        },
        {
            "known_at": datetime(2026, 1, 1, tzinfo=UTC),
            "frequency": DataFrequency.ANNUAL,
            "limit": 0,
        },
        {
            "known_at": datetime(2026, 1, 1, tzinfo=UTC),
            "frequency": DataFrequency.ANNUAL,
            "limit": True,
        },
        {
            "known_at": datetime(2026, 1, 1, tzinfo=UTC),
            "frequency": DataFrequency.ANNUAL,
            "limit": 101,
        },
    ],
)
def test_request_rejects_invalid_values(values: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        SecFundamentalMetricRequest.model_validate(values)


def test_request_rejects_extra_fields() -> None:
    with pytest.raises(ValidationError, match="Extra inputs"):
        SecFundamentalMetricRequest(
            known_at=datetime(2026, 1, 1, tzinfo=UTC),
            frequency=DataFrequency.ANNUAL,
            unexpected="value",
        )


def test_exact_definitions_and_formulas_are_deterministic() -> None:
    assert tuple(item.metric_name for item in SEC_FUNDAMENTAL_METRIC_DEFINITIONS) == (
        "fundamental.net_margin",
        "fundamental.liabilities_to_assets",
        "fundamental.liabilities_to_equity",
        "fundamental.revenue_yoy_growth",
        "fundamental.net_income_yoy_change_rate",
    )
    assert tuple(item.formula for item in SEC_FUNDAMENTAL_METRIC_DEFINITIONS) == (
        "net_income / revenue",
        "liabilities / assets",
        "liabilities / stockholders_equity",
        "current_revenue / previous_revenue - 1",
        "(current_net_income - previous_net_income) / abs(previous_net_income)",
    )
    assert all(item.unit == "ratio" for item in SEC_FUNDAMENTAL_METRIC_DEFINITIONS)


@pytest.mark.parametrize("value", [0.25, True, "NaN", "Infinity", "-Infinity"])
def test_candidate_rejects_non_decimal_or_non_finite_value(value: object) -> None:
    with pytest.raises(ValidationError):
        _candidate(value=value)


def test_candidate_preserves_decimal_and_serializes_explicitly() -> None:
    candidate = _candidate(value=Decimal("0.2500"))

    assert isinstance(candidate.value, Decimal)
    assert candidate.to_json_dict()["value"] == "0.2500"
    assert candidate.input_observation_ids() == tuple(
        item.observation_id for item in candidate.input_roles
    )


def test_candidate_requires_deterministic_role_order() -> None:
    with pytest.raises(ValidationError, match="ordered by role"):
        _candidate(input_roles=tuple(reversed(_candidate().input_roles)))
