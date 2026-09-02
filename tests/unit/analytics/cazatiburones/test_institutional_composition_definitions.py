from investment_analyst.analytics.cazatiburones.institutional_composition_definitions import (
    SEC_13F_AMENDMENT_TYPES,
    SEC_13F_COMPOSITION_POLICY_VERSION,
)


def test_policy_declares_only_the_supported_as_filed_amendment_types() -> None:
    assert SEC_13F_COMPOSITION_POLICY_VERSION == "sec-13f-composition-policy-v1"
    assert frozenset({"RESTATEMENT", "NEW HOLDINGS ENTRIES"}) == SEC_13F_AMENDMENT_TYPES
