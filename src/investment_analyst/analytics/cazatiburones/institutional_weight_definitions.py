"""Versioned definitions for declared 13F position weights."""

from investment_analyst.core.models.enums import MetricCategory
from investment_analyst.core.models.metric import MetricDefinition

DEFINITION_VERSION = "cazatiburones-institutional-weight-v1"
ALGORITHM_VERSION = "cazatiburones-institutional-weight-v1"
WEIGHT_FIELDS = (
    (
        "cazatiburones.institutional.declared_position_value_weight",
        "institutional_reported_fair_value",
    ),
    (
        "cazatiburones.institutional.declared_option_position_value_weight",
        "institutional_option_fair_value",
    ),
)
INSTITUTIONAL_WEIGHT_DEFINITIONS = tuple(
    MetricDefinition(
        metric_key=key,
        display_name=key.rsplit(".", 1)[-1],
        category=MetricCategory.CAZATIBURONES,
        description="Declared position value divided by the declared effective-close value total.",
        formula="position_value / effective_close_value_total",
        unit="ratio",
        limitations=["as-filed effective close only; not portfolio concentration"],
        references=["Form 13F informationTable value"],
        definition_version=DEFINITION_VERSION,
    )
    for key, _ in WEIGHT_FIELDS
)
