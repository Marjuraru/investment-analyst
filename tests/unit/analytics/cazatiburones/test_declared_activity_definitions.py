from investment_analyst.analytics.cazatiburones.declared_activity_definitions import (
    DEFINITION_VERSION,
    FEATURE_DEFINITIONS,
)


def test_declared_activity_definitions_are_versioned_and_explicit() -> None:
    assert FEATURE_DEFINITIONS
    assert all(item.definition_version == DEFINITION_VERSION for item in FEATURE_DEFINITIONS)
    assert {item.key for item in FEATURE_DEFINITIONS} >= {
        "holding_delta_ratio",
        "delta_percent_of_class",
        "filing_delay_days",
    }
    assert all(item.parameters is not None for item in FEATURE_DEFINITIONS)
