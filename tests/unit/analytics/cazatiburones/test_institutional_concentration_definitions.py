from investment_analyst.analytics.cazatiburones.institutional_concentration_definitions import (
    DECLARED_CONCENTRATION_FORMULAS,
    SEC_13F_CONCENTRATION_POLICY_VERSION,
)


def test_declared_concentration_policy_has_stable_version_and_explicit_formulas() -> None:
    assert SEC_13F_CONCENTRATION_POLICY_VERSION == "sec-13f-concentration-policy-v1"
    assert DECLARED_CONCENTRATION_FORMULAS == {
        "position_count": "count(as_filed_rows)",
        "largest_declared_weight": "max(row_value / effective_close_value_total)",
        "top_five_declared_weight": "sum(five_largest(row_value / effective_close_value_total))",
        "top_ten_declared_weight": "sum(ten_largest(row_value / effective_close_value_total))",
        "herfindahl_index": "sum((row_value / effective_close_value_total) ** 2)",
    }
