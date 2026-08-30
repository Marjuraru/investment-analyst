from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from investment_analyst.analytics.cazatiburones.activity_rule_models import (
    ActivityRuleDefinition,
    ActivityRuleEvaluation,
    ActivityRuleEvaluationResult,
)

_AVAILABLE_AT = datetime(2025, 1, 1, tzinfo=UTC)


def _evaluation(**overrides: object) -> ActivityRuleEvaluation:
    base = dict(
        rule_id="insider-acquisition-declared",
        definition_version="cazatiburones-activity-rules-v1",
        family="insider",
        form="4",
        participant_id="0000000001",
        subject_id="equity:us:aapl",
        feature_key="acquisition_count",
        feature_status="available",
        value_observed=Decimal("1"),
        threshold_applied=Decimal("1"),
        status="met",
        available_at=_AVAILABLE_AT,
    )
    base.update(overrides)
    return ActivityRuleEvaluation(**base)


def test_met_requires_an_observed_value_and_no_reason() -> None:
    evaluation = _evaluation()
    assert evaluation.status == "met"
    assert evaluation.reason is None


def test_not_evaluable_requires_a_reason_and_no_observed_value() -> None:
    evaluation = _evaluation(
        feature_status="missing",
        value_observed=None,
        threshold_applied=None,
        status="not_evaluable",
        reason="feature absent from evidence",
    )
    assert evaluation.value_observed is None


def test_not_evaluable_without_reason_is_rejected() -> None:
    with pytest.raises(ValidationError):
        _evaluation(
            feature_status="missing",
            value_observed=None,
            threshold_applied=None,
            status="not_evaluable",
        )


def test_met_without_observed_value_is_rejected() -> None:
    with pytest.raises(ValidationError):
        _evaluation(value_observed=None)


def test_met_with_a_not_evaluable_reason_is_rejected() -> None:
    with pytest.raises(ValidationError):
        _evaluation(reason="should not be here")


def test_met_requires_an_available_feature_status() -> None:
    with pytest.raises(ValidationError):
        _evaluation(feature_status="missing")


def test_no_score_verdict_or_confidence_field_exists() -> None:
    forbidden = {"score", "final_score", "confidence", "verdict", "rank", "ranking"}
    assert forbidden.isdisjoint(ActivityRuleEvaluation.model_fields)
    assert forbidden.isdisjoint(ActivityRuleEvaluationResult.model_fields)


def test_decimal_compare_requires_comparator_and_threshold() -> None:
    with pytest.raises(ValidationError):
        ActivityRuleDefinition(
            rule_id="x",
            family="insider",
            forms=("4",),
            feature_key="acquisition_count",
            condition_type="decimal_compare",
            unit="count",
            label_es="x",
            limitations="x",
            definition_version="v1",
        )


def test_boolean_equals_rejects_comparator_and_threshold() -> None:
    with pytest.raises(ValidationError):
        ActivityRuleDefinition(
            rule_id="x",
            family="beneficial_ownership",
            forms=("SC 13D",),
            feature_key="threshold_appearance",
            condition_type="boolean_equals",
            expected_value=True,
            comparator="eq",
            unit="boolean",
            label_es="x",
            limitations="x",
            definition_version="v1",
        )


def test_result_coverage_cannot_be_lower_than_returned_evaluations() -> None:
    with pytest.raises(ValidationError):
        ActivityRuleEvaluationResult(evaluations=(_evaluation(),), total_features_evaluated=0)
