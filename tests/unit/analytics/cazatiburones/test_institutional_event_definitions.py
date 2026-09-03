"""Unit tests for fixed descriptive institutional event definitions."""

from investment_analyst.analytics.cazatiburones.institutional_event_definitions import (
    COOLDOWN_SECONDS,
    POLICY_VERSION,
    RULES,
)
from investment_analyst.analytics.cazatiburones.institutional_metric_definitions import (
    INSTITUTIONAL_METRIC_DEFINITIONS,
)


def test_one_rule_per_metric_and_direction_without_configurable_threshold() -> None:
    assert POLICY_VERSION == "cazatiburones-persisted-institutional-events-v1"
    assert COOLDOWN_SECONDS == 86_400
    expected_metric_keys = {item.metric_key for item in INSTITUTIONAL_METRIC_DEFINITIONS}
    assert len(expected_metric_keys) == 5

    rule_keys = {(rule.metric_key, rule.direction) for rule in RULES}
    assert len(RULES) == 10
    assert len(rule_keys) == 10

    for metric_key in expected_metric_keys:
        assert (metric_key, "increased") in rule_keys
        assert (metric_key, "reduced") in rule_keys

    rule_ids = {rule.rule_id for rule in RULES}
    assert len(rule_ids) == 10
    for rule in RULES:
        assert rule.definition_version == POLICY_VERSION
        assert rule.direction in {"increased", "reduced"}
        assert rule.unit in {"shares", "USD", "ratio"}
