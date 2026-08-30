# ruff: noqa: E501
"""Pure, deterministic evaluation of the declared activity-rule catalog.

Two entry points, by issuer and by manager, are kept separate and never cross. Neither
function performs I/O, persistence, deduplication, or cross-rule/participant/issuer
aggregation; each evaluation is produced independently from one declared feature.
"""

from collections import defaultdict
from collections.abc import Iterable
from decimal import Decimal

from investment_analyst.analytics.cazatiburones.activity_rule_definitions import (
    ACTIVITY_RULE_CATALOG,
)
from investment_analyst.analytics.cazatiburones.activity_rule_models import (
    ActivityRuleDefinition,
    ActivityRuleEvaluation,
    ActivityRuleEvaluationResult,
)
from investment_analyst.analytics.cazatiburones.declared_activity_models import (
    DeclaredActivityFeatureSet,
    DeclaredActivityQueryResult,
)
from investment_analyst.analytics.cazatiburones.institutional_change_models import (
    DescriptiveMetric,
    InstitutionalChangeResult,
)

# `entry`/`exit` are the only institutional feature keys that can repeat within one
# InstitutionalChangeResult (once per changed position); every other key used by the
# catalog is appended exactly once by institutional_change_engine.compare.
_EXISTENTIAL_INSTITUTIONAL_KEYS = frozenset({"entry", "exit"})
_INSTITUTIONAL_FORM = "13F-HR"


def evaluate_declared_activity_rules(
    result: DeclaredActivityQueryResult,
) -> ActivityRuleEvaluationResult:
    evaluations: list[ActivityRuleEvaluation] = []
    total = 0
    for feature_set in result.insider_features:
        for rule in _rules_for_family("insider"):
            total += 1
            evaluations.append(_evaluate_feature_set_rule(feature_set, rule))
    for feature_set in result.beneficial_features:
        for rule in _rules_for_family("beneficial_ownership"):
            total += 1
            evaluations.append(_evaluate_feature_set_rule(feature_set, rule))
    return ActivityRuleEvaluationResult(
        evaluations=tuple(evaluations), total_features_evaluated=total
    )


def evaluate_institutional_rules(
    results: Iterable[InstitutionalChangeResult],
) -> ActivityRuleEvaluationResult:
    evaluations: list[ActivityRuleEvaluation] = []
    total = 0
    rules = _rules_for_family("institutional_13f")
    for result in results:
        grouped: dict[str, list[DescriptiveMetric]] = defaultdict(list)
        for metric in result.metrics:
            grouped[metric.key].append(metric)
        for rule in rules:
            total += 1
            evaluations.append(_evaluate_institutional_rule(result, rule, grouped))
    return ActivityRuleEvaluationResult(
        evaluations=tuple(evaluations), total_features_evaluated=total
    )


def _rules_for_family(family: str) -> tuple[ActivityRuleDefinition, ...]:
    return tuple(rule for rule in ACTIVITY_RULE_CATALOG if rule.family == family)


def _evaluate_feature_set_rule(
    feature_set: DeclaredActivityFeatureSet, rule: ActivityRuleDefinition
) -> ActivityRuleEvaluation:
    metric = {item.key: item for item in feature_set.metrics}.get(rule.feature_key)
    common = dict(
        rule_id=rule.rule_id,
        definition_version=rule.definition_version,
        family=rule.family,
        form=feature_set.form,
        participant_id=feature_set.participant_cik,
        subject_id=feature_set.asset_id,
        feature_key=rule.feature_key,
        revision_ids=feature_set.revision_ids,
        event_date=feature_set.event_date,
        available_at=feature_set.available_at,
    )
    if metric is None:
        return ActivityRuleEvaluation(
            **common,
            feature_status="missing",
            status="not_evaluable",
            reason="feature absent from evidence",
        )
    if metric.status != "available":
        return ActivityRuleEvaluation(
            **common,
            feature_status=metric.status,
            status="not_evaluable",
            reason=f"feature status is {metric.status}",
        )
    met, threshold_applied = _apply_condition(rule, metric.value)
    return ActivityRuleEvaluation(
        **common,
        feature_status="available",
        value_observed=metric.value,
        threshold_applied=threshold_applied,
        status="met" if met else "not_met",
    )


def _evaluate_institutional_rule(
    result: InstitutionalChangeResult,
    rule: ActivityRuleDefinition,
    grouped: dict[str, list[DescriptiveMetric]],
) -> ActivityRuleEvaluation:
    common = dict(
        rule_id=rule.rule_id,
        definition_version=rule.definition_version,
        family=rule.family,
        form=_INSTITUTIONAL_FORM,
        participant_id=result.manager_cik,
        subject_id=result.manager_cik,
        feature_key=rule.feature_key,
        revision_ids=(),
        event_date=result.current_period,
        available_at=result.available_at,
    )
    candidates = grouped.get(rule.feature_key, [])
    if not candidates:
        return ActivityRuleEvaluation(
            **common,
            feature_status="missing",
            status="not_evaluable",
            reason=_missing_reason(rule),
        )
    if rule.feature_key in _EXISTENTIAL_INSTITUTIONAL_KEYS:
        available = [item for item in candidates if item.status == "available"]
        if not available:
            return ActivityRuleEvaluation(
                **common,
                feature_status=candidates[0].status,
                status="not_evaluable",
                reason=f"feature status is {candidates[0].status}",
            )
        value: Decimal | bool = any(bool(item.value) for item in available)
    else:
        if len(candidates) > 1:
            raise ValueError(
                f"institutional feature '{rule.feature_key}' unexpectedly repeated in one result"
            )
        metric = candidates[0]
        if metric.status != "available":
            return ActivityRuleEvaluation(
                **common,
                feature_status=metric.status,
                status="not_evaluable",
                reason=_missing_reason(rule)
                if metric.status == "missing"
                else f"feature status is {metric.status}",
            )
        value = metric.value
    met, threshold_applied = _apply_condition(rule, value)
    return ActivityRuleEvaluation(
        **common,
        feature_status="available",
        value_observed=value,
        threshold_applied=threshold_applied,
        status="met" if met else "not_met",
    )


def _missing_reason(rule: ActivityRuleDefinition) -> str:
    if rule.minimum_sample is not None:
        return (
            f"feature '{rule.feature_key}' unavailable (requires a minimum of "
            f"{rule.minimum_sample} historical observations per "
            "institutional-change-definitions-v1)"
        )
    return "feature absent from evidence"


def _apply_condition(
    rule: ActivityRuleDefinition, value: Decimal | bool
) -> tuple[bool, Decimal | bool]:
    if rule.condition_type == "decimal_compare":
        if not isinstance(value, Decimal):
            raise ValueError(f"rule '{rule.rule_id}' requires a Decimal feature value")
        comparator = rule.comparator
        threshold = rule.threshold
        if comparator == "gt":
            met = value > threshold
        elif comparator == "gte":
            met = value >= threshold
        elif comparator == "lt":
            met = value < threshold
        elif comparator == "lte":
            met = value <= threshold
        else:
            met = value == threshold
        return met, threshold
    if not isinstance(value, bool):
        raise ValueError(f"rule '{rule.rule_id}' requires a boolean feature value")
    return value == rule.expected_value, rule.expected_value
