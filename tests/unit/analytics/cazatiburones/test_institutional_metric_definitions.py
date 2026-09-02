from investment_analyst.analytics.cazatiburones.institutional_metric_definitions import (
    DEFINITION_VERSION,
    INSTITUTIONAL_METRIC_DEFINITIONS,
)


def test_institutional_catalog_is_versioned_and_limited_to_normalized_fields() -> None:
    assert DEFINITION_VERSION == "cazatiburones-institutional-metrics-v1"
    assert len(INSTITUTIONAL_METRIC_DEFINITIONS) == 5
