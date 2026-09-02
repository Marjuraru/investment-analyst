from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from investment_analyst.analytics.cazatiburones.institutional_metric_models import (
    InstitutionalMetricCandidate,
)


def test_candidate_rejects_float_values() -> None:
    with pytest.raises(ValidationError):
        InstitutionalMetricCandidate(
            asset_id="equity:us:aapl",
            metric_key="x",
            value=0.1,
            unit="shares",
            as_of=datetime(2025, 1, 1, tzinfo=UTC),
            available_at=datetime(2025, 1, 1, tzinfo=UTC),
            known_at=datetime(2025, 1, 1, tzinfo=UTC),
            parameters={},
            input_observation_ids=(uuid4(), uuid4()),
            quality="valid",
        )
