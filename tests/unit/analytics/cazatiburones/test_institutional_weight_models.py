from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from pydantic import ValidationError

from investment_analyst.analytics.cazatiburones.institutional_weight_models import (
    InstitutionalWeightCandidate,
)
from investment_analyst.core.models.enums import DataQuality


def test_weight_candidate_rejects_float_and_future_input() -> None:
    now = datetime(2025, 1, 1, tzinfo=UTC)
    with pytest.raises(ValidationError):
        InstitutionalWeightCandidate(
            asset_id="equity:us:aapl",
            metric_key="weight",
            value=1.0,
            available_at=now,
            known_at=now,
            input_observation_id=uuid4(),
            parameters={},
            quality=DataQuality.VALID,
        )
    with pytest.raises(ValidationError):
        InstitutionalWeightCandidate(
            asset_id="equity:us:aapl",
            metric_key="weight",
            value=Decimal("1"),
            available_at=datetime(2025, 1, 2, tzinfo=UTC),
            known_at=now,
            input_observation_id=uuid4(),
            parameters={},
            quality=DataQuality.VALID,
        )
