from datetime import UTC, datetime
from uuid import uuid4

from investment_analyst.analytics.cazatiburones.institutional_weight_identity import (
    expected_weight_result_id,
)


def test_weight_identity_is_stable_and_uses_requested_cut() -> None:
    observation_id = uuid4()
    parameters = {"effective_artifact_id": "artifact", "cusip": "037833100"}
    first = expected_weight_result_id(
        asset_id="equity:us:aapl",
        metric_key="weight",
        known_at=datetime(2025, 1, 1, tzinfo=UTC),
        parameters=parameters,
        input_observation_id=observation_id,
    )
    assert first == expected_weight_result_id(
        asset_id="equity:us:aapl",
        metric_key="weight",
        known_at=datetime(2025, 1, 1, tzinfo=UTC),
        parameters=parameters,
        input_observation_id=observation_id,
    )
    assert first != expected_weight_result_id(
        asset_id="equity:us:aapl",
        metric_key="weight",
        known_at=datetime(2025, 1, 2, tzinfo=UTC),
        parameters=parameters,
        input_observation_id=observation_id,
    )
