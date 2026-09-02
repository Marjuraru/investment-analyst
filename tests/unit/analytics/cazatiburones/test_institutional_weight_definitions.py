from investment_analyst.analytics.cazatiburones.institutional_weight_definitions import (
    ALGORITHM_VERSION,
    INSTITUTIONAL_WEIGHT_DEFINITIONS,
    WEIGHT_FIELDS,
)


def test_declared_weight_catalog_is_versioned_and_ratio_only() -> None:
    assert len(INSTITUTIONAL_WEIGHT_DEFINITIONS) == 2
    assert {item.metric_key for item in INSTITUTIONAL_WEIGHT_DEFINITIONS} == {
        key for key, _ in WEIGHT_FIELDS
    }
    assert all(
        item.unit == "ratio" and item.definition_version == ALGORITHM_VERSION
        for item in INSTITUTIONAL_WEIGHT_DEFINITIONS
    )
