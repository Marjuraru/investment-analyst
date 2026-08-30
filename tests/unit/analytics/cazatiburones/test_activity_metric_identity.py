from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

from investment_analyst.analytics.cazatiburones.activity_metric_identity import (
    expected_activity_metric_result_id,
)
from investment_analyst.analytics.cazatiburones.activity_metric_models import (
    ActivityMetricCandidate,
)
from investment_analyst.core.models.enums import DataQuality

_AT = datetime(2025, 1, 1, tzinfo=UTC)


def _candidate(**overrides: object) -> ActivityMetricCandidate:
    fields: dict[str, object] = {
        "asset_id": "equity:us:aapl",
        "metric_key": "cazatiburones.insider.holding_delta_ratio",
        "value": Decimal("0.25"),
        "unit": "ratio",
        "as_of": _AT,
        "available_at": _AT,
        "known_at": _AT,
        "parameters": {"family": "insider", "participant_cik": "0000000001"},
        "input_observation_ids": (uuid4(), uuid4()),
        "algorithm_version": "cazatiburones-activity-metrics-v1",
        "quality": DataQuality.VALID,
    }
    fields.update(overrides)
    return ActivityMetricCandidate(**fields)


def test_identity_is_deterministic_for_identical_inputs() -> None:
    ids = (uuid4(), uuid4())
    first = _candidate(input_observation_ids=ids)
    second = _candidate(input_observation_ids=ids)
    assert expected_activity_metric_result_id(first) == expected_activity_metric_result_id(second)


def test_identity_excludes_the_computed_value() -> None:
    ids = (uuid4(), uuid4())
    first = _candidate(input_observation_ids=ids, value=Decimal("0.25"))
    second = _candidate(input_observation_ids=ids, value=Decimal("0.99"))
    assert expected_activity_metric_result_id(first) == expected_activity_metric_result_id(second)


def test_identity_changes_with_known_at() -> None:
    ids = (uuid4(), uuid4())
    first = _candidate(input_observation_ids=ids, known_at=_AT)
    second = _candidate(
        input_observation_ids=ids,
        known_at=datetime(2025, 6, 1, tzinfo=UTC),
        available_at=_AT,
    )
    assert expected_activity_metric_result_id(first) != expected_activity_metric_result_id(second)


def test_identity_changes_with_input_observation_ids() -> None:
    first = _candidate(input_observation_ids=(uuid4(), uuid4()))
    second = _candidate(input_observation_ids=(uuid4(), uuid4()))
    assert expected_activity_metric_result_id(first) != expected_activity_metric_result_id(second)


def test_identity_changes_with_parameters() -> None:
    ids = (uuid4(), uuid4())
    first = _candidate(
        input_observation_ids=ids, parameters={"family": "insider", "participant_cik": "0000000001"}
    )
    second = _candidate(
        input_observation_ids=ids, parameters={"family": "insider", "participant_cik": "0000000002"}
    )
    assert expected_activity_metric_result_id(first) != expected_activity_metric_result_id(second)
