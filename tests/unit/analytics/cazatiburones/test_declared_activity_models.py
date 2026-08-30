from decimal import Decimal

import pytest
from pydantic import ValidationError

from investment_analyst.analytics.cazatiburones.declared_activity_models import (
    DeclaredActivityFeatureSet,
)
from investment_analyst.analytics.cazatiburones.institutional_change_models import DescriptiveMetric


def test_descriptive_metric_rejects_float_and_missing_available_value() -> None:
    with pytest.raises(ValidationError):
        DescriptiveMetric(key="x", status="available", value=1.0)
    with pytest.raises(ValidationError):
        DescriptiveMetric(key="x", status="available")
    assert DescriptiveMetric(key="x", status="available", value=Decimal("1")).value == Decimal("1")


def test_result_contract_has_no_score_or_recommendation_fields() -> None:
    assert {"score", "verdict", "confidence", "recommendation"}.isdisjoint(
        DeclaredActivityFeatureSet.model_fields
    )
