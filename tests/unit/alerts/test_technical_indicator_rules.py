"""Contracts for the packaged technical indicator screening rules."""

from decimal import Decimal

from investment_analyst.alerts.analytical_models import AnalyticalRuleState
from investment_analyst.alerts.analytical_rule_catalog import INITIAL_ANALYTICAL_RULES


def test_technical_indicator_rules_are_independent_and_silent() -> None:
    rules = {
        item.rule_id: item
        for item in INITIAL_ANALYTICAL_RULES
        if item.rule_id.startswith("market.technical.")
    }

    assert set(rules) == {
        "market.technical.rsi-low-review",
        "market.technical.macd-positive-histogram-review",
    }
    assert all(rule.state is AnalyticalRuleState.SILENT for rule in rules.values())
    assert all(len(rule.conditions) == 1 for rule in rules.values())
    assert rules["market.technical.rsi-low-review"].conditions[0].threshold == Decimal("30")
    assert rules["market.technical.macd-positive-histogram-review"].conditions[
        0
    ].threshold == Decimal("0")
