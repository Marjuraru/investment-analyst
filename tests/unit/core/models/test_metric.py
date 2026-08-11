"""Tests for metric definitions and results."""

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from pydantic import ValidationError

from investment_analyst.core.models import (
    DataQuality,
    MetricCategory,
    MetricDefinition,
    MetricResult,
)


def test_metric_definition_and_result_are_serializable() -> None:
    definition = MetricDefinition(
        metric_key="market.simple_return",
        display_name="Simple Return",
        category=MetricCategory.MARKET,
        description="Price change relative to the previous observation.",
        formula="(current_price / previous_price) - 1",
        unit="ratio",
        default_parameters={"periods": 1},
        limitations=["Does not include distributions."],
        references=["Internal metric specification."],
        definition_version="1.0.0",
    )
    observation_id = uuid4()
    result = MetricResult(
        asset_id="equity:us:aapl",
        metric_key=definition.metric_key,
        value=Decimal("0.0125"),
        unit="ratio",
        as_of=datetime(2026, 7, 10, tzinfo=UTC),
        available_at=datetime(2026, 7, 10, 16, 1, tzinfo=UTC),
        computed_at=datetime(2026, 7, 10, 16, 2, tzinfo=UTC),
        parameters={"periods": 1},
        input_observation_ids=[observation_id],
        algorithm_version="1.0.0",
        quality=DataQuality.VALID,
    )

    serialized = result.model_dump_json()

    assert definition.metric_key == "market.simple_return"
    assert str(result.result_id) in serialized
    assert str(observation_id) in serialized
    assert '"value":"0.0125"' in serialized


def test_metric_result_requires_input_observations() -> None:
    with pytest.raises(ValidationError, match="input_observation_id"):
        MetricResult(
            asset_id="equity:us:aapl",
            metric_key="market.simple_return",
            value=Decimal("0.0125"),
            unit="ratio",
            as_of=datetime(2026, 7, 10, tzinfo=UTC),
            available_at=datetime(2026, 7, 10, 16, 1, tzinfo=UTC),
            computed_at=datetime(2026, 7, 10, 16, 2, tzinfo=UTC),
            input_observation_ids=[],
            algorithm_version="1.0.0",
            quality=DataQuality.VALID,
        )


def test_metric_result_defaults_and_validates_derived_lineage() -> None:
    observation_id = uuid4()
    result = MetricResult(
        asset_id="equity:us:aapl",
        metric_key="market.technical.ema",
        value=Decimal("200"),
        unit="USD",
        as_of=datetime(2026, 7, 10, tzinfo=UTC),
        available_at=datetime(2026, 7, 10, 16, 1, tzinfo=UTC),
        computed_at=datetime(2026, 7, 10, 16, 2, tzinfo=UTC),
        input_observation_ids=[observation_id],
        algorithm_version="market-ema-v1-decimal34",
        quality=DataQuality.VALID,
    )

    assert result.input_metric_result_ids == []
    legacy_document = result.model_dump(mode="python")
    legacy_document.pop("input_metric_result_ids")
    assert MetricResult.model_validate(legacy_document).input_metric_result_ids == []
    duplicate_dependency = uuid4()
    with pytest.raises(ValidationError, match="input_metric_result_ids must be unique"):
        MetricResult.model_validate(
            result.model_dump(
                mode="python",
                exclude={"input_metric_result_ids"},
            )
            | {"input_metric_result_ids": [duplicate_dependency, duplicate_dependency]}
        )
