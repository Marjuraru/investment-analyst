"""Descriptive institutional event definitions and policy constants."""

from investment_analyst.analytics.cazatiburones.institutional_event_models import (
    InstitutionalEventRule,
)
from investment_analyst.analytics.cazatiburones.institutional_metric_definitions import (
    INSTITUTIONAL_METRIC_DEFINITIONS,
)

POLICY_VERSION = "cazatiburones-persisted-institutional-events-v1"
COOLDOWN_SECONDS = 86_400


def _build_rules() -> tuple[InstitutionalEventRule, ...]:
    rules: list[InstitutionalEventRule] = []
    for metric_def in INSTITUTIONAL_METRIC_DEFINITIONS:
        metric_key = metric_def.metric_key
        slug = metric_key.split(".")[-1].replace("_", "-")
        rules.append(
            InstitutionalEventRule(
                rule_id=f"institutional-{slug}-increased",
                metric_key=metric_key,
                direction="increased",
                unit=metric_def.unit,
                definition_version=POLICY_VERSION,
            )
        )
        rules.append(
            InstitutionalEventRule(
                rule_id=f"institutional-{slug}-reduced",
                metric_key=metric_key,
                direction="reduced",
                unit=metric_def.unit,
                definition_version=POLICY_VERSION,
            )
        )
    return tuple(rules)


RULES: tuple[InstitutionalEventRule, ...] = _build_rules()
