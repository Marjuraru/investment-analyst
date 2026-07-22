"""Contract tests for transparent fundamental research metrics."""

from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

import pytest
from pydantic import ValidationError

from investment_analyst.analytics.fundamentals.research_models import (
    FUNDAMENTAL_RESEARCH_METRIC_COUNT,
    FUNDAMENTAL_RESEARCH_METRIC_DEFINITIONS,
    AaplFundamentalResearchRequest,
    FundamentalResearchMetricInput,
    get_fundamental_research_metric_definition,
)
from investment_analyst.core.models import DataFrequency


def test_metric_catalog_is_ordered_versioned_and_independent() -> None:
    keys = tuple(definition.metric_key for definition in FUNDAMENTAL_RESEARCH_METRIC_DEFINITIONS)

    assert FUNDAMENTAL_RESEARCH_METRIC_COUNT == 40
    assert len(keys) == FUNDAMENTAL_RESEARCH_METRIC_COUNT
    assert keys == tuple(sorted(keys))
    assert len(keys) == len(set(keys))
    assert all(key.startswith("fundamental.research.") for key in keys)
    assert all(
        definition.algorithm_version.endswith("decimal34")
        for definition in FUNDAMENTAL_RESEARCH_METRIC_DEFINITIONS
    )
    assert not any("score" in key or "recommend" in key for key in keys)


def test_unknown_metric_and_invalid_request_are_rejected() -> None:
    with pytest.raises(ValueError, match="unsupported fundamental research metric"):
        get_fundamental_research_metric_definition("fundamental.research.unknown")

    with pytest.raises(ValidationError, match="annual or quarterly"):
        AaplFundamentalResearchRequest(
            known_at=datetime(2026, 1, 1, tzinfo=UTC),
            frequency=DataFrequency.DAY_1,
        )

    with pytest.raises(ValidationError, match="must not be later"):
        AaplFundamentalResearchRequest(
            known_at=datetime(2026, 1, 1, tzinfo=UTC),
            frequency=DataFrequency.ANNUAL,
            start_period_end="2025-09-28",
            end_period_end="2025-09-27",
        )


@pytest.mark.parametrize("value", [1.5, True])
def test_financial_metric_inputs_reject_binary_float_and_bool(value: object) -> None:
    with pytest.raises(ValidationError, match="Decimal"):
        FundamentalResearchMetricInput(
            role="revenue",
            field_name="fundamental.revenue",
            observation_id=UUID("00000000-0000-0000-0000-000000000001"),
            value=value,
            available_at=datetime(2025, 10, 31, tzinfo=UTC),
        )


def test_financial_metric_input_serializes_decimal_exactly() -> None:
    metric_input = FundamentalResearchMetricInput(
        role="revenue",
        field_name="fundamental.revenue",
        observation_id=UUID("00000000-0000-0000-0000-000000000001"),
        value=Decimal("123.4500"),
        available_at=datetime(2025, 10, 31, tzinfo=UTC),
    )

    assert metric_input.value == Decimal("123.4500")
