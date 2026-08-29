import pytest
from pydantic import ValidationError

from investment_analyst.analytics.cazatiburones.institutional_change_models import DescriptiveMetric


def test_missing_metric_cannot_be_represented_as_zero() -> None:
    with pytest.raises(ValidationError):
        DescriptiveMetric(key="delta_value", status="missing", value=0)
