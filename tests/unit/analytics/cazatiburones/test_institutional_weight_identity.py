from datetime import UTC, datetime
from uuid import uuid4

from investment_analyst.analytics.cazatiburones.institutional_weight_identity import (
    expected_weight_result_id,
    semantic_weight_result_id,
)
from investment_analyst.core.models.enums import DataQuality


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


def test_semantic_weight_identity_excludes_compatible_cut_but_keeps_evidence() -> None:
    observation_id = uuid4()
    parameters = {"effective_artifact_id": "artifact", "cusip": "037833100"}
    coordinates = {
        "asset_id": "equity:us:aapl",
        "metric_key": "weight",
        "as_of": datetime(2025, 1, 1, tzinfo=UTC),
        "available_at": datetime(2025, 1, 1, tzinfo=UTC),
        "quality": DataQuality.VALID,
        "algorithm_version": "cazatiburones-institutional-weight-v1",
        "parameters": parameters,
        "input_observation_id": observation_id,
    }
    first = semantic_weight_result_id(**coordinates, known_at=datetime(2025, 1, 2, tzinfo=UTC))
    later_cut = semantic_weight_result_id(
        **coordinates,
        known_at=datetime(2025, 2, 1, tzinfo=UTC),
    )
    revised_input = semantic_weight_result_id(
        **{**coordinates, "input_observation_id": uuid4()},
        known_at=datetime(2025, 2, 1, tzinfo=UTC),
    )

    assert first.version == 8
    assert first == later_cut
    assert first != revised_input
