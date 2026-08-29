from investment_analyst.analytics.cazatiburones.institutional_change_definitions import (
    METRIC_DEFINITIONS,
)


def test_definitions_are_versioned_and_explicit() -> None:
    assert "delta_quantity" in METRIC_DEFINITIONS
    assert "portfolio_top_n_concentration" in METRIC_DEFINITIONS
