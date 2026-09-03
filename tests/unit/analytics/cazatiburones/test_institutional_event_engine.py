"""Unit tests for pure institutional event evaluation and cooldown projection."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

from investment_analyst.analytics.cazatiburones.institutional_event_engine import (
    project_institutional_events,
)
from investment_analyst.core.models.enums import DataQuality
from investment_analyst.core.models.metric import MetricResult


def _create_metric(
    *,
    metric_key: str = "cazatiburones.institutional.delta_reported_shares",
    value: Decimal = Decimal("1000"),
    unit: str = "shares",
    available_at: datetime | None = None,
    quality: DataQuality = DataQuality.VALID,
    manager_cik: str = "0001350694",
    cusip: str = "037833100",
    title_of_class: str = "COM",
    put_call: str | None = None,
    report_period: str = "2024-09-30",
    prior_report_period: str = "2024-06-30",
) -> MetricResult:
    dt = available_at or datetime(2024, 11, 14, 16, 0, 0, tzinfo=UTC)
    obs_ids = [uuid4(), uuid4()]
    return MetricResult(
        result_id=uuid4(),
        asset_id="equity:us:aapl",
        metric_key=metric_key,
        value=value,
        unit=unit,
        as_of=dt,
        available_at=dt,
        computed_at=dt,
        parameters={
            "manager_cik": manager_cik,
            "cusip": cusip,
            "title_of_class": title_of_class,
            "put_call": put_call,
            "report_period": report_period,
            "prior_report_period": prior_report_period,
        },
        input_observation_ids=obs_ids,
        algorithm_version="cazatiburones-institutional-metrics-v1",
        quality=quality,
    )


def test_point_in_time_available_at_and_known_at() -> None:
    t1 = datetime(2024, 11, 14, 10, 0, 0, tzinfo=UTC)
    t2 = datetime(2024, 11, 14, 18, 0, 0, tzinfo=UTC)

    metric1 = _create_metric(value=Decimal("500"), available_at=t1)
    metric2 = _create_metric(
        value=Decimal("800"),
        available_at=t2,
        report_period="2024-12-31",
        prior_report_period="2024-09-30",
    )

    evaluations, events, candidates = project_institutional_events((metric2, metric1))

    # Output ordering is strictly deterministic by available_at then event_id
    assert len(events) == 2
    assert events[0].available_at == t1
    assert events[1].available_at == t2
    assert events[0].metric_result_id == metric1.result_id
    assert events[1].metric_result_id == metric2.result_id


def test_decimal_exactness_and_missing_not_zero() -> None:
    # Decimal exactness preserved without float conversion
    exact_value = Decimal("1234567.890123456789012345")
    metric = _create_metric(
        metric_key="cazatiburones.institutional.reported_shares_delta_ratio",
        value=exact_value,
        unit="ratio",
    )

    evaluations, events, candidates = project_institutional_events((metric,))

    assert len(events) == 1
    assert isinstance(events[0].value, Decimal)
    assert events[0].value == exact_value
    assert not isinstance(events[0].value, float)

    # Absence of metric is never converted to zero; empty input produces empty results
    empty_evals, empty_events, empty_candidates = project_institutional_events(())
    assert empty_evals == ()
    assert empty_events == ()
    assert empty_candidates == ()


def test_event_traceable_to_metric_result_and_two_observations() -> None:
    metric = _create_metric(value=Decimal("1500"))
    evaluations, events, candidates = project_institutional_events((metric,))

    assert len(events) == 1
    event = events[0]
    assert event.metric_result_id == metric.result_id
    assert event.metric_key == metric.metric_key
    assert event.algorithm_version == metric.algorithm_version
    assert event.unit == metric.unit
    assert event.available_at == metric.available_at
    assert len(event.input_observation_ids) == 2
    assert event.input_observation_ids == tuple(metric.input_observation_ids)
    assert event.parameters["manager_cik"] == "0001350694"


def test_non_valid_quality_is_not_evaluable() -> None:
    metric_partial = _create_metric(value=Decimal("1000"), quality=DataQuality.PARTIAL)
    metric_invalid = _create_metric(value=Decimal("1000"), quality=DataQuality.SUSPECT)

    evaluations, events, candidates = project_institutional_events((metric_partial, metric_invalid))

    assert len(events) == 0
    assert len(candidates) == 0
    assert len(evaluations) == 4  # 2 rules per metric * 2 metrics
    for evaluation in evaluations:
        assert evaluation.status == "not_evaluable"
        assert evaluation.reason == "quality_not_valid"
        assert evaluation.value is None


def test_zero_or_opposite_direction_is_not_met() -> None:
    metric_zero = _create_metric(value=Decimal("0"))
    evaluations, events, candidates = project_institutional_events((metric_zero,))

    assert len(events) == 0
    assert len(candidates) == 0
    # Both increased and reduced are not_met
    assert len(evaluations) == 2
    for evaluation in evaluations:
        assert evaluation.status == "not_met"
        assert evaluation.reason == "declared_zero_or_opposite_direction"
        assert evaluation.value is None

    # Positive value: increased is met, reduced is not_met
    metric_pos = _create_metric(value=Decimal("500"))
    evaluations_pos, events_pos, candidates_pos = project_institutional_events((metric_pos,))
    assert len(events_pos) == 1
    status_by_rule = {item.rule_id: item.status for item in evaluations_pos}
    assert status_by_rule["institutional-delta-reported-shares-increased"] == "met"
    assert status_by_rule["institutional-delta-reported-shares-reduced"] == "not_met"


def test_cooldown_groups_by_manager_and_declared_position() -> None:
    t0 = datetime(2025, 1, 1, 12, 0, 0, tzinfo=UTC)
    t_within = t0 + timedelta(hours=6)  # within 86400s
    t_after = t0 + timedelta(days=2)  # after 86400s

    # Event 1: base position
    m1 = _create_metric(value=Decimal("100"), available_at=t0)
    # Event 2: same position within cooldown -> suppressed
    m2 = _create_metric(
        value=Decimal("200"),
        available_at=t_within,
        report_period="2025-03-31",
        prior_report_period="2024-12-31",
    )
    # Event 3: different position (different cusip) at same time -> eligible
    m3_diff_pos = _create_metric(
        value=Decimal("300"),
        available_at=t_within,
        cusip="999999999",
        report_period="2025-03-31",
        prior_report_period="2024-12-31",
    )
    # Event 4: different manager at same time -> eligible
    m4_diff_mgr = _create_metric(
        value=Decimal("400"),
        available_at=t_within,
        manager_cik="0009999999",
        report_period="2025-03-31",
        prior_report_period="2024-12-31",
    )
    # Event 5: same position after cooldown window -> eligible
    m5_after = _create_metric(
        value=Decimal("500"),
        available_at=t_after,
        report_period="2025-06-30",
        prior_report_period="2025-03-31",
    )

    evals, events, candidates = project_institutional_events(
        (m1, m2, m3_diff_pos, m4_diff_mgr, m5_after)
    )

    status_by_metric_id = {
        event.metric_result_id: cand.status for event, cand in zip(events, candidates, strict=True)
    }

    assert status_by_metric_id[m1.result_id] == "eligible"
    assert status_by_metric_id[m2.result_id] == "suppressed"
    assert status_by_metric_id[m3_diff_pos.result_id] == "eligible"
    assert status_by_metric_id[m4_diff_mgr.result_id] == "eligible"
    assert status_by_metric_id[m5_after.result_id] == "eligible"


def test_suppressed_candidate_carries_cooldown_evidence() -> None:
    t0 = datetime(2025, 1, 1, 12, 0, 0, tzinfo=UTC)
    t_suppressed = t0 + timedelta(hours=2)

    m1 = _create_metric(value=Decimal("100"), available_at=t0)
    m2 = _create_metric(
        value=Decimal("200"),
        available_at=t_suppressed,
        report_period="2025-03-31",
        prior_report_period="2024-12-31",
    )

    evals, events, candidates = project_institutional_events((m1, m2))

    assert candidates[0].status == "eligible"
    assert candidates[0].cooldown_until is None
    assert candidates[0].suppressed_by_event_id is None

    suppressed = candidates[1]
    assert suppressed.status == "suppressed"
    assert suppressed.cooldown_until == t0 + timedelta(seconds=86_400)
    assert suppressed.suppressed_by_event_id == events[0].event_id


def test_no_score_verdict_ranking_or_percentile_emitted() -> None:
    metric = _create_metric(value=Decimal("100"))
    evals, events, candidates = project_institutional_events((metric,))

    event_dict = events[0].model_dump()
    candidate_dict = candidates[0].model_dump()

    forbidden_keys = {
        "score",
        "verdict",
        "ranking",
        "percentile",
        "recommendation",
        "signal",
        "alert",
        "confidence",
        "weight",
    }
    assert not forbidden_keys.intersection(event_dict.keys())
    assert not forbidden_keys.intersection(candidate_dict.keys())


def test_no_effective_portfolio_or_cross_manager_comparison() -> None:
    # Multiple managers processed purely independently without cross-manager net or aggregation
    m1 = _create_metric(value=Decimal("100"), manager_cik="0001000001")
    m2 = _create_metric(value=Decimal("200"), manager_cik="0001000002")

    evals, events, candidates = project_institutional_events((m1, m2))

    assert len(events) == 2
    assert {e.manager_cik for e in events} == {"0001000001", "0001000002"}
    # No portfolio aggregate is produced
    for event in events:
        assert "effective_portfolio" not in event.parameters
        assert "net_position" not in event.parameters
