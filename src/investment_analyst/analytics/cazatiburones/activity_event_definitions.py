"""Fixed descriptive rules over persisted declared-activity metrics."""

from investment_analyst.analytics.cazatiburones.activity_event_models import ActivityEventRule

POLICY_VERSION = "cazatiburones-persisted-activity-events-v1"
COOLDOWN_SECONDS = 86_400
RULES: tuple[ActivityEventRule, ...] = (
    ActivityEventRule(
        rule_id="insider-holding-increased",
        metric_key="cazatiburones.insider.holding_delta_ratio",
        direction="increased",
        unit="ratio",
        definition_version=POLICY_VERSION,
    ),
    ActivityEventRule(
        rule_id="insider-holding-reduced",
        metric_key="cazatiburones.insider.holding_delta_ratio",
        direction="reduced",
        unit="ratio",
        definition_version=POLICY_VERSION,
    ),
    ActivityEventRule(
        rule_id="beneficial-percent-increased",
        metric_key="cazatiburones.beneficial.delta_percent_of_class",
        direction="increased",
        unit="percentage_points",
        definition_version=POLICY_VERSION,
    ),
    ActivityEventRule(
        rule_id="beneficial-percent-reduced",
        metric_key="cazatiburones.beneficial.delta_percent_of_class",
        direction="reduced",
        unit="percentage_points",
        definition_version=POLICY_VERSION,
    ),
    ActivityEventRule(
        rule_id="beneficial-shares-increased",
        metric_key="cazatiburones.beneficial.delta_shares_beneficially_owned",
        direction="increased",
        unit="shares",
        definition_version=POLICY_VERSION,
    ),
    ActivityEventRule(
        rule_id="beneficial-shares-reduced",
        metric_key="cazatiburones.beneficial.delta_shares_beneficially_owned",
        direction="reduced",
        unit="shares",
        definition_version=POLICY_VERSION,
    ),
)
