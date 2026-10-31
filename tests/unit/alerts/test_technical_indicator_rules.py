"""Contracts for the packaged technical indicator screening rules."""

from decimal import Decimal

from investment_analyst.alerts.analytical_models import AnalyticalRuleState
from investment_analyst.alerts.analytical_rule_catalog import INITIAL_ANALYTICAL_RULES

_EXPECTED_VERSIONS = {
    "market.technical.macd-incremental-positive-histogram-review": (
        "market-macd-incremental-v2-decimal34"
    ),
    "market.technical.macd-positive-histogram-review": "market-macd-v1-decimal34",
    "market.technical.rsi-incremental-low-review": "market-rsi-incremental-v2-decimal34",
    "market.technical.rsi-low-review": "market-rsi-wilder-v1-decimal34",
}


def test_technical_indicator_rules_are_independent_and_silent() -> None:
    rules = {
        item.rule_id: item
        for item in INITIAL_ANALYTICAL_RULES
        if item.rule_id.startswith("market.technical.")
    }

    assert set(rules) == set(_EXPECTED_VERSIONS)
    assert all(rule.state is AnalyticalRuleState.SILENT for rule in rules.values())
    assert all(len(rule.conditions) == 1 for rule in rules.values())
    assert all(rule.rule_version == "1.0" for rule in rules.values())
    assert all(rule.confirmations_required == 2 for rule in rules.values())
    assert all(rule.cooldown_seconds == 86_400 for rule in rules.values())
    assert {
        rule_id: rule.conditions[0].algorithm_version for rule_id, rule in rules.items()
    } == _EXPECTED_VERSIONS
    assert rules["market.technical.rsi-low-review"].conditions[0].threshold == Decimal("30")
    assert rules["market.technical.rsi-low-review"].conditions[0].exit_threshold == Decimal("35")
    assert rules["market.technical.rsi-incremental-low-review"].conditions[0].threshold == Decimal(
        "30"
    )
    assert rules["market.technical.rsi-incremental-low-review"].conditions[
        0
    ].exit_threshold == Decimal("35")
    assert rules["market.technical.macd-positive-histogram-review"].conditions[
        0
    ].threshold == Decimal("0")
    assert rules["market.technical.macd-incremental-positive-histogram-review"].conditions[
        0
    ].threshold == Decimal("0")
