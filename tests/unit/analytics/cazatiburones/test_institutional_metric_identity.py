from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

from investment_analyst.analytics.cazatiburones.institutional_metric_identity import (
    expected_institutional_metric_result_id,
)
from investment_analyst.analytics.cazatiburones.institutional_metric_models import (
    InstitutionalMetricCandidate,
)
from investment_analyst.core.models.enums import DataQuality


def test_identity_excludes_value_and_computed_at() -> None:
    candidate = InstitutionalMetricCandidate(
        asset_id="equity:us:aapl",
        metric_key="x",
        value=Decimal("1"),
        unit="shares",
        as_of=datetime(2025, 1, 1, tzinfo=UTC),
        available_at=datetime(2025, 1, 1, tzinfo=UTC),
        known_at=datetime(2025, 1, 1, tzinfo=UTC),
        parameters={},
        input_observation_ids=(uuid4(), uuid4()),
        quality=DataQuality.VALID,
    )
    assert expected_institutional_metric_result_id(
        candidate
    ) == expected_institutional_metric_result_id(
        candidate.model_copy(update={"value": Decimal("2")})
    )
