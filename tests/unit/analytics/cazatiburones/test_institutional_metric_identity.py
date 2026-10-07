from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

from investment_analyst.analytics.cazatiburones.institutional_metric_identity import (
    expected_institutional_metric_result_id,
    semantic_institutional_metric_result_id,
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


def test_semantic_identity_excludes_compatible_cut_but_keeps_input_observations() -> None:
    at = datetime(2025, 1, 1, tzinfo=UTC)
    input_ids = (uuid4(), uuid4())
    candidate = InstitutionalMetricCandidate(
        asset_id="equity:us:aapl",
        metric_key="x",
        value=Decimal("1"),
        unit="shares",
        as_of=at,
        available_at=at,
        known_at=at,
        parameters={"manager_cik": "0001067983"},
        input_observation_ids=input_ids,
        quality=DataQuality.VALID,
    )
    later_cut = candidate.model_copy(update={"known_at": datetime(2025, 2, 1, tzinfo=UTC)})
    revised_input = candidate.model_copy(update={"input_observation_ids": (input_ids[0], uuid4())})

    assert semantic_institutional_metric_result_id(candidate).version == 8
    assert semantic_institutional_metric_result_id(
        candidate
    ) == semantic_institutional_metric_result_id(later_cut)
    assert semantic_institutional_metric_result_id(
        candidate
    ) != semantic_institutional_metric_result_id(revised_input)
