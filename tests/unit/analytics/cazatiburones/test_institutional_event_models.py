"""Unit tests for strict institutional event models."""

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from pydantic import ValidationError

from investment_analyst.analytics.cazatiburones.institutional_event_models import (
    InstitutionalCandidate,
    InstitutionalEvaluation,
    InstitutionalEvent,
    InstitutionalEventMaterializationSummary,
    InstitutionalEventRule,
    InstitutionalEventSnapshot,
)


def test_evaluation_shape_validation() -> None:
    result_id = uuid4()
    with pytest.raises(ValidationError, match="met evaluation requires a value"):
        InstitutionalEvaluation(
            rule_id="test-rule",
            metric_result_id=result_id,
            status="met",
            value=None,
            unit="shares",
        )

    with pytest.raises(ValidationError, match="non-met evaluation must not carry a value"):
        InstitutionalEvaluation(
            rule_id="test-rule",
            metric_result_id=result_id,
            status="not_met",
            value=Decimal("100"),
            unit="shares",
        )

    with pytest.raises(ValidationError, match="non-met evaluation must not carry a value"):
        InstitutionalEvaluation(
            rule_id="test-rule",
            metric_result_id=result_id,
            status="not_evaluable",
            value=Decimal("100"),
            unit="shares",
        )

    valid_met = InstitutionalEvaluation(
        rule_id="test-rule",
        metric_result_id=result_id,
        status="met",
        value=Decimal("100"),
        unit="shares",
    )
    assert valid_met.value == Decimal("100")

    valid_not_met = InstitutionalEvaluation(
        rule_id="test-rule",
        metric_result_id=result_id,
        status="not_met",
        reason="declared_zero_or_opposite_direction",
        unit="shares",
    )
    assert valid_not_met.value is None


def test_candidate_shape_validation() -> None:
    event_id = uuid4()
    candidate_id = uuid4()

    with pytest.raises(ValidationError, match="suppressed candidate requires cooldown evidence"):
        InstitutionalCandidate(
            candidate_id=candidate_id,
            event_id=event_id,
            status="suppressed",
        )

    eligible = InstitutionalCandidate(
        candidate_id=candidate_id,
        event_id=event_id,
        status="eligible",
    )
    assert eligible.status == "eligible"
    assert eligible.cooldown_until is None

    suppressed = InstitutionalCandidate(
        candidate_id=candidate_id,
        event_id=event_id,
        status="suppressed",
        cooldown_until=datetime(2025, 1, 2, tzinfo=UTC),
        suppressed_by_event_id=uuid4(),
    )
    assert suppressed.status == "suppressed"


def test_snapshot_identities_validation() -> None:
    event_one = uuid4()
    event_two = uuid4()
    now = datetime(2025, 1, 1, tzinfo=UTC)

    event_item = InstitutionalEvent(
        event_id=event_one,
        asset_id="equity:us:aapl",
        manager_cik="0001350694",
        report_period="2024-12-31",
        prior_report_period="2024-09-30",
        cusip="037833100",
        title_of_class="COM",
        put_call=None,
        rule_id="test-rule",
        metric_result_id=uuid4(),
        metric_key="cazatiburones.institutional.delta_reported_shares",
        algorithm_version="cazatiburones-institutional-metrics-v1",
        unit="shares",
        value=Decimal("1000"),
        available_at=now,
        input_observation_ids=(uuid4(), uuid4()),
        parameters={},
    )

    with pytest.raises(ValidationError, match="snapshot events must be unique"):
        InstitutionalEventSnapshot(
            snapshot_id=uuid4(),
            asset_id="equity:us:aapl",
            manager_cik="0001350694",
            known_at=now,
            recorded_at=now,
            policy_version="test-policy",
            evaluations=(),
            events=(event_item, event_item),
            candidates=(
                InstitutionalCandidate(candidate_id=uuid4(), event_id=event_one, status="eligible"),
            ),
        )

    with pytest.raises(ValidationError, match="snapshot candidates must exactly cover events"):
        InstitutionalEventSnapshot(
            snapshot_id=uuid4(),
            asset_id="equity:us:aapl",
            manager_cik="0001350694",
            known_at=now,
            recorded_at=now,
            policy_version="test-policy",
            evaluations=(),
            events=(event_item,),
            candidates=(
                InstitutionalCandidate(candidate_id=uuid4(), event_id=event_two, status="eligible"),
            ),
        )


def test_models_extra_forbid() -> None:
    with pytest.raises(ValidationError):
        InstitutionalEventRule(
            rule_id="rule",
            metric_key="key",
            direction="increased",
            unit="shares",
            definition_version="v1",
            extra_field="disallowed",
        )


def test_materialization_summary() -> None:
    now = datetime(2025, 1, 1, tzinfo=UTC)
    summary = InstitutionalEventMaterializationSummary(
        asset_id="equity:us:aapl",
        manager_cik="0001350694",
        known_at=now,
        snapshot_id=uuid4(),
        created=True,
        events=5,
        candidates=5,
    )
    assert summary.created is True
    assert summary.events == 5

    with pytest.raises(ValidationError):
        InstitutionalEventMaterializationSummary(
            asset_id="equity:us:aapl",
            manager_cik="0001350694",
            known_at=now,
            snapshot_id=uuid4(),
            created=True,
            events=-1,
            candidates=0,
        )
