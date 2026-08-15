"""Integration-level catalog boundary for silent technical candidates."""

from investment_analyst.alerts.analytical_rule_catalog import INITIAL_ANALYTICAL_RULES


def test_technical_indicator_candidates_use_the_existing_rule_catalog() -> None:
    rules = {rule.rule_id: rule for rule in INITIAL_ANALYTICAL_RULES}

    assert rules["market.technical.rsi-low-review"].confirmations_required == 2
    assert rules["market.technical.rsi-low-review"].cooldown_seconds == 86_400
    assert rules["market.technical.macd-positive-histogram-review"].confirmations_required == 2
    assert rules["market.technical.macd-positive-histogram-review"].cooldown_seconds == 86_400
