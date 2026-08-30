from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

from investment_analyst.analytics.cazatiburones.activity_event_engine import project_activity_events
from investment_analyst.core.models.enums import DataQuality
from investment_analyst.core.models.metric import MetricResult


def _metric(*, value: str, available_at: datetime) -> MetricResult:
    return MetricResult(
        result_id=uuid4(),
        asset_id="equity:us:aapl",
        metric_key="cazatiburones.insider.holding_delta_ratio",
        value=Decimal(value),
        unit="ratio",
        as_of=available_at,
        available_at=available_at,
        computed_at=available_at,
        parameters={"participant_cik": "0000000001"},
        input_observation_ids=[uuid4(), uuid4()],
        algorithm_version="cazatiburones-activity-metrics-v1",
        quality=DataQuality.VALID,
    )


def test_engine_projects_event_and_cooldown_without_scores() -> None:
    first = _metric(value="1", available_at=datetime(2025, 1, 1, tzinfo=UTC))
    second = _metric(value="2", available_at=datetime(2025, 1, 1, tzinfo=UTC) + timedelta(hours=1))

    evaluations, events, candidates = project_activity_events((first, second))

    assert [item.status for item in evaluations] == ["met", "not_met", "met", "not_met"]
    assert len(events) == 2
    assert candidates[0].status == "eligible"
    assert candidates[1].status == "suppressed"


def test_engine_treats_zero_as_not_met() -> None:
    evaluations, events, candidates = project_activity_events(
        (_metric(value="0", available_at=datetime(2025, 1, 1, tzinfo=UTC)),)
    )

    assert {item.status for item in evaluations} == {"not_met"}
    assert events == ()
    assert candidates == ()
