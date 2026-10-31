"""Integration-level catalog boundary for silent technical candidates."""

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


def test_technical_indicator_candidates_use_the_existing_rule_catalog() -> None:
    rules = {rule.rule_id: rule for rule in INITIAL_ANALYTICAL_RULES}

    for rule_id, algorithm_version in _EXPECTED_VERSIONS.items():
        rule = rules[rule_id]
        assert rule.state is AnalyticalRuleState.SILENT
        assert rule.rule_version == "1.0"
        assert rule.confirmations_required == 2
        assert rule.cooldown_seconds == 86_400
        assert rule.conditions[0].algorithm_version == algorithm_version
