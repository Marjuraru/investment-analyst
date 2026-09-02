"""Versioned catalog of persistible institutional 13F descriptive metrics."""

from investment_analyst.core.models.enums import MetricCategory
from investment_analyst.core.models.metric import MetricDefinition

DEFINITION_VERSION = "cazatiburones-institutional-metrics-v1"
ALGORITHM_VERSION = "cazatiburones-institutional-metrics-v1"

INSTITUTIONAL_METRIC_DEFINITIONS = (
    MetricDefinition(
        metric_key="cazatiburones.institutional.delta_reported_shares",
        display_name="Reported shares delta",
        category=MetricCategory.CAZATIBURONES,
        description="Current reported shares minus prior reported shares.",
        formula="current - prior",
        unit="shares",
        limitations=["requires one unambiguous shared position in adjacent effective closes"],
        references=["Form 13F informationTable sshPrnamt"],
        definition_version=DEFINITION_VERSION,
    ),
    MetricDefinition(
        metric_key="cazatiburones.institutional.delta_reported_fair_value",
        display_name="Reported fair value delta",
        category=MetricCategory.CAZATIBURONES,
        description="Current reported fair value minus prior reported fair value.",
        formula="current - prior",
        unit="USD",
        limitations=["monetary policy quality is preserved from observations"],
        references=["Form 13F informationTable value"],
        definition_version=DEFINITION_VERSION,
    ),
    MetricDefinition(
        metric_key="cazatiburones.institutional.delta_option_underlying_shares",
        display_name="Option underlying shares delta",
        category=MetricCategory.CAZATIBURONES,
        description="Current option underlying shares minus prior option underlying shares.",
        formula="current - prior",
        unit="shares",
        limitations=["only PUT/CALL observations are compared"],
        references=["Form 13F informationTable sshPrnamt"],
        definition_version=DEFINITION_VERSION,
    ),
    MetricDefinition(
        metric_key="cazatiburones.institutional.delta_option_fair_value",
        display_name="Option fair value delta",
        category=MetricCategory.CAZATIBURONES,
        description="Current option fair value minus prior option fair value.",
        formula="current - prior",
        unit="USD",
        limitations=["only PUT/CALL observations are compared"],
        references=["Form 13F informationTable value"],
        definition_version=DEFINITION_VERSION,
    ),
    MetricDefinition(
        metric_key="cazatiburones.institutional.reported_shares_delta_ratio",
        display_name="Reported shares delta ratio",
        category=MetricCategory.CAZATIBURONES,
        description="(current reported shares - prior reported shares) / prior reported shares.",
        formula="(current - prior) / prior",
        unit="ratio",
        limitations=["not evaluable when prior shares are zero or absent"],
        references=["Form 13F informationTable sshPrnamt"],
        definition_version=DEFINITION_VERSION,
    ),
)

METRIC_FIELDS = (
    (
        "cazatiburones.institutional.delta_reported_shares",
        "institutional_reported_shares",
        "shares",
        False,
    ),
    (
        "cazatiburones.institutional.delta_reported_fair_value",
        "institutional_reported_fair_value",
        "USD",
        False,
    ),
    (
        "cazatiburones.institutional.delta_option_underlying_shares",
        "institutional_option_underlying_shares",
        "shares",
        False,
    ),
    (
        "cazatiburones.institutional.delta_option_fair_value",
        "institutional_option_fair_value",
        "USD",
        False,
    ),
    (
        "cazatiburones.institutional.reported_shares_delta_ratio",
        "institutional_reported_shares",
        "ratio",
        True,
    ),
)
